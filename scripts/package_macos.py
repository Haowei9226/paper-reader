"""Build an offline, dual-architecture macOS app without executing Mac binaries.

The Windows build host can download and validate official Python runtimes and
platform wheels, then put their Unix modes and symlinks directly into a ZIP.
Actual macOS launch/Keychain checks still require a Mac.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import posixpath
import re
import stat
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "scripts" / "packaging"
PYTHON_VERSION = "3.12.16"
PYTHON_RELEASE = "20261110"
RUNTIMES = {
    "arm64": {
        "triple": "aarch64-apple-darwin",
        "platform": "macosx_11_0_arm64",
        "sha256": "2f11b8aa3726bdbd87dd9eb76568412faddf4f47e26913fa45d6a2e424972acd",
    },
    "x86_64": {
        "triple": "x86_64-apple-darwin",
        "platform": "macosx_10_15_x86_64",
        "sha256": "b78a4c479f1964ec484880bee567568266f68da102269fb93a05fe8716b45f91",
    },
}
PACKAGE_ROOT = "PaperReader-macOS"
APP = PACKAGE_ROOT + "/文献阅读器.app"
RESOURCES = APP + "/Contents/Resources"
PRIVATE_PARTS = {"data", "originals", "backups", "exports", "incoming", "__pycache__", ".git", ".cache", "node_modules"}


def sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


def download(url: str, target: Path, expected: str) -> Path:
    if target.is_file() and sha256(target) == expected:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".download")
    print(f"Downloading {target.name}", flush=True)
    request = urllib.request.Request(url, headers={"User-Agent": "PaperReader-Packager/1"})
    try:
        with urllib.request.urlopen(request, timeout=90) as source, temporary.open("wb") as output:
            while chunk := source.read(1024 * 1024):
                output.write(chunk)
        if sha256(temporary) != expected:
            raise ValueError(f"SHA-256 mismatch: {target.name}")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def prepare(cache: Path, offline=False):
    prepared = {}
    pins = {}
    for line in (ROOT / "requirements.lock").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^\s;]+)", line)
        if not match:
            raise ValueError("Mac packaging requires exact, unconditional dependency pins")
        pins[re.sub(r"[-_.]+", "_", match[1]).lower()] = match[2]
    for arch, runtime in RUNTIMES.items():
        filename = f"cpython-{PYTHON_VERSION}+{PYTHON_RELEASE}-{runtime['triple']}-install_only_stripped.tar.gz"
        url = f"https://github.com/astral-sh/python-build-standalone/releases/download/{PYTHON_RELEASE}/{filename}"
        archive = cache / filename
        if offline:
            if not archive.is_file() or sha256(archive) != runtime["sha256"]:
                raise ValueError(f"Offline cache missing or invalid: {filename}")
        else:
            archive = download(url, archive, runtime["sha256"])
        wheels = cache / ("wheels-" + arch)
        wheels.mkdir(parents=True, exist_ok=True)
        print(f"Preparing {arch} Python dependency wheels", flush=True)
        command = [
            sys.executable, "-m", "pip", "download", "--quiet", "--disable-pip-version-check",
            "--platform", runtime["platform"], "--python-version", "312",
            "--implementation", "cp", "--abi", "cp312", "--only-binary=:all:",
            "--dest", str(wheels), "-r", str(ROOT / "requirements.lock"),
        ]
        if offline:
            command.extend(["--no-index", "--find-links", str(wheels)])
        subprocess.run(command, check=True)
        selected = {}
        for path in sorted(wheels.glob("*.whl")):
            distribution, version, *_ = path.name.split("-")
            distribution = re.sub(r"[-_.]+", "_", distribution).lower()
            if pins.get(distribution) == version:
                if distribution in selected:
                    raise ValueError(f"Ambiguous cached dependency wheel: {distribution}")
                selected[distribution] = path
        if selected.keys() != pins.keys():
            raise ValueError(f"Pinned dependency wheel missing for {arch}")
        prepared[arch] = {"archive": archive, "wheels": sorted(selected.values()), "url": url}
    return prepared


def safe_name(name: str) -> str:
    if "\\" in name or "\x00" in name or ":" in name:
        raise ValueError(f"Unsupported archive path: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Unsafe archive path: {name!r}")
    return str(path)


def zip_info(name: str, mode: int = 0o644, *, symlink=False, directory=False):
    item = zipfile.ZipInfo(name.rstrip("/") + ("/" if directory else ""))
    item.create_system = 3
    kind = stat.S_IFLNK if symlink else stat.S_IFDIR if directory else stat.S_IFREG
    item.external_attr = ((kind | (mode & 0o777)) << 16) | (0x10 if directory else 0)
    item.compress_type = zipfile.ZIP_DEFLATED
    return item


class Writer:
    def __init__(self, archive):
        self.archive, self.names = archive, set()

    def write(self, name: str, value: bytes, mode=0o644, *, symlink=False, directory=False):
        name = safe_name(name).rstrip("/") + ("/" if directory else "")
        if name in self.names:
            raise ValueError(f"Duplicate package member: {name}")
        self.names.add(name)
        self.archive.writestr(zip_info(name, mode, symlink=symlink, directory=directory), value)

    def file(self, name: str, path: Path, mode=0o644):
        self.write(name, path.read_bytes(), mode)


def wheel_destination(name: str) -> str:
    """Map a wheel's .data categories into an ordinary Python installation."""
    parts = PurePosixPath(safe_name(name)).parts
    site = "python/lib/python3.12/site-packages"
    if parts[0].endswith(".data"):
        if len(parts) < 3:
            raise ValueError(f"Malformed wheel data member: {name}")
        category, tail = parts[1], "/".join(parts[2:])
        destinations = {"purelib": site, "platlib": site, "scripts": "python/bin",
                        "headers": "python/include/python3.12", "data": "python"}
        if category not in destinations:
            raise ValueError(f"Unsupported wheel data category: {category}")
        return destinations[category] + "/" + tail
    return site + "/" + "/".join(parts)


