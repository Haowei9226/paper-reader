from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import mimetypes
import os
import re
import shutil
import threading
import time
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import DATA, ROOT, VERSION, ocr
from .backup import prepare_restore
from .counterpart import selection_counterpart
from .jobs import Scheduler
from .metadata import (BIB_FIELDS, ITEM_TYPES, PUBLICATION_EXPORT_NOTES, PUBLICATION_SKIP_MESSAGES, PUBLICATION_STATUSES,
                       bibliography_filename, missing_publication_fields, pdf_filename,
                       publication_date, refresh_pdf_authors, schedule_lookup)
from .navigation import find_in_paper, reading_locator
from .pdf import COLORS, export_pdf, length, page_data, rects_for_range, slice_text
from .reading import export_reading_html
from .runtime import Runtime, local_url
from .secrets import CLOUD_NAMES, Secrets
from .store import Store, dumps, index_text, now, search_tokens, uid, unpack

# Windows registry MIME associations may otherwise serve module workers as text/plain.
mimetypes.add_type("text/javascript", ".mjs")
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("application/wasm", ".wasm")

PAPER_SORT_COLUMNS = {
    "updated": "p.updated",
    "created": "p.created",
    "title": "p.title COLLATE NOCASE",
    "authors": "NULLIF(TRIM(p.authors), '') COLLATE NOCASE",
    "year": "NULLIF(CAST(p.year AS INTEGER), 0)",
    "item_type": "NULLIF(p.item_type, '')",
    "publication_date": "COALESCE(NULLIF(p.publication_date, ''), NULLIF(TRIM(p.year), ''))",
    "publisher": "NULLIF(p.publisher, '') COLLATE NOCASE",
    "source": "NULLIF(TRIM(p.source), '') COLLATE NOCASE",
    "last_read_at": "p.last_read_at",
    "pages": "NULLIF(p.pages, 0)",
    "status": "CASE p.status WHEN 'unread' THEN 0 WHEN 'reading' THEN 1 ELSE 2 END",
    "annotations_count": "(SELECT count(*) FROM annotations WHERE paper_id=p.id)",
    "translated_count": "(SELECT count(*) FROM translations WHERE paper_id=p.id AND active=1)",
}
PAPER_EDIT_FIELDS = (*BIB_FIELDS, "publication_status", "language", "filename")
MAX_BATCH_PAPERS = 100


