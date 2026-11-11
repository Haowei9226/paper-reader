from __future__ import annotations

import contextlib
import json
import re
import shutil
import sqlite3
import time
import uuid
from pathlib import Path

from . import DATA, SHARED_BUILD


OCR_PAGE_QUERY = """SELECT p.number,p.ocr_job_id,EXISTS(
    SELECT 1 FROM blocks b WHERE b.paper_id=p.paper_id AND b.page=p.number
        AND b.kind!='image' AND TRIM(b.text)!=''
) AS has_text FROM pages p WHERE p.paper_id=?"""
NO_TEXT_ERROR = "此 PDF 没有可提取文字，可查看原件或尝试本机文字识别。"


def uid() -> str:
    return uuid.uuid4().hex


def now() -> float:
    return time.time()


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def unpack(row, fields=()):
    if row is None:
        return None
    value = dict(row)
    for key in fields:
        if key in value:
            value[key] = json.loads(value[key])
    return value


def search_tokens(text: str) -> list[str]:
    tokens = re.findall(r"[a-zA-Z0-9]+|[\u3400-\u9fff]+", text.lower())
    result = []
    for token in tokens:
        if re.search(r"[\u3400-\u9fff]", token):
            result.extend(token if len(token) == 1 else [token[i:i + 2] for i in range(len(token) - 1)])
        else:
            result.append(token)
    return result


def index_text(text: str) -> str:
    return " ".join(search_tokens(text))


