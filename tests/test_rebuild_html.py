from reader import pdf
from reader.worker import run

from .conftest import import_and_parse, source_block
from .test_annotations import add_translation


def queue_rebuild(client, paper_id):
    response = client.post(f"/api/papers/{paper_id}/jobs", json={"kind": "parse", "rebuild": True})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def run_job(client, job_id):
    with client.store.connect() as db:
        db.execute("UPDATE jobs SET state='running' WHERE id=? AND state='queued'", (job_id,))
    run(client.store, job_id)
    return client.store.job(job_id)


def stale_second_page(client, paper_id):
    with client.store.connect() as db:
        block = db.execute("SELECT id FROM blocks WHERE paper_id=? AND page=2 AND text LIKE 'This page verifies%'",
                           (paper_id,)).fetchone()[0]
        db.execute("UPDATE blocks SET text='Outdated cached page text' WHERE id=?", (block,))
        client.store.reindex_block(db, block)


def test_rebuild_reads_all_pages_and_retains_notes_translations_and_pdf(client):
    paper_id = import_and_parse(client)["id"]
    path = f"/api/papers/{paper_id}"
    block = source_block(client, paper_id)
    translation_id, _ = add_translation(client, paper_id, block)
    start = block["text"].index("coordinate their work")
    note = client.post(path + "/annotations", json={"targets": [
        {"block_id": block["id"], "start": start, "end": start + len("coordinate their work")},
    ], "note": "保留这条双语笔记"}).json()
    locator = {"page": 1, "block_id": block["id"], "mode": "html"}
    client.patch(path, json={"locator": locator, "authors": "Checked manually"})
    original = client.store.original(paper_id).read_bytes()
    stale_second_page(client, paper_id)
    client.store.set_settings({"metadata_lookup": True})

    job_id = queue_rebuild(client, paper_id)
    assert client.post(path + "/jobs", json={"kind": "parse", "rebuild": True}).status_code == 400
    assert client.get(path + "/find", params={"q": "Outdated cached page text"}).json()["items"]
    job = run_job(client, job_id)
    assert job["state"] == "done", job["message"]
    assert job["result"]["converted_pages"] == 2 and not job["result"]["preserved_pages"]
    assert not client.get(path + "/find", params={"q": "Outdated cached page text"}).json()["items"]
    assert client.get(path + "/find", params={"q": "This page verifies navigation"}).json()["items"][0]["page"] == 2
    assert client.get(path + "/annotations").json() == [note]
    assert client.get(f"/api/blocks/{block['id']}/translations").json()[0]["id"] == translation_id
    current = client.get(path).json()
    assert current["locator"] == locator and current["authors"] == "Checked manually"
    assert client.store.original(paper_id).read_bytes() == original
    assert not any(job["kind"] == "metadata" for job in client.get("/api/jobs").json())


def test_rebuild_preserves_a_page_whose_saved_text_is_incompatible(client):
    paper_id = import_and_parse(client)["id"]
    path = f"/api/papers/{paper_id}"
    block = source_block(client, paper_id)
    legacy = block["text"].replace("their work", "team tasks")
    assert pdf.length(legacy) == pdf.length(block["text"])
    with client.store.connect() as db:
        db.execute("UPDATE blocks SET text=? WHERE id=?", (legacy, block["id"]))
        client.store.reindex_block(db, block["id"])
    start = legacy.index("team tasks")
    note = client.post(path + "/annotations", json={"targets": [
        {"block_id": block["id"], "start": start, "end": start + len("team tasks")},
    ], "note": "原有文字定位"}).json()
    stale_second_page(client, paper_id)

    job = run_job(client, queue_rebuild(client, paper_id))
    assert job["state"] == "done", job["message"]
    assert job["result"]["converted_pages"] == 1 and job["result"]["preserved_pages"] == [1]
    assert client.get(path + "/annotations").json() == [note]
    assert next(item["text"] for item in client.get(path + "/pages/1").json()["blocks"] if item["id"] == block["id"]) == legacy
    assert client.get(path + "/find", params={"q": "This page verifies navigation"}).json()["items"]


def test_failed_rebuild_keeps_the_last_readable_version_until_retry(client, monkeypatch):
    paper_id = import_and_parse(client)["id"]
    path = f"/api/papers/{paper_id}"
    stale_second_page(client, paper_id)
    before = client.get(path + "/pages/2").json()
    extract = pdf._extract_page

    def fail_after_staging(page, *args, **kwargs):
        if page.number == 1:
            raise RuntimeError("simulated damaged page")
        return extract(page, *args, **kwargs)

    job_id = queue_rebuild(client, paper_id)
    with monkeypatch.context() as patch:
        patch.setattr(pdf, "_extract_page", fail_after_staging)
        assert run_job(client, job_id)["state"] == "failed"
    assert client.get(path).json()["parse_state"] == "ready"
    assert client.get(path + "/pages/2").json() == before
    assert client.post(f"/api/jobs/{job_id}/resume").json()["state"] == "queued"
    assert run_job(client, job_id)["result"]["converted_pages"] == 2
