"""Locate source footnotes and endnotes using PDF typography and internal links."""
from __future__ import annotations

import json
import re
import statistics
from collections import defaultdict

import pymupdf as fitz

from .formulas import _span_styles
from .metadata import DOI_PATTERN, doi_name, identity
from .margins import ANONYMOUS, SUBMISSION
from .pdf import PDF_LAYOUT_LOCK, extract_block, geometry, length, recover_pdf_glyphs, slice_text
from .reading import REFERENCE_HEADING
from .references import (_author_date, _block_lines, _external_target, _http_urls, _link_library_entries,
                         _reference_number, _reference_text, ARXIV_ID, DATE, paper_references)


NOTE_HEADING = re.compile(r"^(?:(?:\d+(?:\.\d+)*[.、]?|[一二三四五六七八九十]+[、.])\s+)?(?:footnotes?|endnotes?|notes|注释|脚注|尾注)\s*[:：.]?$", re.I)
NOTE_START = re.compile(r"^(?:[\[［【(（](?P<wrapped>[0-9０-９]{1,4}|[一二三四五六七八九十]{1,5})[\]］】)）]|(?P<symbol>[*†‡§]{1,3})|(?P<number>[0-9０-９]{1,4})[.、)）]?)(?=\s|[^\W\d_]|$)\s*")
CONTINUED = re.compile(r"^(?:[(（]?continued[)）]?|[(（]?续[)）]?)[.:：]?\s*", re.I)
MARKER = re.compile(r"(?:[\[［(（]?[0-9]{1,4}[\]］)）]?|[*†‡§]{1,3})")
PROSE = re.compile(r"[A-Za-z]{3,}|[\u3400-\u9fff]{2,}")


def _anchor(block, start, end):
    return {"block_id": block["id"], "page": block["page"], "start": start, "end": end,
            "quote": slice_text(block["text"], start, end)}


def _boxes(block, start, end):
    return [box for box in block["geo"]["boxes"][start:end] if box]


def _bounds(boxes):
    return [min(box[0] for box in boxes), min(box[1] for box in boxes),
            max(box[2] for box in boxes), max(box[3] for box in boxes)]


def _label(value):
    value = value.strip("[]［］【】()（） .、")
    return str(_reference_number(value)) if value and all(char.isdecimal() or char in "一二三四五六七八九十" for char in value) else value


def _lines(blocks):
    result = []
    for block in blocks:
        for line in _block_lines(block):
            start, end = line["offset"], line["offset"] + length(line["text"])
            boxes = _boxes(block, start, end)
            if boxes:
                result.append({"block": block, "text": line["text"], "start": start, "end": end,
                               "bbox": _bounds(boxes), "size": statistics.median(box[3] - box[1] for box in boxes)})
    return result


def _add_line(note, line, content=None):
    block = line["block"]
    fragment = _anchor(block, line["start"], line["end"])
    if (note["fragments"] and note["fragments"][-1]["block_id"] == block["id"]
            and note["fragments"][-1]["end"] <= fragment["start"]
            and (note["fragments"][-1]["end"] == fragment["start"] or
                 slice_text(block["text"], note["fragments"][-1]["end"], fragment["start"]).isspace())):
        previous = note["fragments"][-1]
        note["fragments"][-1] = _anchor(block, previous["start"], fragment["end"])
    else:
        note["fragments"].append(fragment)
    note["parts"].append(line["text"] if content is None else content)


def _new_note(line, marker, kind):
    block, value = line["block"], marker[0].rstrip()
    end = line["start"] + length(value)
    anchor = _anchor(block, line["start"], end)
    note = {"id": f"note:{kind}:{block['id']}:{line['start']}", "kind": kind,
            "label": _label(marker[marker.lastgroup]), "anchor": anchor, "fragments": [], "parts": [],
            "bbox": line["bbox"], "size": line["size"], "label_style": marker.lastgroup}
    _add_line(note, line, line["text"][marker.end():])
    return note


