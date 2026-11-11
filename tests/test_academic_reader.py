import json

import httpx
import pymupdf as fitz

from reader import ai
from reader.pdf import length, slice_text
from reader.reading import classify_reading_blocks, reading_runs
from reader.store import dumps, now, uid
from reader.worker import run
from .conftest import import_and_parse, sample_pdf, source_block
from .test_annotations import add_translation


def test_local_model_recovers_from_repeat_guard_with_one_bounded_retry(client, monkeypatch):
    bodies = []
    real_client = httpx.Client

    def handle(request):
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return httpx.Response(500, json={"error": "prediction aborted, token repeat limit reached"})
        return httpx.Response(200, json={"message": {"content": '[{"id":0,"translation":"继续"}]'}, "done_reason": "stop"})

    monkeypatch.setattr(ai.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handle), **kw))
    assert ai.Client(client.store, {"provider": "local"}).ask('返回 {"segments":[]}。', "测试") == {"segments": [{"id": 0, "translation": "继续"}]}
    assert len(bodies) == 2 and bodies[0]["format"] == "json" and "format" not in bodies[1]


def finish(store, kind, pid, payload, expected_state="done"):
    job = store.create_job(kind, pid, payload)
    with store.connect() as db:
        db.execute("UPDATE jobs SET state='running' WHERE id=?", (job["id"],))
    run(store, job["id"])
    result = store.job(job["id"])
    assert result["state"] == expected_state, result["message"]
    return result["result"]


def test_license_and_figure_do_not_split_the_same_sentence():
    paper = {"title": "Example Reading", "authors": "Example Author", "outline": []}
    blocks = [{"kind": kind, "text": text, "bbox": [45, y, 289, y + 20], "section": ""}
              for kind, text, y in [("heading", "Example Reading", 75), ("heading", "Abstract", 145),
                ("paragraph", "We argue that future evaluation frameworks should", 160),
                ("image", "", 510), ("paragraph", "Figure 1: Study overview.", 540),
                ("paragraph", "This work is licensed under a Creative Commons Attribution License.", 655),
                ("paragraph", "capture relational agency in nego\u00ad tiations.", 170),
                ("paragraph", "Figure 1 shows the study pipeline in detail.", 205)]]
    classify_reading_blocks(blocks, paper, 1, 792)
    assert reading_runs(blocks)[1] == [blocks[2], blocks[6]]
    assert [blocks[i]["reading_role"] for i in (3, 4, 5, 7)] == ["image", "caption", "extra", "text"]