def install_runtime(writer: Writer, prefix: str, source):
    overlay = {}
    for wheel in source["wheels"]:
        with zipfile.ZipFile(wheel) as archive:
            for member in archive.infolist():
                if member.is_dir():
                    continue
                destination = wheel_destination(member.filename)
                if destination in overlay:
                    raise ValueError(f"Dependency wheels overlap: {destination}")
                overlay[destination] = (wheel, member.filename)
    with tarfile.open(source["archive"], "r:gz") as archive:
        for member in archive:
            name = safe_name(member.name)
            if not name.startswith("python/") and name != "python":
                raise ValueError(f"Unexpected Python runtime member: {name}")
            if name in overlay or member.isdir():
                continue
            destination = prefix + "/" + name
            if member.issym():
                target = member.linkname
                if target.startswith("/") or "\\" in target or ":" in target:
                    raise ValueError(f"Unsafe runtime symlink: {name}")
                resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
                if not resolved.startswith("python/"):
                    raise ValueError(f"Runtime symlink escapes installation: {name}")
                writer.write(destination, target.encode("utf-8"), member.mode, symlink=True)
            elif member.isfile():
                with archive.extractfile(member) as stream:
                    writer.write(destination, stream.read(), member.mode)
            elif member.islnk():
                target = safe_name(member.linkname)
                if not target.startswith("python/"):
                    raise ValueError(f"Unsafe runtime hard link: {name}")
                with archive.extractfile(member) as stream:
                    writer.write(destination, stream.read(), member.mode)
            else:
                raise ValueError(f"Unsupported runtime member: {name}")
    for destination, (wheel, name) in overlay.items():
        with zipfile.ZipFile(wheel) as archive:
            member = archive.getinfo(name)
            mode = (member.external_attr >> 16) & 0o777 or 0o644
            writer.write(prefix + "/" + destination, archive.read(member), mode)


