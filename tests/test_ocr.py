import hashlib

import pymupdf as fitz
import pytest

from reader import ocr
from reader.pdf import length
from reader.worker import run
from .conftest import import_and_parse


def scanned_pdf(*pages):
    with fitz.open() as doc, fitz.open() as source:
        for text, image, font in pages:
            page = doc.new_page(width=400, height=220)
            if image:
                raster = source.new_page(width=400, height=220)
                raster.insert_text((35, 72), text, fontsize=18, fontname=font)
                image_data = raster.get_pixmap(matrix=fitz.Matrix(3, 3)).tobytes("png")
                page.insert_image(page.rect, stream=image_data)
            elif text:
                page.insert_text((35, 72), text, fontsize=18, fontname=font)
        return doc.tobytes()


def start_job(client, paper_id, **payload):
    response = client.post(f"/api/papers/{paper_id}/jobs", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["id"]


def run_job(client, job_id):
    with client.store.connect() as db:
        db.execute("UPDATE jobs SET state='running' WHERE id=?", (job_id,))
    run(client.store, job_id)
    return client.store.job(job_id)


@pytest.fixture
def fake_engine(monkeypatch):
    engine = {"available": True, "default_language": "en-GB", "max_dimension": 10000,
              "languages": [{"tag": "en-GB", "name": "English"}, {"tag": "zh-Hans-CN", "name": "简体中文"}]}
    monkeypatch.setattr(ocr, "status", lambda: engine)
    return engine


def fake_line(text, *, angle=0):
    words = [{"text": word, "box": [40 + 48 * index, 50, 45, 20]}
             for index, word in enumerate(text.split())]
    return ocr._line_blocks({"angle": angle, "lines": [{"text": text, "words": words}]}, 400, 220, 400, 220)


def test_ocr_preserves_mixed_pdf_anchors_and_supports_search_notes_and_rebuild(client, fake_engine, monkeypatch):
    paper = import_and_parse(client, scanned_pdf(
        ("Native text remains anchored.", False, "helv"),
        ("Scanned PDF reference text.", True, "helv"),
    ))
    pid = paper["id"]
    assert (paper["parse_state"], paper["ocr_pending_pages"]) == ("ready", 1)
    assert client.get("/api/ocr/status").json()["languages"] == fake_engine["languages"]
    native = client.get(f"/api/papers/{pid}/pages/1").json()["blocks"][0]
    native_note = client.post(f"/api/papers/{pid}/annotations", json={
        "targets": [{"block_id": native["id"], "start": 0, "end": 6}], "note": "Existing",
    })
    assert native_note.status_code == 200, native_note.text
    original_hash = hashlib.sha256(client.store.original(pid).read_bytes()).hexdigest()
    assert not client.get(f"/api/papers/{pid}/pages/2").json()["ocr"]
    assert client.post(f"/api/papers/{pid}/jobs", json={"kind": "ocr", "language": "unknown"}).status_code == 400

    def recognize(page, language, max_dimension):
        assert (page.number, language, max_dimension) == (1, "en-GB", 10000)
        return fake_line("Scanned PDF reference text.")

    monkeypatch.setattr(ocr, "recognize_page", recognize)
    job_id = start_job(client, pid, kind="ocr", language="en-GB")
    assert client.post(f"/api/papers/{pid}/jobs", json={"kind": "ocr"}).status_code == 400
    job = run_job(client, job_id)
    assert (job["state"], job["result"]["recognized_pages"]) == ("done", 1)
    current = client.get(f"/api/papers/{pid}").json()
    assert (current["ocr_pages"], current["ocr_pending_pages"]) == (1, 0)
    assert client.get(f"/api/papers/{pid}/pages/1").json()["blocks"][0] == native
    assert hashlib.sha256(client.store.original(pid).read_bytes()).hexdigest() == original_hash

    page = client.get(f"/api/papers/{pid}/pages/2").json()
    assert page["ocr"]
    assert next(b for b in page["blocks"] if b["kind"] == "image")["reading_role"] == "embedded"
    block = next(b for b in page["blocks"] if b["text"])
    assert len(block["geometry"]["boxes"]) == length(block["text"])
    assert client.get(f"/api/papers/{pid}/find", params={"q": "reference text"}).json()["items"][0]["page"] == 2
    assert "Scanned PDF reference text." in client.get(f"/api/papers/{pid}/export/html").text
    note = client.post(f"/api/papers/{pid}/annotations", json={
        "targets": [{"block_id": block["id"], "start": 0, "end": 7}], "note": "OCR anchored note",
    })
    assert note.status_code == 200, note.text
    assert note.json()["targets"][0]["rects"]
    response = client.get(f"/api/papers/{pid}/export/pdf")
    with fitz.open(stream=response.content, filetype="pdf") as exported:
        assert exported[1].first_annot is not None

    rebuilt = run_job(client, start_job(client, pid, kind="parse", rebuild=True))
    assert rebuilt["state"] == "done", rebuilt["message"]
    assert rebuilt["result"]["preserved_pages"] == [2]
    assert client.get(f"/api/papers/{pid}/pages/2").json() == page
    saved = client.get(f"/api/papers/{pid}/annotations").json()
    assert any(a["targets"] == note.json()["targets"] for a in saved)


def test_ocr_resumes_failed_pages_and_can_retry_empty_pages_with_other_language(client, fake_engine, monkeypatch):
    paper = import_and_parse(client, scanned_pdf(
        ("First page.", True, "helv"), ("", False, "helv"), ("Third page.", True, "helv"),
    ))
    pid = paper["id"]
    assert (paper["parse_state"], paper["ocr_pending_pages"]) == ("no_text", 3)
    calls = []

    def recognize(page, language, _):
        calls.append((page.number, language))
        if page.number == 1:
            return []
        if page.number == 2 and calls.count((2, language)) == 1:
            raise ocr.OcrError("test error")
        return fake_line("Recognized on this page.")

    monkeypatch.setattr(ocr, "recognize_page", recognize)
    job_id = start_job(client, pid, kind="ocr")
    first = run_job(client, job_id)
    assert (first["state"], first["progress"], first["result"]["failed_pages"]) == ("partial", 2, [3])
    assert client.post(f"/api/jobs/{job_id}/resume").status_code == 200
    second = run_job(client, job_id)
    assert (second["state"], second["result"]["recognized_pages"], second["result"]["empty_pages"]) == ("done", 2, 1)
    assert calls == [(0, "en-GB"), (1, "en-GB"), (2, "en-GB"), (2, "en-GB")]
    updated = client.get(f"/api/papers/{pid}").json()
    assert (updated["parse_state"], updated["ocr_pages"], updated["ocr_pending_pages"], updated["ocr_empty_pages"]) == ("ready", 2, 1, 1)
    other = start_job(client, pid, kind="ocr", language="zh-Hans-CN")
    assert client.post(f"/api/jobs/{other}/pause").status_code == 200
    assert client.delete(f"/api/papers/{pid}").status_code == 200
    assert client.post(f"/api/jobs/{other}/resume").status_code == 400
    assert client.post(f"/api/papers/{pid}/restore").status_code == 200
    assert client.post(f"/api/jobs/{other}/resume").status_code == 200
    assert run_job(client, other)["state"] == "done"
    assert calls[-1] == (1, "zh-Hans-CN")


@pytest.mark.parametrize(("prefix", "text", "font"), [
    ("en", "Windows OCR reads this scan.", "helv"),
    ("zh", "本地文字识别测试。", "china-s"),
])
def test_windows_ocr_on_real_raster_page(prefix, text, font):
    engine = ocr.status()
    language = next((entry["tag"] for entry in engine["languages"] if entry["tag"].split("-")[0] == prefix), None)
    if language is None:
        pytest.skip(f"Windows OCR language {prefix} unavailable")
    with fitz.open(stream=scanned_pdf((text, True, font)), filetype="pdf") as doc:
        assert not doc[0].get_text().strip()
        blocks = ocr.recognize_page(doc[0], language, engine["max_dimension"])
    recognized = " ".join(block[1] for block in blocks)
    assert text in recognized
    assert blocks[0][2][0] == pytest.approx(35, abs=4)
    assert blocks[0][2][3] == pytest.approx(72, abs=5)
