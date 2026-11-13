"""Conservative, coordinate-based article credits; source strings stay untouched."""
from __future__ import annotations

import re
import statistics
from difflib import SequenceMatcher

from .metadata import identity

EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[a-z]{2,}", re.I)
AFFILIATION = re.compile(r"\b(?:universit(?:y|ies)|institut\w*|department|school|laborator\w*|research|college|faculty|academy|centre|center)\b|大学|学院|研究所|实验室", re.I)
ABSTRACT = re.compile(r"^(?:abstract|摘\s*要)(?:\s*[:：.—–-]|\s+(?=\S)|\s*$)", re.I)
PUBLICATION = re.compile(
    r"^(?:received\b|accepted\b|revised\b|published(?:\s+online)?\b|digital object identif\w*\b|"
    r"issn\s*:|to (?:cite|link to) this article\b|view (?:supplementary material|related articles|crossmark data)\b|"
    r"submit your article to this journal\b|article views\s*:|full terms\s*&\s*conditions\b|"
    r"vol\.\s*:\s*\(\d+\)|authorized licensed use limited to\b)", re.I,
)
SIDEBAR = re.compile(r"^(?:open access\s*$|edited by\b|reviewed by\b|[*∗]?\s*correspondence\b|citation\s*$|citation\s+[A-Z]|type\s+(?:original|research|review)|doi\s+10\.)", re.I)
COPYRIGHT = re.compile(r"^(?:copyright\s*(?:©|\d{4}|$)|©|[ß]\s*\d{4}|\d{4}-\d{4}\s+©)|\bcopyright\s+©", re.I)


def _size(block):
    raw = block.get("geometry")
    if raw and not isinstance(raw, dict):
        from .pdf import geometry
        raw = geometry(raw)
    boxes = (raw or {}).get("boxes", ())
    sizes = [r[3] - r[1] for r in boxes if r and r[3] > r[1]]
    return statistics.median(sizes) if sizes else min(24, block["bbox"][3] - block["bbox"][1])


def _author_keys(authors):
    keys = set()
    for author in re.split(r"[;；]", authors or ""):
        if key := identity(author):
            keys.add(key)
        if "," in author:
            parts = [part.strip() for part in author.split(",")]
            keys.add(identity(" ".join(parts[1:] + parts[:1])))
    return {key for key in keys if len(key) >= 5}


def _prose(text):
    words = re.findall(r"[A-Za-z]+", text)
    lower = sum(word[:1].islower() for word in words)
    return (len(words) >= 17 and lower >= len(words) * .72
            or len(words) >= 9 and lower >= len(words) * .72 and bool(re.search(r"[.!?]\s*$", text))
            or len(re.findall(r"[\u3400-\u9fff]", text)) >= 60)


def is_frontmatter_section(text, paper):
    """Legacy heading detectors sometimes stored a title/byline as section."""
    key = identity(text)
    if not key:
        return False
    if key in _author_keys(paper.get("authors", "")):
        return True
    title, source = identity(paper.get("title", "")), identity(paper.get("source", ""))
    return (len(key) >= 20 and key in title
            or len(source) >= 12 and source in key
            or bool(PUBLICATION.match(text.strip()) or COPYRIGHT.search(text.strip())))


def title_indices(blocks, paper, page, height):
    """Match geometric title lines, tolerating isolated PDF glyph omissions."""
    title = identity(paper.get("title", ""))
    authors = _author_keys(paper.get("authors", ""))
    # Some imported metadata includes the next byline. It is evidence for the
    # boundary, never a reason to append those names to the displayed title.
    cuts = [title.find(author) for author in authors if title.find(author) >= 35]
    if len(cuts) >= 2:
        title = title[:min(cuts)]
    if page not in (1, 2) or not title:
        return [], False, ""
    if page == 2 and not any(ABSTRACT.match(b["text"].strip()) for b in blocks):
        return [], False, ""
    candidates = sorted((i for i, b in enumerate(blocks)
                         if b["kind"] not in ("image", "legacy") and b.get("reading_role") != "embedded"
                         and b["bbox"][1] <= height * .38),
                        key=lambda i: (blocks[i]["bbox"][1], blocks[i]["bbox"][0]))
    for first in range(len(candidates)):
        text, joined = "", ""
        for last in range(first, min(first + 6, len(candidates))):
            block = blocks[candidates[last]]
            fragment = identity(block["text"])
            if not fragment:
                break
            text += block["text"].strip() + " "
            joined += fragment
            exact = joined == title
            near = (len(title) >= 35 and abs(len(joined) - len(title)) <= 3
                    and SequenceMatcher(None, joined, title).ratio() >= .97)
            mixed = joined.startswith(title) and any(key in joined[len(title):] for key in authors)
            if exact or near or mixed:
                indices = candidates[first:last + 1]
                if page == 2 and _size(blocks[indices[0]]) < 12:
                    break
                clean = " ".join(blocks[i]["text"].strip() for i in indices)
                if mixed:
                    # Exact title prefix length in Unicode text, not identity's
                    # punctuation-free offset; no source range is altered.
                    count = 0
                    for offset, char in enumerate(clean):
                        count += len(identity(char))
                        if count >= len(title):
                            clean = clean[:offset + 1]
                            break
                return indices, mixed, clean.strip()
            if len(joined) > len(title) + 4:
                break
            if len(joined) >= 15 and not title.startswith(joined) and SequenceMatcher(None, joined, title[:len(joined)]).ratio() < .92:
                break
    return [], False, ""


