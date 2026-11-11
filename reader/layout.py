"""Project the PDF layout into HTML without modifying the library's text anchors."""
from __future__ import annotations

import hashlib
import os
import tempfile

import pymupdf as fitz

from .formulas import page_notation
from .pdf import PDF_LAYOUT_LOCK, geometry, length, reading_order, slice_text
from .reading import classify_reading_blocks
from .tables import cell_ranges, page_tables


def classify_page(path, paper, number, height, blocks, *, width, detect=True, ocr=False):
    # The OCR recognizer supplies the reading order, including the order of columns.
    if not ocr:
        blocks[:] = reading_order(blocks, width)
    detect = detect and not ocr
    tables = page_tables(path, number, blocks) if detect else ()
    equations, notation = page_notation(path, number) if detect else ((), ())
    members = {box: equation for equation in equations for box in equation.members}
    styled = {(item.bbox, item.text): item.styles for item in notation}
    table_by_block, equations_by_block = {}, {}
    caption_ids = {table.caption_id for table in tables if table.caption_id}
    for block in blocks:
        if block["kind"] == "image" or block["id"] in caption_ids:
            continue
        box = tuple(block["bbox"])
        if styles := styled.get((box, block["text"])):
            block["notation"] = styles
        if table := next((table for table in tables if table.contains(box)), None):
            table_by_block[block["id"]] = table
        elif equation := members.get(box):
            equations_by_block[block["id"]] = equation
    classify_reading_blocks(blocks, paper, number, height, table_by_block, equations_by_block, caption_ids)
    if number == 1:
        titles = sorted((block for block in blocks if block["reading_role"] == "title"),
                        key=lambda block: (block["bbox"][1], block["bbox"][0]))
        if titles:
            blocks[:] = titles + [block for block in blocks if block["reading_role"] != "title"]
    for position, block in enumerate(blocks):
        block["position"] = position
        if block["kind"] == "image" and any(table.contains(block["bbox"]) for table in tables):
            block["reading_role"] = "embedded"
    return tables, table_by_block, equations_by_block


def _crop(store, paper_id, number, box, kind):
    source = store.original(paper_id)
    digest = hashlib.sha256(repr((number, box, source.stat().st_mtime_ns)).encode()).hexdigest()[:20]
    asset = f"{paper_id}/{kind}-{digest}.png"
    target = store.root / "assets" / asset
    with PDF_LAYOUT_LOCK:
        if not target.is_file():
            target.parent.mkdir(exist_ok=True)
            with fitz.open(source) as doc:
                page = doc[number - 1]
                rect = fitz.Rect(box)
                if kind == "table":
                    rect = (rect + (-1, -1, 1, 1)) & page.rect
                data = page.get_pixmap(clip=rect, matrix=fitz.Matrix(2.5, 2.5), annots=False).tobytes("png")
            with tempfile.NamedTemporaryFile(dir=target.parent, suffix=".tmp", delete=False) as temporary:
                temporary.write(data)
            os.replace(temporary.name, target)
    return asset


def cell_translation(block, start, end):
    translation = block.get("translation")
    if not translation:
        return None
    if translation["manual"] and slice_text(block["text"], start, end) == block["text"].strip():
        return {"target_start": 0, "target_end": length(translation["text"])}
    for span in translation["segments"]:
        if span["source_start"] == start and span["source_end"] == end:
            a, b = span["target_start"], span["target_end"]
            if 0 <= a < b <= length(translation["text"]):
                return span
    return None


def map_table_blocks(table, blocks, boxes):
    spans = {}
    for block in blocks:
        mapped = cell_ranges(block["text"], boxes(block), table)
        if not mapped:
            return {}
        spans[block["id"]] = mapped
    return spans


def table_cell_translations(mapped, blocks):
    """Only reuse a joined translation when its parts still occupy one cell."""
    matches, group_cells = {}, {}
    for block in blocks:
        bid = block["id"]
        cells = {(start, end): (row, col) for start, end, row, col in mapped.get(bid, ())}
        for span in (block.get("translation") or {}).get("segments", ()):
            if marker := span.get("cell_group"):
                group_cells.setdefault(marker, set()).add(cells.get((span["source_start"], span["source_end"])))
        for start, end in cells:
            if span := cell_translation(block, start, end):
                matches[bid, start, end] = span
    valid = {marker for marker, cells in group_cells.items() if len(cells) == 1 and None not in cells}
    return {key: span for key, span in matches.items() if not span.get("cell_group") or span["cell_group"] in valid}


def describe_layout(store, paper, number, blocks, tables, table_by_block, equations_by_block):
    paper_id = paper["id"]
    table_groups = {table: [] for table in tables}
    equation_groups = {}
    for block in blocks:
        bid = block["id"]
        if table := table_by_block.get(bid):
            table_groups[table].append(block)
        if equation := equations_by_block.get(bid):
            equation_groups.setdefault(equation, []).append(block)
    for equation, group in equation_groups.items():
        group[0]["equation"] = {
            "asset": _crop(store, paper_id, number, equation.bbox, "formula"),
            "number": equation.number, "block_ids": [b["id"] for b in group],
            "width": round(equation.bbox[2] - equation.bbox[0]),
            "height": round(equation.bbox[3] - equation.bbox[1]),
        }
    result = []
    positions = {b["id"]: b["position"] for b in blocks}
    by_id = {b["id"]: b for b in blocks}
    for index, (table, group) in enumerate(table_groups.items()):
        parts = {}
        mapped = map_table_blocks(table, group, lambda b: geometry(b["geometry"]))
        translations = table_cell_translations(mapped, group)
        for block in sorted(group, key=lambda b: (b["bbox"][1], b["bbox"][0])):
            for start, end, row, col in mapped.get(block["id"], ()):
                span = translations.get((block["id"], start, end))
                target = (span["target_start"], span["target_end"]) if span else None
                part = {"block_id": block["id"], "start": start, "end": end, "target": target,
                        "group": span.get("cell_group") if span else None}
                parts.setdefault((row, col), []).append(part)
        cells = []
        for cell in table.cells if mapped else ():
            content = parts.get((cell.row, cell.column), [])
            groups = {part.pop("group") for part in content}
            joined = len(content) > 1 and len(groups) == 1 and None not in groups
            if cell.row < table.header_rows and len(content) > 1 and not joined and sum(p["end"] - p["start"] for p in content) <= 350:
                originals = [by_id[part["block_id"]] for part in content]
                if not any(b.get("notation") or (b.get("translation") or {}).get("manual") for b in originals):
                    for part in content:
                        part["target"] = None
            cells.append({"row": cell.row, "column": cell.column, "rowspan": cell.rowspan, "colspan": cell.colspan,
                          "parts": content, "joined": joined})
        result.append({
            "id": f"table-{number}-{index + 1}", "asset": _crop(store, paper_id, number, table.bbox, "table"),
            "caption_id": table.caption_id, "block_ids": [b["id"] for b in group], "cells": cells,
            "position": min((block["position"] for block in group), default=positions.get(table.caption_id, len(blocks))),
            "rows": len(table.rows) - 1 if cells else 0, "columns": len(table.columns) - 1 if cells else 0,
            "header_rows": table.header_rows if cells else 0,
            "widths": [(b - a) / (table.bbox[2] - table.bbox[0]) * 100 for a, b in zip(table.columns, table.columns[1:])],
        })
    return result
