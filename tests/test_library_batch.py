import json

import httpx

from reader import ai
from reader.runtime import Runtime
from reader.store import Store, dumps

from .conftest import import_and_parse, sample_pdf, source_block
from .test_annotations import add_translation
from .test_metadata import TITLE, crossref, make_pdf, set_registry
from .test_translation_recovery import start


def test_column_fields_are_sortable_and_read_time_is_independent(client):
    first, second, third = [import_and_parse(client, sample_pdf(pages=n))["id"] for n in (2, 3, 4)]
    first_path = f"/api/papers/{first}"
    assert client.get(first_path).json()["last_read_at"] is None
    opened = client.patch(first_path, json={"opened": True}).json()
    assert opened["last_read_at"] > 0 and opened["locator"] == {}
    fields = {"publication_date": "2024-5", "publisher": "Zeta Press", "item_type": "conference-paper"}
    edited = client.patch(first_path, json=fields).json()
    assert edited["publication_date"] == "2024-05" and edited["publisher"] == fields["publisher"]
    assert edited["last_read_at"] == opened["last_read_at"] and edited["has_attachment"]
    assert client.patch(first_path, json={"publication_date": "2024-02-31"}).status_code == 400
    client.patch(f"/api/papers/{second}", json={"year": "2026", "publisher": "Alpha Press", "item_type": "book", "opened": True})
    for sort, expected in (("publication_date", [second, first, third]), ("last_read_at", [second, first, third])):
        result = client.get("/api/papers", params={"sort": sort, "direction": "desc"}).json()
        assert [paper["id"] for paper in result["items"]] == expected
    result = client.get("/api/papers", params={"sort": "publisher", "direction": "asc", "limit": 1}).json()
    assert result["items"][0]["id"] == second
    assert client.get("/api/papers", params={"q": "Zeta Press"}).json()["total"] == 1


def test_previous_library_recovers_last_read_from_saved_locator(client):
    pid = import_and_parse(client)["id"]
    locator = {"page": 2, "mode": "html", "saved_at": 1789560000000}
    with client.store.connect() as db:
        db.execute("UPDATE papers SET locator=? WHERE id=?", (dumps(locator), pid))
        for column in ("publication_date", "publisher", "item_type", "last_read_at"):
            db.execute(f"ALTER TABLE papers DROP COLUMN {column}")
    paper = Store(client.store.root).paper(pid)
    assert paper["last_read_at"] == locator["saved_at"] / 1000
    assert paper["locator"] == locator and paper["has_attachment"]
    assert (paper["item_type"], paper["publication_date"], paper["publisher"]) == ("", "", "")


def test_batch_queue_deduplicates_and_runs_independent_translations(client, monkeypatch):
    first, second, waiting, busy, trashed = [import_and_parse(client, sample_pdf(pages=n))["id"] for n in (2, 3, 4, 5, 6)]
    client.patch(f"/api/papers/{second}", json={"language": "zh"})
    client.delete(f"/api/papers/{trashed}")
    with client.store.connect() as db:
        db.execute("UPDATE papers SET parse_state='pending' WHERE id=?", (waiting,))
    prior = client.store.create_job("translate", busy, {"provider": "local"})
    block = source_block(client, first)
    tid, _ = add_translation(client, first, block)
    manual = client.patch(f"/api/translations/{tid}", json={"text": "保留人工术语"}).json()
    connections = []

    def local_ready(_runtime, settings):
        connections.append(settings["local_url"])
        return {"url": settings["local_url"]}

    monkeypatch.setattr(Runtime, "ensure", local_ready)
    response = client.post("/api/papers/batch", json={"action": "translate", "provider": "local", "force": True,
        "paper_ids": [first, second, first, waiting, busy, trashed]})
    assert response.status_code == 200, response.text
    queued, skipped = response.json()["queued"], response.json()["skipped"]
    assert [job["paper_id"] for job in queued] == [first, second]
    assert [item["paper_id"] for item in skipped] == [waiting, busy, trashed]
    assert len(connections) == 1
    assert all(job["kind"] == "translate" and job["payload"]["preserve_manual"] and job["payload"]["force"] for job in queued)
    assert all("page_start" not in job["payload"] for job in queued)
    retry = client.post("/api/papers/batch", json={"action": "translate", "paper_ids": [first, second, busy]}).json()
    assert not retry["queued"] and len(retry["skipped"]) == 3
    assert client.store.job(prior["id"])["state"] == "queued"

    class Model:
        provider, model, settings = "local", "batch-fixture", {"context_tokens": 4096}

        def __init__(self, *_args):
            pass

        def ask(self, _instruction, value, **_options):
            return {"segments": [{"id": item["id"], "translation": item["text"]}
                                 for item in json.loads(value)["segments"]]}

    monkeypatch.setattr(ai, "Client", Model)
    for job in queued:
        completed = start(client.store, job)
        assert completed["state"] == "done", completed["message"]
        assert client.store.paper(job["paper_id"])["translated_count"] > 0
    assert client.get(f"/api/blocks/{block['id']}/translations").json()[0]["id"] == manual["id"]


