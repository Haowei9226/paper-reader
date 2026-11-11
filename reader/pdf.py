"""PDF extraction and annotation geometry share one canonical text representation."""
from __future__ import annotations

import hashlib
import json
import os
import re
import statistics
import tempfile
import threading
import unicodedata
import zlib
from collections import defaultdict
from itertools import groupby
from pathlib import Path

import pymupdf as fitz

from .store import NO_TEXT_ERROR, OCR_PAGE_QUERY, Store, dumps, now
from .margins import line_number_rows, page_furniture_rows, page_margin_blocks

PDF_LAYOUT_LOCK = threading.RLock()
COLORS = {"yellow": (1, .81, .28), "green": (.42, .78, .58), "blue": (.42, .66, .96), "pink": (.95, .49, .64)}
SURROGATE = re.compile(r"[\ud800-\udfff]")


def scalar_text(text: str) -> str:
    if SURROGATE.search(text):
        return text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
    return text


def recover_pdf_glyphs(page, blocks):
    """Repair broken ToUnicode entries using the painted glyph in the embedded font."""
    def font_key(font):
        return re.sub(r"[\s-]+", "", font.split("+", 1)[-1]).casefold()

    def glyph_key(font, origin):
        return font, tuple(round(value, 3) for value in origin)

    broken = defaultdict(list)
    for block in blocks:
        for line in block.get("lines", ()):
            for span in line["spans"]:
                font = None
                for char in span["chars"]:
                    if not SURROGATE.search(char["c"]):
                        continue
                    char["c"] = scalar_text(char["c"])
                    if char["c"] == "\ufffd":
                        font = font or font_key(span["font"])
                        broken[glyph_key(font, char["origin"])].append(char)
    if not broken:
        return

    def valid(code):
        return 0 < code <= 0x10ffff and not 0xd800 <= code <= 0xdfff and code != 0xfffd

    try:
        trace = page.get_texttrace()
        font_refs = defaultdict(set)
        needed = {font for font, _ in broken}
        for xref, _, _, name, *_ in page.get_fonts():
            name = font_key(name)
            if name in needed:
                font_refs[name].add(xref)
    except (ValueError, RuntimeError, AttributeError):
        return

    mupdf = getattr(fitz, "mupdf", None)
    get_name = getattr(mupdf, "fz_get_glyph_name2", None)
    get_code = getattr(mupdf, "fz_unicode_from_glyph_name", None)
    fonts, names = {}, {}

    def embedded_code(xref, glyph):
        key = (xref, glyph)
        if key not in names:
            names[key] = None
            try:
                if xref not in fonts:
                    fonts[xref] = None
                    if font_data := page.parent.extract_font(xref)[3]:
                        fonts[xref] = fitz.Font(fontbuffer=font_data)
                if font := fonts[xref]:
                    code = get_code(get_name(font.this, glyph))
                    if valid(code):
                        names[key] = code
            except (ValueError, RuntimeError, AttributeError, TypeError, IndexError):
                pass
        return names[key]

    recovered = defaultdict(set)
    for span in trace:
        font = font_key(span["font"])
        if font not in needed:
            continue
        for code, glyph, origin, _ in span["chars"]:
            key = glyph_key(font, origin)
            if key not in broken:
                continue
            if valid(code):
                recovered[key].add(code)
            elif get_name and get_code and glyph > 0:
                candidates = {embedded_code(ref, glyph) for ref in font_refs[key[0]]}
                if candidates and None not in candidates:
                    recovered[key].update(candidates)
    for key, candidates in recovered.items():
        if len(candidates) == 1:
            for char in broken[key]:
                char["c"] = chr(next(iter(candidates)))


def length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def slice_text(text: str, start: int, end: int) -> str:
    if start < 0 or end <= start or end > length(text):
        raise ValueError("标注文字范围无效")
    try:
        return text.encode("utf-16-le")[start * 2:end * 2].decode("utf-16-le")
    except UnicodeDecodeError as exc:
        raise ValueError("请选择完整字符") from exc


