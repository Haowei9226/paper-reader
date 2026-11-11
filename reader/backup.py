from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import time
import zipfile
from contextlib import closing
from pathlib import Path, PurePosixPath

from .store import Store, dumps, uid


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        while data := stream.read(1024 * 1024):
            value.update(data)
    return value.hexdigest()


def create_backup(store: Store, progress=None):
    name = f"library-{time.strftime('%Y%m%d-%H%M%S')}-{uid()[:6]}.zip"
    destination = store.root / "backups" / name
    snapshot = store.root / "incoming" / (uid() + ".sqlite3")
    with store.connect() as source, closing(sqlite3.connect(snapshot)) as target:
        source.backup(target)
    with closing(sqlite3.connect(snapshot)) as db:
        papers = [r[0] for r in db.execute("SELECT id FROM papers")]
        assets = [r[0] for r in db.execute("SELECT asset FROM blocks WHERE asset IS NOT NULL")]
    files = [(snapshot, "library.sqlite3")]
    for paper_id in papers:
        files.append((store.original(paper_id), "originals/" + paper_id + ".pdf"))
    for asset in dict.fromkeys(assets):
        path = (store.root / "assets" / asset).resolve()
        if not path.is_relative_to((store.root / "assets").resolve()):
            raise ValueError("附件路径不合法")
        files.append((path, "assets/" + asset.replace("\\", "/")))
    manifest = {"format": "paper-reader-backup", "version": 1, "created": time.time(), "files": []}
    try:
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=3, allowZip64=True) as archive:
            for i, (path, arcname) in enumerate(files):
                if progress:
                    progress(i, len(files), f"正在备份 {i + 1}/{len(files)} 个文件")
                if not path.is_file():
                    raise ValueError("有文献附件缺失，备份已停止，请先检查文献库")
                manifest["files"].append({"name": arcname, "size": path.stat().st_size, "sha256": digest(path)})
                archive.write(path, arcname)
            archive.writestr("manifest.json", dumps(manifest))
        return destination
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    finally:
        snapshot.unlink(missing_ok=True)


def prepare_restore(store: Store, archive_path: Path):
    stage = store.root.parent / (store.root.name + "-restore-" + uid())
    stage.mkdir()
    try:
        with zipfile.ZipFile(archive_path) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or "manifest.json" not in names:
                raise ValueError("备份文件清单无效")
            if archive.getinfo("manifest.json").file_size > 20 * 1024 * 1024:
                raise ValueError("备份文件清单过大")
            manifest = json.loads(archive.read("manifest.json"))
            if manifest.get("format") != "paper-reader-backup" or manifest.get("version") != 1:
                raise ValueError("不支持此备份格式")
            entries = manifest.get("files", [])
            expected = {e["name"] for e in entries}
            if set(names) != expected | {"manifest.json"} or len(entries) != len(expected) or "library.sqlite3" not in expected:
                raise ValueError("备份内容与清单不一致")
            for entry in entries:
                pure = PurePosixPath(entry["name"])
                if pure.is_absolute() or ".." in pure.parts or "\\" in entry["name"] or ":" in entry["name"]:
                    raise ValueError("备份含有不合法路径")
                if entry["name"] != "library.sqlite3" and pure.parts[0] not in ("assets", "originals"):
                    raise ValueError("备份含有不支持的文件")
                info = archive.getinfo(entry["name"])
                if info.file_size != entry["size"] or (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError("备份文件属性无效")
                target = stage.joinpath(*pure.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry["name"]) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
                if digest(target) != entry["sha256"]:
                    raise ValueError("备份校验失败，文献库没有被替换")
        with closing(sqlite3.connect(stage / "library.sqlite3")) as db:
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or db.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("备份数据库完整性校验失败")
            if db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] != "1":
                raise ValueError("此备份需要其他版本的阅读器")
            for (paper_id,) in db.execute("SELECT id FROM papers"):
                if not (stage / "originals" / (paper_id + ".pdf")).is_file():
                    raise ValueError("备份缺少 PDF 附件")
            db.execute("UPDATE jobs SET state='paused',message='从备份恢复，可继续任务' WHERE state IN ('queued','running')")
            db.commit()
        return stage
    except BaseException:
        # This directory was created above and cannot refer to user-selected paths.
        if stage.parent == store.root.parent and stage.name.startswith(store.root.name + "-restore-"):
            shutil.rmtree(stage)
        raise
