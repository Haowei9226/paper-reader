"""Infer display paragraph boundaries from source geometry without changing anchors."""
from __future__ import annotations

import json
import re
import statistics
import zlib


ITEM = re.compile(r"^\s*(?:[•◦●▪▫‣]|(?:\d{1,3}|[A-Za-z])[).、]\s|\((?:[A-Za-z]|\d{1,3}|(?-i:[ivx]{2,6}))\)\s|RQ\d+[a-z]?[.:：]\s)", re.I)
NOTE = re.compile(r"^\s*(?:\d{1,3}(?=[^\d\s]|\s+\S)|[*†‡§])")
ABBREVIATION = re.compile(r"\b(?:et\s+al|e\.g|i\.e|vs|dr|mr|mrs|prof|fig|eq)\.[\s)\]’”]*$", re.I)
END = re.compile(r"[.!?。！？][\s\"'”’）)\]」』]*$")


def _slice(text, start, end):
    return text.encode("utf-16-le")[start * 2:end * 2].decode("utf-16-le")


def source_lines(block):
    data = block.get("geometry") or {}
    if isinstance(data, bytes):
        data = json.loads(zlib.decompress(data))
    elif isinstance(data, str):
        data = json.loads(data)
    boxes, rows = data.get("boxes", ()), data.get("lines", ())
    grouped, offset = {}, 0
    for char in block["text"]:
        count = 2 if ord(char) > 0xffff else 1
        if offset < len(boxes) and offset < len(rows) and boxes[offset] and not char.isspace():
            row = grouped.setdefault(rows[offset], {"start": offset, "end": offset + count, "boxes": []})
            row["end"] = offset + count
            row["boxes"].append(boxes[offset])
        offset += count
    result = []
    for row in grouped.values():
        bounds = row.pop("boxes")
        result.append({**row, "bbox": [min(b[0] for b in bounds), min(b[1] for b in bounds),
                                      max(b[2] for b in bounds), max(b[3] for b in bounds)],
                       "size": statistics.median(b[3] - b[1] for b in bounds),
                       "text": _slice(block["text"], row["start"], row["end"])})
    if not result and block["text"].strip():
        result.append({"start": 0, "end": offset, "bbox": block["bbox"],
                       "size": min(24, block["bbox"][3] - block["bbox"][1]), "text": block["text"]})
    return result


def _finished(text):
    return bool(END.search(text.rstrip()) and not ABBREVIATION.search(text.rstrip()))


def _breaks(lines):
    advances = [b["bbox"][1] - a["bbox"][1] for a, b in zip(lines, lines[1:])
                if 0 < b["bbox"][1] - a["bbox"][1] < a["size"] * 3
                and abs(a["bbox"][0] - b["bbox"][0]) < a["size"] * 3]
    advance = statistics.median(sorted(advances)[:max(1, (len(advances) * 2 + 2) // 3)]) if advances else 0
    result = []
    for a, b in zip(lines, lines[1:]):
        size = a["size"]
        if not size or not .8 <= b["size"] / size <= 1.2 or b["bbox"][1] <= a["bbox"][1]:
            continue
        indent = b["bbox"][0] - a["bbox"][0]
        gap = b["bbox"][1] - a["bbox"][3]
        spaced = len(lines) >= 3 and advance and b["bbox"][1] - a["bbox"][1] > advance * 1.45 and gap > size * .65
        indented = indent > max(4.5, size * .6) and _finished(a["text"]) and not ITEM.match(a["text"])
        if ITEM.match(b["text"]) or spaced or indented:
            result.append(b["start"])
    return result


def refine_reading_flow(blocks, height):
    """Separate real paragraphs and continue text around floating illustrations."""
    key = lambda block: block.get("id", id(block))
    lines = {key(b): source_lines(b) for b in blocks if b.get("kind") != "image"}
    sizes = [row["size"] for b in blocks if b.get("reading_role") == "text" and len(b["text"]) > 70
             for row in lines.get(key(b), ()) if row["size"] > 0]
    if not sizes:
        sizes = [row["size"] for b in blocks if b.get("reading_role") == "text" and len(b["text"]) > 25
                 for row in lines.get(key(b), ()) if row["size"] > 0]
    normal = statistics.median(sizes) if sizes else 0
    note_columns = []
    for block in blocks:
        rows = lines.get(key(block), ())
        if block.get("reading_role") != "text" or not rows:
            continue
        size = statistics.median(row["size"] for row in rows)
        small = normal and size <= normal * .84 and block["bbox"][1] >= height * .63
        follows = any(abs(block["bbox"][0] - x) <= size * 3 and block["bbox"][1] >= y - 2 for x, y in note_columns)
        if small and (NOTE.match(block["text"]) or follows):
            block["reading_role"], block["reading_element"] = "extra", "footnote"
            note_columns.append((block["bbox"][0], block["bbox"][3]))
        else:
            boundaries = sorted(set([*block.get("reading_breaks", ()), *_breaks(rows)]))
            if boundaries:
                block["reading_breaks"] = boundaries

    previous = None
    for block in blocks:
        joined = block.pop("continues_previous", False)
        role = block.get("reading_role")
        if role != "text":
            if role in ("title", "heading", "subheading", "table", "formula", "reference"):
                previous = None
            continue
        current_rows = lines.get(key(block), ())
        if previous and current_rows and lines.get(key(previous)):
            a, b = lines[key(previous)][-1], current_rows[0]
            size, other = a["size"], b["size"]
            drop_cap = bool(previous.get("reading_heading") and re.fullmatch(r"\s*[A-Z]\s*", a["text"]))
            if (drop_cap and size and .2 <= other / size <= 1.28
                    and abs(b["bbox"][1] - a["bbox"][1]) <= max(size, other)
                    and a["bbox"][0] - other <= b["bbox"][0] <= a["bbox"][2] + other * 2):
                block["continues_previous"] = True
                previous = block
                continue
            if size and .78 <= other / size <= 1.28 and not ITEM.match(b["text"]):
                left, right = a["bbox"], b["bbox"]
                gap, indent = right[1] - left[3], right[0] - left[0]
                tail = _slice(previous["text"], previous.get("reading_breaks", [0])[-1],
                              len(previous["text"].encode("utf-16-le")) // 2)
                hanging = bool(ITEM.match(tail) and not _finished(a["text"]) and 0 <= indent <= size * 4)
                drop_cap = bool(previous.get("reading_heading") and re.fullmatch(r"\s*[A-Z]\s*", tail))
                same_column = right[1] >= left[1] - size * .2 and gap <= size * .85 and gap >= -size * .7
                aligned = abs(indent) <= max(3, size * .45) or -size * 1.8 <= indent < 0 or hanging or drop_cap
                cross_column = right[1] < left[1] - size * 2 and right[0] > left[2] - size * .5
                if cross_column:
                    starts = [x["bbox"][0] for candidate in blocks if candidate.get("reading_role") == "text"
                              for x in lines.get(key(candidate), ())
                              if abs(x["bbox"][0] - right[0]) < size * 3 and len(x["text"]) > 25]
                    aligned = not starts or right[0] <= min(starts) + size * .6
                # A PDF block is usually an actual paragraph. Do not merge two
                # complete paragraphs just because their boxes happen to touch.
                incomplete = len(a["text"].strip()) >= 8 and not _finished(a["text"])
                wrapped = len(lines[key(previous)]) == 1 and joined
                if (same_column and aligned or cross_column and aligned) and (incomplete or wrapped or hanging or drop_cap):
                    block["continues_previous"] = True
        previous = block
