import json

import httpx
import pytest

from reader import ai
from reader.pdf import length, slice_text
from reader.store import Store, dumps, now, uid
from reader.terminology import paper_terms
from reader.worker import run
from .conftest import import_and_parse


def create_paper(client, passages):
    pid = import_and_parse(client)["id"]
    ids = [uid() for _ in passages]
    with client.store.connect() as db:
        db.execute("DELETE FROM blocks WHERE paper_id=?", (pid,))
        for position, ((page, text), bid) in enumerate(zip(passages, ids)):
            db.execute("INSERT INTO blocks VALUES(?,?,?,?,?,?,?,?,?,?)", (bid, pid, page, position, "paragraph", text, "[40,190,480,220]", None, None, ""))
    return pid, ids


def start(store, job):
    with store.connect() as db:
        db.execute("UPDATE jobs SET state='running' WHERE id=?", (job["id"],))
    run(store, job["id"])
    return store.job(job["id"])


def offline_openai(monkeypatch, handler):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-key-for-translation-tests")
    real = httpx.Client
    monkeypatch.setattr(ai.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


def reply(value):
    return httpx.Response(200, json={"status": "completed", "output": [
        {"type": "message", "content": [{"type": "output_text", "text": json.dumps(value, ensure_ascii=False)}]},
    ]})


def request_parts(request):
    data = json.loads(request.content)
    assert data["store"] is False and data["text"]["format"]["strict"] is True
    parts = json.loads(data["input"][1]["content"])["segments"]
    return parts, data["text"]["format"]["schema"]


def test_openai_repairs_only_an_omitted_segment_and_preserves_alignment(client, monkeypatch):
    pid, ids = create_paper(client, [(1, "This is the first passage."), (1, "This is the missing passage."), (1, "This is the final passage.")])
    requests = []

    def handle(request):
        parts, schema = request_parts(request)
        requests.append([p["text"] for p in parts])
        if len(requests) == 1:
            assert schema["properties"]["translations"]["required"] == ["s0", "s1", "s2"]
            return reply({"translations": {"s0": {"translation": "第一段。", "alignments": []}, "s2": {"translation": "最后一段。", "alignments": []}}, "terms": []})
        assert schema["required"] == ["translation", "alignments"]
        return reply({"translation": "补译的段落。", "alignments": []})

    offline_openai(monkeypatch, handle)
    result = start(client.store, client.store.create_job("translate", pid, {"provider": "openai"}))
    assert result["state"] == "done" and result["progress"] == result["total"] == 3
    assert requests[1:] == [["This is the missing passage."]]
    for bid, expected in zip(ids, ("第一段。", "补译的段落。", "最后一段。")):
        history = client.get(f"/api/blocks/{bid}/translations").json()
        assert len(history) == 1 and history[0]["text"] == expected and history[0]["segments"][0]["target_end"] == len(expected)


def test_unrecoverable_block_does_not_stop_later_pages_and_resumes_only_that_block(client, monkeypatch):
    pid, ids = create_paper(client, [(1, "The first paragraph finishes normally."), (1, "This paragraph needs attention."),
                                      (1, "The next paragraph has a valid translation."), (2, "This later page is still processed.")])
    with client.store.connect() as db:
        db.execute("INSERT INTO translations VALUES(?,?,?,?,?,?,?,?,?,?)", (uid(), ids[1], pid, "原有译文", "[]", "openai", "fixture", 0, 1, now()))
    recovering = False
    requests = []

    def handle(request):
        parts, schema = request_parts(request)
        requests.append([p["text"] for p in parts])
        valid = [p for p in parts if recovering or "attention" not in p["text"]]
        if "translation" in schema["properties"]:
            return reply({"translation": "补译成功。", "alignments": []} if valid else {})
        return reply({"translations": {f"s{p['id']}": {"translation": "已完成。", "alignments": []} for p in valid}, "terms": []})

    offline_openai(monkeypatch, handle)
    job = client.store.create_job("translate", pid, {"provider": "openai", "force": True})
    partial = start(client.store, job)
    assert partial["state"] == "partial" and partial["progress"] == 3 and partial["total"] == 4
    assert partial["result"]["failed_blocks"] == [{"block_id": ids[1], "page": 1}]
    assert client.get(f"/api/blocks/{ids[1]}/translations").json()[0]["text"] == "原有译文"
    assert client.get(f"/api/blocks/{ids[-1]}/translations").json()[0]["active"]

    recovering = True
    requests.clear()
    assert client.post(f"/api/jobs/{job['id']}/resume").json()["state"] == "queued"
    result = start(client.store, job)
    assert result["state"] == "done" and result["result"]["failed_blocks"] == []
    assert requests == [["This paragraph needs attention."]]
    assert len(client.get(f"/api/blocks/{ids[0]}/translations").json()) == 1


@pytest.mark.parametrize("failure", ["timeout", "truncated"])
def test_an_unusable_openai_batch_splits_and_recovers(client, monkeypatch, failure):
    pid, _ = create_paper(client, [(1, f"Study paragraph {n} is ready for translation.") for n in range(4)])
    sizes = []

    def handle(request):
        parts, _ = request_parts(request)
        sizes.append(len(parts))
        if len(sizes) == 1:
            if failure == "timeout":
                assert request.extensions["timeout"]["read"] == 90
                raise httpx.ReadTimeout("synthetic read timeout", request=request)
            return httpx.Response(200, json={"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}, "output": []})
        return reply({"translations": {f"s{p['id']}": {"translation": "已补译。", "alignments": []} for p in parts}, "terms": []})

    offline_openai(monkeypatch, handle)
    result = start(client.store, client.store.create_job("translate", pid, {"provider": "openai"}))
    assert result["state"] == "done" and result["progress"] == 4
    assert sizes == [4, 2, 2]


def test_terms_follow_the_paper_across_providers_repairs_and_selection(client, monkeypatch):
    first = "A technology probe may elicit reflection."
    second = "A technology probe was used with 12 participants. The evidence suggests an association."
    pid, ids = create_paper(client, [(1, first), (2, second)])
    requests, connections = [], []

    def handle(request):
        body = json.loads(request.content)
        provider = {"api.openai.com": "openai", "api.deepseek.com": "deepseek"}.get(request.url.host, "local")
        payload = json.loads((body.get("input") or body["messages"])[1]["content"])
        requests.append((provider, payload))
        parts = payload["segments"]
        if provider == "local":
            answer = {"segments": [{"id": 0, "translation": "〔技术探针〕（technology probe）可能引发反思。"}],
                      "terms": [{"source": "reflection", "target": "验证"}]}
            return httpx.Response(200, json={"message": {"content": json.dumps(answer)}})
        if provider == "openai":
            assert body["store"] is False
        else:
            assert body["response_format"]["type"] == "json_object"
        if "terms" not in payload:
            assert [part["text"] for part in parts] == ["The evidence suggests an association."]
            return reply({"translation": "证据提示存在关联。", "alignments": []})
        assert payload["terms"] == [{"source": "technology probe", "target": "技术探针"}]
        variant = {"id": 0, "translation": "对 12 名参与者使用了技术探索工具（Technology Probe）。",
                   "alignments": [{"source": "technology probe", "translation": "技术探索工具"}]}
        if provider == "openai":
            return reply({"translations": {"s0": variant}, "terms": []})
        summary = {"id": 1, "translation": "证据提示存在关联。"}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"segments": [variant, summary]})}}]})

    real = httpx.Client

    def connection(**kwargs):
        value = real(transport=httpx.MockTransport(handle), **kwargs)
        connections.append(value)
        return value

    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-openai-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-deepseek-key")
    monkeypatch.setattr(ai.httpx, "Client", connection)
    store = client.store
    initial = start(store, store.create_job("translate", pid, {"provider": "local", "page_start": 1, "page_end": 1}))
    assert initial["state"] == "done"
    assert initial["result"]["terms"] == [{"source": "technology probe", "target": "技术探针"}]
    assert client.get(f"/api/blocks/{ids[0]}/translations").json()[0]["text"] == "技术探针（technology probe）可能引发反思。"
    store = Store(store.root)
    for provider in ("deepseek", "openai"):
        count = len(connections)
        job = start(store, store.create_job("translate", pid, {"provider": provider, "page_start": 2, "page_end": 2, "force": True}))
        assert job["state"] == "done" and len(connections) == count + 1 and connections[-1].is_closed
        version = client.get(f"/api/blocks/{ids[1]}/translations").json()[0]
        assert version["text"] == "对 12 名参与者使用了技术探针（Technology Probe）。证据提示存在关联。"
        phrase = version["segments"][1]
        assert slice_text(version["text"], phrase["target_start"], phrase["target_end"]) == "技术探针"
    assert [provider for provider, _ in requests] == ["local", "deepseek", "openai", "openai"]
    assert len(client.get(f"/api/blocks/{ids[0]}/translations").json()) == 1

    submitted = client.post(f"/api/papers/{pid}/jobs", json={"kind": "reading", "task": "translate_selection",
        "provider": "openai", "block_ids": [ids[1]], "question": "技术探针", "target_language": "en"})
    assert submitted.status_code == 200
    assert start(store, submitted.json())["state"] == "done"
    assert client.get(f"/api/papers/{pid}/ai-results").json()[0]["answer"] == "technology probe"
    assert len(requests) == 4