def frontmatter_elements(blocks, paper, page, height):
    """Return index -> element, title indices, mixed flag and clean title."""
    titles, mixed, clean = title_indices(blocks, paper, page, height)
    elements = {index: "title" for index in titles}
    if page not in (1, 2):
        return elements, titles, mixed, clean
    title_bottom = max((blocks[i]["bbox"][3] for i in titles), default=0)
    title_left = min((blocks[i]["bbox"][0] for i in titles), default=0)
    title_right = max((blocks[i]["bbox"][2] for i in titles), default=float("inf"))
    source_key = identity(paper.get("source", ""))
    # These explicit labels identify publisher metadata, not arbitrary prose
    # containing words such as "received" or "copyright" in the article body.
    for i, block in enumerate(blocks):
        if i in elements or block["kind"] in ("image", "legacy") or block.get("reading_role") == "embedded":
            continue
        text, box = block["text"].strip(), block["bbox"]
        side = bool(titles and (box[2] < title_left - 10 or box[0] > title_right + 10))
        if (page == 1 and side and box[3] - box[1] > (box[2] - box[0]) * 8
                and re.search(r"©|copyright|\bdoi\s*:", text, re.I)):
            elements[i] = "copyright"
        elif COPYRIGHT.search(text) and (page == 1 or box[1] > height * .85):
            elements[i] = "copyright"
        elif (page == 1 or box[1] > height * .85) and PUBLICATION.match(text):
            if not re.match(r"^(?:received|accepted|revised|published)\b", text, re.I) or re.search(r"\d{4}", text[:65]):
                elements[i] = "publication"
        elif page == 1 and (side or box[3] < title_bottom) and SIDEBAR.match(text):
            elements[i] = "publication"
        elif (titles and box[3] < min(blocks[j]["bbox"][1] for j in titles) + 2
              and (len(source_key) >= 12 and source_key in identity(text)
                   or box[3] < height * .13 and re.match(r"^proceedings of .*(?:conference|symposium)", text, re.I)
                   or re.fullmatch(r"[A-Z][a-z]+\s*[–—-]\s*\d{4}", text))):
            elements[i] = "publication"
    for i, block in enumerate(blocks):
        box = block["bbox"]
        if (i not in elements and EMAIL.search(block["text"]) and box[2] < title_left - 10
                and any(element == "publication" and blocks[j]["bbox"][2] < title_left - 10
                        and -3 <= box[1] - blocks[j]["bbox"][3] <= 20 for j, element in elements.items())):
            elements[i] = "publication"
    abstracts = [b for b in blocks if ABSTRACT.match(b["text"].strip())]
    for i, block in enumerate(blocks):
        if (titles and i not in elements and re.match(r"^(?:keywords|key words|index terms)\s+\S", block["text"].strip(), re.I)
                and any(min(block["bbox"][3], b["bbox"][3]) > max(block["bbox"][1], b["bbox"][1])
                        and (block["bbox"][0] > b["bbox"][2] or block["bbox"][2] < b["bbox"][0]) for b in abstracts)):
            elements[i] = "keywords"
    if not titles:
        return elements, titles, mixed, clean
    # Bound the author band geometrically across all columns. An unlabelled
    # abstract or a teaser figure terminates it just as an Abstract heading does.
    boundaries = [b["bbox"][1] for i, b in enumerate(blocks)
                  if b["bbox"][1] >= title_bottom - 2 and i not in elements
                  and (ABSTRACT.match(b["text"].strip()) or _prose(b["text"])
                       or b["kind"] == "image" and b["bbox"][3] - b["bbox"][1] > 30
                       or re.match(r"^(?:\d+[. ]|[IVX]+\.\s*)?(?:introduction|引言)\b", b["text"].strip(), re.I))]
    body_top = min(boundaries, default=min(height * .55, title_bottom + 200))
    band = {i for i, b in enumerate(blocks) if i not in elements and b["kind"] not in ("image", "legacy")
            and title_bottom - 3 <= b["bbox"][1] < body_top and b["bbox"][3] <= body_top + 3
            and len(b["text"].strip()) < 650}
    authors = _author_keys(paper.get("authors", ""))
    seeds = set()
    for i in band:
        text = blocks[i]["text"]
        if any(author in identity(EMAIL.sub("", text)) for author in authors):
            elements[i] = "author"
            seeds.add(i)
        elif EMAIL.search(text):
            elements[i] = "affiliation"
            seeds.add(i)
    if seeds or mixed:
        for i in band - elements.keys():
            if AFFILIATION.search(blocks[i]["text"]) and not _prose(blocks[i]["text"]):
                elements[i] = "affiliation"
                seeds.add(i)
    # City/country and wrapped institution fragments need their nearby credit
    # evidence. Do not hide every short sentence above the first section.
    if seeds or mixed:
        changed = True
        while changed:
            changed = False
            for i in band - elements.keys():
                a = blocks[i]["bbox"]
                if _prose(blocks[i]["text"]):
                    continue
                if any(min(a[2], (b := blocks[j]["bbox"])[2]) - max(a[0], b[0]) > 8
                       and -3 <= a[1] - b[3] <= 20 for j in seeds):
                    elements[i] = "affiliation"
                    seeds.add(i)
                    changed = True
    return elements, titles, mixed, clean