class Store:
    def __init__(self, root: Path = DATA):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        for name in ("originals", "assets", "incoming", "exports", "backups"):
            (self.root / name).mkdir(exist_ok=True)
        self.path = self.root / "library.sqlite3"
        self.initialize()

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=30000")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def initialize(self):
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                INSERT OR IGNORE INTO meta VALUES('schema_version','1');
                CREATE TABLE IF NOT EXISTS papers(
                    id TEXT PRIMARY KEY, sha256 TEXT NOT NULL UNIQUE, filename TEXT NOT NULL,
                    title TEXT NOT NULL, authors TEXT NOT NULL DEFAULT '', year TEXT NOT NULL DEFAULT '',
                    doi TEXT NOT NULL DEFAULT '', abstract TEXT NOT NULL DEFAULT '', language TEXT DEFAULT 'en',
                    source TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'unread',
                    starred INTEGER NOT NULL DEFAULT 0, pages INTEGER NOT NULL DEFAULT 0,
                    parse_state TEXT NOT NULL DEFAULT 'pending', parse_engine TEXT NOT NULL DEFAULT 'native',
                    parse_error TEXT NOT NULL DEFAULT '', outline TEXT NOT NULL DEFAULT '[]',
                    locator TEXT NOT NULL DEFAULT '{}', deleted_at REAL, created REAL NOT NULL, updated REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS collections(
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, parent_id TEXT REFERENCES collections(id), created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_collections(
                    paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
                    collection_id TEXT NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
                    PRIMARY KEY(paper_id,collection_id)
                );
                CREATE TABLE IF NOT EXISTS tags(
                    paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE, name TEXT NOT NULL,
                    PRIMARY KEY(paper_id,name)
                );
                CREATE TABLE IF NOT EXISTS pages(
                    paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
                    number INTEGER NOT NULL, width REAL NOT NULL, height REAL NOT NULL,
                    rotation INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(paper_id,number)
                );
                CREATE TABLE IF NOT EXISTS blocks(
                    id TEXT PRIMARY KEY, paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
                    page INTEGER NOT NULL, position INTEGER NOT NULL, kind TEXT NOT NULL,
                    text TEXT NOT NULL, bbox TEXT NOT NULL, geometry BLOB, asset TEXT,
                    section TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS blocks_paper ON blocks(paper_id,page,position);
                CREATE TABLE IF NOT EXISTS translations(
                    id TEXT PRIMARY KEY, block_id TEXT NOT NULL REFERENCES blocks(id),
                    paper_id TEXT NOT NULL REFERENCES papers(id), text TEXT NOT NULL, segments TEXT NOT NULL,
                    provider TEXT NOT NULL, model TEXT NOT NULL, manual INTEGER NOT NULL DEFAULT 0,
                    active INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS translation_block ON translations(block_id,active);
                CREATE INDEX IF NOT EXISTS translation_paper ON translations(paper_id,active);
                CREATE TABLE IF NOT EXISTS annotations(
                    id TEXT PRIMARY KEY, paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
                    targets TEXT NOT NULL, translated_targets TEXT NOT NULL DEFAULT '[]',
                    quote TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '', color TEXT NOT NULL DEFAULT 'yellow',
                    alignment TEXT NOT NULL DEFAULT 'review', created REAL NOT NULL, updated REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS annotations_paper ON annotations(paper_id,created);
                CREATE TABLE IF NOT EXISTS jobs(
                    id TEXT PRIMARY KEY, paper_id TEXT REFERENCES papers(id), kind TEXT NOT NULL,
                    state TEXT NOT NULL, payload TEXT NOT NULL, progress INTEGER NOT NULL DEFAULT 0,
                    total INTEGER NOT NULL DEFAULT 0, message TEXT NOT NULL DEFAULT '',
                    result TEXT NOT NULL DEFAULT '{}', created REAL NOT NULL, updated REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state,created);
                CREATE INDEX IF NOT EXISTS jobs_paper_kind ON jobs(paper_id,kind,created);
                CREATE TABLE IF NOT EXISTS ai_results(
                    id TEXT PRIMARY KEY, paper_id TEXT NOT NULL REFERENCES papers(id), kind TEXT NOT NULL,
                    question TEXT NOT NULL, answer TEXT NOT NULL, citations TEXT NOT NULL,
                    provider TEXT NOT NULL, model TEXT NOT NULL, scope TEXT NOT NULL, created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS pending_purges(paper_id TEXT PRIMARY KEY);
                CREATE VIRTUAL TABLE IF NOT EXISTS paper_search USING fts5(paper_id UNINDEXED,content);
                CREATE VIRTUAL TABLE IF NOT EXISTS block_search USING fts5(block_id UNINDEXED,paper_id UNINDEXED,content);
            """)
            columns = {r[1] for r in db.execute("PRAGMA table_info(papers)")}
            if "manual_fields" not in columns:
                db.execute("ALTER TABLE papers ADD COLUMN manual_fields TEXT NOT NULL DEFAULT '[]'")
            if "original_filename" not in columns:
                db.execute("ALTER TABLE papers ADD COLUMN original_filename TEXT NOT NULL DEFAULT ''")
                db.execute("UPDATE papers SET original_filename=filename")
            if "metadata_status" not in columns:
                db.execute("ALTER TABLE papers ADD COLUMN metadata_status TEXT NOT NULL DEFAULT 'local'")
            if "metadata_source" not in columns:
                db.execute("ALTER TABLE papers ADD COLUMN metadata_source TEXT NOT NULL DEFAULT ''")
            for field in ("item_type", "publication_date", "publisher", "publication_status"):
                if field not in columns:
                    db.execute(f"ALTER TABLE papers ADD COLUMN {field} TEXT NOT NULL DEFAULT ''")
            if "last_read_at" not in columns:
                db.execute("ALTER TABLE papers ADD COLUMN last_read_at REAL")
                db.execute("""UPDATE papers SET last_read_at=json_extract(locator,'$.saved_at') / 1000.0
                              WHERE json_type(locator,'$.saved_at') IN ('integer','real')
                              AND json_extract(locator,'$.saved_at') > 0""")
            if "ocr_job_id" not in {r[1] for r in db.execute("PRAGMA table_info(pages)")}:
                db.execute("ALTER TABLE pages ADD COLUMN ocr_job_id TEXT NOT NULL DEFAULT ''")
                db.execute("UPDATE papers SET parse_error=? WHERE parse_state='no_text' AND parse_error=?",
                           (NO_TEXT_ERROR, "此 PDF 没有可提取文字，仍可查看原件；扫描件文字识别暂未启用。"))

    def paper(self, paper_id: str, db=None):
        if db is None:
            with self.connect() as cx:
                return self.paper(paper_id, cx)
        paper = unpack(db.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone(), ("outline", "locator"))
        if paper:
            paper["has_attachment"] = self.original(paper_id).is_file()
            paper["collections"] = [r[0] for r in db.execute("SELECT collection_id FROM paper_collections WHERE paper_id=?", (paper_id,))]
            paper["tags"] = [r[0] for r in db.execute("SELECT name FROM tags WHERE paper_id=? ORDER BY name", (paper_id,))]
            paper["annotations_count"] = db.execute("SELECT count(*) FROM annotations WHERE paper_id=?", (paper_id,)).fetchone()[0]
            paper["translated_count"] = db.execute("SELECT count(*) FROM translations WHERE paper_id=? AND active=1", (paper_id,)).fetchone()[0]
            paper["blocks_count"] = db.execute("SELECT count(*) FROM blocks WHERE paper_id=? AND kind!='image'", (paper_id,)).fetchone()[0]
            ocr, pending, empty = db.execute(f"""SELECT count(CASE WHEN has_text AND ocr_job_id!='' THEN 1 END),
                count(CASE WHEN NOT has_text THEN 1 END),
                count(CASE WHEN NOT has_text AND ocr_job_id!='' THEN 1 END)
                FROM ({OCR_PAGE_QUERY})""", (paper_id,)).fetchone()
            paper.update(ocr_pages=ocr, ocr_pending_pages=pending, ocr_empty_pages=empty)
        return paper

    def reindex_paper(self, db, paper_id):
        p = db.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
        tags = " ".join(r[0] for r in db.execute("SELECT name FROM tags WHERE paper_id=?", (paper_id,)))
        notes = " ".join(r["note"] + " " + r["quote"] + " " + " ".join(t.get("quote", "") for t in json.loads(r["translated_targets"])) for r in db.execute("SELECT note,quote,translated_targets FROM annotations WHERE paper_id=?", (paper_id,)))
        content = " ".join(str(p[k] or "") for k in ("title", "authors", "year", "doi", "abstract", "source",
                                                     "publisher", "publication_date", "filename", "original_filename"))
        db.execute("DELETE FROM paper_search WHERE paper_id=?", (paper_id,))
        db.execute("INSERT INTO paper_search VALUES(?,?)", (paper_id, index_text(content + " " + tags + " " + notes)))

    def reindex_block(self, db, block_id):
        b = db.execute("SELECT paper_id,text FROM blocks WHERE id=?", (block_id,)).fetchone()
        translations = " ".join(r[0] for r in db.execute("SELECT text FROM translations WHERE block_id=? AND active=1", (block_id,)))
        db.execute("DELETE FROM block_search WHERE block_id=?", (block_id,))
        db.execute("INSERT INTO block_search VALUES(?,?,?)", (block_id, b[0], index_text(b[1] + " " + translations)))

    def settings(self):
        defaults = {"provider": "local", "local_url": "http://127.0.0.1:11435", "local_model": "qwen3.5:4b",
                    "deepseek_model": "deepseek-flash", "openai_model": "gpt-4.1-mini", "context_tokens": 4096, "font_size": 17,
                    "layout": "parallel", "parse_engine": "native", "metadata_lookup": not SHARED_BUILD, "auto_rename": True}
        with self.connect() as db:
            defaults.update({r[0]: json.loads(r[1]) for r in db.execute("SELECT key,value FROM settings")})
        return defaults

    def set_settings(self, values):
        with self.connect() as db:
            for key, value in values.items():
                db.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, dumps(value)))

    def create_job(self, kind, paper_id=None, payload=None, *, db=None):
        if db is None:
            with self.connect() as cx:
                return self.create_job(kind, paper_id, payload, db=cx)
        job_id = uid()
        db.execute("INSERT INTO jobs(id,paper_id,kind,state,payload,created,updated) VALUES(?,?,?,'queued',?,?,?)",
                   (job_id, paper_id, kind, dumps(payload or {}), now(), now()))
        return unpack(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone(), ("payload", "result"))

    def job(self, job_id):
        with self.connect() as db:
            return unpack(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone(), ("payload", "result"))

    def progress(self, job_id, done, total, message=""):
        with self.connect() as db:
            changed = db.execute("UPDATE jobs SET progress=?,total=?,message=?,updated=? WHERE id=? AND state='running'",
                                 (done, total, message, now(), job_id)).rowcount
            if not changed:
                raise InterruptedError("任务已暂停或取消")

    def original(self, paper_id):
        return self.root / "originals" / (paper_id + ".pdf")

    def purge_trashed(self, db, paper_ids):
        if any(not re.fullmatch(r"[0-9a-f]{32}", pid) for pid in paper_ids):
            raise ValueError("回收站中有无效的文献编号，已停止删除。")
        ids = dumps(paper_ids)
        matching = "(SELECT value FROM json_each(?))"
        db.executemany("INSERT OR IGNORE INTO pending_purges VALUES(?)", ((pid,) for pid in paper_ids))
        for table in ("paper_search", "block_search", "annotations", "translations", "ai_results", "jobs", "blocks"):
            db.execute(f"DELETE FROM {table} WHERE paper_id IN {matching}", (ids,))
        db.execute(f"DELETE FROM papers WHERE id IN {matching} AND deleted_at IS NOT NULL", (ids,))

    def finish_purges(self):
        # The database records the cleanup before deleting any files. A locked
        # attachment can be retried after the reader is restarted.
        with self.connect() as db:
            pending = [row[0] for row in db.execute("""SELECT paper_id FROM pending_purges
                WHERE paper_id NOT IN (SELECT id FROM papers)""")]
        for pid in pending:
            if not re.fullmatch(r"[0-9a-f]{32}", pid):
                continue
            original = self.original(pid)
            assets = self.root / "assets" / pid
            try:
                if any(path.resolve() != path or not path.is_relative_to(self.root) for path in (original, assets)):
                    continue
                original.unlink(missing_ok=True)
                if assets.is_dir():
                    shutil.rmtree(assets)
            except (OSError, RuntimeError):
                continue
            with self.connect() as db:
                db.execute("DELETE FROM pending_purges WHERE paper_id=?", (pid,))
        with self.connect() as db:
            return db.execute("SELECT count(*) FROM pending_purges").fetchone()[0]