def create_app(root: Path = DATA, *, background=True, stop_server=None, frontend_dir: Path | None = None):
    store = Store(root)
    runtime = Runtime(store.root)
    maintenance = threading.Event()
    scheduler = None

    @asynccontextmanager
    async def lifespan(app):
        nonlocal scheduler
        await asyncio.to_thread(store.finish_purges)
        if background:
            scheduler = Scheduler(store)
            scheduler.start()
        yield
        if scheduler:
            scheduler.close()
        runtime.close()

    app = FastAPI(title="文献阅读器", version=VERSION, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.store = store
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"])

    @app.middleware("http")
    async def local_access(request: Request, call_next):
        origin = request.headers.get("origin")
        allowed_origins = ("http://" + request.headers.get("host", ""), "http://127.0.0.1:5178", "http://localhost:5178")
        if request.method not in ("GET", "HEAD", "OPTIONS") and origin and origin not in allowed_origins:
            return JSONResponse({"detail": "不接受其他网站发起的文献库修改"}, status_code=403)
        if maintenance.is_set() and request.url.path != "/api/health":
            return JSONResponse({"detail": "文献库正在恢复，请稍后重试"}, status_code=503)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ValueError)
    async def invalid_value(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    def need_paper(paper_id):
        paper = store.paper(paper_id)
        if not paper:
            raise HTTPException(404, "文献不存在")
        return paper

    @app.get("/api/health")
    def health():
        return {"app": "paper-reader", "version": VERSION, "maintenance": maintenance.is_set(),
                "library_id": hashlib.sha256(str(store.root).encode()).hexdigest()}

    @app.post("/api/app/quit")
    async def quit_reader():
        if stop_server is None:
            raise HTTPException(409, "请从快捷方式启动阅读器后再使用此按钮")
        stop_server()
        return {"ok": True}

    def cloud_settings():
        return {f"{provider}_configured": Secrets(store.root, provider).present() for provider in CLOUD_NAMES}

    @app.get("/api/bootstrap")
    def bootstrap():
        with store.connect() as db:
            counts = dict(db.execute("SELECT status,count(*) FROM papers WHERE deleted_at IS NULL GROUP BY status").fetchall())
            counts["all"] = db.execute("SELECT count(*) FROM papers WHERE deleted_at IS NULL").fetchone()[0]
            counts["starred"] = db.execute("SELECT count(*) FROM papers WHERE starred=1 AND deleted_at IS NULL").fetchone()[0]
            counts["trash"] = db.execute("SELECT count(*) FROM papers WHERE deleted_at IS NOT NULL").fetchone()[0]
            counts["notes"] = db.execute("SELECT count(*) FROM annotations a JOIN papers p ON p.id=a.paper_id WHERE p.deleted_at IS NULL").fetchone()[0]
            collections = [dict(r) for r in db.execute("SELECT c.*,(SELECT count(*) FROM paper_collections pc JOIN papers p ON p.id=pc.paper_id WHERE pc.collection_id=c.id AND p.deleted_at IS NULL) AS count FROM collections c ORDER BY created")]
            tags = [dict(r) for r in db.execute("SELECT t.name,count(*) AS count FROM tags t JOIN papers p ON p.id=t.paper_id WHERE p.deleted_at IS NULL GROUP BY t.name ORDER BY count DESC,name LIMIT 100")]
        return {"counts": counts, "collections": collections, "tags": tags, "settings": store.settings(), **cloud_settings(), "version": VERSION}

    @app.get("/api/papers")
    def papers(q: str = "", collection: str = "", tag: str = "", status: str = "", starred: bool = False,
               trash: bool = False, sort: str = "updated", direction: Literal["asc", "desc"] | None = None,
               offset: int = 0, limit: int = 100):
        limit, offset = min(max(limit, 1), 200), max(offset, 0)
        where, args = ["p.deleted_at IS NOT NULL" if trash else "p.deleted_at IS NULL"], []
        if collection:
            where.append("p.id IN (WITH RECURSIVE nested(id) AS (SELECT id FROM collections WHERE id=? UNION ALL SELECT c.id FROM collections c JOIN nested n ON c.parent_id=n.id) SELECT paper_id FROM paper_collections WHERE collection_id IN (SELECT id FROM nested))")
            args.append(collection)
        if tag:
            where.append("EXISTS(SELECT 1 FROM tags WHERE paper_id=p.id AND name=?)")
            args.append(tag)
        if status in ("unread", "reading", "read"):
            where.append("p.status=?")
            args.append(status)
        if starred:
            where.append("p.starred=1")
        if q.strip():
            tokens = list(dict.fromkeys(search_tokens(q.strip())))[:16]
            if tokens:
                match = " AND ".join('"' + t.replace('"', '""') + '"' for t in tokens)
                where.append("(p.id IN (SELECT paper_id FROM paper_search WHERE paper_search MATCH ?) OR p.id IN (SELECT paper_id FROM block_search WHERE block_search MATCH ?))")
                args.extend([match, match])
        column = PAPER_SORT_COLUMNS.get(sort, PAPER_SORT_COLUMNS["updated"])
        direction = direction or ("asc" if sort in ("title", "authors", "item_type", "publisher", "source", "status") else "desc")
        ordering = f"{column} {direction.upper()} NULLS LAST, p.created DESC, p.id"
        with store.connect() as db:
            clause = " AND ".join(where)
            total = db.execute("SELECT count(*) FROM papers p WHERE " + clause, args).fetchone()[0]
            ids = [r[0] for r in db.execute("SELECT p.id FROM papers p WHERE " + clause + " ORDER BY " + ordering + " LIMIT ? OFFSET ?", args + [limit, offset])]
            return {"items": [store.paper(pid, db) for pid in ids], "total": total, "offset": offset, "limit": limit}

    @app.post("/api/collections")
    def add_collection(body: dict):
        name = str(body.get("name", "")).strip()[:120]
        if not name:
            raise ValueError("请输入集合名称")
        cid, parent = uid(), body.get("parent_id") or None
        with store.connect() as db:
            if parent and not db.execute("SELECT 1 FROM collections WHERE id=?", (parent,)).fetchone():
                raise ValueError("上级集合不存在")
            db.execute("INSERT INTO collections VALUES(?,?,?,?)", (cid, name, parent, now()))
        return {"id": cid, "name": name, "parent_id": parent}

    @app.patch("/api/collections/{collection_id}")
    def edit_collection(collection_id: str, body: dict):
        values = {}
        if "name" in body:
            values["name"] = str(body["name"]).strip()[:120]
            if not values["name"]:
                raise ValueError("请输入集合名称")
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM collections WHERE id=?", (collection_id,)).fetchone():
                raise HTTPException(404, "集合不存在")
            if "parent_id" in body:
                parent = body["parent_id"] or None
                if parent:
                    if not isinstance(parent, str) or not db.execute("SELECT 1 FROM collections WHERE id=?", (parent,)).fetchone():
                        raise ValueError("上级集合不存在")
                    if db.execute("""WITH RECURSIVE descendants(id) AS (
                        SELECT id FROM collections WHERE id=?
                        UNION SELECT c.id FROM collections c JOIN descendants d ON c.parent_id=d.id
                    ) SELECT 1 FROM descendants WHERE id=?""", (collection_id, parent)).fetchone():
                        raise ValueError("不能将集合移动到自身或它的子集合中")
                values["parent_id"] = parent
            if values:
                db.execute("UPDATE collections SET " + ", ".join(key + "=?" for key in values) + " WHERE id=?",
                           (*values.values(), collection_id))
        return {"ok": True}

    @app.delete("/api/collections/{collection_id}")
    def delete_collection(collection_id: str):
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT parent_id FROM collections WHERE id=?", (collection_id,)).fetchone()
            if row:
                db.execute("UPDATE collections SET parent_id=? WHERE parent_id=?", (row[0], collection_id))
                db.execute("DELETE FROM collections WHERE id=?", (collection_id,))
        return {"ok": True}

    def update_tag(name, replacement=None):
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            affected = [row[0] for row in db.execute("SELECT paper_id FROM tags WHERE name=?", (name,))]
            if replacement == name:
                return {"count": len(affected)}
            if replacement is not None:
                db.execute("INSERT OR IGNORE INTO tags SELECT paper_id,? FROM tags WHERE name=?", (replacement, name))
            db.execute("DELETE FROM tags WHERE name=?", (name,))
            for paper_id in affected:
                store.reindex_paper(db, paper_id)
        return {"count": len(affected)}

    @app.patch("/api/tags")
    def rename_tag(name: str, body: dict):
        replacement = str(body.get("name", "")).strip()[:80]
        if not replacement:
            raise ValueError("请输入标签名称")
        return update_tag(name, replacement)

    @app.delete("/api/tags")
    def delete_tag(name: str):
        return update_tag(name)

    @app.post("/api/import")
    async def import_pdfs(files: list[UploadFile] = File(...), collection: str = ""):
        if collection:
            with store.connect() as db:
                if not db.execute("SELECT 1 FROM collections WHERE id=?", (collection,)).fetchone():
                    raise ValueError("集合不存在，请刷新后再导入")
        imported, duplicates, errors = [], [], []
        for upload in files:
            tmp = store.root / "incoming" / (uid() + ".pdf")
            try:
                name = Path((upload.filename or "document.pdf").replace("\\", "/")).name[:250]
                digest = hashlib.sha256()
                size = 0
                with tmp.open("wb") as output:
                    while chunk := await upload.read(1024 * 1024):
                        if size == 0 and b"%PDF-" not in chunk[:1024]:
                            raise ValueError("请选择有效的 PDF 文件")
                        size += len(chunk)
                        if size > 2 * 1024 ** 3:
                            raise ValueError("单份 PDF 暂限 2GB")
                        digest.update(chunk)
                        output.write(chunk)
                if size == 0:
                    raise ValueError("文件为空")
                with store.connect() as db:
                    old = db.execute("SELECT id FROM papers WHERE sha256=?", (digest.hexdigest(),)).fetchone()
                    if old:
                        duplicates.append({"filename": name, "id": old[0]})
                        if collection:
                            db.execute("INSERT OR IGNORE INTO paper_collections VALUES(?,?)", (old[0], collection))
                        continue
                    pid = uid()
                    os.replace(tmp, store.original(pid))
                    db.execute("INSERT INTO papers(id,sha256,filename,original_filename,title,metadata_status,created,updated) VALUES(?,?,?,?,?,'pending',?,?)",
                               (pid, digest.hexdigest(), pdf_filename(name), name, Path(name).stem, now(), now()))
                    if collection:
                        db.execute("INSERT INTO paper_collections VALUES(?,?)", (pid, collection))
                    store.reindex_paper(db, pid)
                store.create_job("parse", pid)
                imported.append(store.paper(pid))
            except (ValueError, OSError) as exc:
                errors.append({"filename": upload.filename, "message": str(exc) if isinstance(exc, ValueError) else "无法保存文件，请检查磁盘空间"})
            finally:
                tmp.unlink(missing_ok=True)
                await upload.close()
        return {"imported": imported, "duplicates": duplicates, "errors": errors}

    @app.get("/api/papers/{paper_id}")
    def paper(paper_id: str):
        return need_paper(paper_id)

    @app.patch("/api/papers/{paper_id}")
    def edit_paper(paper_id: str, body: dict):
        paper = need_paper(paper_id)
        values = {}
        for key in PAPER_EDIT_FIELDS:
            if key in body:
                values[key] = str(body[key])[:20000 if key == "abstract" else 2000]
        if "title" in values and not values["title"].strip():
            raise ValueError("标题不能为空")
        if "filename" in values:
            if not values["filename"].strip():
                raise ValueError("文件名不能为空")
            values["filename"] = pdf_filename(values["filename"])
        if "item_type" in values and values["item_type"] and values["item_type"] not in ITEM_TYPES:
            raise ValueError("条目类型无效")
        if "publication_status" in values and values["publication_status"] not in PUBLICATION_STATUSES:
            raise ValueError("发表状态无效")
        if values.get("publication_date"):
            if not (published := publication_date(values["publication_date"])):
                raise ValueError("出版日期请填写 YYYY、YYYY-MM 或 YYYY-MM-DD")
            values["publication_date"] = published
        if "status" in body:
            if body["status"] not in ("unread", "reading", "read"):
                raise ValueError("阅读状态无效")
            values["status"] = body["status"]
        if "starred" in body:
            values["starred"] = int(bool(body["starred"]))
        locator = reading_locator(body["locator"], paper["pages"]) if "locator" in body else None
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            read_now = body.get("opened") is True
            if locator is not None:
                previous = json.loads(db.execute("SELECT locator FROM papers WHERE id=?", (paper_id,)).fetchone()[0])
                # A delayed save from a previous view must not replace the current position.
                if "saved_at" not in locator or locator["saved_at"] >= previous.get("saved_at", 0):
                    db.execute("UPDATE papers SET locator=?,updated=? WHERE id=?", (dumps(locator), now(), paper_id))
                    read_now |= "saved_at" in locator
            if read_now:
                db.execute("UPDATE papers SET last_read_at=? WHERE id=? AND deleted_at IS NULL", (now(), paper_id))
            if values:
                current = db.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
                manual = set(json.loads(current["manual_fields"]))
                for key in PAPER_EDIT_FIELDS:
                    if key in values and values[key] != current[key]:
                        manual.add(key)
                values["manual_fields"] = dumps(sorted(manual))
                values["updated"] = now()
                db.execute("UPDATE papers SET " + ",".join(key + "=?" for key in values) + " WHERE id=?", list(values.values()) + [paper_id])
            if "tags" in body:
                db.execute("DELETE FROM tags WHERE paper_id=?", (paper_id,))
                for tag in list(dict.fromkeys(str(t).strip()[:80] for t in body["tags"]))[:50]:
                    if tag:
                        db.execute("INSERT INTO tags VALUES(?,?)", (paper_id, tag))
            if "collections" in body:
                db.execute("DELETE FROM paper_collections WHERE paper_id=?", (paper_id,))
                for cid in list(dict.fromkeys(body["collections"]))[:100]:
                    if db.execute("SELECT 1 FROM collections WHERE id=?", (cid,)).fetchone():
                        db.execute("INSERT INTO paper_collections VALUES(?,?)", (paper_id, cid))
            if values or "tags" in body or "collections" in body:
                store.reindex_paper(db, paper_id)
        return store.paper(paper_id)

    @app.post("/api/papers/{paper_id}/metadata")
    def find_metadata(paper_id: str):
        paper = need_paper(paper_id)
        if paper["deleted_at"] is not None:
            raise ValueError("请先恢复这篇文献")
        return schedule_lookup(store, paper_id, requested=True)

    @app.post("/api/papers/{paper_id}/metadata/authors")
    def reread_authors(paper_id: str):
        if need_paper(paper_id)["deleted_at"] is not None:
            raise ValueError("请先恢复这篇文献")
        return refresh_pdf_authors(store, paper_id)

    @app.post("/api/papers/{paper_id}/rename")
    def rename_attachment(paper_id: str):
        need_paper(paper_id)
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            paper = store.paper(paper_id, db)
            fields = set(json.loads(paper["manual_fields"])) | {"filename"}
            db.execute("UPDATE papers SET filename=?,manual_fields=?,updated=? WHERE id=?",
                       (bibliography_filename(paper), dumps(sorted(fields)), now(), paper_id))
            store.reindex_paper(db, paper_id)
        return store.paper(paper_id)

    @app.delete("/api/papers/{paper_id}")
    def remove_paper(paper_id: str):
        need_paper(paper_id)
        with store.connect() as db:
            db.execute("UPDATE papers SET deleted_at=?,updated=? WHERE id=?", (now(), now(), paper_id))
        return {"ok": True}

    def trash_selection(db, paper_id):
        where, args = (" AND id=?", (paper_id,)) if paper_id else ("", ())
        items = [tuple(row) for row in db.execute(
            "SELECT id,deleted_at FROM papers WHERE deleted_at IS NOT NULL" + where + " ORDER BY id", args)]
        return items, hashlib.sha256(dumps(items).encode()).hexdigest()

    @app.get("/api/trash")
    def trash_status(paper_id: str = ""):
        with store.connect() as db:
            items, snapshot = trash_selection(db, paper_id)
        return {"count": len(items), "snapshot": snapshot}

    @app.post("/api/trash/{action}")
    def update_trash(action: Literal["restore", "purge"], body: dict, paper_id: str = ""):
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            items, snapshot = trash_selection(db, paper_id)
            if body.get("snapshot") != snapshot:
                raise HTTPException(409, "回收站内容已变化，请重新打开确认窗口。")
            ids = [pid for pid, _ in items]
            if action == "restore":
                db.execute("UPDATE papers SET deleted_at=NULL,updated=? WHERE id IN (SELECT value FROM json_each(?))", (now(), dumps(ids)))
            elif ids:
                if db.execute("""SELECT 1 FROM jobs WHERE state IN ('queued','running')
                    AND (kind='backup' OR paper_id IN (SELECT value FROM json_each(?))) LIMIT 1""", (dumps(ids),)).fetchone():
                    raise HTTPException(409, "相关文献或备份还有任务在排队或运行，请完成后再删除。")
                store.purge_trashed(db, ids)
        remaining = store.finish_purges() if action == "purge" else 0
        return {"count": len(ids), "pending_cleanup": remaining}

    @app.post("/api/papers/{paper_id}/restore")
    def restore_paper(paper_id: str):
        need_paper(paper_id)
        with store.connect() as db:
            db.execute("UPDATE papers SET deleted_at=NULL,updated=? WHERE id=?", (now(), paper_id))
        return store.paper(paper_id)

    @app.get("/api/papers/{paper_id}/file")
    def original(paper_id: str):
        paper = need_paper(paper_id)
        return FileResponse(store.original(paper_id), media_type="application/pdf", content_disposition_type="inline", filename=pdf_filename(paper["filename"]))

    @app.get("/api/papers/{paper_id}/pages/{number}")
    def page(paper_id: str, number: int, geometry: bool = True):
        paper = need_paper(paper_id)
        result = page_data(store, paper, number, include_geometry=geometry)
        if not result:
            raise HTTPException(404, "这一页还未解析，请稍后重试")
        return result

    @app.get("/api/papers/{paper_id}/find")
    def find_text(paper_id: str, q: str = Query(max_length=200), scope: Literal["both", "source", "translation"] = "both",
                  limit: int = Query(60, ge=1, le=100), offset: int = Query(0, ge=0)):
        with store.connect() as db:
            if not db.execute("SELECT 1 FROM papers WHERE id=?", (paper_id,)).fetchone():
                raise HTTPException(404, "文献不存在")
            return find_in_paper(db, paper_id, q, scope, limit, offset)

    @app.get("/api/papers/{paper_id}/references")
    def references(paper_id: str):
        from .references import paper_references

        return paper_references(store, need_paper(paper_id))

    @app.post("/api/papers/{paper_id}/counterpart")
    def counterpart(paper_id: str, body: dict):
        side = body.get("side")
        with store.connect() as db:
            if not db.execute("SELECT 1 FROM papers WHERE id=?", (paper_id,)).fetchone():
                raise HTTPException(404, "文献不存在")
            return selection_counterpart(db, paper_id, side, body.get("targets", []), pdf_rects=side == "translation" and body.get("pdf") is True)

    @app.get("/api/assets/{asset_path:path}")
    def asset(asset_path: str):
        base = (store.root / "assets").resolve()
        path = (base / asset_path).resolve()
        if not path.is_relative_to(base) or not path.is_file() or path.suffix.lower() not in (".png", ".jpg", ".jpeg"):
            raise HTTPException(404)
        return FileResponse(path)

    def normalize_targets(db, paper_id, targets):
        result = []
        for t in targets[:50]:
            block = db.execute("SELECT * FROM blocks WHERE id=? AND paper_id=?", (t["block_id"], paper_id)).fetchone()
            if not block:
                raise ValueError("标注对应的原文不存在")
            start, end = int(t["start"]), int(t["end"])
            quote = slice_text(block["text"], start, end)
            if isinstance(t.get("quote"), str) and t["quote"] != quote:
                raise ValueError("原文已更新，请重新选中文字")
            result.append({"block_id": block["id"], "start": start, "end": end, "quote": quote,
                           "prefix": block["text"].encode("utf-16-le")[max(0, start - 30) * 2:start * 2].decode("utf-16-le", errors="ignore"),
                           "page": block["page"]})
        return result

    def normalize_translated(db, paper_id, targets):
        result = []
        for t in targets[:50]:
            tr = db.execute("SELECT * FROM translations WHERE id=? AND paper_id=? AND block_id=?", (t["translation_id"], paper_id, t["block_id"])).fetchone()
            if not tr:
                raise ValueError("对应译文不存在")
            start, end = int(t["start"]), int(t["end"])
            result.append({"block_id": tr["block_id"], "translation_id": tr["id"], "start": start, "end": end, "quote": slice_text(tr["text"], start, end)})
        return result

    def inferred_mapping(db, paper_id, targets, translated, selected_language):
        from .ai import mapped_ranges, translated_ranges
        if selected_language == "translation" and translated:
            targets = []
            for t in translated:
                tr = db.execute("SELECT * FROM translations WHERE id=?", (t["translation_id"],)).fetchone()
                ranges = list(mapped_ranges(json.loads(tr["segments"]), t["block_id"], t["start"], t["end"], "target"))
                if not ranges:
                    source = db.execute("SELECT text FROM blocks WHERE id=?", (t["block_id"],)).fetchone()[0]
                    targets.append({"block_id": t["block_id"], "start": 0, "end": length(source)})
                else:
                    targets.extend({"block_id": bid, "start": start, "end": end} for bid, start, end, _ in ranges)
            return normalize_targets(db, paper_id, targets), translated
        if targets and not translated:
            for t in targets:
                tr = db.execute("SELECT * FROM translations WHERE block_id=? AND active=1 ORDER BY created DESC LIMIT 1", (t["block_id"],)).fetchone()
                if not tr:
                    continue
                for item in translated_ranges(db, paper_id, t["block_id"], tr, t["start"], t["end"]):
                    if item not in translated:
                        translated.append(item)
            translated = normalize_translated(db, paper_id, translated)
        return targets, translated

    def annotation_json(db, row):
        ann = unpack(row, ("targets", "translated_targets"))
        for target in ann["targets"]:
            block = db.execute("SELECT * FROM blocks WHERE id=?", (target["block_id"],)).fetchone()
            if block:
                target["page"] = block["page"]
                target["rects"] = rects_for_range(block, target["start"], target["end"])
        return ann

    @app.get("/api/papers/{paper_id}/annotations")
    def annotations(paper_id: str, q: str = "", offset: int = 0, limit: int = 1000):
        need_paper(paper_id)
        with store.connect() as db:
            rows = db.execute("SELECT * FROM annotations WHERE paper_id=? AND (note LIKE ? OR quote LIKE ?) ORDER BY created DESC LIMIT ? OFFSET ?", (paper_id, "%" + q + "%", "%" + q + "%", min(max(limit, 1), 3000), max(offset, 0))).fetchall()
            return [annotation_json(db, r) for r in rows]

    @app.get("/api/annotations")
    def all_annotations(q: str = "", limit: int = 100, offset: int = 0):
        with store.connect() as db:
            args = ("%"+q+"%",) * 4
            match = "p.deleted_at IS NULL AND (a.note LIKE ? OR a.quote LIKE ? OR p.title LIKE ? OR EXISTS(SELECT 1 FROM json_each(a.translated_targets) t WHERE json_extract(t.value,'$.quote') LIKE ?))"
            total = db.execute("SELECT count(*) FROM annotations a JOIN papers p ON p.id=a.paper_id WHERE " + match, args).fetchone()[0]
            rows = db.execute("SELECT a.*,p.title AS paper_title FROM annotations a JOIN papers p ON p.id=a.paper_id WHERE " + match + " ORDER BY a.updated DESC LIMIT ? OFFSET ?", args+(min(max(limit, 1), 200), max(0,offset))).fetchall()
            return {"items": [annotation_json(db, row) for row in rows], "total": total}

    @app.get("/api/annotations/{annotation_id}")
    def get_annotation(annotation_id: str):
        with store.connect() as db:
            db.execute("BEGIN")
            row = db.execute("""SELECT a.* FROM annotations a JOIN papers p ON p.id=a.paper_id
                                WHERE a.id=? AND p.deleted_at IS NULL""", (annotation_id,)).fetchone()
            if not row:
                raise HTTPException(404, "标注不存在")
            return annotation_json(db, row)

    def annotation_search(q: str):
        q = q.strip()
        if not q:
            return "1", {}
        literal = q.replace("!", "!!").replace("%", "!%").replace("_", "!_")
        return """(p.title LIKE :query ESCAPE '!' OR p.authors LIKE :query ESCAPE '!'
                    OR a.note LIKE :query ESCAPE '!' OR a.quote LIKE :query ESCAPE '!'
                    OR EXISTS (SELECT 1 FROM json_each(a.translated_targets) t
                               WHERE json_extract(t.value, '$.quote') LIKE :query ESCAPE '!'))""", {"query": f"%{literal}%"}

    @app.get("/api/annotation-papers")
    def annotation_papers(q: str = "", limit: int = 50, offset: int = 0):
        match, params = annotation_search(q)
        groups = f"""SELECT p.id AS paper_id, p.title, p.authors, p.year,
                            count(*) AS total, sum({match}) AS matched, max(a.updated) AS updated
                     FROM papers p JOIN annotations a ON a.paper_id=p.id
                     WHERE p.deleted_at IS NULL
                     GROUP BY p.id HAVING matched > 0"""
        with store.connect() as db:
            db.execute("BEGIN")
            total = db.execute(f"SELECT count(*) FROM ({groups})", params).fetchone()[0]
            rows = db.execute(f"{groups} ORDER BY updated DESC, paper_id LIMIT :limit OFFSET :offset",
                              {**params, "limit": min(max(limit, 1), 200), "offset": max(offset, 0)}).fetchall()
            return {"items": [dict(row) for row in rows], "total": total}

    @app.get("/api/annotation-papers/{paper_id}")
    def paper_annotation_page(paper_id: str, q: str = "", limit: int = 50, offset: int = 0,
                              sort: Literal["updated", "source"] = "updated"):
        match, params = annotation_search(q)
        params["paper_id"] = paper_id
        join = "FROM annotations a JOIN papers p ON p.id=a.paper_id"
        anchor_join = ""
        where = f"WHERE a.paper_id=:paper_id AND {match}"
        order = "a.updated DESC, a.id"
        if sort == "source":
            anchor_join = "LEFT JOIN blocks b ON b.id=json_extract(a.targets, '$[0].block_id') AND b.paper_id=a.paper_id"
            order = """coalesce(b.page, json_extract(a.targets, '$[0].page')) NULLS LAST,
                       b.position NULLS LAST, json_extract(a.targets, '$[0].start') NULLS LAST, """ + order
        with store.connect() as db:
            db.execute("BEGIN")
            if not db.execute("SELECT 1 FROM papers WHERE id=? AND deleted_at IS NULL", (paper_id,)).fetchone():
                raise HTTPException(404, "文献不存在")
            total = db.execute(f"SELECT count(*) {join} {where}", params).fetchone()[0]
            rows = db.execute(f"SELECT a.* {join} {anchor_join} {where} ORDER BY {order} LIMIT :limit OFFSET :offset",
                              {**params, "limit": min(max(limit, 1), 200), "offset": max(offset, 0)}).fetchall()
            return {"items": [annotation_json(db, row) for row in rows], "total": total}

    @app.post("/api/papers/{paper_id}/annotations")
    def add_annotation(paper_id: str, body: dict):
        need_paper(paper_id)
        color = body.get("color", "yellow")
        if color not in COLORS:
            raise ValueError("标注颜色无效")
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            targets = normalize_targets(db, paper_id, body.get("targets", []))
            translated = normalize_translated(db, paper_id, body.get("translated_targets", []))
            targets, translated = inferred_mapping(db, paper_id, targets, translated, body.get("selected_language", "source"))
            note = str(body.get("note", ""))[:100000]
            if not targets and not note.strip():
                raise ValueError("请选中文字或填写笔记")
            aid = uid()
            db.execute("INSERT INTO annotations VALUES(?,?,?,?,?,?,?,?,?,?)", (aid, paper_id, dumps(targets), dumps(translated), "\n".join(t["quote"] for t in targets), note, color, "review" if targets else "note", now(), now()))
            store.reindex_paper(db, paper_id)
            return annotation_json(db, db.execute("SELECT * FROM annotations WHERE id=?", (aid,)).fetchone())

    @app.patch("/api/annotations/{annotation_id}")
    def edit_annotation(annotation_id: str, body: dict):
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM annotations WHERE id=?", (annotation_id,)).fetchone()
            if not old:
                raise HTTPException(404, "标注不存在")
            values = {"updated": now()}
            if "note" in body:
                values["note"] = str(body["note"])[:100000]
            if "color" in body:
                if body["color"] not in COLORS:
                    raise ValueError("标注颜色无效")
                values["color"] = body["color"]
            if "targets" in body or "translated_targets" in body:
                source = normalize_targets(db, old["paper_id"], body.get("targets", json.loads(old["targets"])))
                translated = normalize_translated(db, old["paper_id"], body.get("translated_targets", json.loads(old["translated_targets"])))
                values.update({"targets": dumps(source), "translated_targets": dumps(translated), "alignment": "manual", "quote": "\n".join(t["quote"] for t in source)})
            db.execute("UPDATE annotations SET " + ",".join(k + "=?" for k in values) + " WHERE id=?", list(values.values()) + [annotation_id])
            store.reindex_paper(db, old["paper_id"])
            return annotation_json(db, db.execute("SELECT * FROM annotations WHERE id=?", (annotation_id,)).fetchone())

    @app.delete("/api/annotations/{annotation_id}")
    def delete_annotation(annotation_id: str):
        with store.connect() as db:
            row = db.execute("SELECT paper_id FROM annotations WHERE id=?", (annotation_id,)).fetchone()
            db.execute("DELETE FROM annotations WHERE id=?", (annotation_id,))
            if row:
                store.reindex_paper(db, row[0])
        return {"ok": True}

    @app.patch("/api/translations/{translation_id}")
    def edit_translation(translation_id: str, body: dict):
        text = str(body.get("text", ""))[:100000]
        if not text.strip():
            raise ValueError("译文不能为空")
        with store.connect() as db:
            old = db.execute("SELECT * FROM translations WHERE id=?", (translation_id,)).fetchone()
            if not old:
                raise HTTPException(404)
            tid = uid()
            db.execute("UPDATE translations SET active=0 WHERE block_id=?", (old["block_id"],))
            db.execute("INSERT INTO translations VALUES(?,?,?,?,?,?,?,?,?,?)", (tid, old["block_id"], old["paper_id"], text, "[]", old["provider"], old["model"], 1, 1, now()))
            for a in db.execute("SELECT * FROM annotations WHERE paper_id=?", (old["paper_id"],)).fetchall():
                targets = json.loads(a["translated_targets"])
                if any(t["translation_id"] == translation_id for t in targets):
                    db.execute("UPDATE annotations SET alignment='review',updated=? WHERE id=?", (now(), a["id"]))
            store.reindex_block(db, old["block_id"])
        return {"id": tid}

    @app.get("/api/blocks/{block_id}/translations")
    def translation_history(block_id: str):
        with store.connect() as db:
            return [unpack(r, ("segments",)) for r in db.execute("SELECT * FROM translations WHERE block_id=? ORDER BY created DESC", (block_id,))]

    async def prepare_provider(payload, settings):
        provider = payload.get("provider", settings["provider"])
        if provider == "local":
            ready = await asyncio.to_thread(runtime.ensure, settings)
            payload["local_url"] = ready["url"]
        elif provider in CLOUD_NAMES:
            if not Secrets(store.root, provider).present():
                raise ValueError(f"请先在设置中配置 {CLOUD_NAMES[provider]} API Key")
        else:
            raise ValueError("模型服务无效")
        payload["provider"] = provider

    @app.get("/api/jobs")
    def jobs():
        with store.connect() as db:
            return [unpack(r, ("payload", "result")) for r in db.execute("""
                SELECT j.*,p.title AS paper_title FROM jobs j LEFT JOIN papers p ON j.paper_id=p.id
                WHERE j.state IN ('queued','running') OR j.id IN (SELECT id FROM jobs ORDER BY created DESC LIMIT 100)
                ORDER BY j.created DESC
            """)]

    @app.get("/api/ocr/status")
    def ocr_status():
        return ocr.status()

    def check_job_conflict(db, paper_id, kind, payload, job_id=""):
        rebuild = kind == "parse" and payload.get("rebuild") is True
        fill_publication = kind == "metadata" and payload.get("fill_publication_fields") is True
        if rebuild or kind == "ocr" or fill_publication:
            paper = db.execute("SELECT deleted_at,parse_state,publication_status FROM papers WHERE id=?", (paper_id,)).fetchone()
            if not paper or paper["deleted_at"] is not None:
                raise ValueError("请先恢复这篇文献")
            if not store.original(paper_id).is_file():
                messages = {"parse": "找不到原 PDF，无法重新转换 HTML", "ocr": "找不到原 PDF，无法识别扫描页",
                            "metadata": "找不到原 PDF，无法核对出版信息"}
                raise ValueError(messages[kind])
            if kind == "ocr" and paper["parse_state"] not in ("ready", "no_text"):
                raise ValueError("请先完成 PDF 解析")
            if fill_publication and paper["parse_state"] in ("pending", "processing"):
                raise ValueError("请等 PDF 转换完成后再补全出版信息")
            if fill_publication and paper["publication_status"] in PUBLICATION_SKIP_MESSAGES:
                raise ValueError(PUBLICATION_SKIP_MESSAGES[paper["publication_status"]])
        for row in db.execute("""SELECT kind,payload FROM jobs WHERE paper_id IS ? AND id!=?
                               AND state IN ('queued','running')""", (paper_id, job_id)):
            if row["kind"] == "parse" and json.loads(row["payload"]).get("rebuild") is True:
                raise ValueError("此文献正在重新转换 HTML，请等转换完成")
            if rebuild or row["kind"] == kind or {row["kind"], kind} == {"parse", "ocr"}:
                raise ValueError("此文献已有相关任务正在处理，请等它完成或先暂停")
            if fill_publication and row["kind"] == "parse":
                raise ValueError("请等 PDF 转换完成后再补全出版信息")

    @app.post("/api/papers/batch")
    async def batch_papers(body: dict):
        ids = body.get("paper_ids")
        if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_BATCH_PAPERS or any(
            not isinstance(pid, str) or not 1 <= len(pid) <= 100 for pid in ids
        ):
            raise ValueError(f"请选择 1 至 {MAX_BATCH_PAPERS} 篇文献")
        action = body.get("action")
        status = body.get("status")
        payload = {}
        if action == "translate":
            settings = store.settings()
            provider = body.get("provider", settings["provider"])
            if provider not in ("local", *CLOUD_NAMES):
                raise ValueError("模型服务无效")
            payload = {"provider": provider, "neighbor_context": True, "force": body.get("force") is True, "preserve_manual": True}
            await prepare_provider(payload, settings)
        elif action == "metadata":
            payload = {"requested": True, "fill_publication_fields": True}
        elif action != "status" or status not in ("unread", "reading", "read"):
            raise ValueError("批量操作无效")

        queued, skipped, updated = [], [], 0
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for pid in dict.fromkeys(ids):
                paper = db.execute("""SELECT title,deleted_at,parse_state,publication_date,publisher,item_type,manual_fields,publication_status
                                      FROM papers WHERE id=?""", (pid,)).fetchone()
                title = paper["title"] if paper else "文献不可用"
                try:
                    if not paper or paper["deleted_at"] is not None:
                        raise ValueError("文献已移入回收站或不存在")
                    if action == "status":
                        updated += db.execute("UPDATE papers SET status=?,updated=? WHERE id=? AND status!=?",
                                              (status, now(), pid, status)).rowcount
                        continue
                    if action == "translate":
                        if paper["parse_state"] != "ready":
                            raise ValueError("等待 HTML 转换完成或先识别扫描件")
                        if not store.original(pid).is_file():
                            raise ValueError("找不到原 PDF")
                    elif paper["publication_status"] in PUBLICATION_SKIP_MESSAGES:
                        raise ValueError(PUBLICATION_SKIP_MESSAGES[paper["publication_status"]])
                    elif not missing_publication_fields(paper):
                        raise ValueError("出版信息已填写或由人工维护")
                    check_job_conflict(db, pid, action, payload)
                except ValueError as error:
                    skipped.append({"paper_id": pid, "title": title, "reason": str(error)})
                    continue
                job = store.create_job(action, pid, payload, db=db)
                queued.append({**job, "paper_title": title})
        return {"queued": queued, "updated": updated, "skipped": skipped}

    @app.post("/api/jobs/{job_id}/{action}")
    async def control_job(job_id: str, action: str):
        job = store.job(job_id)
        if not job:
            raise HTTPException(404)
        if action == "pause" and job["state"] in ("queued", "running"):
            state = "paused"
            message = "已暂停，保留原阅读版" if job["payload"].get("rebuild") else "已暂停，已完成的内容已保存"
        elif action == "resume" and job["state"] in ("paused", "failed", "partial"):
            state, message = "queued", "等待继续"
            if job["kind"] in ("reading", "translate", "align"):
                await prepare_provider(job["payload"], store.settings())
        elif action == "cancel" and job["state"] not in ("done", "cancelled"):
            state, message = "cancelled", "已取消，保留已完成内容"
        else:
            raise ValueError("当前任务不能执行此操作")
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if state == "queued":
                check_job_conflict(db, job["paper_id"], job["kind"], job["payload"], job_id)
            changed = db.execute("UPDATE jobs SET state=?,message=?,payload=?,updated=? WHERE id=? AND state=?",
                                 (state, message, dumps(job["payload"]), now(), job_id, job["state"])).rowcount
            if not changed:
                raise ValueError("任务状态已变化，请刷新后再操作")
            if job["kind"] == "metadata" and not job["payload"].get("fill_publication_fields"):
                db.execute("UPDATE papers SET metadata_status=? WHERE id=?", ("queued" if state == "queued" else "local", job["paper_id"]))
        if state == "paused" and scheduler and scheduler.active_id == job_id and scheduler.process and scheduler.process.poll() is None:
            scheduler.process.terminate()
        return store.job(job_id)

    @app.post("/api/papers/{paper_id}/jobs")
    async def add_job(paper_id: str, body: dict):
        paper = need_paper(paper_id)
        kind = body.get("kind")
        if kind not in ("translate", "reading", "align", "parse", "ocr"):
            raise ValueError("不支持此任务")
        if kind in ("translate", "reading", "align") and paper["parse_state"] not in ("ready", "processing"):
            raise ValueError("请先完成正文解析")
        payload = {k: body[k] for k in ("provider", "task", "question", "block_ids", "page_start", "page_end", "force", "preserve_manual", "annotation_id", "selected_language", "neighbor_context", "conversation_memory", "target_language") if k in body}
        if kind == "parse" and body.get("rebuild") is True:
            payload = {"rebuild": True}
        if kind == "ocr":
            if paper["deleted_at"] is not None:
                raise ValueError("请先恢复这篇文献")
            if paper["parse_state"] not in ("ready", "no_text"):
                raise ValueError("请先完成 PDF 解析")
            if not paper["ocr_pending_pages"]:
                raise ValueError("没有待识别的扫描页")
            engine = await asyncio.to_thread(ocr.status)
            payload = {"language": ocr.resolve_language(engine, body.get("language"), paper["language"])}
        if "question" in payload:
            payload["question"] = str(payload["question"])[:12000]
        if kind in ("translate", "reading", "align"):
            await prepare_provider(payload, store.settings())
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            check_job_conflict(db, paper_id, kind, payload)
            if kind == "ocr":
                current = store.paper(paper_id, db)
                if not current["ocr_pending_pages"]:
                    raise ValueError("没有待识别的扫描页")
            return store.create_job(kind, paper_id, payload, db=db)

    @app.get("/api/papers/{paper_id}/ai-results")
    def ai_results(paper_id: str):
        need_paper(paper_id)
        with store.connect() as db:
            return [unpack(r, ("citations",)) for r in db.execute("SELECT * FROM ai_results WHERE paper_id=? ORDER BY created DESC,id DESC LIMIT 100", (paper_id,))]

    @app.post("/api/ai-results/{result_id}/save-note")
    def save_ai_note(result_id: str):
        with store.connect() as db:
            result = db.execute("SELECT * FROM ai_results WHERE id=?", (result_id,)).fetchone()
            if not result:
                raise HTTPException(404)
        return add_annotation(result["paper_id"], {"note": f"AI 阅读记录 · {result['model']} · {result['scope']}\n\n{result['answer']}"})

    @app.get("/api/settings")
    def settings():
        return {**store.settings(), **cloud_settings()}

    @app.patch("/api/settings")
    def save_settings(body: dict):
        values = {}
        for key in ("provider", "local_url", "local_model", "deepseek_model", "openai_model", "context_tokens", "font_size", "layout", "metadata_lookup", "auto_rename"):
            if key in body:
                values[key] = body[key]
        if "provider" in values and values["provider"] not in ("local", *CLOUD_NAMES):
            raise ValueError("模型服务无效")
        if "local_url" in values:
            values["local_url"] = local_url(str(values["local_url"]))
        for key in ("local_model", "deepseek_model", "openai_model"):
            if key in values and (not isinstance(values[key], str) or not re.fullmatch(r"[\w./:-]{1,120}", values[key])):
                raise ValueError("模型名称格式无效")
        if "context_tokens" in values:
            values["context_tokens"] = min(max(int(values["context_tokens"]), 4096), 32768)
        if "font_size" in values:
            values["font_size"] = min(max(int(values["font_size"]), 14), 26)
        if "layout" in values and values["layout"] not in ("parallel", "interleaved", "single"):
            raise ValueError("阅读布局无效")
        for key in ("metadata_lookup", "auto_rename"):
            if key in values and not isinstance(values[key], bool):
                raise ValueError("导入设置无效")
        for provider in CLOUD_NAMES:
            key = body.get(f"{provider}_api_key") or (body.get("api_key") if provider == "deepseek" else None)
            if key:
                Secrets(store.root, provider).save(key)
            if body.get(f"clear_{provider}_api_key") or (provider == "deepseek" and body.get("clear_api_key")):
                Secrets(store.root, provider).clear()
        store.set_settings(values)
        return settings()

    @app.get("/api/models/local")
    def local_model():
        return runtime.status(store.settings())

    @app.post("/api/models/local/start")
    async def start_local():
        return await asyncio.to_thread(runtime.ensure, store.settings())

    @app.post("/api/models/test")
    async def test_model(body: dict):
        from .ai import Client
        payload = {"provider": body["provider"]} if "provider" in body else {}
        await prepare_provider(payload, store.settings())
        client = Client(store, payload)
        result = await asyncio.to_thread(client.ask, '只返回 JSON {"ok":true}。', "连接测试")
        if result.get("ok") is not True:
            raise ValueError("模型响应未通过连接测试，请重试")
        return {"ok": True, "provider": payload["provider"], "model": client.model}

    @app.get("/api/papers/{paper_id}/export/pdf")
    def annotated_pdf(paper_id: str):
        paper = need_paper(paper_id)
        output = store.root / "exports" / (uid() + ".pdf")
        export_pdf(store, paper_id, output)
        return FileResponse(output, filename=pdf_filename(pdf_filename(paper["filename"])[:-4][:145] + " - 批注"), media_type="application/pdf")

    @app.get("/api/papers/{paper_id}/export/notes")
    def export_notes(paper_id: str):
        paper = need_paper(paper_id)
        with store.connect() as db:
            values = [annotation_json(db, r) for r in db.execute("SELECT * FROM annotations WHERE paper_id=? ORDER BY created", (paper_id,))]
        lines = ["# " + paper["title"], "", paper["authors"], ""]
        for a in values:
            if a["quote"]:
                lines.extend(["> " + a["quote"].replace("\n", "\n> "), ""])
            for t in a["translated_targets"]:
                lines.extend(["译文：" + t.get("quote", ""), ""])
            if a["targets"]:
                lines.extend(["页码：" + ", ".join(str(t["page"]) for t in a["targets"]), ""])
            lines.extend([a["note"], "", "---", ""])
        filename = pdf_filename(paper["title"])[:-4] + " - 阅读笔记.md"
        disposition = f"attachment; filename=reading-notes.md; filename*=UTF-8''{quote(filename, safe='')}"
        return Response("\n".join(lines), media_type="text/markdown; charset=utf-8", headers={"Content-Disposition": disposition})

    @app.get("/api/papers/{paper_id}/export/html")
    def export_html(paper_id: str):
        paper = need_paper(paper_id)
        return Response(export_reading_html(store, paper), media_type="text/html; charset=utf-8",
                        headers={"Content-Disposition": "attachment; filename=bilingual-reading.html"})

    @app.get("/api/export/metadata")
    def export_metadata(format: str = "csv"):
        if format not in ("csv", "bibtex", "ris"):
            raise ValueError("不支持此导出格式")
        if format in ("bibtex", "ris"):
            with store.connect() as db:
                records = [dict(r) for r in db.execute("SELECT * FROM papers WHERE deleted_at IS NULL ORDER BY created")]
            if format == "ris":
                lines = []
                for record in records:
                    entry_type = "MANSCPT" if record["publication_status"] == "unpublished" else "GEN"
                    lines.extend(["TY  - " + entry_type, "ID  - " + record["id"]])
                    if note := PUBLICATION_EXPORT_NOTES.get(record["publication_status"]):
                        lines.append("N1  - " + note)
                    for key, field in (("title", "TI"), ("year", "PY"), ("doi", "DO"), ("source", "T2"), ("abstract", "AB")):
                        if record[key]:
                            lines.append(field + "  - " + " ".join(record[key].splitlines()))
                    for author in re.split(r"[;；]", record["authors"]):
                        if author.strip():
                            lines.append("AU  - " + " ".join(author.strip().splitlines()))
                    lines.extend(["ER  - ", ""])
                return Response("\n".join(lines), media_type="application/x-research-info-systems; charset=utf-8", headers={"Content-Disposition": "attachment; filename=library.ris"})
            def tex(value):
                escapes = {"\\": r"\textbackslash{}", "{": r"\{", "}": r"\}", "%": r"\%", "&": r"\&", "#": r"\#", "_": r"\_", "$": r"\$"}
                return "".join(escapes.get(c, c) for c in " ".join(value.splitlines()))
            entries = []
            for record in records:
                fields = []
                if note := PUBLICATION_EXPORT_NOTES.get(record["publication_status"]):
                    fields.append("  note = {" + tex(note) + "}")
                for key, field in (("title", "title"), ("year", "year"), ("doi", "doi"), ("source", "howpublished")):
                    if record[key]:
                        fields.append("  " + field + " = {" + tex(record[key]) + "}")
                if record["authors"]:
                    fields.append("  author = {" + tex(" and ".join(a.strip() for a in re.split(r"[;；]", record["authors"]) if a.strip())) + "}")
                entry_type = "unpublished" if record["publication_status"] == "unpublished" else "misc"
                entries.append("@" + entry_type + "{ref" + record["id"][:12] + ",\n" + ",\n".join(fields) + "\n}")
            return Response("\n\n".join(entries), media_type="application/x-bibtex; charset=utf-8", headers={"Content-Disposition": "attachment; filename=library.bib"})
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["title", "authors", "year", "doi", "source", "status", "tags", "filename", "publication_status"])
        with store.connect() as db:
            for row in db.execute("SELECT * FROM papers WHERE deleted_at IS NULL ORDER BY created"):
                values = [row[k] for k in ("title", "authors", "year", "doi", "source", "status")] + ["; ".join(r[0] for r in db.execute("SELECT name FROM tags WHERE paper_id=?", (row["id"],))), row["filename"], row["publication_status"]]
                writer.writerow(["'" + str(v) if str(v).lstrip().startswith(("=", "+", "-", "@")) else v for v in values])
        return Response("\ufeff" + buffer.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=library.csv"})

    @app.post("/api/backups")
    def backup():
        return store.create_job("backup")

    @app.get("/api/backups")
    def backups():
        return [{"filename": p.name, "size": p.stat().st_size, "created": p.stat().st_mtime} for p in sorted((store.root / "backups").glob("*.zip"), reverse=True)]

    @app.get("/api/backups/{filename}")
    def get_backup(filename: str):
        path = (store.root / "backups" / filename).resolve()
        if path.parent != (store.root / "backups").resolve() or not path.is_file() or path.suffix != ".zip":
            raise HTTPException(404)
        return FileResponse(path, filename=filename, media_type="application/zip")

    @app.post("/api/restore")
    async def restore_library(file: UploadFile = File(...)):
        nonlocal scheduler
        with store.connect() as db:
            if db.execute("SELECT 1 FROM jobs WHERE state IN ('running','queued')").fetchone():
                raise ValueError("请先在任务面板暂停正在进行的任务，再恢复文献库")
        incoming = store.root / "incoming" / (uid() + ".zip")
        try:
            with incoming.open("wb") as output:
                while chunk := await file.read(1024 * 1024):
                    output.write(chunk)
            stage = await asyncio.to_thread(prepare_restore, store, incoming)
        except (zipfile.BadZipFile, KeyError, json.JSONDecodeError) as exc:
            raise ValueError("不是有效的阅读器备份文件") from exc
        finally:
            incoming.unlink(missing_ok=True)
            await file.close()
        previous = store.root.parent / (store.root.name + "-before-restore-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uid()[:6])
        maintenance.set()
        try:
            if scheduler:
                scheduler.close()
            runtime.close()
            with store.connect() as db:
                db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            for provider in CLOUD_NAMES:
                key = Secrets(store.root, provider).path
                if key.exists():
                    shutil.copy2(key, stage / key.name)
            store.root.rename(previous)
            try:
                stage.rename(store.root)
            except BaseException:
                previous.rename(store.root)
                raise
            store.initialize()
            if background:
                scheduler = Scheduler(store)
                scheduler.start()
            return {"ok": True, "previous_copy": previous.name}
        finally:
            maintenance.clear()

    dist = frontend_dir or (ROOT / "web" / "dist")
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=dist, html=True), name="ui")
    else:
        @app.get("/")
        def missing_frontend():
            return HTMLResponse("<h1>文献阅读器</h1><p>请先运行 Setup.cmd 构建阅读界面。</p>")
    return app
