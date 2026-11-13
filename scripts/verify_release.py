"""Inspect a frozen public source tree and release archives without publishing.

Private inputs are read only when --private-root is explicitly provided. Their
values never enter the report. Use the packaging Python environment to inspect
Windows PyInstaller executables. No executable or imported application is run.
"""
from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import io
import json
import marshal
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import tempfile
import types
import zipfile


PRIVATE_PARTS = {".git", ".venv", ".cache", ".pytest_cache", ".playwright-cli",
                 "node_modules", "originals", "incoming", "backups", "exports"}
PRIVATE_NAMES = {".reader-local.json", ".ds_store", "thumbs.db", "desktop.ini"}
PRIVATE_SUFFIXES = {".dpapi", ".sqlite", ".sqlite3", ".db", ".log"}
APP_PARTS = {"reader", "web", "scripts", "tests"}
EMAIL = re.compile(rb"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
USER_PATH = re.compile(rb"(?i)(?:[A-Z]:[\\/](?:Users|User)[\\/]|/(?:Users|home)/)[^\s\x00\"']+")
TOKEN = re.compile(rb"(?:sk-(?:proj-)?[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9_]{25,}|github_pat_[A-Za-z0-9_]{25,})")
PRIVATE_KEY = re.compile(rb"-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----")
FIXTURE_EMAIL_DOMAINS = {b"example.com", b"example.org", b"example.net", b"localhost.invalid"}
DEFAULT_LIMIT = 4 * 1024 ** 3


def digest_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def encoded_values(values):
    return {value.encode(encoding) for value in values if value
            for encoding in ("utf-8", "utf-16-le", "utf-16-be")}


def private_inputs(root: Path | None):
    """The ignored runtime configuration is deliberately never opened."""
    secrets, encrypted, pdf_hashes, db_hashes, markers = set(), [], set(), set(), set()
    if root is not None:
        root = root.resolve()
        key_file = root / ".env.local"
        if key_file.is_file():
            for line in key_file.read_text(encoding="utf-8-sig").splitlines():
                key, separator, value = line.strip().removeprefix("export ").partition("=")
                if separator and key.strip() in {"OPENAI_API_KEY", "DEEPSEEK_API_KEY"}:
                    value = value.strip()
                    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                        value = value[1:-1]
                    if value:
                        secrets.add(value)
        for key in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY"):
            if value := os.environ.get(key, "").strip():
                secrets.add(value)
        data = root / "data"
        encrypted = [path.read_bytes() for path in data.glob("*.key.dpapi") if path.is_file()]
        pdf_hashes = {digest_file(path) for path in (data / "originals").rglob("*.pdf") if path.is_file()}
        db = data / "library.sqlite3"
        if db.is_file():
            db_hashes.add(digest_file(db))
        # Only the current operator's local identity is compared. It is never
        # written into application files, command arguments, or the report.
        username = getpass.getuser()
        if len(username) >= 4 and username.casefold() not in {"user", "users", "root", "admin", "administrator"}:
            markers.add(username)
        for spelling in (str(root), str(root).replace("\\", "/"), str(root).replace("\\", "\\\\")):
            markers.add(spelling)
        for scope in ("--local", "--global"):
            result = subprocess.run(["git", "config", scope, "--get", "user.email"], cwd=root,
                                    capture_output=True, text=True, check=False)
            if result.returncode == 0 and (email := result.stdout.strip()):
                markers.add(email)
    return {
        "secrets": encoded_values(secrets) | {value for value in encrypted if value},
        "markers": encoded_values(markers), "pdf_hashes": pdf_hashes, "db_hashes": db_hashes,
        "counts": {"configured_keys": len(secrets), "encrypted_key_files": len(encrypted),
                   "personal_pdf_hashes": len(pdf_hashes), "personal_database_hashes": len(db_hashes),
                   "local_identity_markers": len(markers)},
    }


class Scanner:
    def __init__(self, private, temporary: Path, byte_limit=DEFAULT_LIMIT):
        self.private, self.temporary, self.byte_limit = private, temporary, byte_limit
        self.findings, self.errors, self.artifacts = [], [], []
        self.members = self.bytes = self.carchive_members = self.pyz_modules = self.upstream_pdfs = 0

    def finding(self, name, rule):
        item = {"member": name, "rule": rule}
        if item not in self.findings:
            self.findings.append(item)
        if len(self.findings) > 1000:
            raise ValueError("Too many findings; stop and review the input allowlist")

    def path_allowed(self, name, upstream=False):
        levels = name.replace("\\", "/").split("!")
        parts = PurePosixPath("/".join(levels)).parts
        if (any(PurePosixPath(level).is_absolute() or re.match(r"^[A-Za-z]:", level)
                or "\x00" in level for level in levels)
                or any(part == ".." for part in parts)):
            self.finding(name, "unsafe_archive_path")
            return False
        if upstream:
            return True
        lowered = [part.casefold() for part in parts]
        suffix = PurePosixPath(parts[-1]).suffix.casefold() if parts else ""
        normalized = "/".join(lowered)
        public_h11_fixture = "/runtime/" in normalized and "/site-packages/h11/tests/data/" in normalized
        if (any(part in PRIVATE_PARTS or (part == "data" and not public_h11_fixture) for part in lowered)
                or any(part.startswith(".env") or part in PRIVATE_NAMES for part in lowered)
                or suffix in PRIVATE_SUFFIXES or suffix == ".pdf"):
            self.finding(name, "private_file_name")
            return False
        return True

    @staticmethod
    def is_application(name):
        path = name.replace("\\", "/").replace("!", "/")
        if "/runtime/" in path or "/licenses/" in path:
            return False
        parts = PurePosixPath(path).parts
        return (any(part in APP_PARTS for part in parts)
                or any(part.startswith("reader.") for part in parts)
                or parts[-1].casefold().startswith("readme")
                or parts[-1] in {"desktop_entry", "desktop_entry.py", "Launch-Reader.pyw"}
                or parts[-1].casefold().endswith(".exe"))

    def check_bytes(self, name, content, upstream=False, application=None):
        self.members += 1
        self.bytes += len(content)
        if self.members > 200_000 or self.bytes > self.byte_limit:
            raise ValueError("Expanded archive inspection limit exceeded")
        if any(needle in content for needle in self.private["secrets"]):
            self.finding(name, "known_api_or_encrypted_key")
        digest = hashlib.sha256(content).hexdigest()
        if digest in self.private["pdf_hashes"]:
            self.finding(name, "personal_pdf_hash")
        if digest in self.private["db_hashes"]:
            self.finding(name, "personal_database_hash")
        if upstream and name.casefold().endswith(".pdf"):
            self.upstream_pdfs += 1
        app = self.is_application(name) if application is None else application
        if app and not upstream:
            folded = content.lower() if self.private["markers"] else b""
            if any(marker.lower() in folded for marker in self.private["markers"]):
                self.finding(name, "local_identity_or_path_marker")
            # The minified browser bundle also includes unmodified PDF.js and
            # other public libraries. Generic examples/author emails in those
            # libraries, and accidental ASCII patterns in compressed PE data,
            # are not local personal information. Exact local markers and keys
            # are still checked above. Review shapes in first-party source and
            # the expanded reader bytecode, rather than the mixed bundle.
            normalized = name.replace("\\", "/").replace("!", "/")
            shape_review = "/web/dist/" not in normalized and not name.casefold().endswith(".exe")
            if shape_review:
                if USER_PATH.search(content):
                    self.finding(name, "absolute_user_directory")
                if TOKEN.search(content) or PRIVATE_KEY.search(content):
                    self.finding(name, "credential_shape_requires_review")
                if any(match.group(1).lower() not in FIXTURE_EMAIL_DOMAINS for match in EMAIL.finditer(content)):
                    self.finding(name, "non_fixture_email_requires_review")

    def inspect(self, name, content, depth=0, upstream=False):
        if depth > 12:
            raise ValueError("Nested archive depth exceeded")
        upstream = upstream or "ThirdPartySources/" in name.replace("\\", "/")
        self.check_bytes(name, content, upstream)
        lower = name.casefold()
        if lower.endswith((".zip", ".whl")):
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                self.inspect_zip(name, archive, depth, upstream)
        elif lower.endswith((".tar.gz", ".tgz", ".tar.xz", ".tar")):
            with tarfile.open(fileobj=io.BytesIO(content), mode="r:*") as archive:
                for member in archive:
                    logical = name + "!" + member.name
                    if member.isfile() and self.path_allowed(logical, upstream):
                        if member.size > 512 * 1024 ** 2:
                            raise ValueError("Archive member is too large")
                        with archive.extractfile(member) as source:
                            self.inspect(logical, source.read(), depth + 1, upstream)
        elif lower.endswith(".exe"):
            self.inspect_executable(name, content)

    def inspect_zip(self, name, archive, depth=0, upstream=False):
        for member in archive.infolist():
            if member.is_dir():
                continue
            logical = name + "!" + member.filename
            child_upstream = upstream or "ThirdPartySources/" in member.filename.replace("\\", "/")
            if self.path_allowed(logical, child_upstream):
                if member.file_size > 512 * 1024 ** 2:
                    raise ValueError("Archive member is too large")
                self.inspect(logical, archive.read(member), depth + 1, child_upstream)

    def inspect_executable(self, name, content):
        # Python's bundled pip contains ordinary Windows launcher executables
        # even in a Mac runtime. Inspect their bytes, but do not treat them as
        # PyInstaller archives. Our actual application must contain its archive.
        if b'MEI\014\013\012\013\016' not in content:
            basename = name.replace("\\", "/").replace("!", "/").rsplit("/", 1)[-1]
            if basename.casefold() in {"paperreader.exe", "文献阅读器.exe"}:
                raise ValueError("Application executable is missing its embedded archive")
            return
        try:
            from PyInstaller.archive.readers import CArchiveReader
        except ImportError:
            raise ValueError("PyInstaller is required to inspect a Windows executable") from None
        path = self.temporary / (hashlib.sha256(content).hexdigest() + ".exe")
        path.write_bytes(content)
        try:
            archive = CArchiveReader(str(path))
            for member, entry in archive.toc.items():
                self.carchive_members += 1
                logical = name + "!" + member
                if entry[-1] == "z":
                    embedded = archive.open_embedded_archive(member)
                    for module in embedded.toc:
                        self.pyz_modules += 1
                        value = embedded.extract(module)
                        if value is not None:
                            data = marshal.dumps(value) if isinstance(value, types.CodeType) else value
                            if isinstance(data, bytes):
                                self.check_bytes(logical + "!" + module, data,
                                                 application=module == "reader" or module.startswith("reader."))
                elif self.path_allowed(logical):
                    data = archive.extract(member)
                    if isinstance(data, bytes):
                        self.inspect(logical, data)
        finally:
            path.unlink(missing_ok=True)

    def scan_source(self, root):
        root = Path(root).resolve()
        for path in sorted(root.rglob("*")):
            if path.is_file():
                relative = path.relative_to(root).as_posix()
                name = "source/" + relative
                if path.is_symlink():
                    self.finding(name, "source_symlink_requires_review")
                elif self.path_allowed(name):
                    self.check_bytes(name, path.read_bytes())

    def scan_archive(self, path):
        path = Path(path)
        self.artifacts.append({"name": path.name, "bytes": path.stat().st_size, "sha256": digest_file(path)})
        if path.suffix.casefold() == ".zip":
            with zipfile.ZipFile(path) as archive:
                self.inspect_zip(path.name, archive)
        else:
            self.inspect(path.name, path.read_bytes())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, help="A frozen public allowlist snapshot, never a live personal checkout")
    parser.add_argument("--archive", action="append", type=Path, default=[], help="Release ZIP or executable; repeat as needed")
    parser.add_argument("--private-root", type=Path, help="Explicit local-only known-key, paper-hash and identity comparison")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--byte-limit", type=int, default=DEFAULT_LIMIT)
    args = parser.parse_args()
    if not args.source and not args.archive:
        parser.error("Provide --source and/or --archive")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    private = private_inputs(args.private_root)
    temporary_parent = (args.report.parent / "inspection-temp").resolve()
    temporary_parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="release-", dir=temporary_parent) as directory:
        temporary = Path(directory).resolve()
        if not temporary.is_relative_to(temporary_parent):
            raise RuntimeError("Unexpected inspection temporary directory")
        scanner = Scanner(private, temporary, args.byte_limit)
        try:
            if args.source:
                scanner.scan_source(args.source)
            for archive in args.archive:
                scanner.scan_archive(archive)
        except Exception as error:
            # Do not include exception text: filesystem/library messages may
            # reveal the operator's username or local input paths.
            scanner.errors.append(type(error).__name__)
        report = {
            "status": "failed" if scanner.findings or scanner.errors else "passed",
            "private_values_logged": False, "private_comparison_enabled": args.private_root is not None,
            "input_checks": private["counts"], "artifacts": scanner.artifacts,
            "expanded_members": scanner.members, "expanded_bytes": scanner.bytes,
            "carchive_members": scanner.carchive_members, "pyz_modules": scanner.pyz_modules,
            "upstream_test_pdfs_checked": scanner.upstream_pdfs,
            "findings": scanner.findings, "errors": scanner.errors,
            "limits": ["Exact byte and hash comparisons cannot identify every transformed secret or excerpt.",
                       "Documentation images require visual review.",
                       "Public dependency source fixtures are permitted; known private bytes and hashes are still checked."],
        }
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({key: report[key] for key in ("status", "expanded_members", "expanded_bytes", "carchive_members", "pyz_modules")}))
        if scanner.findings or scanner.errors:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