def geometry(blob):
    return json.loads(zlib.decompress(blob)) if blob else {"boxes": [], "lines": []}


def rects_for_range(block, start: int, end: int):
    slice_text(block["text"], start, end)
    geo = geometry(block["geometry"])
    lines = defaultdict(list)
    for i in range(start, min(end, len(geo["boxes"]))):
        box = geo["boxes"][i]
        if box:
            lines[geo["lines"][i]].append(box)
    rects = []
    for boxes in lines.values():
        rects.append([min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)])
    return rects


def _append(parts, boxes, rows, text, box, line):
    for ch in unicodedata.normalize("NFKC", scalar_text(text)):
        parts.append(ch)
        count = length(ch)
        boxes.extend([box] * count)
        rows.extend([line] * count)


def extract_block(raw, page, *, span_styles=None):
    parts, boxes, rows, sizes = [], [], [], []
    styles = []
    last = ""
    for row, line in enumerate(raw.get("lines", [])):
        chars = [char for span in line["spans"] for char in span.get("chars", [])]
        line_text = "".join(c["c"] for c in chars).strip()
        if not line_text:
            continue
        if parts:
            if last.endswith("-") and line_text[0].islower():
                while parts and parts[-1].isspace():
                    removed = parts.pop()
                    for _ in range(length(removed)):
                        boxes.pop()
                        rows.pop()
                if parts and parts[-1] == "-":
                    parts.pop()
                    boxes.pop()
                    rows.pop()
                if styles and styles[-1][1] > len(boxes):
                    start, _, style = styles.pop()
                    if start < len(boxes):
                        styles.append((start, len(boxes), style))
            elif not (re.search(r"[\u3400-\u9fff]$", last) and re.match(r"[\u3400-\u9fff]", line_text)):
                _append(parts, boxes, rows, " ", None, row)
        style_by_span = span_styles(line["spans"]) if span_styles else {}
        for span in line["spans"]:
            sizes.append(span["size"])
            start = len(boxes)
            for char in span.get("chars", []):
                box = fitz.Rect(char["bbox"]) * page.rotation_matrix
                _append(parts, boxes, rows, char["c"], [round(v, 3) for v in box], row)
            if len(boxes) > start and id(span) in style_by_span:
                styles.append((start, len(boxes), style_by_span[id(span)]))
        last = line_text
    text = "".join(parts)
    geo = {"boxes": boxes, "lines": rows}
    if span_styles:
        geo["styles"] = styles
    return text, geo, statistics.median(sizes) if sizes else 10


def reading_order(blocks, width):
    """Keep spanning headings/figures between column bands, then read each column."""
    if not blocks:
        return []
    narrow = [b for b in blocks if b["bbox"][2] - b["bbox"][0] < width * .60 and len(b["text"]) > 60]
    left = [b for b in narrow if b["bbox"][2] < width * .57]
    right = [b for b in narrow if b["bbox"][0] > width * .40]
    if len(left) < 2 or len(right) < 2:
        # A column can consist of one long PDF text block. Require a clear gutter
        # and parallel body text before treating these pages as two columns.
        parallel = any(
            a[0] < width * .28 and a[2] <= width * .53 and b[0] >= width * .47 and b[2] > width * .70
            and b[0] - a[2] >= width * .015 and min(a[3], b[3]) - max(a[1], b[1]) >= 16
            for a in (block["bbox"] for block in left) for b in (block["bbox"] for block in right)
        )
        if not parallel:
            return sorted(blocks, key=lambda b: (round(b["bbox"][1] / 4), b["bbox"][0]))
    spanning = sorted([b for b in blocks if b["bbox"][2] - b["bbox"][0] > width * .65], key=lambda b: b["bbox"][1])
    result, remaining = [], list(blocks)
    for span in spanning + [None]:
        cutoff = span["bbox"][1] if span else float("inf")
        band = [b for b in remaining if b is not span and b["bbox"][1] < cutoff]
        for col in (0, 1):
            result.extend(sorted([b for b in band if int((b["bbox"][0] + b["bbox"][2]) / 2 > width / 2) == col], key=lambda b: b["bbox"][1]))
        for b in band:
            remaining.remove(b)
        if span and span in remaining:
            result.append(span)
            remaining.remove(span)
    return result


