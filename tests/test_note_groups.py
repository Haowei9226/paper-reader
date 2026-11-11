from urllib.parse import unquote

from .conftest import import_and_parse, source_block
from .test_annotations import add_translation


def test_note_groups_page_by_paper_and_keep_existing_notes(client):
    paper_a, paper_b, empty = (char * 32 for char in "abc")
    with client.store.connect() as db:
        db.executemany(
            "INSERT INTO papers(id,sha256,filename,title,authors,year,created,updated) VALUES(?,?,?,?,?,?,1,1)",
            [(paper_a, "a", "A.pdf", "Same title", "Author A", "2024"),
             (paper_b, "b", "B.pdf", "Same title", "Needle Group", "2025"),
             (empty, "c", "C.pdf", "No notes", "", "")],
        )
        db.executemany(
            "INSERT INTO annotations VALUES(?,?,'[]','[]','',?,'yellow','note',?,?)",
            [(f"a-{index}", paper_a, "needle 50%_!" if index == 0 else "Ordinary note", index + 10, index + 10)
             for index in range(101)] + [("b-0", paper_b, "First note", 1, 1), ("b-1", paper_b, "Second note", 2, 2)],
        )

    groups = "/api/annotation-papers"
    page_one = client.get(groups, params={"limit": 1}).json()
    assert page_one == {"items": [{"paper_id": paper_a, "title": "Same title", "authors": "Author A", "year": "2024",
                                  "total": 101, "matched": 101, "updated": 110}], "total": 2}
    page_two = client.get(groups, params={"limit": 1, "offset": 1}).json()
    assert [item["paper_id"] for item in page_two["items"]] == [paper_b]
    assert client.get(groups, params={"offset": 2}).json() == {"items": [], "total": 2}
    last_note = client.get(f"{groups}/{paper_a}", params={"offset": 100}).json()
    assert last_note["total"] == 101 and [item["id"] for item in last_note["items"]] == ["a-0"]

    results = client.get(groups, params={"q": "needle"}).json()["items"]
    assert [(item["total"], item["matched"]) for item in results] == [(101, 1), (2, 2)]
    assert client.get(f"{groups}/{paper_b}", params={"q": "Needle"}).json()["total"] == 2
    assert client.get(groups, params={"q": "50%_!"}).json()["items"][0]["matched"] == 1
    assert client.get(groups, params={"q": "%"}).json()["total"] == 1
    assert client.get(groups, params={"q": "Same title"}).json()["items"][0]["matched"] == 101

    assert client.patch(f"/api/papers/{paper_a}", json={"title": "新标题 / 方法"}).status_code == 200
    renamed = client.get(groups, params={"q": "新标题"}).json()["items"]
    assert [(item["paper_id"], item["matched"], item["updated"]) for item in renamed] == [(paper_a, 101, 110)]
    download = client.get(f"/api/papers/{paper_a}/export/notes")
    assert unquote(download.headers["Content-Disposition"].split("filename*=UTF-8''")[1]) == "新标题 方法 - 阅读笔记.md"
    assert "# 新标题 / 方法" in download.text and "needle 50%_!" in download.text

    assert client.delete(f"/api/papers/{paper_a}").status_code == 200
    assert [item["paper_id"] for item in client.get(groups).json()["items"]] == [paper_b]
    assert client.get(f"{groups}/{paper_a}").status_code == 404
    assert client.get("/api/annotations/a-0").status_code == 404
    assert client.post(f"/api/papers/{paper_a}/restore").status_code == 200
    assert client.get(f"{groups}/{paper_a}").json()["total"] == 101
    assert client.get("/api/annotations/a-0").json()["note"] == "needle 50%_!"


def test_note_groups_search_excerpts_and_sort_by_source(client):
    paper = import_and_parse(client)
    pid = paper["id"]
    block = source_block(client, pid)
    with client.store.connect() as db:
        late_block = db.execute("SELECT id FROM blocks WHERE paper_id=? AND page=2 AND length(text)>=5 ORDER BY position LIMIT 1", (pid,)).fetchone()[0]

    def add_note(body):
        response = client.post(f"/api/papers/{pid}/annotations", json=body)
        assert response.status_code == 200, response.text
        return response.json()["id"]

    independent = add_note({"note": "通读之后的想法"})
    later_in_block = add_note({"targets": [{"block_id": block["id"], "start": 7, "end": 11}], "note": "Some context"})
    translation_id, translated = add_translation(client, pid, block)
    translated_start = translated.index("协调工作")
    translated_note = add_note({"selected_language": "translation", "translated_targets": [
        {"block_id": block["id"], "translation_id": translation_id, "start": translated_start, "end": translated_start + 4},
    ]})
    later_page = add_note({"targets": [{"block_id": late_block, "start": 0, "end": 5}]})
    assert client.patch(f"/api/annotations/{later_in_block}", json={"note": "Updated context"}).status_code == 200
    endpoint = f"/api/annotation-papers/{pid}"
    by_source = client.get(endpoint, params={"sort": "source"}).json()
    assert [item["id"] for item in by_source["items"]] == [translated_note, later_in_block, later_page, independent]
    assert [item["id"] for item in client.get(endpoint, params={"limit": 1}).json()["items"]] == [later_in_block]
    for query in ("协调工作", "coordinate their work"):
        match = client.get(endpoint, params={"q": query}).json()
        assert match["total"] == 1 and match["items"][0]["id"] == translated_note
        assert client.get("/api/annotation-papers", params={"q": query}).json()["items"][0]["matched"] == 1
    assert client.get(f"/api/annotations/{translated_note}").json()["targets"][0]["rects"]
