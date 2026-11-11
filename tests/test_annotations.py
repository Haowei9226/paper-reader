import json

import pymupdf as fitz
import pytest

from reader.pdf import length
from reader.store import dumps, now, uid
from .conftest import import_and_parse, sample_pdf, source_block


def add_translation(client, paper_id, block):
    tid = uid()
    text = "共享表征有助于团队协调工作。这是用于验证双语阅读和笔记保存的测试。"
    segment = [{"source_start": 0, "source_end": length(block["text"]), "target_start": 0, "target_end": length(text)}]
    with client.store.connect() as db:
        db.execute("INSERT INTO translations VALUES(?,?,?,?,?,?,?,?,?,?)", (tid, block["id"], paper_id, text, dumps(segment), "local", "test-model", 0, 1, now()))
        client.store.reindex_block(db, block["id"])
    return tid, text


@pytest.mark.parametrize("rotated", [False, True])
def test_three_view_annotation_export_and_version_protection(client, rotated):
    paper = import_and_parse(client, sample_pdf(rotated=rotated))
    pid = paper["id"]
    block = source_block(client, pid)
    tid, translated = add_translation(client, pid, block)
    start = translated.index("协调工作")
    ann = client.post(f"/api/papers/{pid}/annotations", json={"selected_language": "translation", "translated_targets": [{"block_id": block["id"], "translation_id": tid, "start": start, "end": start + 4}], "note": "中文笔记：对应到英文短语"}).json()
    aid = ann["id"]
    assert ann["targets"][0]["rects"]
    source_start = block["text"].index("coordinate their work")
    manual = client.patch("/api/annotations/" + aid, json={"targets": [{"block_id": block["id"], "start": source_start, "end": source_start + len("coordinate their work")}], "translated_targets": ann["translated_targets"]}).json()
    assert manual["alignment"] == "manual"
    assert manual["targets"][0]["quote"] == "coordinate their work"
    assert client.get('/api/annotations',params={'q':'协调工作'}).json()['total']==1
    response = client.get(f"/api/papers/{pid}/export/pdf")
    assert response.status_code == 200
    document = fitz.open(stream=response.content, filetype="pdf")
    exported_page = document[0]
    exported = list(exported_page.annots())
    assert exported and "中文笔记" in exported[0].info["content"]
    selected = exported_page.get_textbox(exported[0].rect)
    assert "coordinate" in selected
    assert client.get(f"/api/papers/{pid}/export/notes").text.find("协调工作") >= 0
    assert "<html" in client.get(f"/api/papers/{pid}/export/html").text
    new = client.patch("/api/translations/" + tid, json={"text": "重新修改的译文：共享表征帮助团队协作。"}).json()
    preserved = client.get(f"/api/papers/{pid}/annotations").json()[0]
    assert preserved["alignment"] == "review"
    assert preserved["note"] == manual["note"]
    assert preserved["translated_targets"][0]["translation_id"] == tid
    history = client.get("/api/blocks/" + block["id"] + "/translations").json()
    assert len(history) == 2 and new["id"] != tid
    assert next(t for t in history if t["id"] == tid)["text"] == translated
    client.patch("/api/annotations/" + aid, json={"color": "green", "note": "修改后两侧共用此笔记"})
    assert client.get(f"/api/papers/{pid}/annotations").json()[0]["color"] == "green"
    client.delete("/api/annotations/" + aid)
    assert client.get(f"/api/papers/{pid}/annotations").json() == []


def test_invalid_ranges_cannot_create_corrupt_annotation(client):
    p = import_and_parse(client)
    b = source_block(client, p["id"])
    response = client.post(f"/api/papers/{p['id']}/annotations", json={"targets": [{"block_id": b["id"], "start": 0, "end": 100000}]})
    assert response.status_code == 400
    assert not client.get(f"/api/papers/{p['id']}/annotations").json()