def front_lines(page):
    """Read title and byline geometry without using PDF metadata or body block breaks."""
    blocks = page.get_text("rawdict", flags=fitz.TEXTFLAGS_RAWDICT & ~fitz.TEXT_PRESERVE_IMAGES)["blocks"]
    recover_pdf_glyphs(page, blocks)
    margin_lines = _margin_lines(blocks, page.cropbox.height, page.number + 1, page.parent.metadata.get("title", ""))
    lines = []
    for block in blocks:
        for line in block.get("lines", ()):
            if id(line) in margin_lines:
                continue
            if abs(line["dir"][0]) < .7 or line["bbox"][1] > page.cropbox.height * .58:
                continue
            spans = line["spans"]
            if not spans:
                continue
            base = max(spans, key=lambda span: span["size"])
            size = base["size"]
            # Smaller superscripts are usually author affiliation numbers or note marks.
            text = " ".join("".join(c["c"] for s in spans if s["size"] >= size * .83 for c in s["chars"]).split())
            if text:
                lines.append((text, line["bbox"], size, base["origin"][1]))
    return sorted(lines, key=lambda line: (round(line[3] / 3), line[1][0], line[3]))


def inferred_title(lines):
    candidates = [line for line in lines if 5 <= len(line[0]) <= 300 and not re.search(
        r"(?:arxiv:|https?://|copyright|all rights reserved|permission to|grants permission|©)", line[0], re.I)]
    if not candidates:
        return ""
    largest = max(line[2] for line in candidates)
    prominent = [line for line in candidates if line[2] >= largest * .95]
    title = [prominent[0][0]]
    previous = prominent[0]
    for line in prominent[1:6]:
        if not largest * .55 <= line[3] - previous[3] <= largest * 1.8:
            break
        title.append(line[0])
        previous = line
    text = " ".join(title)
    return text if len(text) <= 300 else title[0]


def _save_image(image_dir, block_id, pixmap, rebuild):
    target = image_dir / (block_id + ".png")
    if not rebuild:
        pixmap.save(target)
    else:
        data = pixmap.tobytes("png")
        if not target.is_file() or target.read_bytes() != data:
            # Keep the old image available until the new reading data is published.
            digest = hashlib.sha256(data).hexdigest()[:20]
            target = image_dir / f"{block_id}-{digest}.png"
            if not target.is_file() or target.read_bytes() != data:
                with tempfile.NamedTemporaryFile(dir=image_dir, suffix=".tmp", delete=False) as temporary:
                    temporary.write(data)
                try:
                    os.replace(temporary.name, target)
                finally:
                    Path(temporary.name).unlink(missing_ok=True)
    return f"{image_dir.name}/{target.name}"


def _margin_lines(blocks, height, number=None, title=""):
    rows = []
    for block in blocks:
        for line in block.get("lines", ()):
            if not line["spans"] or abs(line.get("dir", (1, 0))[0]) < .7:
                continue
            text = "".join(char["c"] for span in line["spans"] for char in span.get("chars", ()))
            rows.append({"key": id(line), "text": text, "bbox": line["bbox"],
                         "size": statistics.median(span["size"] for span in line["spans"])})
    hidden = line_number_rows(rows, height)
    if number is not None:
        hidden |= page_furniture_rows(rows, height, number, title)
    return hidden