def test_academic_batch_keeps_cross_page_ranges_and_cloud_scope(client, monkeypatch):
    pid = import_and_parse(client)["id"]
    left, right, license_block, later = (uid() for _ in range(4))
    original = "The study suggests that these nego\u00ad tiations [19, 66] should"
    continuation = "capture relational agency in everyday practice [19, 83]."
    following = "At the same time, the study found new opportunities."
    with client.store.connect() as db:
        db.execute("DELETE FROM blocks WHERE paper_id=?", (pid,))
        for bid, page, position, text in (
            (left, 1, 0, original),
            (license_block, 1, 1, "This work is licensed under a Creative Commons Attribution License."),
            (right, 2, 0, continuation + " " + following),
            (later, 2, 1, "The next paragraph stays outside this translation scope."),
        ):
            db.execute("INSERT INTO blocks VALUES(?,?,?,?,?,?,?,?,?,?)", (bid, pid, page, position, "paragraph", text, "[40,200,500,245]", None, None, ""))
            client.store.reindex_block(db, bid)
    calls = []
    joined_target = "研究提示，这些协商（negotiations）[19, 66] 应体现日常实践中的关系能动性 [19, 83]。"
    next_target = "与此同时，研究发现了新的机会。"
    omit = 0

    class Model:
        provider = "deepseek"
        model = "offline-fixture"
        settings = {"context_tokens": 4096}

        def __init__(self, *args):
            pass

        def ask(self, instructions, text, **_options):
            nonlocal omit
            payload = json.loads(text)
            calls.append((instructions, payload))
            slots = []
            for p in payload["segments"]:
                if p.get("page_end"):
                    assert p["page"] == 1 and p["page_end"] == 2
                    assert p["text"] == original.replace("\u00ad ", "") + " " + continuation
                    assert "continues_previous" not in p and "continues_next" not in p
                    translation = joined_target.replace(" [19, 83]", "") if omit else joined_target
                    if omit > 0:
                        omit -= 1
                    alignments = [{"source": "negotiations", "translation": "协商"},
                                  {"source": "relational agency", "translation": "关系能动性（relational agency）"}]
                elif p["page"] == 1:
                    translation, alignments = "研究提示，这些协商 [19, 66] 应", []
                else:
                    assert p["text"] == following
                    translation, alignments = next_target, []
                slots.append(dict(id=p["id"], translation=translation, alignments=alignments))
            return {"segments": slots, "terms": [{"source": "negotiations", "target": "协商"}]}

    monkeypatch.setattr(ai, "Client", Model)
    scope = {"page_start": 1, "page_end": 1}
    history = lambda bid: client.get(f"/api/blocks/{bid}/translations").json()
    finish(client.store, "translate", pid, scope)
    assert len(calls) == 1
    assert "context" not in calls[0][1] and not history(right)
    assert {p["page"] for p in calls[0][1]["segments"]} == {1}

    omit = 1
    finish(client.store, "translate", pid, {**scope, "neighbor_context": True})
    assert len(calls) == 3
    instructions, payload = calls[1]
    assert "中文（英文原词）" in instructions
    assert len(payload["segments"]) == 2 and len(calls[-1][1]["segments"]) == 1
    assert not history(later) and not history(license_block)
    first, second = history(left)[0], history(right)[0]
    assert first["text"] == joined_target.replace("关系能动性", "关系能动性（relational agency）")
    assert second["text"] == next_target
    phrase = next(span for span in first["segments"] if not span.get("source_block_id") and span["source_start"] > 0)
    assert slice_text(original, phrase["source_start"], phrase["source_end"]) == "nego\u00ad tiations"
    hit = client.get(f"/api/papers/{pid}/find", params={"q": "negotiations", "scope": "source"}).json()["items"][0]
    assert hit["match"] == "nego\u00ad tiations"

    counterpart = f"/api/papers/{pid}/counterpart"
    offset = continuation.index("relational agency")
    source_selection = dict(block_id=right, start=offset, end=offset + len("relational agency"))
    forward = client.post(counterpart, json={"side": "source", "targets": [source_selection]}).json()["ranges"]
    assert [(item["block_id"], item["quote"]) for item in forward] == [(left, "关系能动性（relational agency）")]
    annotation = client.post(f"/api/papers/{pid}/annotations", json={"targets": [source_selection]}).json()
    assert annotation["translated_targets"][0]["translation_id"] == first["id"]
    target = dict(block_id=left, translation_id=first["id"], start=0, end=length(first["text"]))
    reverse = client.post(counterpart, json={"side": "translation", "targets": [target]}).json()["ranges"]
    assert [(item["block_id"], item["quote"]) for item in reverse] == [(left, original), (right, continuation)]
    from_translation = client.post(f"/api/papers/{pid}/annotations", json={
        "selected_language": "translation", "translated_targets": [target],
    }).json()
    assert [item["block_id"] for item in from_translation["targets"]] == [left, right]

    force = {**scope, "force": True, "neighbor_context": True}
    finish(client.store, "translate", pid, force)
    after = [history(left)[0]["id"], history(right)[0]["id"]]
    assert after != [first["id"], second["id"]]
    assert not history(later)
    omit = -1
    failed = finish(client.store, "translate", pid, force, expected_state="failed")
    assert {item["block_id"] for item in failed["failed_blocks"]} == {left, right}
    finish(client.store, "translate", pid, {**scope, "force": True}, expected_state="failed")
    assert [history(left)[0]["id"], history(right)[0]["id"]] == after


def test_table_batches_cells_without_sending_or_changing_values(client, monkeypatch):
    with fitz.open() as doc:
        page = doc.new_page(width=610, height=792)
        page.insert_text((50, 50), "Tabular Reader Fixture", fontsize=16)
        for y, cells in zip((108, 128, 148, 168, 188), [
            ("Code", "Program level", "Age"), ("FP1", "Graduate", "27"), ("FP2", "Junior", "20"),
            ("FP3", "Graduate", "29"), ("FP4", "Junior", "22"),
        ]):
            for x, value in zip((74, 156, 275), cells):
                page.insert_text((x, y), value, fontsize=10)
        page.insert_text((65, 211), "Table 1: Program level and participant ages", fontsize=12)
        doc.set_metadata({"title": "Tabular Reader Fixture"})
        data = doc.tobytes()
    pid = import_and_parse(client, data)["id"]
    requests = []

    class Model:
        provider = "local"
        model = "offline-fixture"
        settings = {"context_tokens": 4096}

        def __init__(self, *args):
            pass

        def ask(self, _instructions, text, **_options):
            payload = json.loads(text)
            if "table" in payload:
                requests.append(payload)
            return {"segments": [{"id": p["id"], "translation": {"Graduate": "研究生（Graduate）", "Junior": "大三"}.get(p["text"], "译文")}
                                 for p in payload["segments"] if not ("table" in payload and len(requests) == 1 and p["text"] == "Junior")]}

    monkeypatch.setattr(ai, "Client", Model)
    finish(client.store, "translate", pid, {})
    assert len(requests) == 2 and [p["text"] for p in requests[1]["segments"]] == ["Junior"]
    sent = [p["text"] for p in requests[0]["segments"]]
    assert sent.count("Graduate") == 1 and "FP1" not in sent and "27" not in sent
    table = [b for b in client.get(f"/api/papers/{pid}/pages/1").json()["blocks"] if b["reading_role"] == "table"]
    text = " ".join(b["translation"]["text"] for b in table)
    assert "研究生" in text and "FP1" in text and "27" in text
    finish(client.store, "translate", pid, {"force": True})
    assert len(requests) == 3 and "Graduate" not in [p["text"] for p in requests[2]["segments"]]
    table = [b for b in client.get(f"/api/papers/{pid}/pages/1").json()["blocks"] if b["reading_role"] == "table"]
    assert "研究生" in " ".join(b["translation"]["text"] for b in table)


