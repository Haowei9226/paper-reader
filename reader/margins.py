"""Identify small, regularly numbered rails outside the page's body text."""
from __future__ import annotations

import re
import statistics
from collections import defaultdict

NUMBER = re.compile(r"\d{1,5}")
ANONYMOUS = re.compile(r"^(?:\d{1,4}\s+)?anon\.?(?:\s+\d{1,4})?$", re.I)
SUBMISSION = re.compile(r"^(?:\d{4}[.]\s*)?manuscript\s+submitted\s+to\s+acm(?:\s+\d{1,4})?[.]?$", re.I)


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


def page_margin_blocks(blocks, height, number, title=""):
    rows = [{"key": index, "text": block["text"], "bbox": block["bbox"]}
            for index, block in enumerate(blocks) if block.get("kind") != "image"]
    return line_number_blocks(blocks, height) | page_furniture_rows(rows, height, number, title)
