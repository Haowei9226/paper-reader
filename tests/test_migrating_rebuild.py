"""Exercise source-anchor migration through the real staged rebuild publisher."""
import hashlib
import json
import zlib

import pymupdf as fitz

from reader import pdf
from reader.store import dumps

from .conftest import import_and_parse
from .test_annotations import add_translation
from .test_rebuild_html import queue_rebuild, run_job


TITLE = "A Migration Validation Paper"


def migration_pdf():
    with fitz.open() as doc:
        font = fitz.Font("tibo")
        for number in (1, 2):
            page = doc.new_page(width=595, height=842)
            page.insert_font(fontname="embedded", fontbuffer=font.buffer)
            page.insert_text((48, 75 if number == 1 else 30), TITLE, fontsize=18 if number == 1 else 9)
            page.insert_text((48, 190), "The \ufb01rst method retains every translated word.", fontname="embedded", fontsize=11)
            page.insert_text((48, 260), "The follow-up paragraph marks the newly extracted page.", fontsize=11)
        doc.set_metadata({"title": TITLE})
        return doc.tobytes()


def source_row(client, paper_id, page=1):
    with client.store.connect() as db:
        return dict(db.execute("SELECT * FROM blocks WHERE paper_id=? AND page=? AND text LIKE 'The first method%'",
                               (paper_id, page)).fetchone())


def simulate_truncated_legacy_ligature(client, block):
    geo = pdf.geometry(block["geometry"])
    index = block["text"].index("first") + 1
    old = dict(block)
    old["text"] = block["text"][:index] + block["text"][index + 1:]
    for key in ("boxes", "lines"):
        del geo[key][index]
    old["geometry"] = zlib.compress(dumps(geo).encode())
    with client.store.connect() as db:
        db.execute("UPDATE blocks SET text=?,geometry=? WHERE id=?", (old["text"], old["geometry"], block["id"]))
        client.store.reindex_block(db, block["id"])
    return old


def records(client, paper_id, table):
    with client.store.connect() as db:
        return [dict(row) for row in db.execute(f"SELECT * FROM {table} WHERE paper_id=? ORDER BY id", (paper_id,))]


def add_historical_note(client, paper_id, block):
    old_id, _ = add_translation(client, paper_id, block)
    response = client.patch(f"/api/translations/{old_id}", json={"text": "人工核对的现行译文保持原样。"})
    assert response.status_code == 200
    start = block["text"].index("frst")
    response = client.post(f"/api/papers/{paper_id}/annotations", json={
        "targets": [{"block_id": block["id"], "start": start, "end": start + 4}],
        "translated_targets": [{"block_id": block["id"], "translation_id": old_id, "start": 0, "end": 4}],
        "note": "原笔记不能被重转覆盖", "color": "green",
    })
    assert response.status_code == 200, response.text
    return old_id, response.json()["id"]


def current_block(client, block_id):
    with client.store.connect() as db:
        return dict(db.execute("SELECT * FROM blocks WHERE id=?", (block_id,)).fetchone())


def test_rebuild_migrates_real_same_glyph_repair_and_keeps_all_translation_content(client):
    original = migration_pdf()
    paper = import_and_parse(client, original)
    pid, path = paper["id"], f"/api/papers/{paper['id']}"
    candidate = source_row(client, pid)
    old = simulate_truncated_legacy_ligature(client, candidate)
    _, aid = add_historical_note(client, pid, old)
    locator = {"page": 1, "block_id": old["id"], "mode": "html", "side": "translation", "offset": .47,
               "scroll": .3, "page_fraction": .42, "saved_at": 1794417671981}
    assert client.patch(path, json={"locator": locator}).status_code == 200
    before_tr, before_ann = records(client, pid, "translations"), records(client, pid, "annotations")[0]

    job = run_job(client, queue_rebuild(client, pid))
    assert job["state"] == "done", job["message"]
    assert job["result"]["converted_pages"] == 2 and job["result"]["preserved_pages"] == []
    assert current_block(client, old["id"])["text"] == candidate["text"]
    assert job["result"]["migrated_source_records"] == {"translations": 1, "annotations": 1}
    after_tr = records(client, pid, "translations")
    assert len(after_tr) == len(before_tr) == 2
    assert sorted((row["active"], row["manual"]) for row in after_tr) == [(0, 0), (1, 1)]
    for before, after in zip(before_tr, after_tr):
        assert {key: value for key, value in before.items() if key != "segments"} == {
            key: value for key, value in after.items() if key != "segments"}
        for a, b in zip(json.loads(before["segments"]), json.loads(after["segments"])):
            assert b["source_end"] == a["source_end"] + 1
            assert {key: value for key, value in a.items() if key != "source_end"} == {
                key: value for key, value in b.items() if key != "source_end"}
    after_ann = records(client, pid, "annotations")[0]
    assert after_ann["id"] == aid and after_ann["quote"] == "first"
    assert json.loads(after_ann["targets"])[0]["quote"] == "first"
    assert {key: value for key, value in after_ann.items() if key not in ("targets", "quote")} == {
        key: value for key, value in before_ann.items() if key not in ("targets", "quote")}
    assert client.get(path).json()["locator"] == locator
    assert client.store.original(pid).read_bytes() == original


