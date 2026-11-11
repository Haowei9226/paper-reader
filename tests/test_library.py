from .conftest import import_and_parse, sample_pdf


def test_import_duplicate_collections_and_search(client):
    parent = client.post("/api/collections", json={"name": "博士研究"}).json()["id"]
    child = client.post("/api/collections", json={"name": "协作阅读", "parent_id": parent}).json()["id"]
    content = sample_pdf()
    paper = import_and_parse(client, content, child)
    pid = paper["id"]
    assert paper["pages"] == 2
    assert paper["title"] == "Shared Representations in Collaborative Reading"
    duplicate = client.post("/api/import?collection=" + parent, files={"files": ("renamed.pdf", content, "application/pdf")}).json()
    assert not duplicate["imported"] and duplicate["duplicates"][0]["id"] == pid
    edited = client.patch("/api/papers/" + pid, json={"tags": ["HCI", "分布式认知"], "year": "2026", "status": "reading"}).json()
    assert set(edited["collections"]) == {parent, child}
    assert client.get("/api/papers", params={"q": "coordination"}).status_code == 200
    assert client.get("/api/papers", params={"q": "coordinate"}).json()["total"] == 1
    assert client.get("/api/papers", params={"q": "中文"}).json()["total"] == 1
    assert client.get("/api/papers", params={"q": "认知"}).json()["total"] == 1
    assert client.get("/api/papers", params={"collection": parent}).json()["total"] == 1
    assert "@misc" in client.get("/api/export/metadata?format=bibtex").text
    assert "TY  - GEN" in client.get("/api/export/metadata?format=ris").text
    client.delete("/api/collections/" + parent)
    assert client.get("/api/papers").json()["total"] == 1
    client.delete("/api/papers/" + pid)
    assert client.get("/api/papers").json()["total"] == 0
    assert client.get("/api/papers?trash=true").json()["total"] == 1
    assert client.post("/api/papers/" + pid + "/restore").status_code == 200
    assert client.get("/api/papers").json()["total"] == 1


def test_invalid_upload_and_origin_protection(client):
    result = client.post("/api/import", files={"files": ("fake.pdf", b"not a PDF", "application/pdf")}).json()
    assert result["errors"] and not result["imported"]
    assert client.get("/api/papers").json()["total"] == 0
    rejected = client.post("/api/collections", json={"name": "bad"}, headers={"Origin": "https://other.example"})
    assert rejected.status_code == 403
    assert client.patch("/api/settings", json={"local_url": "https://other.example"}).status_code == 400
    assert client.get("/api/assets/..%2F..%2Freader%2Fapp.py").status_code == 404


def test_library_sort_orders_search_results_before_pagination(client):
    papers = [
        ("a", "Zulu Corpus", "Zoe", "2018", 30, 10),
        ("b", "alpha Corpus", "Amy", "2024", 20, 20),
        ("c", "Alpha Corpus", "amy", "2024", 20, 20),
        ("d", "Beta Corpus", "", "", 10, 40),
        ("e", "Another Project", "Adam", "2010", 50, 50),
    ]
    with client.store.connect() as db:
        for pid, title, author, year, created, updated in papers:
            db.execute("""INSERT INTO papers(id,sha256,filename,title,authors,year,created,updated)
                          VALUES(?,?,?,?,?,?,?,?)""", (pid, pid, pid + ".pdf", title, author, year, created, updated))
            client.store.reindex_paper(db, pid)

    def ordered(field, direction=None):
        params = {"q": "Corpus", "sort": field, "limit": 2}
        if direction:
            params["direction"] = direction
        ids = []
        for offset in (0, 2):
            response = client.get("/api/papers", params={**params, "offset": offset})
            assert response.status_code == 200
            page = response.json()
            assert page["total"] == 4
            ids.extend(item["id"] for item in page["items"])
        return ids

    assert ordered("title") == ["b", "c", "d", "a"]
    assert ordered("title", "desc") == ["a", "d", "b", "c"]
    assert ordered("authors", "asc") == ["b", "c", "a", "d"]
    assert ordered("authors", "desc") == ["a", "b", "c", "d"]
    assert ordered("year") == ["b", "c", "a", "d"]
    assert ordered("year", "asc") == ["a", "b", "c", "d"]
    assert ordered("created", "asc") == ["d", "b", "c", "a"]
    assert ordered("updated") == ["d", "b", "c", "a"]