def _endnotes(lines, outline):
    notes, occupied, current, active = [], set(), None, False
    last_number = None
    for line in lines:
        text, block = line["text"], line["block"]
        if NOTE_HEADING.fullmatch(text):
            if current:
                notes.append(current)
            current, active, last_number = None, True, None
            occupied.add((block["id"], line["start"]))
            continue
        if not active:
            continue
        marker = NOTE_START.match(text)
        if marker:
            value = _label(marker[marker.lastgroup])
            if value.isdecimal() and (last_number is None and int(value) != 1 or
                                      last_number is not None and not last_number < int(value) <= last_number + 20):
                marker = None
        if not marker and (REFERENCE_HEADING.fullmatch(text) or
                           block["kind"] == "heading" and (block["page"], identity(text)) in outline):
            if current:
                notes.append(current)
            current, active = None, False
            continue
        if marker:
            if current:
                notes.append(current)
            current = _new_note(line, marker, "endnote")
            if current["label"].isdecimal():
                last_number = int(current["label"])
        elif current:
            _add_line(current, line)
        occupied.add((block["id"], line["start"]))
    if current:
        notes.append(current)
    return notes, occupied


def _prose_marker(block, start, end):
    before = slice_text(block["text"], 0, start) if start else ""
    after = slice_text(block["text"], end, length(block["text"])) if end < length(block["text"]) else ""
    if len(PROSE.findall(block["text"])) < 2 or not before.strip():
        return False
    if re.search(r"(?:\b(?:eq(?:uation)?|fig(?:ure)?|table)|[=+*/^<>])\s*[.(]?\s*$", before, re.I):
        return False
    if after and after[0].isalnum():
        return False
    clause = re.split(r"\.(?=\s|$)|[!?。！？;；]", before)[-1]
    if re.search(r"[=+*/^<>]", clause):
        return False
    # Single-letter bases, units, powers of ten and short algebraic terms are not note cues.
    return bool(re.search(r"(?:[A-Za-z]{3,}(?:[-‐‑‒–—−]?\d+(?:\.\d+)*)?|[\u3400-\u9fff]{2,})[\s.!?。！？,，;；:：)）\]”’\"']*$", before))


def _raw_pdf_evidence(path, pages):
    superscripts, links, rules = [], [], defaultdict(list)
    try:
        with PDF_LAYOUT_LOCK, fitz.open(path) as doc:
            for page in doc:
                number = page.number + 1
                blocks = pages.get(number, [])
                coordinates = defaultdict(list)
                for block in blocks:
                    for index, box in enumerate(block["geo"]["boxes"]):
                        if box:
                            coordinates[tuple(box)].append((block, index))
                raw = page.get_text("rawdict", flags=fitz.TEXTFLAGS_RAWDICT & ~fitz.TEXT_PRESERVE_IMAGES)["blocks"]
                recover_pdf_glyphs(page, raw)
                for raw_block in raw:
                    if raw_block["type"] != 0:
                        continue
                    text, geo, _ = extract_block(raw_block, page, span_styles=_span_styles)
                    for start, end, style in geo["styles"]:
                        if style != "sup":
                            continue
                        quote = slice_text(text, start, end)
                        clean = quote.strip()
                        if not MARKER.fullmatch(clean):
                            continue
                        first = start + length(quote[:len(quote) - len(quote.lstrip())])
                        box = geo["boxes"][first]
                        if not box:
                            continue
                        for block, offset in coordinates[tuple(box)]:
                            stop = offset + length(clean)
                            if stop <= length(block["text"]) and slice_text(block["text"], offset, stop) == clean and _prose_marker(block, offset, stop):
                                superscripts.append({"label": _label(clean), "anchor": _anchor(block, offset, stop),
                                                     "bbox": _bounds(_boxes(block, offset, stop)), "evidence": "superscript"})
                for link in page.get_links():
                    if link["kind"] == fitz.LINK_GOTO and link.get("page", -1) >= 0 and isinstance(link.get("to"), fitz.Point):
                        target = link["page"] + 1
                        if target <= len(doc):
                            links.append({"page": number, "from": link["from"] * page.rotation_matrix,
                                          "target_page": target, "to": link["to"] * doc[target - 1].rotation_matrix})
                for drawing in page.get_drawings():
                    box = drawing["rect"] * page.rotation_matrix
                    if box.width >= 30 and box.height <= 2 and box.width <= page.rect.width * .85:
                        rules[number].append(box.y0)
    except (OSError, ValueError, RuntimeError, IndexError):
        pass
    return superscripts, links, rules