def _extract_page(page, paper_id, image_dir, section, *, rebuild=False):
    raw = page.get_text("rawdict")
    recover_pdf_glyphs(page, raw["blocks"])
    margin_lines = _margin_lines(raw["blocks"], page.cropbox.height, page.number + 1, page.parent.metadata.get("title", ""))
    values = []
    for n, block in enumerate(raw["blocks"]):
        bbox = [round(v, 3) for v in fitz.Rect(block["bbox"]) * page.rotation_matrix]
        bid = hashlib.sha256(f"{paper_id}:native-v1:{page.number}:{n}".encode()).hexdigest()[:32]
        if block["type"] == 0:
            lines = [line for line in block["lines"] if id(line) not in margin_lines]
            if not lines:
                continue
            if len(lines) != len(block["lines"]):
                block = {**block, "lines": lines}
                bounds = fitz.Rect(lines[0]["bbox"])
                for line in lines[1:]:
                    bounds |= fitz.Rect(line["bbox"])
                bbox = [round(v, 3) for v in bounds * page.rotation_matrix]
            text, geo, size = extract_block(block, page)
            if not text.strip():
                continue
            spans = [span for line in block["lines"] for span in line["spans"]]
            bold = sum(len(s.get("chars", [])) for s in spans if s.get("flags", 0) & 16) > len(text) * .6
            horizontal = all(abs(line.get("dir", (1, 0))[0]) >= .7 for line in block["lines"])
            values.append({"id": bid, "text": text, "geometry": geo, "size": size, "bold": bold,
                           "horizontal": horizontal, "bbox": bbox, "kind": "paragraph", "asset": None})
        elif block["type"] == 1 and bbox[2] - bbox[0] > 45 and bbox[3] - bbox[1] > 30:
            pixmap = page.get_pixmap(clip=fitz.Rect(bbox), matrix=fitz.Matrix(1.3, 1.3))
            asset = _save_image(image_dir, bid, pixmap, rebuild)
            values.append({"id": bid, "text": "", "geometry": None, "size": 0, "bbox": bbox, "kind": "image", "asset": asset})
    # Vector diagrams and ruled tables otherwise disappear from a text-only HTML export.
    for n, rectangle in enumerate(page.cluster_drawings()):
        display = rectangle * page.rotation_matrix
        if display.width < 65 or display.height < 25 or display.get_area() > page.rect.get_area() * .8:
            continue
        if any(b["kind"] == "image" and (fitz.Rect(b["bbox"]) & display).get_area() > display.get_area() * .8 for b in values):
            continue
        bid = hashlib.sha256(f"{paper_id}:drawing-v1:{page.number}:{n}".encode()).hexdigest()[:32]
        pixmap = page.get_pixmap(clip=display, matrix=fitz.Matrix(1.5, 1.5))
        asset = _save_image(image_dir, bid, pixmap, rebuild)
        values.append({"id": bid, "text": "", "geometry": None, "size": 0, "bbox": list(display), "kind": "image", "asset": asset})
    ordered = reading_order(values, page.rect.width)
    sizes = sorted((b["size"], len(b["text"])) for b in values if b["size"] > 0 and b.get("horizontal"))
    midpoint = sum(weight for _, weight in sizes) / 2
    weight = 0
    normal_size = 10
    for size, count in sizes:
        weight += count
        if weight >= midpoint:
            normal_size = size
            break
    for b in ordered:
        if b["text"] and b.get("horizontal") and len(b["text"]) < 140 and (b["size"] > normal_size * 1.28
                or (b.get("bold") and b["size"] > normal_size * 1.12)
                or re.match(r"^(?:\d+(?:\.\d+)*[.\s]|Abstract\b|摘要|References\b|参考文献|Conclusion\b)", b["text"], re.I)):
            b["kind"] = "heading"
            section = b["text"].strip()
        b["section"] = section
    return ordered, section


def _block_rows(paper_id, number, blocks):
    for position, block in enumerate(blocks):
        geo = zlib.compress(dumps(block["geometry"]).encode()) if block["geometry"] else None
        yield (block["id"], paper_id, number, position, block["kind"], block["text"],
               dumps(block["bbox"]), geo, block["asset"], block["section"])


def _pdf_outline(doc):
    return [{"level": level, "title": scalar_text(title), "page": page} for level, title, page in doc.get_toc() if page > 0]