def test_existing_phrase_anchors_seed_only_supported_terms(client):
    source = "😀 A technology probe may elicit reflection."
    target = "😀 技术探针（technology probe）可能引发反思。"
    pid, (bid,) = create_paper(client, [(1, source)])
    start = length("😀 A ")
    offset = length("😀 ")
    with client.store.connect() as db:
        db.execute("INSERT INTO translations VALUES(?,?,?,?,?,?,?,?,?,?)", (uid(), bid, pid, target, dumps([
            {"source_start": 0, "source_end": length(source), "target_start": 0, "target_end": length(target)},
            {"source_start": start, "source_end": start + length("technology probe"),
             "target_start": offset, "target_end": offset + length("技术探针")},
        ]), "openai", "legacy", 0, 1, now()))
        glossary = paper_terms(db, client.store.paper(pid, db), "zh")
    assert glossary == {"technology probe": {"source": "technology probe", "target": "技术探针"}}


def test_chinese_to_english_selection_supplies_later_page_translation(client, monkeypatch):
    pid, (bid,) = create_paper(client, [(1, "技术探针可能促进反思。")])
    with client.store.connect() as db:
        db.execute("UPDATE papers SET language='zh' WHERE id=?", (pid,))
    count = 0

    def handle(request):
        nonlocal count
        count += 1
        payload = json.loads(json.loads(request.content)["input"][1]["content"])
        if count == 1:
            assert payload["text"] == "技术探针可能促进反思。"
            return reply({"answer": "The technology probe may encourage reflection.", "citations": [],
                          "terms": [{"source": "技术探针", "target": "technology probe"}]})
        assert payload["terms"] == [{"source": "技术探针", "target": "technology probe"}]
        return reply({"translations": {"s0": {"translation": "The technical probe may encourage reflection.",
            "alignments": [{"source": "技术探针", "translation": "technical probe"}]}}, "terms": []})

    offline_openai(monkeypatch, handle)
    store = client.store
    selected = start(store, store.create_job("reading", pid, {"provider": "openai", "task": "translate_selection",
        "block_ids": [bid], "question": "只将以下选中文字翻译为英文，不要扩写：\n技术探针可能促进反思。"}))
    assert selected["state"] == "done" and selected["result"]["term_language"] == "en"
    assert start(store, store.create_job("translate", pid, {"provider": "openai"}))["state"] == "done"
    assert client.get(f"/api/blocks/{bid}/translations").json()[0]["text"] == "The technology probe may encourage reflection."
