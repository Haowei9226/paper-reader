import io
import json
import zipfile

import httpx
import pytest

from reader import ai
from reader.backup import create_backup, prepare_restore
from reader.secrets import Secrets
from reader.store import dumps, now, uid
from reader.worker import run
from .conftest import import_and_parse, source_block
from .test_annotations import add_translation


def test_translation_checkpoint_and_retranslation_keep_notes(client, monkeypatch):
    p = import_and_parse(client)
    block = source_block(client, p["id"])
    source_start = block["text"].index("coordinate their work")
    original_note = client.post(f"/api/papers/{p['id']}/annotations", json={"targets": [{"block_id": block["id"], "start": source_start, "end": source_start + len("coordinate their work")}], "note": "先读原文，再翻译"}).json()
    assert not original_note["translated_targets"]
    calls = []
    class FakeModel:
        provider = "local"
        model = "test-model"
        settings = {"context_tokens": 4096}
        def __init__(self, *args):
            pass
        def ask(self, instructions, text, **_options):
            calls.append(text)
            supplied = json.loads(text)["segments"]
            return {"segments": [{"id": p["id"], "translation": "测试译文：共享表征支持协作。"} for p in supplied]}
    monkeypatch.setattr(ai, "Client", FakeModel)
    job = client.store.create_job("translate", p["id"], {"block_ids": [block["id"]]})
    with client.store.connect() as db:
        db.execute("UPDATE jobs SET state='running' WHERE id=?", (job["id"],))
    run(client.store, job["id"])
    assert client.store.job(job["id"])["state"] == "done"
    assert len(calls) == 1
    first = client.get(f"/api/papers/{p['id']}/pages/1").json()
    translated = next(b for b in first["blocks"] if b["id"] == block["id"])["translation"]
    assert translated and max(s["source_end"] for s in translated["segments"]) == len(block["text"])
    mapped_note = client.get(f"/api/papers/{p['id']}/annotations").json()[0]
    assert mapped_note["id"] == original_note["id"] and mapped_note["note"] == "先读原文，再翻译"
    assert mapped_note["translated_targets"][0]["translation_id"] == translated["id"]
    assert mapped_note["translated_targets"][0]["quote"] in translated["text"]
    client.patch("/api/translations/" + translated["id"], json={"text": "人工修正的译文"})
    repeat = client.store.create_job("translate", p["id"], {"block_ids": [block["id"]], "force": True, "preserve_manual": True})
    with client.store.connect() as db:
        db.execute("UPDATE jobs SET state='running' WHERE id=?", (repeat["id"],))
    run(client.store, repeat["id"])
    assert len(calls) == 1


def test_deepseek_adapter_and_secret_not_returned(client, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "dummy-key-for-isolated-test")
    real_client = httpx.Client
    seen = []
    def handle(request):
        payload = json.loads(request.content)
        seen.append((request.url.host, payload["model"], payload["response_format"]))
        assert request.headers["Authorization"] == "Bearer dummy-key-for-isolated-test"
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"answer":"可用"}'}, "finish_reason": "stop"}]})
    monkeypatch.setattr(ai.httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs))
    result = ai.Client(client.store, {"provider": "deepseek"}).ask("返回json。", "测试")
    assert result["answer"] == "可用"
    assert seen[0][0] == "api.deepseek.com"
    assert seen[0][1] == "deepseek-flash"
    assert seen[0][2] == {"type": "json_object"}
    assert "dummy-key-for-isolated-test" not in client.get("/api/settings").text


def test_openai_responses_api_connects_without_exposing_key(client, monkeypatch):
    sentinel = "dummy-openai-key-for-isolated-test"
    monkeypatch.setenv("OPENAI_API_KEY", sentinel)
    real_client = httpx.Client
    requests = []

    def handle(request):
        body = json.loads(request.content)
        requests.append(request.url)
        assert str(request.url) == "https://api.openai.com/v1/responses"
        assert request.headers["Authorization"] == "Bearer " + sentinel
        assert body["model"] == "gpt-4.1-mini"
        assert body["store"] is False
        assert body["text"]["format"] == {"type": "json_object"}
        assert body["input"][-1] == {"role": "user", "content": "连接测试"}
        return httpx.Response(200, json={"status": "completed", "output": [
            {"type": "reasoning"},
            {"type": "message", "content": [{"type": "output_text", "text": '{"ok":true}'}]},
        ]})

    monkeypatch.setattr(ai.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handle), **kw))
    configured = client.patch("/api/settings", json={"provider": "openai"}).json()
    assert configured["openai_configured"] is True and sentinel not in json.dumps(configured)
    response = client.post("/api/models/test", json={"provider": "openai"})
    assert response.json() == {"ok": True, "provider": "openai", "model": "gpt-4.1-mini"}
    assert len(requests) == 1
    assert sentinel not in client.get("/api/bootstrap").text