def _heading_outline(db, paper_id):
    title = db.execute("SELECT title FROM papers WHERE id=?", (paper_id,)).fetchone()[0]
    rows = db.execute("""SELECT b.id,b.text,b.kind,b.bbox,b.geometry,b.page,p.height
        FROM blocks b JOIN pages p ON p.paper_id=b.paper_id AND p.number=b.page
        WHERE b.paper_id=? ORDER BY b.page,b.position""", (paper_id,))
    result = []
    for number, group in groupby(rows, key=lambda row: row["page"]):
        blocks = [{**row, "bbox": json.loads(row["bbox"])} for row in group]
        hidden = page_margin_blocks(blocks, blocks[0]["height"], number, title)
        result.extend({"level": 1, "title": block["text"], "page": number}
                      for index, block in enumerate(blocks) if block["kind"] == "heading" and index not in hidden)
    return result[:300]


def parse_pdf(store: Store, job_id: str, paper_id: str):
    from .metadata import merge_bibliography, pdf_clues, write_bibliography

    file = store.original(paper_id)
    doc = fitz.open(file)
    if doc.needs_pass:
        raise ValueError("此 PDF 有密码保护，请先导入已解锁的副本")
    total = len(doc)
    toc = _pdf_outline(doc)
    with store.connect() as db:
        db.execute("UPDATE papers SET pages=?,parse_state='processing',parse_error='',outline=?,updated=? WHERE id=?", (total, dumps(toc), now(), paper_id))
        completed = dict(db.execute("""SELECT number,(SELECT section FROM blocks WHERE paper_id=p.paper_id
                                        AND page=p.number ORDER BY position DESC LIMIT 1)
                                        FROM pages p WHERE paper_id=?""", (paper_id,)))
    image_dir = store.root / "assets" / paper_id
    image_dir.mkdir(exist_ok=True)
    section = ""
    for index in range(total):
        store.progress(job_id, index, total, f"正在解析第 {index + 1} / {total} 页")
        if index + 1 in completed:
            if previous := completed[index + 1]:
                section = previous
            continue
        page = doc[index]
        ordered, section = _extract_page(page, paper_id, image_dir, section)
        with store.connect() as db:
            for record in _block_rows(paper_id, index + 1, ordered):
                db.execute("INSERT OR REPLACE INTO blocks VALUES(?,?,?,?,?,?,?,?,?,?)", record)
                store.reindex_block(db, record[0])
            db.execute("INSERT INTO pages(paper_id,number,width,height,rotation) VALUES(?,?,?,?,?)",
                       (paper_id, index + 1, page.rect.width, page.rect.height, page.rotation))
    clues = pdf_clues(doc)
    settings = store.settings()
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        sample = " ".join(r[0] for r in db.execute("""SELECT text FROM blocks
            WHERE paper_id=? AND kind!='image' AND TRIM(text)!='' ORDER BY page,position LIMIT 12""", (paper_id,)))
        language = "zh" if len(re.findall(r"[\u3400-\u9fff]", sample)) > len(sample) * .15 else "en"
        toc = toc or _heading_outline(db, paper_id)
        p = store.paper(paper_id, db)
        manual = set(json.loads(p.get("manual_fields", "[]")))
        state = "ready" if sample.strip() else "no_text"
        error = NO_TEXT_ERROR if state == "no_text" else ""
        updates = merge_bibliography(p, clues.fields, online=False, auto_rename=settings["auto_rename"], allow_name=not settings["metadata_lookup"])
        updates.update(language=p["language"] if "language" in manual else language, outline=dumps(toc), parse_state=state, parse_error=error,
                       metadata_status=p["metadata_status"] if p["metadata_status"] != "pending" else "local",
                       metadata_source=p["metadata_source"] or ("PDF" if clues.fields else ""))
        write_bibliography(store, db, paper_id, updates)
    doc.close()
    return {"pages": total, "state": state}


