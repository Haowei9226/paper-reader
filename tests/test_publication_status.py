import csv
import io
import json

import pymupdf
import pytest

from reader.metadata import crossref_record
from reader.store import Store
from reader.worker import run

from .conftest import import_and_parse
from .test_metadata import crossref, make_pdf


def run_job(client, job):
    with client.store.connect() as db:
        db.execute("UPDATE jobs SET state='running' WHERE id=?", (job["id"],))
    run(client.store, job["id"])
    return client.store.job(job["id"])


def template_pdf():
    with pymupdf.open(stream=make_pdf(), filetype="pdf") as doc:
        doc.set_xml_metadata('''<x:xmpmeta xmlns:x="adobe:ns:meta/"
            xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns:t="urn:reader-test">
          <rdf:RDF><rdf:Description>
            <t:publisher>Template Press</t:publisher>
            <t:publicationname>Template Conference</t:publicationname>
            <t:publicationdate>2027-02-01</t:publicationdate>
          </rdf:Description></rdf:RDF></x:xmpmeta>''')
        return doc.tobytes()


def test_publication_status_migrates_without_inferring_from_missing_doi(client):
    paper = import_and_parse(client)
    assert not paper["doi"] and paper["publication_status"] == ""
    with client.store.connect() as db:
        db.execute("ALTER TABLE papers DROP COLUMN publication_status")
    recovered = Store(client.store.root).paper(paper["id"])
    assert recovered["publication_status"] == "" and recovered["title"] == paper["title"]
    for status in ("published", "unpublished", "internal", ""):
        edited = client.patch(f"/api/papers/{paper['id']}", json={"publication_status": status})
        assert edited.status_code == 200 and edited.json()["publication_status"] == status
    assert client.patch(f"/api/papers/{paper['id']}", json={"publication_status": "draft"}).status_code == 400


@pytest.mark.parametrize("status,label", [("unpublished", "未发表手稿"), ("internal", "内部资料")])
def test_private_material_skips_auto_and_batch_lookup_and_local_template_fields(client, monkeypatch, status, label):
    def forbidden(*_args, **_kwargs):
        pytest.fail("An automatic lookup sent unpublished material to a registry")

    monkeypatch.setattr("reader.metadata.lookup_online", forbidden)
    client.store.set_settings({"metadata_lookup": True})
    imported = client.post("/api/import", files={"files": ("draft.pdf", template_pdf(), "application/pdf")}).json()["imported"][0]
    pid = imported["id"]
    client.patch(f"/api/papers/{pid}", json={"publication_status": status})
    with client.store.connect() as db:
        parse_id = db.execute("SELECT id FROM jobs WHERE paper_id=? AND kind='parse'", (pid,)).fetchone()[0]
    assert run_job(client, {"id": parse_id})["state"] == "done"
    paper = client.store.paper(pid)
    assert paper["publication_status"] == status and paper["parse_state"] == "ready"
    assert (paper["publisher"], paper["publication_date"], paper["source"]) == ("", "", "")
    with client.store.connect() as db:
        assert not db.execute("SELECT 1 FROM jobs WHERE paper_id=? AND kind='metadata'", (pid,)).fetchone()
    result = client.post("/api/papers/batch", json={"action": "metadata", "paper_ids": [pid]}).json()
    assert not result["queued"] and label in result["skipped"][0]["reason"]
    for payload in ({"requested": False}, {"requested": True, "fill_publication_fields": True}):
        stale = client.store.create_job("metadata", pid, payload)
        finished = run_job(client, stale)
        assert finished["state"] == "done" and label in finished["result"]["skipped_reason"]
        assert finished["message"] == finished["result"]["skipped_reason"]
    assert client.store.paper(pid)["publication_status"] == status