def _footnotes(lines, superscripts, links, rules, occupied, bibliography):
    notes = []
    by_page = defaultdict(list)
    cues = defaultdict(set)
    reference_starts = {(entry["anchor"]["block_id"], entry["anchor"]["start"]) for entry in bibliography}
    for cue in superscripts:
        cues[cue["anchor"]["page"]].add(cue["label"])
    for line in lines:
        by_page[line["block"]["page"]].append(line)
    for number, page_lines in by_page.items():
        body_sizes = [line["size"] for line in page_lines if len(PROSE.findall(line["text"])) >= 5
                      and line["bbox"][1] < line["block"]["height"] * .60]
        body_sizes = body_sizes or [line["size"] for line in page_lines if len(PROSE.findall(line["text"])) >= 5]
        normal = statistics.median(body_sizes) if body_sizes else 0
        candidates, boundaries = [], []
        for index, line in enumerate(page_lines):
            block = line["block"]
            if (block["id"], line["start"]) in occupied or (block["id"], line["start"]) in reference_starts:
                continue
            marker = NOTE_START.match(line["text"])
            if not marker or line["bbox"][1] < block["height"] * .45:
                continue
            value = _label(marker[marker.lastgroup])
            separator = any(0 <= line["bbox"][1] - y <= 45 for y in rules[number])
            linked = any(link["target_page"] == number and abs(link["to"].y - line["bbox"][1]) <= 35
                         for link in links)
            small = normal and line["size"] <= normal * .94 or separator
            if small:
                boundaries.append((index, line, marker))
            if (value in cues[number] or linked) and small:
                candidates.append((index, line, marker))
        for order, (index, line, marker) in enumerate(candidates):
            note = _new_note(line, marker, "footnote")
            stop = candidates[order + 1][0] if order + 1 < len(candidates) else len(page_lines)
            stop = min([stop, *(position for position, other, label in boundaries if position > index
                               and abs(other["bbox"][0] - line["bbox"][0]) < 6
                               and label.lastgroup == marker.lastgroup)])
            for continuation in page_lines[index + 1:stop]:
                box = continuation["bbox"]
                if ((continuation["block"]["id"], continuation["start"]) not in occupied
                        and box[1] >= line["bbox"][1] - 2 and continuation["size"] <= line["size"] * 1.18
                        and abs(box[0] - line["bbox"][0]) < 65
                        and not re.fullmatch(r"\d{1,4}", continuation["text"])):
                    _add_line(note, continuation)
            if len(PROSE.findall(_reference_text(note["parts"]))) >= 2:
                notes.append(note)
    return notes


def _continued_footnotes(notes, lines):
    by_page = defaultdict(list)
    for line in lines:
        by_page[line["block"]["page"]].append(line)
    for note in notes:
        if note["kind"] != "footnote":
            continue
        number = note["anchor"]["page"] + 1
        page_lines = by_page.get(number, [])
        for index, line in enumerate(page_lines):
            if line["bbox"][1] < line["block"]["height"] * .45 or line["size"] > note["size"] * 1.18:
                continue
            marker = NOTE_START.match(line["text"])
            content = line["text"][marker.end():] if marker and _label(marker[marker.lastgroup]) == note["label"] else line["text"]
            continuation = CONTINUED.match(content)
            if not continuation:
                continue
            _add_line(note, line, content[continuation.end():])
            for other in page_lines[index + 1:]:
                if (NOTE_START.match(other["text"]) or other["size"] > note["size"] * 1.18
                        or abs(other["bbox"][0] - line["bbox"][0]) >= 65):
                    break
                _add_line(note, other)
            break


def _inside_notes(anchor, notes):
    return any(fragment["block_id"] == anchor["block_id"] and fragment["start"] <= anchor["start"] < fragment["end"]
               for note in notes for fragment in note["fragments"])


