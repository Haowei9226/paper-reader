"""Recognize raster-only PDF pages with Windows OCR, keeping the source immutable."""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import subprocess
import unicodedata
import zlib
from functools import lru_cache
from pathlib import Path

import pymupdf as fitz

from .pdf import _append, _heading_outline, scalar_text
from .reading import REFERENCE_HEADING, SECTION
from .store import OCR_PAGE_QUERY, dumps, now

HAN = re.compile(r"[\u3400-\u9fff]")
CJK = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")


class OcrError(ValueError):
    pass


@lru_cache(maxsize=1)
def _command():
    script = Path(__file__).with_name("windows_ocr.ps1").read_text(encoding="utf-8")
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    exe = Path(os.environ["SystemRoot"]) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return [str(exe), "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]


def _call_engine(language="", png=b""):
    try:
        result = subprocess.run(_command(), input=png, capture_output=True, check=True,
                                env=dict(os.environ, READER_OCR_LANGUAGE=language),
                                timeout=90 if language else 8, creationflags=subprocess.CREATE_NO_WINDOW)
        return json.loads(result.stdout)
    except subprocess.TimeoutExpired as exc:
        raise OcrError("Windows 文字识别超时，可重试") from exc
    except (OSError, KeyError, subprocess.CalledProcessError, UnicodeError, ValueError) as exc:
        raise OcrError("Windows 文字识别未能启动或未返回结果，请检查系统 OCR 语言包") from exc


def status():
    result = {"available": False, "languages": [], "default_language": None}
    if os.name != "nt":
        return {**result, "reason": "本机文字识别需要 Windows 及对应的 OCR 语言包"}
    try:
        engine = _call_engine()
    except OcrError as exc:
        return {**result, "reason": str(exc)}
    if not engine["languages"]:
        return {**result, "reason": "请在 Windows 设置中安装所需语言的光学字符识别功能"}
    return {**engine, "available": True, "default_language": engine["default_language"] or engine["languages"][0]["tag"]}


def resolve_language(engine, language, paper_language=""):
    if not engine["available"]:
        raise OcrError(engine["reason"])
    tags = [entry["tag"] for entry in engine["languages"]]
    if language is None:
        return next((tag for tag in tags if tag.split("-")[0] == paper_language), engine["default_language"])
    if not isinstance(language, str) or language not in tags:
        raise OcrError("所选 OCR 语言不可用，请选择已安装的 Windows 识别语言")
    return language


def _line_blocks(result, width, height, pixels_wide, pixels_high):
    sx, sy = width / pixels_wide, height / pixels_high
    center = (pixels_wide / 2, pixels_high / 2) * 2
    angle = result["angle"] or 0
    rotation = fitz.Matrix(angle)
    canvas = fitz.Rect(0, 0, pixels_wide, pixels_high)
    blocks = []
    for line in result["lines"]:
        parts, boxes, rows = [], [], []
        previous = ""
        cursor = 0
        for word in line["words"]:
            text = scalar_text(word["text"]).strip()
            if not text:
                continue
            x, y, w, h = word["box"]
            step = w / len(text)
            start = line["text"].find(text, cursor)
            gap = start < 0 or start > cursor
            if start >= 0:
                cursor = start + len(text)
            if previous and gap and not (
                (CJK.match(previous) and (CJK.match(text[0]) or unicodedata.category(text[0]).startswith("P"))) or
                (CJK.match(text[0]) and unicodedata.category(previous).startswith("P"))
            ):
                _append(parts, boxes, rows, " ", None, 0)
            for index, char in enumerate(text):
                left = x + index * step
                rect = fitz.Rect(left, y, left + step, y + h)
                if angle:
                    rect = ((rect - center) * rotation) + center
                rect &= canvas
                box = [round(v, 3) for v in (rect.x0 * sx, rect.y0 * sy, rect.x1 * sx, rect.y1 * sy)] if not rect.is_empty else None
                _append(parts, boxes, rows, char, box, 0)
            previous = text[-1]
        text = "".join(parts)
        located = [box for box in boxes if box]
        if not located or not any(ch.isalnum() for ch in text):
            continue
        bbox = [min(b[0] for b in located), min(b[1] for b in located), max(b[2] for b in located), max(b[3] for b in located)]
        kind = "heading" if SECTION.fullmatch(text) or REFERENCE_HEADING.fullmatch(text) else "paragraph"
        blocks.append((kind, text, bbox, {"boxes": boxes, "lines": rows}))
    return blocks


def recognize_page(page, language, max_dimension):
    width, height = page.rect.width, page.rect.height
    if width <= 0 or height <= 0:
        return []
    scale = min(300 / 72, (max_dimension - 2) / max(width, height), math.sqrt(16_000_000 / (width * height)))
    pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csGRAY, annots=False)
    result = _call_engine(language, pixmap.tobytes("png"))
    return _line_blocks(result, width, height, pixmap.width, pixmap.height)


