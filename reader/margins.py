"""Identify small, regularly numbered rails outside the page's body text."""
from __future__ import annotations

import re
import statistics
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

NUMBER = re.compile(r"\d{1,5}")
ANONYMOUS = re.compile(r"^(?:\d{1,4}\s+)?anon\.?(?:\s+\d{1,4})?$", re.I)
SUBMISSION = re.compile(r"^(?:\d{4}[.]\s*)?manuscript\s+submitted\s+to\s+acm(?:\s+\d{1,4})?[.]?$", re.I)
PRINTED_PAGE = re.compile(r"^(?:[-–—]\s*)?(?:page\s*)?(\d{1,5})(?:\s*(?:of|/)\s*\d{1,5})?(?:\s*[-–—])?$", re.I)
PAGE_OF = re.compile(r"\bpage\s*(\d{1,5})\s*(?:of|/)\s*\d{1,5}\b", re.I)
STRUCTURAL_EDGE = re.compile(r"^(?:(?:\d+(?:\.\d+)*[.\s]|[IVX]+[.]\s*|[A-Z][.]\s*))?(?:abstract|introduction|background|methods?|results?|discussion|conclusions?|references|bibliography|appendix|acknowledg(?:e)?ments|摘要|引言|方法|结果|讨论|结论|参考文献)\b", re.I)


def _signature(text):
    # Only standalone numbers vary. DOI components and dates remain evidence
    # for the same journal rather than collapsing unrelated edge paragraphs.
    text = re.sub(r"(?<![\w.])\d+(?![\w.])", "#", " ".join(text.split()).casefold())
    return "".join(char for char in text if char == "#" or char.isalnum())


@lru_cache(maxsize=32)
def _document_furniture(path, modified, size):
    import pymupdf as fitz
    from .pdf import PDF_LAYOUT_LOCK

    rows, groups = [], defaultdict(list)
    with PDF_LAYOUT_LOCK, fitz.open(path) as doc:
        count = len(doc)
        for number, page in enumerate(doc, 1):
            height = page.rect.height
            for block in page.get_text("dict")["blocks"]:
                if block["type"] != 0:
                    continue
                lines = block["lines"]
                if len(lines) > 1 and block["bbox"][3] - block["bbox"][1] <= height * .026:
                    # A folio and journal label often share one PDF block but
                    # occupy separate same-baseline lines (Springer Page n of m).
                    lines = [{"bbox": block["bbox"], "spans": [{"text": " ".join(
                        "".join(span["text"] for span in line["spans"]) for line in lines)}]}]
                for line in lines:
                    text = "".join(span["text"] for span in line["spans"]).strip()
                    box = tuple(line["bbox"])
                    top = box[3] <= height * .125
                    bottom = box[1] >= height * .90
                    if (not text or not (top or bottom) or len(text) > 240
                            or box[3] - box[1] > height * .026 or STRUCTURAL_EDGE.match(text)):
                        continue
                    signature = _signature(text)
                    row = {"text": text, "bbox": box, "page": number,
                           "element": "header" if top else "footer", "relative_y": box[1] / height,
                           "relative_x": (box[0] + box[2]) / (2 * page.rect.width)}
                    rows.append(row)
                    # Repeated headers must also occupy a consistent vertical
                    # rail. Mirrored odd/even x positions are deliberately fine.
                    group = next((key for key in groups if key[:2] == (row["element"], signature)
                                  and abs(key[2] - row["relative_y"]) <= .004), None)
                    groups[group or (row["element"], signature, row["relative_y"])].append(row)
    result = defaultdict(list)
    for (_, signature, _), items in groups.items():
        pages = {item["page"] for item in items}
        if len(pages) < (2 if count <= 3 else 3):
            continue
        pure_numbers = all(PRINTED_PAGE.fullmatch(item["text"]) for item in items)
        page_labels = all(PAGE_OF.search(item["text"]) for item in items)
        if pure_numbers:
            # Printed pagination may start at 380 or follow a publisher cover.
            # A table row has several numbers on one page, or fails this offset.
            offsets = {int(PRINTED_PAGE.fullmatch(item["text"])[1]) - item["page"] for item in items}
            if (len(items) != len(pages) or len(offsets) != 1
                    or not page_labels and not all(item["relative_x"] < .18 or .42 <= item["relative_x"] <= .58
                               or item["relative_x"] > .82 for item in items)):
                continue
        elif not page_labels and len(re.sub(r"[^\w]", "", signature.replace("#", ""))) < 7:
            continue
        for item in items:
            result[item["page"]].append({"text": item["text"], "bbox": item["bbox"],
                                         "element": "page_number" if pure_numbers or page_labels else item["element"]})
    return {page: tuple(items) for page, items in result.items()}


