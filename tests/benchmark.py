"""Reproducible synthetic capacity checks, isolated from the user's library."""
import json
import time
from pathlib import Path

import pymupdf as fitz
from fastapi.testclient import TestClient

from reader.app import create_app
from reader.store import dumps, index_text, now, uid
from reader.worker import run


def main():
    root = Path("output") / ("capacity-" + uid()[:8])
    app = create_app(root / "data", background=False)
    store = app.state.store
    report = {"synthetic": True, "library_records": 10000, "notes": 100000, "long_pdf_pages": 1000}
    with TestClient(app) as client:
        document = fitz.open()
        for i in range(1000):
            page = document.new_page()
            page.insert_text((48, 65), f"Chapter {i + 1}. Long-document validation", fontsize=15)
            page.insert_text((48, 110), "This synthetic page verifies incremental extraction and navigation.", fontsize=11)
            page.insert_text((48, 145), "Annotations use stable text ranges and original PDF locations.", fontsize=11)
        content = document.tobytes()
        document.close()
        started = time.perf_counter()
        imported = client.post("/api/import", files={"files": ("1000-page-validation.pdf", content, "application/pdf")}).json()["imported"][0]
        pid = imported["id"]
        with store.connect() as db:
            job_id = db.execute("SELECT id FROM jobs WHERE paper_id=?", (pid,)).fetchone()[0]
            db.execute("UPDATE jobs SET state='running' WHERE id=?", (job_id,))
        run(store, job_id)
        assert store.job(job_id)["state"] == "done", store.job(job_id)
        report["long_pdf_import_and_parse_seconds"] = round(time.perf_counter() - started, 3)
        started = time.perf_counter()
        last = client.get(f"/api/papers/{pid}/pages/1000")
        assert last.status_code == 200 and last.json()["number"] == 1000
        report["last_page_api_ms"] = round((time.perf_counter() - started) * 1000, 2)
        stamp = now()
        with store.connect() as db:
            rows = [(f"bench{i:05d}", f"synthetic-{i:05d}", f"paper-{i}.pdf", f"Synthetic research paper {i} 协作阅读", "Validation Author", str(2000 + i % 27), "unread", stamp + i, stamp + i) for i in range(9999)]
            db.executemany("INSERT INTO papers(id,sha256,filename,title,authors,year,status,created,updated) VALUES(?,?,?,?,?,?,?,?,?)", rows)
            db.executemany("INSERT INTO annotations VALUES(?,?,?,?,?,?,?,?,?,?)", ((f"note{i:06d}", f"bench{i % 9999:05d}", "[]", "[]", "", f"Synthetic annotation {i} 博士研究笔记", "yellow", "note", stamp, stamp + i) for i in range(100000)))
            for row in rows:
                store.reindex_paper(db, row[0])
        timings = {}
        for name, path in {"first_100_records": "/api/papers?limit=100", "last_100_records": "/api/papers?limit=100&offset=9900", "chinese_search": "/api/papers?q=协作阅读&limit=100", "notes_search": "/api/annotations?q=博士研究&limit=100"}.items():
            values = []
            for _ in range(3):
                started = time.perf_counter()
                response = client.get(path)
                assert response.status_code == 200, response.text
                values.append((time.perf_counter() - started) * 1000)
            timings[name] = {"median_ms": round(sorted(values)[1], 2), "max_ms": round(max(values), 2), "returned": len(response.json().get("items", []))}
        report["api_timings"] = timings
        report["database_mb"] = round(store.path.stat().st_size / 1024 ** 2, 2)
        report["note"] = "10k records/100k notes use generated metadata. Only the 1000-page PDF is a real stored attachment; no claim about 10k real PDFs or conversion quality."
    (Path("output") / "capacity-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
