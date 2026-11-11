import json

import pymupdf as fitz

from reader import ai, pdf
from reader.academic import translation_blocks
from reader.margins import page_furniture_rows, page_margin_blocks
from .conftest import import_and_parse
from .test_annotations import add_translation
from .test_rebuild_html import queue_rebuild, run_job


TITLE = "A study of shared representations"


def review_pdf():
    with fitz.open() as doc:
        for index in range(3):
            page = doc.new_page(width=612, height=792)
            left = 73 if index % 2 == 0 else 110
            if index == 0:
                page.insert_text((left, 130), TITLE, fontsize=18)
            else:
                page.insert_text((left, 78), str(index + 1) if index % 2 else TITLE, fontsize=9)
                page.insert_text((510, 78), "Anon." if index % 2 else str(index + 1), fontsize=9)
            if index == 0:
                page.insert_text((left, 175), "1 Introduction", fontsize=13)
                page.insert_text((left, 640), "2026. Manuscript submitted to ACM", fontsize=7)
            page.insert_text((left, 205), "Study findings include 27 readers, a 7-point rating, and 6.40 in 2026.", fontsize=9)
            for y, text in ((255, "Anon."), (285, "Manuscript submitted to ACM"),
                            (315, "2 Anon."), (345, TITLE + " 3"), (375, "2026"), (405, "2")):
                page.insert_text((left, y), text, fontsize=10)
            page.insert_text((left, 665), "Manuscript submitted to ACM", fontsize=7)
            if index == 0:
                page.insert_text((498, 665), "1", fontsize=8)
        doc.set_metadata({"title": TITLE})
        return doc.tobytes()


def stored_blocks(client, pid):
    with client.store.connect() as db:
        return [dict(row) for row in db.execute("SELECT * FROM blocks WHERE paper_id=? ORDER BY page,position", (pid,))]


def test_new_parse_filters_only_positioned_headers_and_footers(client):
    paper = import_and_parse(client, review_pdf())
    pid = paper["id"]
    for number in range(1, 4):
        page = client.get(f"/api/papers/{pid}/pages/{number}").json()
        texts = [block["text"] for block in page["blocks"]]
        assert "Anon." in texts and "Manuscript submitted to ACM" in texts
        assert "2 Anon." in texts and TITLE + " 3" in texts
        assert "2026" in texts and "2" in texts
        assert all(block["bbox"][1] > 90 and block["bbox"][3] < 630 for block in page["blocks"])
        assert any("27 readers" in block["text"] for block in page["blocks"])
    assert all("2026. Manuscript" not in entry["title"] for entry in paper["outline"])


def test_legacy_furniture_and_sections_filter_without_changing_anchors(client, monkeypatch):
    with monkeypatch.context() as legacy:
        legacy.setattr(pdf, "_margin_lines", lambda *_args: set())
        paper = import_and_parse(client, review_pdf())
    pid = paper["id"]
    original = stored_blocks(client, pid)
    hidden = set()
    for number in range(1, 4):
        blocks = [{**row, "bbox": json.loads(row["bbox"])} for row in original if row["page"] == number]
        hidden.update(blocks[index]["id"] for index in page_margin_blocks(blocks, 792, number, TITLE))
    assert len(hidden) == 6
    body = next(row for row in original if row["page"] == 2 and "27 readers" in row["text"])
    assert body["section"] == "2 Anon."
    translation_id, _ = add_translation(client, pid, body)
    footer = next(row for row in original if row["id"] in hidden and row["page"] == 2)
    note = client.post(f"/api/papers/{pid}/annotations", json={
        "targets": [{"block_id": footer["id"], "start": 0, "end": 1}], "note": "Keep old PDF anchor",
    }).json()
    projected = client.get(f"/api/papers/{pid}/pages/2").json()["blocks"]
    assert not hidden.intersection(block["id"] for block in projected)
    projected_body = next(block for block in projected if block["id"] == body["id"])
    assert projected_body["section"] == "1 Introduction"
    assert projected_body["text"] == body["text"] and json.dumps(projected_body["bbox"], separators=(",", ":")) == body["bbox"]
    exported = client.get(f"/api/papers/{pid}/export/html").text
    assert all("block-" + block_id not in exported for block_id in hidden)
    with client.store.connect() as db:
        blocks, *_ = translation_blocks(db, client.store.paper(pid, db), client.store.original(pid),
                                         {"block_ids": list(hidden)})
        assert not hidden.intersection(block["id"] for block in blocks)
        assert next(block for block in blocks if block["id"] == body["id"])["section"] == "1 Introduction"
        outline = pdf._heading_outline(db, pid)
        assert sum(entry["title"] == "2 Anon." for entry in outline) == 3
        assert all("2026. Manuscript" not in entry["title"] for entry in outline)
    assert stored_blocks(client, pid) == original

    calls = []

    class Model:
        provider = "local"
        model = "fixture"
        settings = {"context_tokens": 4096}

        def __init__(self, *_args):
            pass

        def ask(self, instructions, text, **_options):
            calls.append(text)
            assert "Anon." not in text and "Manuscript submitted" not in text
            assert "27 readers" in text
            return {"answer": "Fixture answer", "citations": []}

    monkeypatch.setattr(ai, "Client", Model)
    for task in ("summary", "question"):
        job = client.store.create_job("reading", pid, {"task": task, "question": "Explain the findings",
                                                     "block_ids": [body["id"], *hidden]})
        assert run_job(client, job["id"])["state"] == "done"
    assert len(calls) == 2
    rebuilt = run_job(client, queue_rebuild(client, pid))
    assert rebuilt["state"] == "done" and rebuilt["result"]["preserved_pages"] == [2]
    assert client.get(f"/api/papers/{pid}/annotations").json()[0]["targets"] == note["targets"]
    assert client.get(f"/api/blocks/{body['id']}/translations").json()[0]["id"] == translation_id
    assert next(row for row in stored_blocks(client, pid) if row["id"] == body["id"]) == body


def test_numeric_table_rows_and_a_literal_paper_title_are_preserved():
    table = [
        {"key": "first", "text": "3", "bbox": [120, 80, 130, 90]},
        {"key": "second", "text": "27", "bbox": [220, 80, 240, 90]},
    ]
    assert page_furniture_rows(table, 792, 3) == set()
    title = "Manuscript submitted to ACM"
    rows = [{"key": "title", "text": title, "bbox": [80, 640, 400, 660]}]
    assert page_furniture_rows(rows, 792, 1, title) == set()
