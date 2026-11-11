import re

import pymupdf as fitz
import pytest

from reader import ai, pdf
from reader.academic import translation_blocks
from reader.margins import line_number_blocks, line_number_rows
from reader.reading import classify_reading_blocks
from .conftest import import_and_parse
from .test_annotations import add_translation
from .test_rebuild_html import queue_rebuild, run_job


def manuscript_pdf():
    with fitz.open() as doc:
        for index in range(2):
            page = doc.new_page(width=612, height=792)
            left = 73 if index == 0 else 110
            for line in range(52):
                page.insert_text((left - 26, 104 + 10.96 * line), str(index * 52 + line + 1),
                                 fontsize=6, color=(1, 0, 0))
            page.insert_text((left, 76), str(index + 1), fontsize=9)
            page.insert_text((left, 96), "Manuscript review fixture", fontsize=16)
            for line in range(12):
                page.insert_text((left, 130 + 14 * line),
                                 f"Study sentence {line + 1} reports 27 readers in 2026 and a 7-point rating of 6.40.",
                                 fontsize=9)
            page.insert_text((left + 320, 370), "(3)", fontsize=9)
            for row in range(5):
                page.insert_text((left + 20, 430 + 16 * row), str(row + 1), fontsize=6)
                page.insert_text((left + 90, 430 + 16 * row), str(row + 10), fontsize=6)
            page.insert_text((left, 580), "References", fontsize=13)
            page.insert_text((left, 600), "[1] A. Author. 2026. A bibliography entry preserves its numeric label.", fontsize=9)
        doc.set_metadata({"title": "Manuscript review fixture"})
        return doc.tobytes()


def test_new_parse_removes_only_number_rails_and_keeps_body_anchor_ids(tmp_path, monkeypatch):
    with fitz.open(stream=manuscript_pdf(), filetype="pdf") as doc:
        for page in doc:
            destination = tmp_path / str(page.number)
            destination.mkdir()
            with monkeypatch.context() as old:
                old.setattr(pdf, "_margin_lines", lambda *_: set())
                legacy, _ = pdf._extract_page(page, "fixture", destination, "")
            clean, _ = pdf._extract_page(page, "fixture", destination, "")
            removed = {b["id"] for b in legacy} - {b["id"] for b in clean}
            left = 73 if page.number == 0 else 110
            rail = [b for b in legacy if b["id"] in removed and b["bbox"][2] < left - 4]
            assert len(rail) == 52
            assert all(re.fullmatch(r"\d{1,4}", b["text"]) for b in rail)
            before = {b["id"]: b for b in legacy}
            for block in clean:
                old = before[block["id"]]
                if block["text"] != old["text"]:
                    assert block["text"] == "Manuscript review fixture"
                    assert old["text"].endswith(block["text"])
                    assert block["geometry"]["boxes"] == old["geometry"]["boxes"][-len(block["geometry"]["boxes"]):]
                else:
                    assert block["geometry"] == old["geometry"]
                    assert block["bbox"] == old["bbox"]
            assert any("27 readers in 2026" in b["text"] for b in clean)
            assert any(b["text"] == "(3)" for b in clean)
            assert {f"{i + 1} {i + 10}" for i in range(5)} <= {b["text"] for b in clean}
            assert any("[1] A. Author. 2026." in b["text"] for b in clean)