def test_openai_queue_requires_key_and_preserves_provider(client, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr("reader.secrets.local_openai_key", lambda: "")
    paper = import_and_parse(client)
    url = f"/api/papers/{paper['id']}/jobs"
    client.patch("/api/settings", json={"provider": "openai"})
    missing = client.post(url, json={"kind": "reading"})
    assert missing.status_code == 400 and "OpenAI" in missing.json()["detail"]
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-openai-key")
    job = client.post(url, json={"kind": "reading"}).json()
    client.patch("/api/settings", json={"provider": "local"})
    assert client.store.job(job["id"])["payload"] == {"provider": "openai"}


@pytest.mark.parametrize("status,body,message", [
    (429, {"error": {"code": "insufficient_quota", "message": "not for display"}}, "API 额度不足"),
    (200, {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}, "output": []}, "被截断"),
])
def test_openai_errors_do_not_save_partial_results_or_retry_exhausted_quota(client, monkeypatch, status, body, message):
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-openai-key")
    real_client = httpx.Client
    calls = []

    def handle(request):
        calls.append(request.url)
        return httpx.Response(status, json=body)

    monkeypatch.setattr(ai.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handle), **kw))
    response = client.post("/api/models/test", json={"provider": "openai"})
    assert response.status_code == 400
    assert message in response.json()["detail"] and "not for display" not in response.text
    assert len(calls) == 1


def test_backup_restore_keeps_annotations_and_originals(client):
    p = import_and_parse(client)
    b = source_block(client, p["id"])
    tid, text = add_translation(client, p["id"], b)
    ann = client.post(f"/api/papers/{p['id']}/annotations", json={"targets": [{"block_id": b["id"], "start": 0, "end": 20}], "note": "跨版本保留的研究笔记"}).json()
    backup = create_backup(client.store)
    with zipfile.ZipFile(backup) as archive:
        assert "deepseek.key.dpapi" not in archive.namelist()
        assert "openai.key.dpapi" not in archive.namelist()
        assert "library.sqlite3" in archive.namelist()
    content = backup.read_bytes()
    client.delete("/api/annotations/" + ann["id"])
    restored = client.post("/api/restore", files={"file": ("library.zip", content, "application/zip")})
    assert restored.status_code == 200, restored.text
    assert client.get(f"/api/papers/{p['id']}/annotations").json()[0]["note"] == "跨版本保留的研究笔记"
    assert client.get(f"/api/papers/{p['id']}/file").content.startswith(b"%PDF-")
    assert (client.store.root.parent / restored.json()["previous_copy"]).is_dir()


def test_corrupt_backup_never_replaces_live_library(client):
    p = import_and_parse(client)
    backup = create_backup(client.store)
    damaged = client.store.root / "incoming" / "damaged.zip"
    with zipfile.ZipFile(backup) as source, zipfile.ZipFile(damaged, "w") as target:
        for name in source.namelist():
            content = source.read(name)
            target.writestr(name, content[:20] + bytes([content[20] ^ 1]) + content[21:] if name.endswith(".pdf") else content)
    with pytest.raises(ValueError, match="校验"):
        prepare_restore(client.store, damaged)
    assert client.get("/api/papers/" + p["id"]).status_code == 200


def test_windows_key_is_encrypted(client):
    import os
    if os.name != "nt":
        pytest.skip("DPAPI requires Windows")
    secrets = Secrets(client.store.root)
    secrets.save("dummy-secret-dpapi")
    assert secrets.get() == "dummy-secret-dpapi"
    assert b"dummy-secret-dpapi" not in secrets.path.read_bytes()
    assert "dummy-secret-dpapi" not in client.get("/api/settings").text
    openai = Secrets(client.store.root, "openai")
    openai.save("dummy-openai-secret")
    assert openai.get() == "dummy-openai-secret" and secrets.get() == "dummy-secret-dpapi"
    assert b"dummy-openai-secret" not in openai.path.read_bytes()


def test_phrase_mapping_can_join_adjacent_alignment_spans():
    spans = [{"source_start":0,"source_end":56,"target_start":0,"target_end":15},
             {"source_start":34,"source_end":44,"target_start":9,"target_end":11},
             {"source_start":45,"source_end":55,"target_start":11,"target_end":14}]
    chosen = ai.mapping_parts(spans,34,55,"source")
    assert len(chosen) == 2
    assert min(s["target_start"] for s in chosen) == 9
    assert max(s["target_end"] for s in chosen) == 14


def test_manual_edit_during_generation_is_not_overwritten(client, monkeypatch):
    p = import_and_parse(client)
    b = source_block(client, p["id"])
    tid, _ = add_translation(client, p["id"], b)
    class ConcurrentEditModel:
        provider = "local"
        model = "test-model"
        settings = {"context_tokens":4096}
        def __init__(self,*args):
            pass
        def ask(self,instructions,text,**_options):
            client.patch('/api/translations/'+tid,json={'text':'生成期间保存的人工译文'})
            return {'segments':[{'id':s['id'],'translation':'不应覆盖人工译文的模型输出'} for s in json.loads(text)['segments']]}
    monkeypatch.setattr(ai,'Client',ConcurrentEditModel)
    job=client.store.create_job('translate',p['id'],{'block_ids':[b['id']],'force':True,'preserve_manual':True})
    with client.store.connect() as db:
        db.execute("UPDATE jobs SET state='running' WHERE id=?",(job['id'],))
    run(client.store,job['id'])
    history=client.get('/api/blocks/'+b['id']+'/translations').json()
    assert next(t for t in history if t['active'])['text']=='生成期间保存的人工译文'
