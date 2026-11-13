"""Assemble only distributable instructions, source code and license materials."""
from importlib import metadata
from pathlib import Path
import re
import shutil
import sys
import zipfile

ROOT = Path(__file__).resolve().parent.parent
COMMON = ROOT / "output" / "release-common"
VERSION = re.search(r'^VERSION = "([^"]+)"', (ROOT / "reader" / "__init__.py").read_text(encoding="utf-8"), re.M).group(1)

BUILDING = """Rebuilding PaperReader __VERSION__

This application distribution is provided under GNU AGPL v3 (see LICENSE).
Third-party code retains its own notices and licenses. The complete application
source and build scripts are included here; no personal library is included.
The unmodified PyMuPDF 1.28.2 and MuPDF 1.28.2 source archives are provided in
the distribution's ThirdPartySources directory.

Build prerequisites: Python 3.12 and Node.js 22.
1. Create a clean virtualenv and install requirements.lock, plus PyInstaller 6.20.0
   for a Windows executable. Do not include a developer's data/ or .env files.
2. In web/, run npm ci and npm run build.
3. In the project root, run scripts/package_common.py with the build environment.
   Put the official AGPL text in LICENSE and the exact PyMuPDF source archive in
   .cache/package-downloads/{pymupdf-1.28.2.tar.gz,mupdf-1.28.2-source.tar.gz}
   before generating the common files. Official source SHA-256 values:
   PyMuPDF:
   5e0be7908a715aa20333caddd73f1d6f01e4cd0c26e869fa2dd0b7f344da2249
   MuPDF:
   44075a84e329db55b9bef5f342a70fd26d69e48ad1d33cb89d9664581c641156
4. On Windows, run python scripts/package_windows.py --program-only.
5. For macOS, run python scripts/package_macos.py --output
   output/releases/PaperReader-macOS-__VERSION__.zip (on one command line).
   This assembles published Astral Python runtimes and PyPI wheels for both
   architectures without compiling/executing Mac binaries on the build host.
   Runtimes are pinned and SHA-256 checked. Real macOS testing is still required.
6. Run python scripts/package_share.py to combine both platform archives with
   one copy of the instructions, application source, engine source and licenses.
   Distribute these source and license materials alongside platform-only ZIPs.

Source-mode development: python -m reader --no-browser --port 18767
Use READER_DATA_DIR pointing to an empty directory for testing.
The packaged launcher sets READER_SHARED_BUILD=1, which chooses a per-user data
directory and defaults to offline metadata. Users connect their own Ollama
installation. Windows frozen workers use --reader-worker; source-mode workers
use python -m reader.worker. Native Mac API keys use the system login Keychain.
""".replace("__VERSION__", VERSION)


def build():
    COMMON.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "docs" / "SHARING.txt", COMMON / "使用说明.txt")
    licenses = COMMON / "licenses"
    licenses.mkdir(exist_ok=True)
    shutil.copy2(ROOT / "LICENSE", licenses / "AGPL-3.0.txt")
    sources = []
    packages = []
    for line in (ROOT / "requirements.lock").read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            name, version = line.split("==")
            packages.append(name)
            sources.append(f"{name} {version}: https://pypi.org/project/{name}/{version}/#files")
    packages.append("pyinstaller")
    for name in packages:
        dist = metadata.distribution(name)
        for file in dist.files or []:
            if any(part.upper().startswith(("LICENSE", "LICENCE", "COPYING", "NOTICE")) for part in file.parts):
                source = Path(dist.locate_file(file))
                if not source.is_file():
                    continue
                destination = licenses / name / Path(*file.parts[1:])
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
    for name in ("react", "react-dom", "scheduler", "lucide-react", "pdfjs-dist"):
        path = ROOT / "web" / "node_modules" / name
        for file in path.rglob("*"):
            if file.is_file() and file.name.upper().startswith(("LICENSE", "LICENCE", "COPYING", "NOTICE")):
                destination = licenses / name / file.relative_to(path)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(file, destination)
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if python_license.is_file():
        shutil.copy2(python_license, licenses / "Python-LICENSE.txt")
    (licenses / "SOURCES.txt").write_text(
        "PaperReader application: GNU AGPL v3. Source.zip contains the corresponding application source.\n"
        "PyMuPDF 1.28.2 / MuPDF: GNU AGPL v3; unmodified source in ThirdPartySources.\n"
        "PyMuPDF upstream: https://github.com/pymupdf/PyMuPDF/tree/1.28.2\n"
        "MuPDF upstream: https://github.com/ArtifexSoftware/mupdf\n"
        "Python: https://www.python.org/downloads/source/\n"
        "macOS Python runtimes: https://github.com/astral-sh/python-build-standalone/releases/tag/20261110\n"
        "PyInstaller bootloader exception: https://pyinstaller.org/en/stable/license.html\n"
        "Frontend exact versions and upstream sources: Source.zip / web/package-lock.json\n\n"
        + "\n".join(sources) + "\n", encoding="utf-8")
    third_party = COMMON / "ThirdPartySources"
    third_party.mkdir(exist_ok=True)
    for filename in ("pymupdf-1.28.2.tar.gz", "mupdf-1.28.2-source.tar.gz"):
        sdist = ROOT / ".cache" / "package-downloads" / filename
        if sdist.is_file():
            shutil.copy2(sdist, third_party / sdist.name)
        else:
            raise SystemExit("Download and verify the exact PDF engine source archives first (see REBUILD.txt).")
    files = []
    for folder, suffixes in (("reader", {".py", ".ps1", ".css"}), ("scripts", {".py", ".sh", ".command"}),
                             ("tests", {".py"}), ("web/src", {".tsx", ".ts", ".css", ".svg"}),
                             ("web/scripts", {".mjs", ".js", ".ts"}), ("docs/images", {".png", ".svg", ".jpg", ".jpeg", ".webp"})):
        for path in (ROOT / folder).rglob("*"):
            if path.is_file() and path.suffix in suffixes and "__pycache__" not in path.parts:
                files.append(path)
    for name in (".gitignore", ".gitattributes", "LICENSE", "README.md", "README.en.md", "pyproject.toml", "requirements.lock", "requirements.txt", "Launch-Reader.pyw",
                 "Start-Reader.cmd", "Setup.cmd", "Create-Shortcut.ps1", "assets/reader.ico", "web/index.html",
                 "web/tsconfig.json", "web/vite.config.ts", "web/package.json", "web/package-lock.json",
                 "web/scripts/copy-pdf-assets.mjs", "web/public/icon.svg", "docs/SHARING.txt"):
        files.append(ROOT / name)
    with zipfile.ZipFile(COMMON / "Source.zip", "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(set(files)):
            archive.write(path, "Source/" + path.relative_to(ROOT).as_posix())
        archive.writestr("Source/REBUILD.txt", BUILDING)
    print(COMMON)


if __name__ == "__main__":
    build()