def document_furniture(path):
    """Repeated edge lines, cached by source file identity; never write the PDF."""
    if not path:
        return {}
    source = Path(path)
    try:
        info = source.stat()
        return _document_furniture(str(source.resolve()), info.st_mtime_ns, info.st_size)
    except (OSError, RuntimeError, ValueError):
        return {}


def page_margin_elements(blocks, height, number, title="", repeated=()):
    """Return index -> semantic element only for fully supported source blocks."""
    from .metadata import identity

    rows = [{"key": index, "text": block["text"], "bbox": block["bbox"]}
            for index, block in enumerate(blocks) if block.get("kind") != "image"]
    elements = {index: "line_number" for index in line_number_blocks(blocks, height)}
    for index in page_furniture_rows(rows, height, number, title):
        text, box = blocks[index]["text"].strip(), blocks[index]["bbox"]
        elements[index] = "page_number" if PRINTED_PAGE.fullmatch(text) else ("header" if box[3] < height * .5 else "footer")
    samples = repeated.get(number, ()) if isinstance(repeated, dict) else repeated
    for index, block in enumerate(blocks):
        if block.get("kind") == "image" or not block["text"].strip():
            continue
        box, text = block["bbox"], identity(block["text"])
        # PDF blocks may combine a journal name and printed page number into
        # one block. Cover it only when every character belongs to known lines.
        candidates = [sample for sample in samples if min(box[3], sample["bbox"][3]) > max(box[1], sample["bbox"][1])
                      and sample["bbox"][0] >= box[0] - 3 and sample["bbox"][2] <= box[2] + 3]
        candidates.sort(key=lambda item: (item["bbox"][1], item["bbox"][0]))
        matched = any(identity(sample["text"]) == text for sample in candidates)
        if not matched:
            matched = bool(candidates and identity(" ".join(sample["text"] for sample in candidates)) == text)
        if matched:
            kinds = {sample["element"] for sample in candidates}
            elements[index] = "page_number" if kinds == {"page_number"} else ("header" if box[3] < height * .5 else "footer")
    return elements


def is_running_section(text, title=""):
    from .metadata import identity

    text = " ".join(text.split())
    if ANONYMOUS.fullmatch(text) or SUBMISSION.fullmatch(text):
        return True
    without_number = re.sub(r"^\d{1,4}\s+|\s+\d{1,4}$", "", text)
    return bool(title and without_number != text and identity(without_number) == identity(title))


def page_furniture_rows(rows, height, number, title=""):
    from .metadata import identity

    hidden = set()
    if height <= 0:
        return hidden
    title_key = identity(title)
    for row in rows:
        text, box = " ".join(row["text"].split()), row["bbox"]
        if number == 1 and title_key and identity(text) == title_key:
            continue
        at_top = number > 1 and box[3] <= height * .13
        if (at_top and (is_running_section(text, title) or title_key and identity(text) == title_key)
                or box[1] >= height * .78 and SUBMISSION.fullmatch(text)):
            hidden.add(row["key"])
    for row in rows:
        text, box = row["text"].strip(), row["bbox"]
        if text != str(number):
            continue
        at_top = box[3] <= height * .13
        same_line = [other for other in rows if other["key"] != row["key"]
                     and min(box[3], other["bbox"][3]) > max(box[1], other["bbox"][1])]
        has_body_text = any(other["key"] not in hidden and other["text"].strip() for other in same_line)
        footer_label = any(other["key"] in hidden and SUBMISSION.fullmatch(" ".join(other["text"].split()))
                           for other in same_line)
        if ((at_top or box[1] >= height * .93) and not has_body_text) or footer_label:
            hidden.add(row["key"])
    return hidden