def test_temporary_counterpart_uses_active_version_and_manual_mapping(client):
    pid = import_and_parse(client)["id"]
    block = source_block(client, pid)
    tid, translated = add_translation(client, pid, block)
    source = "coordinate their work"
    target = "协调工作"
    start = block["text"].index(source)
    offset = translated.index(target)
    a = {"block_id": block["id"], "start": start, "end": start + len(source)}
    b = {"block_id": block["id"], "translation_id": tid, "start": offset, "end": offset + len(target)}
    url = f"/api/papers/{pid}/counterpart"
    guess = client.post(url, json={"side": "source", "targets": [a]}).json()
    assert guess["ranges"][0]["quote"] == translated
    assert client.get(f"/api/papers/{pid}/annotations").json() == []
    note = client.post(f"/api/papers/{pid}/annotations", json={"targets": [a]}).json()
    client.patch(f"/api/annotations/{note['id']}", json={"targets": [a], "translated_targets": [b]})
    assert client.post(url, json={"side": "source", "targets": [a]}).json()["ranges"][0]["quote"] == target
    reverse = client.post(url, json={"side": "translation", "targets": [b], "pdf": True}).json()["ranges"][0]
    assert reverse["quality"] == "manual" and reverse["quote"] == source and reverse["rects"]
    client.patch(f"/api/translations/{tid}", json={"text": "这是修订版。"})
    assert client.post(url, json={"side": "translation", "targets": [b]}).json()["ranges"] == []


def test_paper_assistant_reuses_only_its_conversation_and_validated_citations(client, monkeypatch):
    first = import_and_parse(client)["id"]
    second = import_and_parse(client, sample_pdf(pages=3))["id"]
    block = source_block(client, first)
    quote = "coordinate their work"
    with client.store.connect() as db:
        for pid, question, answer, cited in ((first, "What helps coordination?", "FIRST_PAPER_ONLY", [dict(block_id=block["id"], quote=quote, page=1)]),
                                             (second, "What was the sample?", "OTHER_PAPER_ONLY", [])):
            db.execute("INSERT INTO ai_results VALUES(?,?,?,?,?,?,?,?,?,?)", (uid(), pid, "question", question, answer, dumps(cited), "local", "fixture", "本篇论文对话", now()))
    calls = []

    class Model:
        provider = "local"
        model = "offline-fixture"
        settings = {"context_tokens": 4096}

        def __init__(self, *args):
            pass

        def ask(self, instructions, text):
            calls.append((instructions, text))
            return {"status": "answered", "answer": "原文说明了协调工作。", "citations": [dict(block_id=block["id"], quote=quote), dict(block_id=block["id"], quote="未在原文出现的词")]}

    monkeypatch.setattr(ai, "Client", Model)
    finish(client.store, "reading", first, {"task": "question", "question": "刚才那一点是什么意思？", "conversation_memory": True})
    assert len(calls) == 1 and "只帮助读懂当前论文" in calls[0][0]
    assert "FIRST_PAPER_ONLY" in calls[0][1] and "OTHER_PAPER_ONLY" not in calls[0][1]
    assert block["id"] in {b["block_id"] for b in json.loads(calls[0][1])["evidence"]}
    result = client.get(f"/api/papers/{first}/ai-results").json()[0]
    assert result["scope"] == "本篇论文对话" and [c["quote"] for c in result["citations"]] == [quote]
    assert len(client.get(f"/api/papers/{second}/ai-results").json()) == 1