def app_files(writer: Writer, version: str, common: Path | None):
    entry = TEMPLATES / "desktop_entry.py"
    dist = ROOT / "web" / "dist"
    if not entry.is_file() or not (dist / "index.html").is_file():
        raise ValueError("Build the frontend and provide scripts/packaging/desktop_entry.py first")
    info = {
        "CFBundleDevelopmentRegion": "zh_CN", "CFBundleName": "文献阅读器",
        "CFBundleDisplayName": "文献阅读器", "CFBundleIdentifier": "local.paperreader.desktop",
        "CFBundleExecutable": "PaperReader", "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": version, "CFBundleVersion": version,
        "LSMinimumSystemVersion": "11.0", "NSHighResolutionCapable": True,
        "LSUIElement": True,
    }
    writer.write(APP + "/Contents/Info.plist", plistlib.dumps(info))
    writer.write(APP + "/Contents/PkgInfo", b"APPL????")
    writer.file(APP + "/Contents/MacOS/PaperReader", TEMPLATES / "macos_launcher.sh", 0o755)
    writer.file(PACKAGE_ROOT + "/启动阅读器.command", TEMPLATES / "macos_start.command", 0o755)
    writer.file(RESOURCES + "/Launch-Reader.pyw", entry)
    for source, target in ((ROOT / "reader", "reader"), (dist, "web/dist")):
        for path in sorted(source.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(source)
            if any(part in PRIVATE_PARTS or part.startswith(".env") for part in relative.parts):
                continue
            if path.suffix.lower() in (".pyc", ".pyo", ".pdf", ".sqlite3", ".dpapi", ".log", ".map"):
                continue
            if source.name == "reader" and path.suffix not in (".py", ".css", ".ps1"):
                raise ValueError(f"Unexpected application file: {relative}")
            writer.file(RESOURCES + "/" + target + "/" + relative.as_posix(), path)
    if common:
        for path in sorted(common.rglob("*")):
            if path.is_file():
                relative = path.relative_to(common)
                if any(part in PRIVATE_PARTS or part.startswith(".env") for part in relative.parts):
                    raise ValueError(f"Private common content: {relative}")
                if path.suffix.lower() in (".pdf", ".sqlite3", ".dpapi", ".log"):
                    raise ValueError(f"Private common content: {relative}")
                writer.file(PACKAGE_ROOT + "/" + relative.as_posix(), path)


def build(destination: Path, prepared, common):
    version = re.search(r'^VERSION = "([^"]+)"', (ROOT / "reader" / "__init__.py").read_text(encoding="utf-8"), re.M).group(1)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_suffix(".building.zip")
    manifest = {"application": "paper-reader", "version": version, "platform": "macOS",
                "architectures": list(prepared), "python": PYTHON_VERSION,
                "data_directory": "~/Library/Application Support/PaperReader/data",
                "personal_data_included": False, "api_keys_included": False,
                "mac_execution_verified": False, "runtimes": {}}
    try:
        with zipfile.ZipFile(staging, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
            writer = Writer(archive)
            app_files(writer, version, common)
            for arch, source in prepared.items():
                print(f"Archiving {arch} runtime and dependencies", flush=True)
                install_runtime(writer, RESOURCES + "/runtime/" + arch, source)
                manifest["runtimes"][arch] = {
                    "source": source["url"], "sha256": sha256(source["archive"]),
                    "wheels": [{"name": wheel.name, "sha256": sha256(wheel)} for wheel in source["wheels"]],
                }
            writer.write(PACKAGE_ROOT + "/PACKAGE-MANIFEST.json", (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode())
        with zipfile.ZipFile(staging) as archive:
            bad = archive.testzip()
            if bad:
                raise ValueError(f"ZIP verification failed: {bad}")
        os.replace(staging, destination)
    finally:
        staging.unlink(missing_ok=True)
    print(json.dumps({"path": str(destination), "bytes": destination.stat().st_size, "sha256": sha256(destination),
                      "mac_execution_verified": False}, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=ROOT / "output" / "package-cache" / "macOS")
    parser.add_argument("--output", type=Path, default=ROOT / "output" / "release" / "PaperReader-macOS.zip")
    parser.add_argument("--common", type=Path, help="Reviewed README, source archive and licenses to include")
    parser.add_argument("--prepare-only", action="store_true", help="Download and verify runtimes and dependency wheels")
    parser.add_argument("--offline", action="store_true", help="Build using already downloaded runtimes and wheels")
    args = parser.parse_args()
    prepared = prepare(args.cache.resolve(), args.offline)
    if not args.prepare_only:
        build(args.output.resolve(), prepared, args.common.resolve() if args.common else None)


if __name__ == "__main__":
    main()
