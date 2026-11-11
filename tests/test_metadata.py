from urllib.parse import unquote

import httpx
import pymupdf

from reader.worker import run

from .conftest import import_and_parse


TITLE = "A Study of Shared Representations in Collaborative Reading"


def make_pdf(title=TITLE, *lines, authors="", stamp=""):
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((48, 70), "Document used for bibliography verification", fontsize=15)
        for index, line in enumerate(lines):
            page.insert_text((48, 112 + index * 18), line, fontsize=11)
        if stamp:
            page.insert_text((20, 445), stamp, fontsize=14, rotate=90)
        doc.set_metadata({"title": title, "author": authors})
        return doc.tobytes()


def lookup(client, paper_id):
    with client.store.connect() as db:
        job = db.execute("SELECT id FROM jobs WHERE paper_id=? AND kind='metadata' AND state='queued'", (paper_id,)).fetchone()[0]
        db.execute("UPDATE jobs SET state='running' WHERE id=?", (job,))
    run(client.store, job)
    assert client.store.job(job)["state"] == "done"
    return client.get("/api/papers/" + paper_id).json()


def set_registry(monkeypatch, handler):
    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs))


def crossref(title=TITLE, doi="10.1234/reader.example"):
    return {"DOI": doi, "title": [title], "author": [{"given": "Alice", "family": "Ng"}, {"given": "Leah", "family": "Lee"}],
            "published": {"date-parts": [[2024, 5, 2]]}, "container-title": ["Journal of Reading"],
            "type": "journal-article", "publisher": "Reading Press",
            "abstract": "<jats:p>Public bibliographic abstract for the test document.</jats:p>"}


def test_automatic_doi_lookup_names_download_and_preserves_original(client, monkeypatch):
    assert client.patch("/api/settings", json={"metadata_lookup": True}).status_code == 200
    pdf = make_pdf(TITLE, "DOI: 10.1234/reader.example")
    paper = import_and_parse(client, pdf)
    pid = paper["id"]
    assert paper["parse_state"] == "ready" and paper["metadata_status"] == "queued"
    assert paper["filename"] == "test-paper.pdf"
    seen = []

    def handle(request):
        seen.append(str(request.url))
        assert request.method == "GET" and request.content == b"" and "authorization" not in request.headers
        return httpx.Response(200, json={"message": crossref()})

    set_registry(monkeypatch, handle)
    found = lookup(client, pid)
    assert seen == ["https://api.crossref.org/works/10.1234/reader.example"]
    assert (found["metadata_status"], found["metadata_source"]) == ("matched", "Crossref")
    assert (found["publication_date"], found["item_type"], found["publisher"]) == ("2024-05-02", "journal-article", "Reading Press")
    assert (found["title"], found["authors"], found["year"], found["source"]) == (TITLE, "Alice Ng; Leah Lee", "2024", "Journal of Reading")
    assert found["doi"] == "10.1234/reader.example" and "<" not in found["abstract"]
    assert found["filename"] == f"Alice Ng - 2024 - {TITLE}.pdf"
    assert found["original_filename"] == "test-paper.pdf"
    original = client.get(f"/api/papers/{pid}/file")
    assert original.content == pdf
    assert found["filename"] in unquote(original.headers["content-disposition"])
    assert "批注" in unquote(client.get(f"/api/papers/{pid}/export/pdf").headers["content-disposition"])
    assert client.get("/api/papers", params={"q": "test-paper"}).json()["total"] == 1


def test_offline_local_chinese_metadata_and_safe_windows_name(client):
    pdf = make_pdf("协作阅读：表征与实践", "Published: 2024", authors="张三；李四")
    paper = import_and_parse(client, pdf)
    assert paper["metadata_source"] == "PDF" and paper["metadata_status"] == "local"
    assert paper["year"] == "2024" and paper["authors"] == "张三；李四"
    assert paper["filename"] == "张三 - 2024 - 协作阅读：表征与实践.pdf"
    path = f"/api/papers/{paper['id']}"
    edited = client.patch(path, json={"filename": 'A: notes / "checked".pdf'}).json()
    assert edited["filename"] == "A notes checked.pdf"
    renamed = client.post(path + "/rename").json()
    assert renamed["filename"] == paper["filename"] and renamed["original_filename"] == "test-paper.pdf"
    assert client.get(path + "/file").content == pdf


def byline_pdf():
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((56, 115), "Reading Together with Shared Representations", fontsize=18)
        page.insert_text((56, 145), "Alice Ng1, Mei (Mia) Chen2, and Bob lee3", fontsize=11)
        page.insert_text((56, 163), "University of Reading, Department of Design", fontsize=10)
        page.insert_text((56, 195), "Abstract", fontsize=13)
        page.insert_text((56, 216), "The paper examines collaborative reading and coordination.", fontsize=11)
        doc.set_metadata({"author": "laptop hp"})
        return doc.tobytes()


def test_pdf_cover_byline_takes_priority_over_device_name(client):
    paper = import_and_parse(client, byline_pdf())
    path = f"/api/papers/{paper['id']}"
    assert paper["title"] == "Reading Together with Shared Representations"
    assert paper["authors"] == "Alice Ng; Mei (Mia) Chen; Bob lee"
    page = client.get(path + "/pages/1").json()
    assert next(b for b in page["blocks"] if "Alice Ng" in b["text"])["reading_role"] == "extra"
    assert next(b for b in page["blocks"] if "coordination" in b["text"])["reading_role"] == "text"
    with client.store.connect() as db:
        db.execute("UPDATE papers SET authors='laptop hp' WHERE id=?", (paper["id"],))
    assert client.post(path + "/metadata/authors").json()["authors"] == paper["authors"]
    client.patch(path, json={"authors": "Checked manually"})
    assert client.post(path + "/metadata/authors").status_code == 400
    assert client.get(path).json()["authors"] == "Checked manually"