def _confirmed_markers(notes, superscripts, links, pages):
    markers = []
    for cue in superscripts:
        anchor = cue["anchor"]
        if _inside_notes(anchor, notes):
            continue
        local = [note for note in notes if note["label"] == cue["label"] and note["kind"] == "footnote"
                 and note["anchor"]["page"] == anchor["page"] and note["bbox"][1] > cue["bbox"][3] + 4]
        candidates = local or [note for note in notes if note["label"] == cue["label"] and note["kind"] == "endnote"
                               and note["anchor"]["page"] >= anchor["page"]]
        if len(candidates) == 1:
            markers.append({"note_id": candidates[0]["id"], "anchor": anchor, "evidence": "superscript"})
    for link in links:
        candidates = [note for note in notes if note["anchor"]["page"] == link["target_page"]
                      and -8 <= note["bbox"][1] - link["to"].y <= 45
                      and note["bbox"][0] - 50 <= link["to"].x <= note["bbox"][2] + 50]
        if not candidates:
            continue
        distance = min(abs(note["bbox"][1] - link["to"].y) for note in candidates)
        candidates = [note for note in candidates if abs(note["bbox"][1] - link["to"].y) <= distance + 1]
        if len(candidates) != 1:
            continue
        note = candidates[0]
        for block in pages.get(link["page"], []):
            for match in MARKER.finditer(block["text"]):
                if _label(match[0]) != note["label"]:
                    continue
                start, end = length(block["text"][:match.start()]), length(block["text"][:match.end()])
                boxes = _boxes(block, start, end)
                if boxes and all((fitz.Rect(box) & link["from"]).get_area() >= fitz.Rect(box).get_area() * .45 for box in boxes):
                    anchor = _anchor(block, start, end)
                    if not _inside_notes(anchor, notes):
                        markers.append({"note_id": note["id"], "anchor": anchor, "evidence": "pdf_link"})
    by_range = defaultdict(list)
    for marker in markers:
        anchor = marker["anchor"]
        by_range[(anchor["block_id"], anchor["start"], anchor["end"])].append(marker)
    return [next((marker for marker in values if marker["evidence"] == "pdf_link"), values[0])
            for values in by_range.values() if len({marker["note_id"] for marker in values}) == 1]


