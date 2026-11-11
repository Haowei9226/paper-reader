"""Build a Windows x64 executable from an explicit, private-data-free allowlist.

Run with the clean packaging environment after building web/dist:
  .cache/package-venv/Scripts/python.exe scripts/package_windows.py
"""
import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from reader import VERSION


def build(common=None):
    if sys.platform != "win32":
        raise SystemExit("Build the Windows executable on Windows.")
    build_root = ROOT / "output" / "build"
    build_root.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="windows-", dir=build_root))
    shutil.copytree(ROOT / "reader", stage / "reader", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(ROOT / "web" / "dist", stage / "web" / "dist")
    shutil.copy2(ROOT / "scripts" / "packaging" / "desktop_entry.py", stage / "desktop_entry.py")
    shutil.copy2(ROOT / "assets" / "reader.ico", stage / "reader.ico")
    # Analysis sees only this staging directory and installed public dependencies.
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--windowed",
               "--name", "PaperReader", "--icon", "reader.ico", "--noupx",
               "--add-data", "web/dist;web/dist", "--add-data", "reader/reading.css;reader",
               "--add-data", "reader/windows_ocr.ps1;reader", "--collect-submodules", "uvicorn",
               "--collect-all", "pymupdf", "desktop_entry.py"]
    subprocess.run(command, cwd=stage, check=True)
    release = ROOT / "output" / "releases"
    release.mkdir(parents=True, exist_ok=True)
    target = release / f"PaperReader-Windows-{VERSION}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        archive.write(stage / "dist" / "PaperReader.exe", "PaperReader-Windows/文献阅读器.exe")
        if common:
            for path in sorted(Path(common).rglob("*")):
                if path.is_file():
                    archive.write(path, "PaperReader-Windows/" + path.relative_to(common).as_posix())
    print(target)
    print("Executable:", stage / "dist" / "PaperReader.exe")
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--common", type=Path, default=ROOT / "output" / "release-common")
    parser.add_argument("--program-only", action="store_true", help="For a combined share ZIP with common materials alongside platform ZIPs")
    args = parser.parse_args()
    build(None if args.program_only else args.common)