def test_manually_entered_doi_cannot_replace_a_confirmed_pdf_byline(client, monkeypatch):
    paper = import_and_parse(client, byline_pdf())
    path = f"/api/papers/{paper['id']}"
    client.patch(path, json={"doi": "10.1234/another-paper"})
    client.post(path + "/metadata")
    set_registry(monkeypatch, lambda request: httpx.Response(
        200, json={"message": crossref("Unrelated Article on Mobile Photography", "10.1234/another-paper")}))
    found = lookup(client, paper["id"])
    assert found["metadata_status"] == "review"
    assert (found["title"], found["authors"]) == (paper["title"], paper["authors"])
    assert found["doi"] == "10.1234/another-paper"


def test_related_doi_and_ambiguous_title_leave_original_filename(client, monkeypatch):
    client.store.set_settings({"metadata_lookup": True})
    paper = import_and_parse(client, make_pdf(TITLE, "Related study: https://doi.org/10.1234/another"))

    def handle(request):
        if request.url.host == "api.datacite.org":
            return httpx.Response(404)
        if request.url.path.endswith("/another"):
            return httpx.Response(200, json={"message": crossref("An Unrelated Paper", "10.1234/another")})
        assert request.url.params["query.title"] == TITLE
        return httpx.Response(200, json={"message": {"items": [crossref(), crossref(doi="10.1234/other-edition")]}})

    set_registry(monkeypatch, handle)
    found = lookup(client, paper["id"])
    assert found["metadata_status"] == "review" and found["doi"] == ""
    assert found["filename"] == "test-paper.pdf" and found["title"] == TITLE


def test_manual_doi_lookup_preserves_edits_and_checks_inflight_changes(client, monkeypatch):
    paper = import_and_parse(client, make_pdf(TITLE))
    pid = paper["id"]
    path = f"/api/papers/{pid}"
    client.patch(path, json={"title": "我核对的标题", "authors": "我核对的作者", "filename": "保留的文件名.pdf", "doi": "10.1234/reader.example"})
    first = client.post(path + "/metadata").json()
    assert client.post(path + "/metadata").json()["id"] == first["id"]

    def handle(request):
        assert request.url.path == "/works/10.1234/reader.example"
        return httpx.Response(200, json={"message": crossref()})

    set_registry(monkeypatch, handle)
    found = lookup(client, pid)
    assert found["metadata_status"] == "matched" and found["year"] == "2024"
    assert (found["title"], found["authors"], found["filename"]) == ("我核对的标题", "我核对的作者", "保留的文件名.pdf")

    client.post(path + "/metadata")
    # Change the identifier while the response for the old identifier is in flight.
    def stale_response(*args, **kwargs):
        client.patch(path, json={"doi": "10.1234/corrected"})
        record = crossref()
        record["published"] = {"date-parts": [[2030]]}
        return record, "Crossref"

    monkeypatch.setattr("reader.metadata.lookup_online", stale_response)
    found = lookup(client, pid)
    assert found["metadata_status"] == "review" and found["doi"] == "10.1234/corrected" and found["year"] == "2024"


def test_arxiv_uses_datacite_and_rename_can_be_disabled(client, monkeypatch):
    client.store.set_settings({"metadata_lookup": True, "auto_rename": False})
    paper = import_and_parse(client, make_pdf(TITLE, "Body text near the identifier", stamp="arXiv:2411.12345v2 [cs.HC]"))

    def handle(request):
        assert request.url.host == "api.datacite.org" and request.url.path.endswith("/10.48550/arXiv.2411.12345")
        return httpx.Response(200, json={"data": {"attributes": {"doi": "10.48550/arxiv.2411.12345", "titles": [{"title": TITLE}],
            "creators": [{"name": "Ng, Alice"}], "publicationYear": 2024, "publisher": {"name": "arXiv"}}}})

    set_registry(monkeypatch, handle)
    found = lookup(client, paper["id"])
    assert found["metadata_source"] == "DataCite" and found["source"] == "arXiv"
    assert found["filename"] == "test-paper.pdf" and found["year"] == "2024"


def test_lookup_unavailable_leaves_pdf_ready(client, monkeypatch):
    client.store.set_settings({"metadata_lookup": True})
    paper = import_and_parse(client, make_pdf(TITLE, "DOI: 10.1234/reader.example"))
    set_registry(monkeypatch, lambda request: httpx.Response(503))
    found = lookup(client, paper["id"])
    assert found["metadata_status"] == "offline" and found["parse_state"] == "ready"
    assert found["filename"] == "test-paper.pdf"


def test_unique_title_fallback_fills_missing_bibliography(client, monkeypatch):
    client.store.set_settings({"metadata_lookup": True})
    paper = import_and_parse(client, make_pdf(TITLE))

    def handle(request):
        assert request.url.path == "/works" and request.url.params["query.title"] == TITLE
        return httpx.Response(200, json={"message": {"items": [crossref("Similar research in reading", "10.1234/other"), crossref()]}})

    set_registry(monkeypatch, handle)
    found = lookup(client, paper["id"])
    assert found["metadata_status"] == "matched" and found["doi"] == "10.1234/reader.example"
    assert found["filename"].startswith("Alice Ng - 2024 - ")