def _referenced_blocks(db, paper):
    paper_id = paper["id"]
    blocks = {row[0] for row in db.execute("SELECT block_id FROM translations WHERE paper_id=?", (paper_id,))}
    references = (
        ("annotations", "targets", ("block_id",)),
        ("annotations", "translated_targets", ("block_id",)),
        ("translations", "segments", ("source_block_id", "target_block_id")),
        ("ai_results", "citations", ("block_id",)),
    )
    for table, column, keys in references:
        for row in db.execute(f"SELECT {column} FROM {table} WHERE paper_id=?", (paper_id,)):
            for anchor in json.loads(row[0]):
                if isinstance(anchor, dict):
                    blocks.update(value for key in keys if isinstance(value := anchor.get(key), str))
    for row in db.execute("""SELECT payload FROM jobs WHERE paper_id=? AND kind IN ('reading','translate')
                             AND state IN ('queued','running','paused','failed','partial')""", (paper_id,)):
        if isinstance(selected := json.loads(row[0]).get("block_ids"), list):
            blocks.update(value for value in selected if isinstance(value, str))
    if saved := json.loads(paper["locator"]).get("block_id"):
        blocks.add(saved)
    return sorted(blocks)


def _publish_rebuilt_html(db, store, job_id, paper_id, total, toc):
    db.execute("BEGIN IMMEDIATE")
    job = db.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not job or job[0] != "running":
        raise InterruptedError("任务已暂停或取消")
    paper = db.execute("SELECT id,deleted_at,pages,locator FROM papers WHERE id=?", (paper_id,)).fetchone()
    if not paper or paper["deleted_at"] is not None:
        raise ValueError("文献已移入回收站，原阅读版仍可在恢复后使用。")
    if paper["pages"] and paper["pages"] != total:
        raise ValueError("原 PDF 的页数发生了变化，已保留原阅读版。")

    # Keep the whole page when a saved range no longer fits the new extraction.
    # An exact block ID and text match lets existing UTF-16 offsets stay valid.
    kept = sorted(row[0] for row in db.execute(f"""
        SELECT old.page FROM blocks old LEFT JOIN rebuilt_blocks new ON new.id=old.id
        WHERE old.paper_id=? AND old.id IN (SELECT value FROM json_each(?))
        AND (new.id IS NULL OR new.page<>old.page OR new.text<>old.text OR (old.kind='image' AND new.bbox<>old.bbox))
        UNION
        SELECT old.page FROM blocks old WHERE old.paper_id=? AND old.text<>''
        AND NOT EXISTS (SELECT 1 FROM rebuilt_blocks new WHERE new.page=old.page AND new.text<>'')
        UNION
        SELECT number FROM ({OCR_PAGE_QUERY}) WHERE ocr_job_id<>'' AND has_text
    """, (paper_id, dumps(_referenced_blocks(db, paper)), paper_id, paper_id)))
    if kept:
        for table, column in (("rebuilt_pages", "number"), ("rebuilt_blocks", "page")):
            db.execute(f"DELETE FROM {table} WHERE {column} IN (SELECT value FROM json_each(?))", (dumps(kept),))

    changed = [row[0] for row in db.execute("""
        SELECT new.id FROM rebuilt_blocks new LEFT JOIN blocks old ON old.id=new.id
        WHERE old.id IS NULL OR new.text<>old.text
    """)]
    db.execute("""DELETE FROM block_search WHERE paper_id=? AND block_id IN (
        SELECT id FROM blocks WHERE paper_id=? AND page IN (SELECT number FROM rebuilt_pages)
        AND id NOT IN (SELECT id FROM rebuilt_blocks)
    )""", (paper_id, paper_id))
    db.execute("""DELETE FROM blocks WHERE paper_id=? AND page IN (SELECT number FROM rebuilt_pages)
                  AND id NOT IN (SELECT id FROM rebuilt_blocks)""", (paper_id,))
    # UPSERT keeps foreign keys to existing translations intact.
    db.execute("""INSERT INTO blocks SELECT * FROM rebuilt_blocks WHERE true ON CONFLICT(id) DO UPDATE SET
        position=excluded.position, kind=excluded.kind, text=excluded.text, bbox=excluded.bbox,
        geometry=excluded.geometry, asset=excluded.asset, section=excluded.section""")
    db.execute("""INSERT INTO pages(paper_id,number,width,height,rotation) SELECT * FROM rebuilt_pages WHERE true
                  ON CONFLICT(paper_id,number) DO UPDATE SET
                  width=excluded.width, height=excluded.height, rotation=excluded.rotation""")
    db.execute("""UPDATE pages SET ocr_job_id='' WHERE paper_id=? AND ocr_job_id<>''
                  AND number IN (SELECT page FROM rebuilt_blocks WHERE text<>'')""", (paper_id,))
    for block_id in changed:
        store.reindex_block(db, block_id)

    has_text = db.execute("SELECT 1 FROM blocks WHERE paper_id=? AND text<>'' LIMIT 1", (paper_id,)).fetchone()
    state = "ready" if has_text else "no_text"
    error = "" if has_text else NO_TEXT_ERROR
    result = {"pages": total, "converted_pages": total - len(kept), "preserved_pages": kept, "state": state}
    if kept:
        message = f"已更新 {result['converted_pages']} 页，保留 {len(kept)} 页的原阅读版"
    else:
        message = "HTML 已重新转换" if has_text else "已重新读取 PDF，未发现可提取的正文"
    stamp = now()
    db.execute("UPDATE papers SET pages=?,parse_state=?,parse_error=?,outline=?,updated=? WHERE id=?",
               (total, state, error, dumps(toc or _heading_outline(db, paper_id)), stamp, paper_id))
    # Publish the pages and completion together so a late cancel cannot mark
    # already published work as cancelled.
    db.execute("UPDATE jobs SET state='done',progress=?,total=?,message=?,result=?,updated=? WHERE id=?",
               (total, total, message, dumps(result), stamp, job_id))
    return result