def test_trash_requires_current_snapshot_and_keeps_active_papers(client):
    from .test_annotations import add_translation
    from .conftest import source_block

    first, second, kept = [import_and_parse(client, sample_pdf(pages=n))["id"] for n in (2, 3, 4)]
    first_block = source_block(client, first)
    add_translation(client, first, first_block)
    client.post(f"/api/papers/{first}/annotations", json={"note": "Only in deleted paper"})
    client.post(f"/api/papers/{kept}/annotations", json={"note": "Keep this note"})

    def snapshot(paper_id=None):
        return client.get("/api/trash", params={"paper_id": paper_id} if paper_id else {}).json()

    client.delete(f"/api/papers/{first}")
    old = snapshot()
    client.delete(f"/api/papers/{second}")
    assert client.post("/api/trash/purge", json=old).status_code == 409
    assert snapshot()["count"] == 2 and client.store.original(first).is_file()
    assert client.post("/api/trash/restore", json=snapshot()).json()["count"] == 2
    assert snapshot()["count"] == 0

    client.delete(f"/api/papers/{first}")
    client.delete(f"/api/papers/{second}")
    backup = client.store.create_job("backup")
    assert client.post("/api/trash/purge", json=snapshot()).status_code == 409
    with client.store.connect() as db:
        db.execute("UPDATE jobs SET state='cancelled' WHERE id=?", (backup["id"],))
    single = client.post("/api/trash/purge", params={"paper_id": first}, json=snapshot(first)).json()
    assert single == {"count": 1, "pending_cleanup": 0}
    assert not client.store.original(first).exists() and not (client.store.root / "assets" / first).exists()
    assert client.get(f"/api/papers/{first}").status_code == 404
    assert client.get(f"/api/blocks/{first_block['id']}/translations").json() == []
    assert snapshot()["count"] == 1
    assert client.post("/api/trash/purge", json=snapshot()).json() == {"count": 1, "pending_cleanup": 0}
    assert client.get("/api/papers").json()["total"] == 1
    assert client.get(f"/api/papers/{kept}/annotations").json()[0]["note"] == "Keep this note"
    with client.store.connect() as db:
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
        assert db.execute("SELECT count(*) FROM block_search WHERE paper_id=?", (first,)).fetchone()[0] == 0


def test_move_collection_and_merge_tags_keep_paper_membership_and_search(client):
    parent = client.post("/api/collections", json={"name": "Parent"}).json()["id"]
    child = client.post("/api/collections", json={"name": "Child", "parent_id": parent}).json()["id"]
    target = client.post("/api/collections", json={"name": "Target"}).json()["id"]
    pid = import_and_parse(client, collection=child)["id"]
    client.patch(f"/api/papers/{pid}", json={"tags": ["oldtopic", "newtopic"]})
    assert client.patch(f"/api/collections/{parent}", json={"parent_id": child}).status_code == 400
    assert client.patch(f"/api/collections/{child}", json={"parent_id": target}).status_code == 200
    assert client.get("/api/papers", params={"collection": parent}).json()["total"] == 0
    assert client.get("/api/papers", params={"collection": target}).json()["total"] == 1
    assert client.patch("/api/tags", params={"name": "oldtopic"}, json={"name": "newtopic"}).json() == {"count": 1}
    assert client.get(f"/api/papers/{pid}").json()["tags"] == ["newtopic"]
    assert client.get("/api/papers", params={"q": "oldtopic"}).json()["total"] == 0
    assert client.delete("/api/tags", params={"name": "newtopic"}).json() == {"count": 1}
    assert client.get(f"/api/papers/{pid}").json()["tags"] == []


