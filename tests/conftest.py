from pathlib import Path

import pymupdf as fitz
import pytest
from fastapi.testclient import TestClient

from reader.app import create_app
from reader.store import now
from reader.worker import run


def sample_pdf(pages=2, rotated=False):
    doc = fitz.open()
    for index in range(pages):
        page = doc.new_page(width=595, height=842)
        if index == 0:
            page.insert_text((48, 67), "Shared Representations in Collaborative Reading", fontsize=18)
            page.insert_text((48, 96), "Reader validation sample - not a published study", fontsize=10)
            page.insert_text((48, 138), "1 Introduction", fontsize=14)
            page.insert_textbox((48, 164, 545, 250), "Shared representations help teams coordinate their work.\nThis sample tests bilingual reading, annotation geometry, and persistent notes.", fontsize=12)
            page.insert_text((48, 300), "2 Methods", fontsize=14)
            page.insert_textbox((48, 326, 545, 440), "The reader preserves the original PDF and links its text to an HTML reading view.\nA note belongs to the selected passage and can be viewed in either language.", fontsize=12)
            page.draw_rect(fitz.Rect(48, 500, 270, 600), color=(.3, .45, .65), fill=(.9, .94, .98))
            page.insert_text((65, 545), "PDF  <->  HTML  <->  Translation", fontsize=10)
            page.insert_text((48, 630), "Figure 1. A schematic used to check source locations.", fontsize=10)
        else:
            page.insert_text((48, 67), f"Chapter {index + 1}", fontsize=18)
            page.insert_text((48, 110), "中文阅读与标注测试", fontname="china-s", fontsize=17)
            page.insert_text((48, 155), "共享表征有助于团队协调工作。中文笔记应该保留，并可定位到原文。", fontname="china-s", fontsize=12)
            page.insert_text((48, 205), "This page verifies navigation, mixed-language text, and saved reading position.", fontsize=11)
        page.insert_text((290, 805), str(index + 1), fontsize=9)
        if rotated:
            page.set_rotation(90)
    doc.set_metadata({"title": "Shared Representations in Collaborative Reading", "author": "Reader validation sample"})
    doc.set_toc([[1, "Introduction", 1], [1, "Chinese reading", min(2, pages)]])
    result = doc.tobytes()
    doc.close()
    return result


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / "data", background=False)
    with TestClient(app) as value:
        value.store = app.state.store
        value.store.set_settings({"metadata_lookup": False})
        yield value


def import_and_parse(client, data=None, collection=""):
    response = client.post("/api/import" + ("?collection=" + collection if collection else ""), files={"files": ("test-paper.pdf", data or sample_pdf(), "application/pdf")})
    assert response.status_code == 200, response.text
    paper = response.json()["imported"][0]
    with client.store.connect() as db:
        job_id = db.execute("SELECT id FROM jobs WHERE paper_id=?", (paper["id"],)).fetchone()[0]
        db.execute("UPDATE jobs SET state='running' WHERE id=?", (job_id,))
    run(client.store, job_id)
    job = client.store.job(job_id)
    assert job["state"] == "done", job["message"]
    return client.get("/api/papers/" + paper["id"]).json()


def source_block(client, paper_id):
    page = client.get(f"/api/papers/{paper_id}/pages/1").json()
    return next(b for b in page["blocks"] if "coordinate their work" in b["text"])