def rebuild_pdf(store: Store, job_id: str, paper_id: str):
    """Regenerate a reading copy without exposing half-converted pages."""
    with fitz.open(store.original(paper_id)) as doc, store.connect() as db:
        if doc.needs_pass:
            raise ValueError("此 PDF 有密码保护，请先导入已解锁的副本")
        total, toc = len(doc), _pdf_outline(doc)
        if not total:
            raise ValueError("PDF 中没有页面，已保留原阅读版。")
        db.execute("CREATE TEMP TABLE rebuilt_pages AS SELECT paper_id,number,width,height,rotation FROM pages WHERE 0")
        db.execute("CREATE TEMP TABLE rebuilt_blocks AS SELECT * FROM blocks WHERE 0")
        db.execute("CREATE UNIQUE INDEX rebuilt_block_ids ON rebuilt_blocks(id)")
        db.execute("CREATE INDEX rebuilt_page_blocks ON rebuilt_blocks(page)")
        db.commit()
        image_dir = store.root / "assets" / paper_id
        image_dir.mkdir(exist_ok=True)
        ocr_sections = dict(db.execute(f"""SELECT ocr.number,
            (SELECT section FROM blocks WHERE paper_id=? AND page=ocr.number
             ORDER BY position DESC LIMIT 1)
            FROM ({OCR_PAGE_QUERY}) ocr WHERE ocr.ocr_job_id<>'' AND ocr.has_text""", (paper_id, paper_id)))
        section = ""
        for page in doc:
            number = page.number + 1
            store.progress(job_id, number - 1, total, f"重新转换 HTML · 第 {number} / {total} 页")
            if number in ocr_sections:
                section = ocr_sections[number] or section
                continue
            blocks, section = _extract_page(page, paper_id, image_dir, section, rebuild=True)
            db.executemany("INSERT INTO rebuilt_blocks VALUES(?,?,?,?,?,?,?,?,?,?)", _block_rows(paper_id, number, blocks))
            db.execute("INSERT INTO rebuilt_pages VALUES(?,?,?,?,?)", (paper_id, number, page.rect.width, page.rect.height, page.rotation))
            db.commit()
        store.progress(job_id, total, total, "正在核对笔记和译文的位置")
        return _publish_rebuilt_html(db, store, job_id, paper_id, total, toc)