def test_legacy_lines_hidden_from_html_translation_and_rebuild_without_losing_notes(client, monkeypatch):
    with monkeypatch.context() as old:
        old.setattr(pdf, "_margin_lines", lambda *_: set())
        paper = import_and_parse(client, manuscript_pdf())
    pid = paper["id"]
    with client.store.connect() as db:
        originals = [dict(row) for row in db.execute("SELECT * FROM blocks WHERE paper_id=? ORDER BY page,position", (pid,))]
    rail = next(block for block in originals if block["page"] == 1 and block["text"] == "1")
    body = next(block for block in originals if "Study sentence" in block["text"])
    translation_id, _ = add_translation(client, pid, body)
    notes = []
    for block in (rail, body):
        note = client.post(f"/api/papers/{pid}/annotations", json={
            "targets": [{"block_id": block["id"], "start": 0, "end": 1}], "note": "Keep existing anchor",
        })
        assert note.status_code == 200, note.text
        notes.append(note.json())
    page = client.get(f"/api/papers/{pid}/pages/1").json()
    assert rail["id"] not in {block["id"] for block in page["blocks"]}
    assert any("27 readers" in block["text"] for block in page["blocks"])
    exported = client.get(f"/api/papers/{pid}/export/html").text
    assert "block-" + rail["id"] not in exported
    with client.store.connect() as db:
        blocks, *_ = translation_blocks(db, client.store.paper(pid, db), client.store.original(pid),
                                         {"block_ids": [rail["id"], body["id"]]})
        assert rail["id"] not in {block["id"] for block in blocks}
        assert db.execute("SELECT text FROM blocks WHERE id=?", (rail["id"],)).fetchone()[0] == "1"

    class Model:
        provider = "local"
        model = "fixture"
        settings = {"context_tokens": 4096}

        def __init__(self, *args):
            pass

        def ask(self, *_args, **_kwargs):
            pytest.fail("A selected margin line number must not be sent for translation")

    monkeypatch.setattr(ai, "Client", Model)
    job = client.store.create_job("translate", pid, {"block_ids": [rail["id"]]})
    assert run_job(client, job["id"])["state"] == "done"
    rebuilt = run_job(client, queue_rebuild(client, pid))
    assert rebuilt["state"] == "done", rebuilt["message"]
    assert rebuilt["result"]["preserved_pages"] == [1]
    with client.store.connect() as db:
        current = dict(db.execute("SELECT * FROM blocks WHERE id=?", (body["id"],)).fetchone())
        assert {key: current[key] for key in ("text", "bbox", "geometry")} == {
            key: body[key] for key in ("text", "bbox", "geometry")}
        assert db.execute("SELECT id FROM translations WHERE block_id=?", (body["id"],)).fetchone()[0] == translation_id
        assert not db.execute("SELECT 1 FROM blocks WHERE paper_id=? AND page=2 AND text='53'", (pid,)).fetchone()
    saved = client.get(f"/api/papers/{pid}/annotations").json()
    assert {note["id"] for note in saved} == {note["id"] for note in notes}
    assert {note["id"]: note["targets"] for note in saved} == {note["id"]: note["targets"] for note in notes}


def test_same_size_lists_and_table_values_are_not_line_number_rails():
    body = {"key": "body", "text": "A normal numbered list inside the article", "bbox": [90, 100, 500, 500], "size": 10}
    numbered = [{"key": i, "text": str(i + 1), "bbox": [65, 110 + 24 * i, 72, 120 + 24 * i], "size": 10} for i in range(12)]
    assert line_number_rows([body, *numbered], 792) == set()
    table = [{**row, "bbox": [130, row["bbox"][1], 136, row["bbox"][3]], "size": 6} for row in numbered]
    assert line_number_rows([body, *table], 792) == set()


def test_grouped_number_rail_uses_original_line_geometry():
    parts, boxes, lines = [], [], []
    for index in range(20):
        if parts:
            parts.append(" ")
            boxes.append(None)
            lines.append(index)
        text = str(index + 1)
        parts.append(text)
        boxes.extend([[45, 100 + index * 25, 45 + len(text) * 3, 106 + index * 25]] * len(text))
        lines.extend([index] * len(text))
    blocks = [
        {"id": "rail", "kind": "paragraph", "section": "", "text": "".join(parts), "bbox": [45, 100, 51, 581],
         "geometry": {"boxes": boxes, "lines": lines}},
        {"id": "body", "kind": "paragraph", "section": "", "text": "The article has a normal paragraph with numeric findings 27 and 2026.",
         "bbox": [73, 100, 500, 600],
         "geometry": {"boxes": [[73, 100, 78, 109]] * 40, "lines": [0] * 40}},
    ]
    assert line_number_blocks(blocks, 792) == {0}
    paper = {"title": "", "authors": "", "outline": []}
    classify_reading_blocks(blocks, paper, 2, 792, equation_blocks={"rail": object()})
    assert [block["id"] for block in blocks] == ["body"]