def test_reading_locator_and_notes_survive_restart(client):
    from fastapi.testclient import TestClient
    from reader.app import create_app
    paper = import_and_parse(client)
    pid = paper["id"]
    path = "/api/papers/" + pid
    assert client.patch(path, json={"locator": {"page": 2}}).json()["locator"]["page"] == 2
    block = client.get(path + "/pages/2").json()["blocks"][1]
    locator = {"page": 2, "block_id": block["id"], "mode": "html", "side": "translation", "offset": .42, "scroll": .65, "page_fraction": .73, "saved_at": 2000}
    assert client.patch(path, json={"locator": locator}).json()["locator"] == locator
    assert client.patch(path, json={"locator": {"page": 1, "saved_at": 1999}}).json()["locator"] == locator
    client.post(f"/api/papers/{pid}/annotations", json={"note": "这与我的研究问题有关"})
    with TestClient(create_app(client.store.root, background=False)) as reopened:
        assert reopened.get(path).json()["locator"] == locator
        assert reopened.get("/api/papers/" + pid + "/annotations").json()[0]["note"] == "这与我的研究问题有关"
        assert reopened.get("/api/papers", params={"q": "研究问题"}).json()["total"] == 1
        assert reopened.get("/api/annotations").json()["total"] == 1


def test_continuous_html_uses_canonical_blocks_without_pdf_geometry(client):
    pid = import_and_parse(client)["id"]
    url = f"/api/papers/{pid}/pages/1"
    pdf = client.get(url).json()
    html = client.get(url, params={"geometry": "false"}).json()
    assert any(block["geometry"]["boxes"] for block in pdf["blocks"])
    assert html == {**pdf, "blocks": [{**block, "geometry": {"boxes": [], "lines": []}} for block in pdf["blocks"]]}


def test_html_reflows_legacy_columns_and_wrapped_lines_without_moving_anchors(client):
    from reader.store import dumps

    pid = import_and_parse(client)["id"]
    blocks = [
        ("left-start", "The left column begins with a long passage about how readers find their place.", [48, 120, 292, 162]),
        ("right", "The right column occupies one long text block. It continues the argument after the left column.", [310, 121, 551, 180]),
        ("left-end", "The second passage in the left column must appear before the right column begins.", [48, 181, 290, 215]),
        ("line-one", "The conclusion of one sentence sits at the end of a fully justified line.", [48, 330, 545, 343]),
        ("line-two", "Another sentence begins on the following line in the same paragraph.", [48, 346, 525, 359]),
        ("new-paragraph", "An indented line begins a separate paragraph.", [63, 362, 365, 375]),
        ("keywords", "Keywords: shared representations, collaborative reading, and", [48, 400, 545, 413]),
        ("keyword-tail", "sustained attention", [48, 416, 175, 429]),
    ]
    with client.store.connect() as db:
        db.execute("DELETE FROM blocks WHERE paper_id=? AND page=1", (pid,))
        db.executemany("INSERT INTO blocks VALUES(?,?,1,?,'paragraph',?,?,NULL,NULL,'')",
                       ((bid, pid, pos, text, dumps(box)) for pos, (bid, text, box) in enumerate(blocks)))
    data = client.get(f"/api/papers/{pid}/pages/1", params={"geometry": False}).json()["blocks"]
    assert [b["id"] for b in data] == ["left-start", "left-end", "right", "line-one", "line-two", "new-paragraph", "keywords", "keyword-tail"]
    assert [b["id"] for b in data if b.get("continues_previous")] == ["line-two"]
    assert [b["id"] for b in data if b["reading_role"] == "extra"] == ["keywords", "keyword-tail"]
    assert {b["id"]: b["text"] for b in data} == {bid: text for bid, text, _ in blocks}
    with client.store.connect() as db:
        assert db.execute("SELECT position FROM blocks WHERE id='right'").fetchone()[0] == 1