def export_pdf(store: Store, paper_id: str, destination: Path):
    doc = fitz.open(store.original(paper_id))
    with store.connect() as db:
        annotations = db.execute("SELECT * FROM annotations WHERE paper_id=? ORDER BY created", (paper_id,)).fetchall()
        for annotation in annotations:
            targets = json.loads(annotation["targets"])
            content = annotation["note"]
            translated = json.loads(annotation["translated_targets"])
            if translated:
                content = "译文摘录：" + "\n".join(t.get("quote", "") for t in translated) + "\n\n" + content
            if not targets:
                page = doc[0]
                note = page.add_text_annot((24, 24), content or "阅读笔记")
                note.set_info(title="文献阅读器")
                note.update()
                continue
            grouped = defaultdict(list)
            for target in targets:
                block = db.execute("SELECT * FROM blocks WHERE id=? AND paper_id=?", (target["block_id"], paper_id)).fetchone()
                if not block:
                    continue
                page = doc[block["page"] - 1]
                for rectangle in rects_for_range(block, target["start"], target["end"]):
                    unrotated = fitz.Rect(rectangle) * page.derotation_matrix
                    grouped[block["page"]].append(fitz.Quad(unrotated.tl, unrotated.tr, unrotated.bl, unrotated.br))
            for page_number, quads in grouped.items():
                if not quads:
                    continue
                page = doc[page_number - 1]
                mark = page.add_highlight_annot(quads)
                mark.set_colors(stroke=COLORS.get(annotation["color"], COLORS["yellow"]))
                mark.set_opacity(.38)
                mark.set_info(title="文献阅读器", content=content or annotation["quote"])
                mark.update()
    doc.save(destination, garbage=3, deflate=True)
    doc.close()


def page_data(store: Store, paper, page_number: int, *, include_geometry: bool = True):
    from .layout import classify_page, describe_layout

    paper_id = paper["id"]
    with store.connect() as db:
        db.execute("BEGIN")
        row = db.execute("SELECT * FROM pages WHERE paper_id=? AND number=?", (paper_id, page_number)).fetchone()
        if not row:
            return None
        result = dict(row)
        attempted_ocr = bool(result.pop("ocr_job_id"))
        blocks = []
        for row in db.execute("""SELECT id,paper_id,page,position,kind,text,bbox,geometry,asset,section
                                 FROM blocks WHERE paper_id=? AND page=? ORDER BY position""", (paper_id, page_number)):
            b = dict(row)
            b["bbox"] = json.loads(b["bbox"])
            tr = db.execute("SELECT * FROM translations WHERE block_id=? AND active=1 ORDER BY created DESC LIMIT 1", (b["id"],)).fetchone()
            b["translation"] = dict(tr) if tr else None
            if b["translation"]:
                b["translation"]["segments"] = json.loads(b["translation"]["segments"])
            blocks.append(b)
    result["ocr"] = attempted_ocr and any(block["text"].strip() for block in blocks)
    tables, table_by_block, equations_by_block = classify_page(
        store.original(paper_id), paper, page_number, result["height"], blocks, width=result["width"], ocr=result["ocr"])
    if result["ocr"]:
        for block in blocks:
            if block["kind"] == "image" and fitz.Rect(block["bbox"]).get_area() >= result["width"] * result["height"] * .60:
                block["reading_role"] = "embedded"
    result["tables"] = describe_layout(store, paper, page_number, blocks, tables, table_by_block, equations_by_block)
    # Keep the character boxes server-side for HTML and use them only for the table mapping.
    for block in blocks:
        block["geometry"] = geometry(block["geometry"] if include_geometry else None)
    result["blocks"] = blocks
    return result