def _note_references(db, paper, text, bibliography):
    references = []
    for match in re.finditer(DOI_PATTERN, text, re.I):
        doi = doi_name(match[0])
        url, kind = _external_target("", doi)
        references.append({"text": doi, "doi": doi, "url": url, "url_kind": kind})
    for url in _http_urls(text):
        if not doi_name(url):
            references.append({"text": url, "doi": "", "url": url, "url_kind": "url"})
    for match in ARXIV_ID.finditer(text):
        url, kind = _external_target(match[0], "")
        references.append({"text": match[0], "doi": "", "url": url, "url_kind": kind})
    for part in re.split(r"[;；]\s*", text):
        part = re.sub(r"^(?:see(?: also)?|cf\.?|参见|参阅|见)\s*[:：]?\s*", "", part, flags=re.I)
        parsed = _author_date(part)
        if parsed:
            names, year, abbreviated, aliases = parsed
            date = DATE.search(part)
            tail = part[date.end():].strip(" .,:;)）") if date else part
            bare = not tail or re.fullmatch(r"(?:pp?\.?|页)\s*[\d–—-]+", tail, re.I)
            authors = {identity(name) for name in [names[0], *aliases]}
            if len(names) == 2 and not abbreviated:
                authors.add(identity("".join(names)))
            resolved = any(len([entry for entry in bibliography if {"author": author, "year": year} in entry.get("citation_keys", [])]) == 1
                           for author in authors)
            if bare and resolved:
                continue
            doi = doi_name(part)
            url, kind = _external_target(part, doi)
            references.append({"text": part, "doi": doi, "url": url, "url_kind": kind})
    _link_library_entries(db, paper, references)
    numbered = {entry["number"]: entry for entry in bibliography if entry.get("numbered", True)}
    for group in re.finditer(r"[\[［【]([\d０-９\s,，、;；–—-]+)[\]］】]", text):
        if re.search(r"(?:\b(?:footnote|endnote|note)|脚注|尾注|注)\s*$", text[max(0, group.start() - 16):group.start()], re.I):
            continue
        labels = re.split(r"[,，、;；]", group[1])
        values = set()
        for label in labels:
            ends = re.split(r"[–—-]", label.strip())
            numbers = [_reference_number(value.strip()) for value in ends if value.strip()]
            if len(numbers) == 1:
                values.add(numbers[0])
            elif len(numbers) == 2 and 0 <= numbers[1] - numbers[0] <= 200:
                values.update(range(numbers[0], numbers[1] + 1))
        if values and values <= numbered.keys():
            references.extend(numbered[number] for number in sorted(values))
    found = []
    for entry in bibliography:
        parsed = _author_date(entry["text"])
        if not parsed:
            continue
        names, year, abbreviated, aliases = parsed
        forms = [names[0], *aliases]
        if len(names) == 2 and not abbreviated:
            forms.extend((names[0] + " and " + names[1], names[0] + " & " + names[1], names[0] + "和" + names[1]))
        for name in forms:
            pattern = r"(?<!\w)" + r"\s*".join(re.escape(part) for part in name.split()) + r"(?:\s+et\s+al\.?|等)?\s*(?:,\s*|[(（]\s*)" + re.escape(year) + r"\s*[)）]?"
            for match in re.finditer(pattern, text, re.I):
                found.append((match.start(), match.end(), entry))
    groups = defaultdict(list)
    for start, end, entry in found:
        if not any(other_start <= start and other_end >= end and (other_start, other_end) != (start, end)
                   for other_start, other_end, _ in found):
            groups[(start, end)].append(entry)
    for group in groups.values():
        unique = {entry["number"]: entry for entry in group}
        if len(unique) == 1:
            references.append(next(iter(unique.values())))
    unique = {}
    for reference in references:
        key = reference["library_match"]["paper_id"] if reference.get("library_match") else reference["url"]
        unique.setdefault(key, {field: reference.get(field) for field in
                               ("text", "url", "url_kind", "library_match", "library_match_method")})
    return list(unique.values())


def paper_notes(store, paper):
    with store.connect() as db:
        rows = db.execute("""SELECT b.*,p.width,p.height FROM blocks b JOIN pages p
            ON p.paper_id=b.paper_id AND p.number=b.page WHERE b.paper_id=? AND b.kind NOT IN ('image','legacy')
            ORDER BY b.page,b.position""", (paper["id"],))
        blocks = [{**row, "bbox": json.loads(row["bbox"]), "geo": geometry(row["geometry"])} for row in rows]
    pages = defaultdict(list)
    for block in blocks:
        pages[block["page"]].append(block)
    lines = _lines(blocks)
    lines = [line for line in lines if not SUBMISSION.fullmatch(line["text"])
             and not (line["bbox"][3] < line["block"]["height"] * .13 and ANONYMOUS.fullmatch(line["text"]))]
    outline = {(entry["page"], identity(entry["title"])) for entry in paper["outline"]}
    notes, occupied = _endnotes(lines, outline)
    bibliography = paper_references(store, paper)["entries"]
    superscripts, links, rules = _raw_pdf_evidence(store.original(paper["id"]), pages)
    notes.extend(_footnotes(lines, superscripts, links, rules, occupied, bibliography))
    _continued_footnotes(notes, lines)
    markers = _confirmed_markers(notes, superscripts, links, pages)
    entries = []
    with store.connect() as db:
        for note in notes:
            text = _reference_text(note["parts"])
            if not text.strip():
                continue
            entries.append({field: note[field] for field in ("id", "kind", "label", "anchor", "fragments")}
                           | {"text": text, "references": _note_references(db, paper, text, bibliography)})
    known = {entry["id"] for entry in entries}
    return {"entries": entries, "markers": [marker for marker in markers if marker["note_id"] in known]}