def _save_page(store, job, number, blocks, *, infer_outline):
    paper_id = job["paper_id"]
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        current = db.execute("""SELECT j.state,p.deleted_at FROM jobs j JOIN papers p ON p.id=j.paper_id
            WHERE j.id=?""", (job["id"],)).fetchone()
        if not current or current["state"] != "running":
            raise InterruptedError("任务已暂停或取消")
        if current["deleted_at"] is not None:
            raise OcrError("文献已移入回收站，请先恢复文献再继续识别")
        # Existing source anchors, even on neighboring pages, are never replaced.
        if db.execute("""SELECT 1 FROM blocks WHERE paper_id=? AND page=?
            AND kind!='image' AND TRIM(text)!='' LIMIT 1""", (paper_id, number)).fetchone():
            return
        first_position = db.execute("SELECT COALESCE(max(position)+1,0) FROM blocks WHERE paper_id=? AND page=?",
                                    (paper_id, number)).fetchone()[0]
        previous = db.execute("""SELECT section FROM blocks WHERE paper_id=? AND page<?
            AND kind!='image' ORDER BY page DESC,position DESC LIMIT 1""", (paper_id, number)).fetchone()
        section = previous["section"] if previous else ""
        for index, (kind, source, bbox, geo) in enumerate(blocks):
            block_id = hashlib.sha256(f"{paper_id}:windows-ocr-v1:{number}:{index}".encode()).hexdigest()[:32]
            if kind == "heading":
                section = source
            db.execute("INSERT INTO blocks VALUES(?,?,?,?,?,?,?,?,?,?)", (
                block_id, paper_id, number, first_position + index, kind, source, dumps(bbox),
                zlib.compress(dumps(geo).encode()), None, section,
            ))
            store.reindex_block(db, block_id)
        db.execute("UPDATE pages SET ocr_job_id=? WHERE paper_id=? AND number=?", (job["id"], paper_id, number))
        if blocks:
            text = " ".join(block[1] for block in blocks)
            language = "zh" if len(HAN.findall(text)) > len(text) * .15 else "en"
            db.execute("""UPDATE papers SET parse_state='ready',parse_error='',updated=?,
                language=CASE WHEN parse_state='no_text' AND NOT EXISTS
                    (SELECT 1 FROM json_each(manual_fields) WHERE value='language') THEN ? ELSE language END
                WHERE id=?""", (now(), language, paper_id))
            if infer_outline and any(block[0] == "heading" for block in blocks):
                db.execute("UPDATE papers SET outline=? WHERE id=?", (dumps(_heading_outline(db, paper_id)), paper_id))


def ocr_job(store, job):
    paper = store.paper(job["paper_id"])
    if not paper or paper["deleted_at"] is not None:
        raise OcrError("请先恢复这篇文献")
    if paper["parse_state"] not in ("ready", "no_text"):
        raise OcrError("请先完成 PDF 解析")
    engine = status()
    language = resolve_language(engine, job["payload"].get("language"), paper["language"])
    with store.connect() as db:
        pages = db.execute(OCR_PAGE_QUERY + " ORDER BY p.number", (paper["id"],)).fetchall()
    remaining = [row["number"] for row in pages if not row["has_text"] and row["ocr_job_id"] != job["id"]]
    recognized = sum(row["ocr_job_id"] == job["id"] and row["has_text"] for row in pages)
    empty = sum(row["ocr_job_id"] == job["id"] and not row["has_text"] for row in pages)
    total = len(remaining) + recognized + empty
    failed = []
    error = ""
    with fitz.open(store.original(paper["id"])) as doc:
        infer_outline = not doc.get_toc()
        for index, number in enumerate(remaining, start=recognized + empty):
            store.progress(job["id"], index, total, f"正在识别第 {number} 页（{index + 1} / {total}）")
            try:
                blocks = recognize_page(doc[number - 1], language, engine["max_dimension"])
            except (RuntimeError, ValueError) as exc:
                failed.append(number)
                if not error and isinstance(exc, OcrError):
                    error = str(exc)
                continue
            _save_page(store, job, number, blocks, infer_outline=infer_outline)
            recognized += bool(blocks)
            empty += not blocks
    store.progress(job["id"], recognized + empty, total, "本机文字识别完成")
    return {"recognized_pages": recognized, "empty_pages": empty, "failed_pages": failed, "error": error}