def test_find_original_and_current_translation_with_exact_offsets(client):
    from reader.store import now, uid
    from .conftest import source_block

    pid = import_and_parse(client)["id"]
    block = source_block(client, pid)
    path = f"/api/papers/{pid}/find"
    found = client.get(path, params={"q": "COORDINATE  their work.", "scope": "source"}).json()["items"]
    assert [(hit["page"], hit["block_id"], hit["match"]) for hit in found] == [(1, block["id"], "coordinate their work.")]

    tid = uid()
    with client.store.connect() as db:
        db.execute("INSERT INTO translations VALUES(?,?,?,?,?,?,?,?,?,?)", (tid, block["id"], pid, "🧭协调工作。再协调工作。", "[]", "local", "fixture", 0, 1, now()))
    first = client.get(path, params={"q": "协调工作", "scope": "translation", "limit": 1}).json()
    second = client.get(path, params={"q": "协调工作", "scope": "translation", "limit": 1, "offset": first["next_offset"]}).json()
    assert first["next_offset"] == 1 and second["next_offset"] is None
    assert [(hit["translation_id"], hit["start"], hit["end"]) for hit in first["items"] + second["items"]] == [(tid, 2, 6), (tid, 8, 12)]
    assert [hit["side"] for hit in client.get(path, params={"q": "协调工作"}).json()["items"]] == ["translation", "translation", "source"]
    assert client.patch(f"/api/translations/{tid}", json={"text": "团队协作。"}).status_code == 200
    assert client.get(path, params={"q": "协调工作", "scope": "translation"}).json()["items"] == []
    assert client.get(path, params={"q": "团队协作", "scope": "translation"}).json()["items"][0]["translation_id"] != tid


def test_pdf_worker_uses_javascript_mime(client):
    from reader import ROOT
    workers = list((ROOT / "web" / "dist" / "assets").glob("pdf.worker*.mjs"))
    if not workers:
        return
    response = client.get("/assets/" + workers[0].name)
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]


def test_metadata_edited_during_conversion_is_preserved(client):
    from reader.worker import run
    response=client.post('/api/import',files={'files':('metadata.pdf',sample_pdf(),'application/pdf')}).json()
    pid=response['imported'][0]['id']
    client.patch('/api/papers/'+pid,json={'title':'我确认的文献标题','authors':'我确认的作者','language':'zh'})
    with client.store.connect() as db:
        job_id=db.execute('SELECT id FROM jobs WHERE paper_id=?',(pid,)).fetchone()[0]
        db.execute("UPDATE jobs SET state='running' WHERE id=?",(job_id,))
    run(client.store,job_id)
    result=client.get('/api/papers/'+pid).json()
    assert result['title']=='我确认的文献标题'
    assert result['authors']=='我确认的作者'
    assert result['language']=='zh'


def test_visible_title_ignores_permission_notice_and_vertical_stamp(client):
    import pymupdf
    doc = pymupdf.open()
    page = doc.new_page()
    notice = "Permission to reproduce this validation sample is granted for testing."
    page.insert_text((65, 50), notice, fontsize=12)
    page.insert_text((140, 115), "A Study of Collaborative Reading", fontsize=18)
    page.insert_text((25, 380), "arXiv:0000.00000", fontsize=22, rotate=90)
    page.insert_textbox((65, 180, 530, 450), "Readers coordinate their work with shared representations. " * 15, fontsize=10)
    paper = import_and_parse(client, doc.tobytes())
    assert paper['title'] == "A Study of Collaborative Reading"
    blocks = client.get(f"/api/papers/{paper['id']}/pages/1").json()['blocks']
    assert next(b for b in blocks if b['text'] == notice)['kind'] == 'paragraph'
    assert next(b for b in blocks if b['text'].startswith('arXiv:'))['kind'] == 'paragraph'
    doc.close()