def restore_old_header_block(client, paper_id):
    with fitz.open(client.store.original(paper_id)) as doc:
        page = doc[1]
        blocks = page.get_text("rawdict")["blocks"]
        pdf.recover_pdf_glyphs(page, blocks)
        for index, raw in enumerate(blocks):
            text, geo, _ = pdf.extract_block(raw, page)
            if text.strip() != TITLE:
                continue
            block = {"id": hashlib.sha256(f"{paper_id}:native-v1:1:{index}".encode()).hexdigest()[:32],
                     "paper_id": paper_id, "page": 2, "position": -1, "kind": "paragraph", "text": text,
                     "bbox": dumps([round(value, 3) for value in raw["bbox"]]),
                     "geometry": zlib.compress(dumps(geo).encode()), "asset": None, "section": TITLE}
            with client.store.connect() as db:
                db.execute("INSERT INTO blocks VALUES(?,?,?,?,?,?,?,?,?,?)", tuple(block.values()))
                client.store.reindex_block(db, block["id"])
            return block
    raise AssertionError("The test PDF must contain a real running title on page two")


def test_rebuild_retains_only_translated_running_header_and_updates_same_page_body(client):
    paper = import_and_parse(client, migration_pdf())
    pid = paper["id"]
    candidate = source_row(client, pid, 2)
    old = simulate_truncated_legacy_ligature(client, candidate)
    add_translation(client, pid, old)
    header = restore_old_header_block(client, pid)
    add_translation(client, pid, header)
    before_header_versions = [row for row in records(client, pid, "translations") if row["block_id"] == header["id"]]

    job = run_job(client, queue_rebuild(client, pid))
    assert job["state"] == "done", job["message"]
    assert job["result"]["converted_pages"] == 2 and job["result"]["preserved_pages"] == []
    assert job["result"]["retained_source_blocks"][header["id"]] == "header"
    retained = current_block(client, header["id"])
    assert retained["kind"] == "legacy"
    assert {key: value for key, value in retained.items() if key != "kind"} == {
        key: value for key, value in header.items() if key != "kind"}
    assert current_block(client, old["id"])["text"] == candidate["text"]
    assert [row for row in records(client, pid, "translations") if row["block_id"] == header["id"]] == before_header_versions
    page = client.get(f"/api/papers/{pid}/pages/2").json()
    assert next(block for block in page["blocks"] if block["id"] == header["id"])["reading_element"] == "legacy_source"


def test_rebuild_cannot_replace_unproven_real_body_and_preserves_its_entire_page(client):
    paper = import_and_parse(client, migration_pdf())
    pid = paper["id"]
    old = source_row(client, pid)
    old["text"] = old["text"].replace("first", "worst")
    with client.store.connect() as db:
        db.execute("UPDATE blocks SET text=? WHERE id=?", (old["text"], old["id"]))
        client.store.reindex_block(db, old["id"])
        page_two = db.execute("SELECT id FROM blocks WHERE paper_id=? AND page=2 AND text LIKE 'The follow-up%'", (pid,)).fetchone()[0]
        db.execute("UPDATE blocks SET text='Old cached paragraph' WHERE id=?", (page_two,))
    add_translation(client, pid, old)
    before_page = [row for row in records(client, pid, "blocks") if row["page"] == 1]
    before_tr = records(client, pid, "translations")

    job = run_job(client, queue_rebuild(client, pid))
    assert job["state"] == "done", job["message"]
    assert job["result"]["converted_pages"] == 1 and job["result"]["preserved_pages"] == [1]
    assert job["result"]["retained_source_blocks"] == {}
    assert [row for row in records(client, pid, "blocks") if row["page"] == 1] == before_page
    assert records(client, pid, "translations") == before_tr
    assert current_block(client, page_two)["text"].startswith("The follow-up paragraph")


def test_publish_failure_rolls_back_migrated_json_and_new_blocks_together(client, monkeypatch):
    paper = import_and_parse(client, migration_pdf())
    pid = paper["id"]
    candidate = source_row(client, pid)
    old = simulate_truncated_legacy_ligature(client, candidate)
    historical_id, aid = add_historical_note(client, pid, old)
    before = {table: records(client, pid, table) for table in ("blocks", "translations", "annotations")}
    observed = {}

    def fail_after_source_upsert(db, block_id):
        observed["source"] = db.execute("SELECT text FROM blocks WHERE id=?", (old["id"],)).fetchone()[0]
        observed["quote"] = db.execute("SELECT quote FROM annotations WHERE id=?", (aid,)).fetchone()[0]
        observed["segments"] = json.loads(db.execute("SELECT segments FROM translations WHERE id=?", (historical_id,)).fetchone()[0])
        raise RuntimeError("fail after block UPSERT and anchor JSON migration")

    job_id = queue_rebuild(client, pid)
    with monkeypatch.context() as patch:
        patch.setattr(client.store, "reindex_block", fail_after_source_upsert)
        job = run_job(client, job_id)
    assert job["state"] == "failed"
    assert observed["source"] == candidate["text"] and observed["quote"] == "first"
    assert observed["segments"][0]["source_end"] == pdf.length(candidate["text"])
    for table, rows in before.items():
        assert records(client, pid, table) == rows
    assert client.get(f"/api/papers/{pid}").json()["parse_state"] == "ready"
    assert client.post(f"/api/jobs/{job_id}/resume").status_code == 200
    assert run_job(client, job_id)["state"] == "done"
    assert current_block(client, old["id"])["text"] == candidate["text"]