def test_batch_reading_status_only_updates_selected_active_papers(client):
    first, second, untouched = [import_and_parse(client, sample_pdf(pages=n))["id"] for n in (2, 3, 4)]
    client.delete(f"/api/papers/{second}")
    response = client.post("/api/papers/batch", json={"action": "status", "status": "read", "paper_ids": [first, second]})
    assert response.status_code == 200, response.text
    assert response.json()["updated"] == 1 and response.json()["skipped"][0]["paper_id"] == second
    assert client.store.paper(first)["status"] == "read" and client.store.paper(first)["last_read_at"] is None
    assert client.store.paper(second)["status"] == client.store.paper(untouched)["status"] == "unread"


def test_batch_publication_lookup_fills_only_missing_columns(client, monkeypatch):
    preprint_title = "A Preprint on Reading Together"
    preprint_doi = "10.48550/arXiv.2402.01234"
    first = import_and_parse(client, make_pdf(TITLE, "DOI: 10.1234/reader.example"))["id"]
    second = import_and_parse(client, make_pdf(preprint_title))["id"]
    complete = import_and_parse(client, sample_pdf())["id"]
    busy = import_and_parse(client, sample_pdf(pages=3))["id"]
    client.patch(f"/api/papers/{second}", json={"doi": preprint_doi, "item_type": "preprint"})
    client.patch(f"/api/papers/{second}", json={"item_type": ""})
    with client.store.connect() as db:
        db.execute("UPDATE papers SET publisher='Existing press' WHERE id=?", (second,))
    client.patch(f"/api/papers/{complete}", json={"publication_date": "2022", "item_type": "book", "publisher": "Existing"})
    prior = client.post(f"/api/papers/{busy}/metadata").json()
    original = client.store.paper(first)
    requests = []

    def registry(request):
        requests.append(str(request.url))
        assert request.method == "GET" and request.content == b""
        if request.url.host == "api.crossref.org":
            return httpx.Response(200, json={"message": crossref()})
        assert request.url.host == "api.datacite.org"
        return httpx.Response(200, json={"data": {"attributes": {
            "doi": preprint_doi, "titles": [{"title": preprint_title}], "publicationYear": 2024, "published": "2024-02",
            "publisher": {"name": "arXiv"}, "types": {"resourceTypeGeneral": "Preprint"},
        }}})

    set_registry(monkeypatch, registry)
    response = client.post("/api/papers/batch", json={
        "action": "metadata", "paper_ids": [first, second, first, complete, busy],
    })
    assert response.status_code == 200, response.text
    result = response.json()
    assert [job["paper_id"] for job in result["queued"]] == [first, second]
    assert [item["paper_id"] for item in result["skipped"]] == [complete, busy]
    assert not requests and client.store.job(prior["id"])["state"] == "queued"
    for job in result["queued"]:
        assert job["kind"] == "metadata" and job["payload"] == {"requested": True, "fill_publication_fields": True}
        finished = start(client.store, job)
        assert finished["state"] == "done" and not finished["result"]["missing_fields"]
    assert requests == ["https://api.crossref.org/works/10.1234/reader.example", "https://api.datacite.org/dois/" + preprint_doi]
    paper = client.store.paper(first)
    assert (paper["publication_date"], paper["publisher"], paper["item_type"]) == ("2024-05-02", "Reading Press", "journal-article")
    for key in ("title", "filename", "authors", "year", "doi", "metadata_status", "metadata_source"):
        assert paper[key] == original[key]
    paper = client.store.paper(second)
    assert (paper["publication_date"], paper["publisher"], paper["item_type"]) == ("2024-02", "Existing press", "")
    again = client.post("/api/papers/batch", json={"action": "metadata", "paper_ids": [first, second]}).json()
    assert not again["queued"] and len(again["skipped"]) == 2


def test_batch_publication_lookup_can_retry_and_preserves_edits_made_during_lookup(client, monkeypatch):
    pid = import_and_parse(client, make_pdf(TITLE, "DOI: 10.1234/reader.example"))["id"]
    before = client.store.paper(pid)
    queued, = client.post("/api/papers/batch", json={"action": "metadata", "paper_ids": [pid]}).json()["queued"]

    def offline(request):
        raise httpx.ConnectError("simulated offline", request=request)

    with monkeypatch.context() as patch:
        set_registry(patch, offline)
        failed = start(client.store, queued)
    assert failed["state"] == "failed" and "暂未连上书目服务" in failed["message"]
    assert client.store.paper(pid)["metadata_status"] == before["metadata_status"]
    assert client.post(f"/api/jobs/{queued['id']}/resume").json()["state"] == "queued"

    def registry(_request):
        client.patch(f"/api/papers/{pid}", json={"publisher": "Checked by reader"})
        record = crossref()
        del record["type"]
        return httpx.Response(200, json={"message": record})

    set_registry(monkeypatch, registry)
    done = start(client.store, queued)
    assert done["state"] == "done" and done["result"]["missing_fields"] == ["item_type"]
    assert done["result"]["filled_fields"] == ["publication_date"] and "待补条目类型" in done["message"]
    assert client.store.paper(pid)["publisher"] == "Checked by reader"
