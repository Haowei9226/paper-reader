"""Retain narrowly identified non-body source anchors during a staged rebuild."""
from __future__ import annotations

import json
import re
import zlib
from collections import defaultdict
from pathlib import Path

from .frontmatter import frontmatter_elements
from .margins import document_furniture, page_margin_elements
from .reading import CAPTION


MATH = re.compile(r"[=+^<>∑∫∏√≔≈≡≠≤≥±×÷\u0370-\u03ff\U0001d400-\U0001d7ff]")
LABELED = re.compile(r"^(?:(?:fig(?:ure)?\.?|table)\s*[sS]?\d+(?:\.\d+)*[a-z]?|[图表]\s*[0-9一二三四五六七八九十]+)(?=\W|$)", re.I)
MARGIN_ELEMENTS = {"header", "footer", "page_number", "line_number"}


def _rows(values):
    return [dict(row) for row in (values.values() if isinstance(values, dict) else values)]


def _box(block):
    value = block["bbox"]
    return json.loads(value) if isinstance(value, str) else list(value)


def _contains(image, box):
    return all((image[0] <= box[0], image[1] <= box[1], image[2] >= box[2], image[3] >= box[3]))


def _cells(block):
    raw = block.get("geometry")
    geo = json.loads(zlib.decompress(raw)) if isinstance(raw, (bytes, bytearray, memoryview)) else raw
    boxes = geo["boxes"]
    if len(boxes) != len(block["text"].encode("utf-16-le")) // 2:
        raise ValueError("Invalid glyph geometry")
    result, offset = [], 0
    for char in block["text"]:
        count = len(char.encode("utf-16-le")) // 2
        if not char.isspace():
            box = boxes[offset]
            if box is None or any(value != box for value in boxes[offset:offset + count]):
                raise ValueError("Missing glyph geometry")
            result.append((char, tuple(box)))
        offset += count
    return result


def _margin_fragment(old, new):
    """A cropped running header can leave a same-ID folio/journal fragment."""
    try:
        text = new["text"].strip()
        if not text or text not in old["text"] or not _contains(_box(old), _box(new)):
            return False
        old_cells, new_cells = _cells(old), _cells(new)
        positions = defaultdict(list)
        for index, cell in enumerate(old_cells):
            positions[cell].append(index)
        previous = -1
        for cell in new_cells:
            matches = [index for index in positions.get(cell, ()) if index > previous]
            if len(matches) != 1:
                return False
            previous = matches[0]
        return bool(new_cells)
    except (KeyError, TypeError, ValueError, UnicodeError, zlib.error):
        return False


def _short_label(block):
    text = block["text"].strip()
    return (0 < len(text) <= 90 and len(re.findall(r"[A-Za-z]+", text)) <= 12
            and len(re.findall(r"[\u3400-\u9fff]", text)) <= 24
            and not re.search(r"[.!?。！？]\s*$", text) and not MATH.search(text))


def _table_image(image, captions):
    for caption in captions:
        box = caption["bbox"]
        overlap = min(image[2], box[2]) - max(image[0], box[0])
        if (overlap >= min(image[2] - image[0], box[2] - box[0]) * .6
                and (-5 <= box[1] - image[3] <= 65 or -5 <= image[1] - box[3] <= 35)):
            return True
    return False


def legacy_candidates(path, paper, old_blocks, page_meta):
    """Return stable block IDs and evidence; never alter source rows or a PDF.

    ``page_meta`` accepts page rows or a mapping from page number to its row.
    Absence of geometric/layout evidence leaves a block in the normal page
    fallback path. Existing legacy anchors retain their prior classification.
    """
    blocks = _rows(old_blocks)
    pages = {row["number"]: row for row in _rows(page_meta)}
    grouped = defaultdict(list)
    result = {}
    for block in blocks:
        if block["kind"] == "legacy":
            result[block["id"]] = "existing_legacy"
        else:
            projected = dict(block)
            projected["bbox"] = _box(block)
            grouped[block["page"]].append(projected)
    repeated = document_furniture(path)
    source = Path(path) if path else None
    for number, items in grouped.items():
        metadata = pages.get(number, {})
        width, height = metadata.get("width", 0), metadata.get("height", 0)
        if not width or not height:
            continue
        front, *_ = frontmatter_elements(items, paper, number, height)
        protected = {b["id"] for i, b in enumerate(items)
                     if (i in front and front[i] in ("title", "author", "affiliation")
                         or b["kind"] in ("caption", "table", "formula", "equation")
                         or b.get("reading_role") in ("caption", "table", "formula")
                         or CAPTION.match(b["text"].strip()) or LABELED.match(b["text"].strip())
                         or MATH.search(b["text"]))}
        margins = page_margin_elements(items, height, number, paper.get("title", ""), repeated)
        for index, element in margins.items():
            block = items[index]
            if (element in MARGIN_ELEMENTS and block["id"] not in protected
                    and (element == "line_number" or len(block["text"].strip()) <= 240)):
                result[block["id"]] = element

        # The display helper also expands figures toward their captions. Such
        # expanded regions cannot justify retaining an old source block here.
        table_captions = [b for b in items if re.match(r"^(?:table\s+\w|表\s*[0-9一二三])", b["text"].strip(), re.I)]
        images = [b["bbox"] for b in items if b["kind"] == "image"
                  and b["bbox"][2] - b["bbox"][0] >= 65 and b["bbox"][3] - b["bbox"][1] >= 25
                  and (b["bbox"][2] - b["bbox"][0]) * (b["bbox"][3] - b["bbox"][1]) < width * height * .60
                  and not _table_image(b["bbox"], table_captions)]
        labels = {b["id"] for b in items if b["kind"] != "image" and b["id"] not in protected
                  and _short_label(b) and any(_contains(image, b["bbox"]) for image in images)}
        if not labels:
            continue
        from .layout import figure_text_blocks

        if source and source.is_file():
            from .formulas import page_notation
            from .tables import page_tables

            tables = page_tables(source, number, items)
            equations, _ = page_notation(source, number)
            members = {box for equation in equations for box in equation.members}
            protected.update(b["id"] for b in items if tuple(b["bbox"]) in members
                             or any(table.contains(b["bbox"]) for table in tables))
        confirmed = figure_text_blocks(items, width, height, protected)
        result.update((bid, "figure_text") for bid in labels & confirmed if bid not in protected)
    return result


def retain_legacy_blocks(path, paper, old_blocks, new_blocks, page_meta, requested_ids=()):
    """Copy only requested confirmed anchors, plus previously retained legacy.

    A same-ID staged row can be replaced only at the exact old page/bounds.
    Source strings, geometry and annotation JSON are never adjusted here.
    """
    old, updated = _rows(old_blocks), _rows(new_blocks)
    candidates = legacy_candidates(path, paper, old, page_meta)
    requested = set(requested_ids) | {block["id"] for block in old if block["kind"] == "legacy"}
    positions = {block["id"]: index for index, block in enumerate(updated)}
    retained = {}
    for block in old:
        bid = block["id"]
        if bid not in requested or bid not in candidates:
            continue
        if bid in positions:
            staged = updated[positions[bid]]
            if (staged["page"] != block["page"] or _box(staged) != _box(block)
                    and not (candidates[bid] in MARGIN_ELEMENTS and _margin_fragment(block, staged))):
                continue
            updated[positions[bid]] = {**block, "kind": "legacy"}
        else:
            positions[bid] = len(updated)
            updated.append({**block, "kind": "legacy"})
        retained[bid] = candidates[bid]
    return updated, retained
