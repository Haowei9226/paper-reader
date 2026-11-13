"""Prepare a source allowlist and audited release assets for GitHub publication.

No upload is performed. Private values stay in memory and are never logged.
Run again after changing any public source, README, or image. The generated
manifest freezes the exact bytes reviewed for a subsequent GitHub commit.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import zipfile


ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "output" / "github-publication"
VERSION = re.search(r'^VERSION = "([^"]+)"', (ROOT / "reader" / "__init__.py").read_text(encoding="utf-8"), re.M).group(1)
COMBINED_NAME = f"PaperReader-Share-{VERSION}.zip"
TRUSTED_COMBINED_SHA256 = ("fd8363ec782b0d5942b910d396869510deb291194336d3e7230b6774a2898497"
                           if VERSION == "0.6.14" else None)
TRUSTED_PLATFORM_SHA256 = {
    "PaperReader-Windows-0.6.14.zip": "840ff8bd39b6e30e7299ad5094c831262840911ce9ebec91370dbd4a8a8e72d2",
    "PaperReader-macOS-0.6.14.zip": "1bb41f104615fd8426b888631c69acb485365f29814a63210f67248189d6c83c",
}
FIXED_FILES = (
    ".gitignore", ".gitattributes", "LICENSE", "README.md", "README.en.md", "README_EN.md",
    "README.zh-CN.md", "pyproject.toml", "requirements.lock", "requirements.txt",
    "Create-Shortcut.ps1", "Launch-Reader.pyw", "Setup.cmd", "Start-Reader.cmd",
    "assets/reader.ico", "web/index.html", "web/package.json", "web/package-lock.json",
    "web/tsconfig.json", "web/vite.config.ts", "web/public/icon.svg", "docs/SHARING.txt",
)
DIRECTORIES = {
    "reader": {".py", ".css", ".ps1"},
    "scripts": {".py", ".sh", ".command", ".ps1", ".mjs"},
    "tests": {".py"},
    "web/src": {".ts", ".tsx", ".css", ".svg"},
    "web/scripts": {".mjs", ".js", ".ts"},
    "docs/images": {".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif"},
}
FORBIDDEN_PARTS = {
    "data", "originals", "backups", "exports", "incoming", "tmp", "output", ".cache",
    ".venv", ".git", "node_modules", "dist", "__pycache__", ".pytest_cache", ".playwright-cli",
}
FORBIDDEN_SUFFIXES = {".pdf", ".sqlite", ".sqlite3", ".db", ".dpapi", ".log", ".pyc", ".pyo"}
MAX_PAYLOAD_CHARS = 60_000


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def public_files() -> list[Path]:
    selected = {ROOT / relative for relative in FIXED_FILES if (ROOT / relative).is_file()}
    for relative, suffixes in DIRECTORIES.items():
        folder = ROOT / relative
        if not folder.is_dir():
            continue
        for path in folder.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in suffixes:
                continue
            subparts = path.relative_to(ROOT).parts
            if any(part in FORBIDDEN_PARTS or part.startswith(".env") for part in subparts):
                continue
            selected.add(path)
    for path in selected:
        relative = path.relative_to(ROOT)
        if path.is_symlink() or not path.resolve().is_relative_to(ROOT.resolve()):
            raise ValueError(f"Public source must not be a symlink: {relative.as_posix()}")
        if (any(part in FORBIDDEN_PARTS or part.startswith(".env") for part in relative.parts)
                or path.suffix.lower() in FORBIDDEN_SUFFIXES):
            raise ValueError(f"Unexpected private file in source allowlist: {relative.as_posix()}")
    return sorted(selected, key=lambda path: path.relative_to(ROOT).as_posix())


def configured_key_needles() -> tuple[list[bytes], int]:
    """Read only the explicit ignored API-key file, without returning/logging text."""
    path = ROOT / ".env.local"
    values = set()
    if path.is_file():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            name, separator, value = line.strip().removeprefix("export ").partition("=")
            if not separator or name.strip() not in {"OPENAI_API_KEY", "DEEPSEEK_API_KEY"}:
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if value:
                values.add(value)
    return list({value.encode(encoding) for value in values for encoding in ("utf-8", "utf-16-le", "utf-16-be")}), len(values)


def private_inputs():
    key_needles, key_count = configured_key_needles()
    encrypted = [path.read_bytes() for path in (ROOT / "data").glob("*.key.dpapi") if path.is_file()]
    pdf_paths = [path for path in (ROOT / "data" / "originals").rglob("*") if path.is_file() and path.suffix.lower() == ".pdf"]
    pdf_hashes = {sha256_file(path) for path in pdf_paths}
    database = ROOT / "data" / "library.sqlite3"
    return {
        "needles": key_needles + [value for value in encrypted if value],
        "key_count": key_count,
        "encrypted_count": len(encrypted),
        "pdf_count": len(pdf_paths),
        "pdf_hashes": pdf_hashes,
        "database_hash": sha256_file(database) if database.is_file() else None,
    }


def prepare_assets():
    """Recover the original small platform ZIPs, leaving the changed ZIP alone."""
    if VERSION != "0.6.14":
        raise ValueError("New release assets need a fresh verify_release.py audit. Use --source-only to freeze reviewed source; old asset approvals cannot be reused.")
    source = ROOT / "output" / "releases" / COMBINED_NAME
    if sha256_file(source) != TRUSTED_COMBINED_SHA256:
        raise ValueError("The original audited combined release archive has changed; re-audit before publication")
    destination = OUTPUT / "assets"
    destination.mkdir(parents=True, exist_ok=True)
    assets = []
    combined_target = destination / source.name
    if not combined_target.is_file() or sha256_file(combined_target) != TRUSTED_COMBINED_SHA256:
        partial = combined_target.with_suffix(".copying.zip")
        shutil.copyfile(source, partial)
        if sha256_file(partial) != TRUSTED_COMBINED_SHA256:
            raise ValueError("Combined release copy failed hash verification")
        os.replace(partial, combined_target)
    assets.append({"name": combined_target.name, "bytes": combined_target.stat().st_size,
                   "sha256": TRUSTED_COMBINED_SHA256, "origin": "byte-identical audited combined archive"})
    with zipfile.ZipFile(source) as archive:
        for name, expected in TRUSTED_PLATFORM_SHA256.items():
            target = destination / name
            if not target.is_file() or sha256_file(target) != expected:
                partial = target.with_suffix(".extracting.zip")
                with archive.open(name) as content, partial.open("wb") as output:
                    shutil.copyfileobj(content, output, length=1024 * 1024)
                if sha256_file(partial) != expected:
                    raise ValueError(f"Audited platform archive failed hash verification: {name}")
                os.replace(partial, target)
            with zipfile.ZipFile(target) as platform:
                if platform.testzip():
                    raise ValueError(f"Platform ZIP CRC verification failed: {name}")
            assets.append({"name": name, "bytes": target.stat().st_size, "sha256": expected,
                           "origin": "byte-identical member extracted from audited combined archive"})
    write_json(OUTPUT / "assets-manifest.json", {"version": VERSION, "assets": assets})
    return assets


def chunk_payload(name: str, value):
    """Write small JSON fragments of a complete JSON payload for tool transfer."""
    serialized = json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    (OUTPUT / f"github-{name}.json").write_text(serialized + "\n", encoding="utf-8")
    fragments = []
    position = 0
    # Fragment strings are JSON-escaped a second time, so measure the envelope.
    while position < len(serialized):
        length = min(48_000, len(serialized) - position)
        while True:
            fragment = serialized[position:position + length]
            envelope = {"kind": name, "index": len(fragments), "content": fragment}
            encoded = json.dumps(envelope, ensure_ascii=True, separators=(",", ":"))
            if len(encoded) <= MAX_PAYLOAD_CHARS - 100:
                break
            length //= 2
            if not length:
                raise ValueError("Cannot fit publication payload into a tool-transfer fragment")
        fragments.append(envelope)
        position += length
    directory = OUTPUT / "payload-batches"
    directory.mkdir(exist_ok=True)
    paths = []
    for envelope in fragments:
        envelope["parts"] = len(fragments)
        path = directory / f"{name}-{envelope['index']:03d}.json"
        text = json.dumps(envelope, ensure_ascii=True, separators=(",", ":"))
        if len(text) > MAX_PAYLOAD_CHARS:
            raise ValueError("Publication payload fragment exceeded size limit")
        path.write_text(text + "\n", encoding="utf-8")
        paths.append(path.relative_to(OUTPUT).as_posix())
    expected = {Path(relative).name for relative in paths}
    for stale in directory.glob(name + "-*.json"):
        if stale.name not in expected and stale.is_file() and stale.resolve().is_relative_to(directory.resolve()):
            stale.unlink()
    return {"parts": len(paths), "serialized_characters": len(serialized), "files": paths}


def prepare_source():
    private = private_inputs()
    files, text_entries, binary_entries = [], [], []
    forbidden_matches = []
    exact_byte_matches = 0
    for path in public_files():
        relative = path.relative_to(ROOT).as_posix()
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        matches = sum(needle in content for needle in private["needles"])
        hash_match = digest in private["pdf_hashes"] or digest == private["database_hash"]
        if matches or hash_match:
            exact_byte_matches += matches
            forbidden_matches.append(relative)
        try:
            text = content.decode("utf-8")
            binary = "\x00" in text
        except UnicodeDecodeError:
            text, binary = None, True
        mode = "100755" if path.suffix in {".sh", ".command"} else "100644"
        blob_sha1 = hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest()
        files.append({"path": relative, "bytes": len(content), "sha256": digest,
                      "git_blob_sha1": blob_sha1, "binary": binary, "mode": mode})
        if binary:
            binary_entries.append({"path": relative, "mode": mode, "encoding": "base64",
                                   "content": base64.b64encode(content).decode("ascii"),
                                   "expected_git_blob_sha1": blob_sha1})
        else:
            text_entries.append({"path": relative, "mode": mode, "type": "blob", "content": text})
    manifest = {"version": VERSION, "file_count": len(files), "total_bytes": sum(file["bytes"] for file in files),
                "text_files": len(text_entries), "binary_files": len(binary_entries), "files": files}
    write_json(OUTPUT / "source-manifest.json", manifest)
    report = {
        "status": "failed" if forbidden_matches else "passed",
        "private_values_logged": False,
        "scope": "exact bytes listed in source-manifest.json, including public documentation images",
        "source_manifest_sha256": sha256_file(OUTPUT / "source-manifest.json"),
        "source_files": len(files), "source_bytes": manifest["total_bytes"],
        "input_checks": {"configured_api_keys_checked": private["key_count"],
                         "dpapi_files_checked_without_decryption": private["encrypted_count"],
                         "user_pdf_hashes_checked": private["pdf_count"],
                         "user_database_hash_checked": private["database_hash"] is not None},
        "exact_secret_byte_matches": exact_byte_matches,
        "private_source_files": forbidden_matches,
        "excluded": ["docs/VALIDATION.md", "data/", "tmp/", "output/", ".env*", "*.key.dpapi",
                     "*.pdf", "*.sqlite*", "*.log", "node_modules/", "web/dist/", "web/public/pdf-assets/"],
        "limits": [
            "Exact byte and full-file hash comparisons do not detect all transformed secrets or document excerpts.",
            "Image pixels must be independently checked; source manifests cannot establish that screenshots contain only synthetic data.",
            "Release asset identity is checked against the prior independently audited combined archive, without modifying original release files.",
        ],
    }
    write_json(OUTPUT / "privacy-report.json", report)
    if forbidden_matches:
        raise ValueError("A private value or document hash was found in the public source allowlist; see privacy-report.json")
    payloads = {"text-tree": chunk_payload("text-tree", {"tree_elements": text_entries}),
                "binary-blobs": chunk_payload("binary-blobs", {"blobs": binary_entries})}
    write_json(OUTPUT / "payload-index.json", payloads)
    return manifest, report, payloads


def verify_manifest():
    path = OUTPUT / "source-manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    current = {path.relative_to(ROOT).as_posix() for path in public_files()}
    expected = {file["path"] for file in manifest["files"]}
    changed = sorted(current.symmetric_difference(expected))
    for file in manifest["files"]:
        path = ROOT / file["path"]
        if not path.is_file() or sha256_file(path) != file["sha256"]:
            changed.append(file["path"])
    result = {"status": "changed" if changed else "unchanged", "file_count": len(expected), "changed_files": sorted(set(changed))}
    print(json.dumps(result, ensure_ascii=False))
    if changed:
        raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-only", action="store_true", help="Regenerate source/privacy/payload files without recopying release assets")
    parser.add_argument("--assets-only", action="store_true", help="Only recover trusted release assets")
    parser.add_argument("--verify", action="store_true", help="Verify current allowlist still matches its frozen manifest")
    parser.add_argument("--payload-kind", choices=("text-tree", "binary-blobs"), default="text-tree")
    parser.add_argument("--payload-chunk", type=int, help="Print one generated <=60000-character JSON transfer envelope")
    args = parser.parse_args()
    if args.payload_chunk is not None:
        if args.payload_chunk < 0:
            raise SystemExit("Payload index must be nonnegative")
        path = OUTPUT / "payload-batches" / f"{args.payload_kind}-{args.payload_chunk:03d}.json"
        print(path.read_text(encoding="utf-8"), end="")
        return
    if args.verify:
        verify_manifest()
        return
    OUTPUT.mkdir(parents=True, exist_ok=True)
    assets = [] if args.source_only else prepare_assets()
    if args.assets_only:
        print(json.dumps({"status": "passed", "assets": assets}, ensure_ascii=False))
        return
    manifest, report, payloads = prepare_source()
    print(json.dumps({"status": report["status"], "file_count": manifest["file_count"], "source_bytes": manifest["total_bytes"],
                      "text_files": manifest["text_files"], "binary_files": manifest["binary_files"],
                      "payloads": payloads, "assets": assets}, ensure_ascii=False))


if __name__ == "__main__":
    main()