def line_number_rows(rows, page_height):
    body = [row for row in rows if len(row["text"].strip()) >= 24 and row["bbox"][2] - row["bbox"][0] >= 80]
    if not body or page_height <= 0:
        return set()
    left = min(row["bbox"][0] for row in body)
    right = max(row["bbox"][2] for row in body)
    normal = statistics.median(row["size"] for row in body)
    rails = defaultdict(list)
    for row in rows:
        text, box = row["text"].strip(), row["bbox"]
        if not NUMBER.fullmatch(text) or not 0 < row["size"] <= normal * .90:
            continue
        if box[2] <= left - 4:
            side = "left"
        elif box[0] >= right + 4:
            side = "right"
        else:
            continue
        if box[2] - box[0] <= row["size"] * 4:
            rails[side].append(row)

    hidden = set()

    def finish(run):
        if len(run) < 8 or run[-1]["bbox"][1] - run[0]["bbox"][1] < page_height * .25:
            return
        steps = [b["bbox"][1] - a["bbox"][1] for a, b in zip(run, run[1:])]
        spacing = statistics.median(steps)
        if all(abs(step - spacing) <= max(1.5, spacing * .15) for step in steps):
            hidden.update(row["key"] for row in run)

    for rail in rails.values():
        run, increment = [], None
        for row in sorted(rail, key=lambda item: item["bbox"][1]):
            if run:
                previous = run[-1]
                delta = int(row["text"].strip()) - int(previous["text"].strip())
                gap = row["bbox"][1] - previous["bbox"][1]
                aligned = min(abs(row["bbox"][edge] - previous["bbox"][edge]) for edge in (0, 2)) <= 2
                if not (delta in (1, 5) and (increment is None or delta == increment) and aligned
                        and 3 <= gap <= page_height * .13):
                    finish(run)
                    run, increment = [], None
                else:
                    increment = delta
            run.append(row)
        finish(run)
    return hidden


def line_number_blocks(blocks, page_height):
    from .pdf import geometry, length

    rows, fragments = [], defaultdict(set)
    for index, block in enumerate(blocks):
        if block.get("kind") == "image":
            continue
        text, box = block["text"], block["bbox"]
        raw_geometry = block.get("geometry")
        geo = raw_geometry if isinstance(raw_geometry, dict) else geometry(raw_geometry)
        boxes = geo["boxes"]
        sizes = [rectangle[3] - rectangle[1] for rectangle in boxes if rectangle]
        size = statistics.median(sizes) if sizes else box[3] - box[1]
        if re.fullmatch(r"[\d\s]+", text) and not NUMBER.fullmatch(text.strip()) and boxes:
            groups = defaultdict(lambda: {"text": "", "boxes": []})
            offset = 0
            for char in text:
                if offset < len(geo["lines"]):
                    group = groups[geo["lines"][offset]]
                    group["text"] += char
                    if rectangle := boxes[offset]:
                        group["boxes"].append(rectangle)
                offset += length(char)
            for line, group in groups.items():
                if not group["text"].strip() or not group["boxes"]:
                    continue
                rectangles = group["boxes"]
                bounds = [min(r[0] for r in rectangles), min(r[1] for r in rectangles),
                          max(r[2] for r in rectangles), max(r[3] for r in rectangles)]
                key = (index, line)
                rows.append({"key": key, "text": group["text"], "bbox": bounds, "size": size})
                fragments[index].add(key)
        else:
            key = (index, None)
            rows.append({"key": key, "text": text, "bbox": box, "size": size})
            fragments[index].add(key)
    hidden = line_number_rows(rows, page_height)
    return {index for index, keys in fragments.items() if keys <= hidden}


def page_margin_blocks(blocks, height, number, title="", repeated=()):
    rows = [{"key": index, "text": block["text"], "bbox": block["bbox"]}
            for index, block in enumerate(blocks) if block.get("kind") != "image"]
    if repeated:
        return set(page_margin_elements(blocks, height, number, title, repeated))
    return line_number_blocks(blocks, height) | page_furniture_rows(rows, height, number, title)
