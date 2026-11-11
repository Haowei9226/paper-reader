"""Combine platform archives with one copy of source and license materials."""
import hashlib
import json
from pathlib import Path
import re
import zipfile

ROOT = Path(__file__).resolve().parent.parent


def build():
    version = re.search(r'^VERSION = "([^"]+)"', (ROOT / "reader" / "__init__.py").read_text(encoding="utf-8"), re.M).group(1)
    release = ROOT / "output" / "releases"
    common = ROOT / "output" / "release-common"
    target = release / f"PaperReader-Share-{version}.zip"
    files = [release / f"PaperReader-Windows-{version}.zip", release / f"PaperReader-macOS-{version}.zip"]
    start = ("文献阅读器 · Windows + Mac 本地分享版\n\n"
             "先将本压缩包完整解压，再按系统解压其中一个平台包：\n"
             f"Windows：PaperReader-Windows-{version}.zip → 文献阅读器.exe\n"
             f"Mac：PaperReader-macOS-{version}.zip → 文献阅读器.app\n"
             "Mac用Finder解压可保留应用执行权限和符号链接；支持Apple芯片和Intel。\n\n"
             "详细步骤、AI配置、数据位置和Mac验证范围，请阅读「使用说明.txt」。\n"
             "首次运行是空文献库，包内不含原分享者的API密钥、论文或笔记。\n"
             "无须安装Python或Node.js。Mac包未经真实Mac运行验证。\n"
             "Source.zip、ThirdPartySources/和licenses/是随附源码与许可，无须运行。\n")
    checksums = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
    staging = target.with_suffix(".building.zip")
    with zipfile.ZipFile(staging, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        archive.writestr("先读我.txt", start)
        for path in files:
            # Keep the macOS archive intact: Windows extraction loses Unix modes.
            archive.write(path, path.name, compress_type=zipfile.ZIP_STORED)
        for path in sorted(common.rglob("*")):
            if path.is_file():
                relative = path.relative_to(common)
                if relative.parts[0] not in {"Source.zip", "使用说明.txt", "licenses", "ThirdPartySources"}:
                    raise ValueError(f"Unexpected common file: {relative}")
                archive.write(path, relative.as_posix(), compress_type=zipfile.ZIP_STORED if path.suffix in {".zip", ".gz"} else zipfile.ZIP_DEFLATED)
        archive.writestr("SHA256.json", json.dumps(checksums, indent=2) + "\n")
    staging.replace(target)
    with zipfile.ZipFile(target) as archive:
        if archive.testzip():
            raise RuntimeError("Share ZIP failed integrity verification")
    report = {"path": str(target), "bytes": target.stat().st_size, "sha256": hashlib.sha256(target.read_bytes()).hexdigest(), "platform_archives": checksums}
    (release / "share-manifest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return target


if __name__ == "__main__":
    build()