@pytest.mark.parametrize("status", ["unpublished", "internal"])
def test_explicit_single_lookup_preserves_manual_publication_status(client, monkeypatch, status):
    pid = import_and_parse(client, make_pdf())["id"]
    client.patch(f"/api/papers/{pid}", json={"publication_status": status})
    requests = []

    def registry(*args, **kwargs):
        requests.append((args, kwargs))
        return crossref_record(crossref()), "Crossref"

    monkeypatch.setattr("reader.metadata.lookup_online", registry)
    job = client.post(f"/api/papers/{pid}/metadata").json()
    finished = run_job(client, job)
    paper = client.store.paper(pid)
    assert requests and finished["state"] == "done"
    assert paper["publication_status"] == status
    assert paper["publisher"] == "Reading Press" and paper["doi"] == "10.1234/reader.example"
    with client.store.connect() as db:
        manual = json.loads(db.execute("SELECT manual_fields FROM papers WHERE id=?", (pid,)).fetchone()[0])
    assert "publication_status" in manual


def test_status_change_during_lookup_discards_remote_results_and_preserves_cleared_fields(client, monkeypatch):
    paper = import_and_parse(client, template_pdf())
    pid = paper["id"]
    assert paper["publisher"] == "Template Press"

    def delayed_registry(*_args, **_kwargs):
        client.patch(f"/api/papers/{pid}", json={
            "publication_status": "internal", "publisher": "", "publication_date": "", "source": "",
        })
        return crossref_record(crossref()), "Crossref"

    monkeypatch.setattr("reader.metadata.lookup_online", delayed_registry)
    job = client.post(f"/api/papers/{pid}/metadata").json()
    finished = run_job(client, job)
    found = client.store.paper(pid)
    assert finished["state"] == "done" and "发表状态已变化" in finished["message"]
    assert found["publication_status"] == "internal" and found["metadata_status"] == "local"
    assert (found["publisher"], found["publication_date"], found["source"], found["doi"]) == ("", "", "", "")
    assert found["year"] == paper["year"]


def test_explicit_lookup_promotes_an_already_queued_automatic_lookup(client, monkeypatch):
    client.store.set_settings({"metadata_lookup": True})
    pid = import_and_parse(client, make_pdf())["id"]
    client.patch(f"/api/papers/{pid}", json={"publication_status": "unpublished"})
    with client.store.connect() as db:
        old_job = db.execute("SELECT id FROM jobs WHERE paper_id=? AND kind='metadata'", (pid,)).fetchone()[0]
    monkeypatch.setattr("reader.metadata.lookup_online", lambda *_args, **_kwargs: (crossref_record(crossref()), "Crossref"))
    requested = client.post(f"/api/papers/{pid}/metadata").json()
    assert requested["id"] == old_job and requested["payload"]["requested"] is True
    assert run_job(client, requested)["state"] == "done"
    assert client.store.paper(pid)["publisher"] == "Reading Press"


@pytest.mark.parametrize("status,bibtype,ristype,note", [
    ("unpublished", "unpublished", "MANSCPT", "Unpublished manuscript"),
    ("internal", "misc", "GEN", "Internal material (not for publication)"),
    ("published", "misc", "GEN", "Published work"),
])
def test_publication_status_survives_bibliographic_exports(client, status, bibtype, ristype, note):
    paper = import_and_parse(client)
    client.patch(f"/api/papers/{paper['id']}", json={"publication_status": status})
    rows = list(csv.DictReader(io.StringIO(client.get("/api/export/metadata").text.lstrip("\ufeff"))))
    assert list(rows[0]) == ["title", "authors", "year", "doi", "source", "status", "tags", "filename", "publication_status"]
    assert rows[0]["publication_status"] == status
    bibtex = client.get("/api/export/metadata?format=bibtex").text
    ris = client.get("/api/export/metadata?format=ris").text
    assert f"@{bibtype}{{" in bibtex and f"note = {{{note}}}" in bibtex
    assert f"TY  - {ristype}" in ris and f"N1  - {note}" in ris
