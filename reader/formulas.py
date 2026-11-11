"""Keep the layout of equations and the original offsets of inline notation."""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache

import pymupdf as fitz

from .pdf import PDF_LAYOUT_LOCK, extract_block, length, recover_pdf_glyphs, slice_text

MATH_FONT = re.compile(r"math|\b(?:cmmi|cmsy|cmex)|\btx(?:sys|exs|mia)|stix|symbol", re.I)
MATH_GLYPH = re.compile(r"[\U0001d400-\U0001d7ff∑∫∏∈√∞≠≤≥≈±×÷]")
OPERATOR = re.compile(r"[=∑∫∏√≔≈≡≠≤≥±×÷]")
NUMBER = re.compile(r"(?:^|\s)[（(]([A-Z]?\d+(?:\.\d+)*[a-z]?)[)）]\s*$")
WORDS = re.compile(r"[A-Za-z]{2,}|[\u3400-\u9fff]{2,}")
OPERATORS_AS_WORDS = {"arg", "cos", "exp", "lim", "ln", "log", "max", "min", "mod", "sim", "sin", "tan"}
TOKEN = re.compile(r"\[\[M\d+\]\]")
KEEP_NOTATION = "math 中的标记代表原文数学符号。译文必须原样保留 text 中的每个 [[M数字]] 标记一次。"


@dataclass(frozen=True)
class Equation:
    bbox: tuple[float, ...]
    members: tuple[tuple[float, ...], ...]
    number: str | None


@dataclass(frozen=True)
class StyledText:
    bbox: tuple[float, ...]
    text: str
    styles: tuple[tuple[int, int, str], ...]


def page_notation(path, number):
    try:
        stat = path.stat()
        return _detect(str(path), stat.st_mtime_ns, stat.st_size, number)
    except (OSError, ValueError, RuntimeError, IndexError):
        return (), ()


def _raw_span_text(span):
    return "".join(char["c"] for char in span["chars"])


def _is_math(span, text):
    if MATH_GLYPH.search(text):
        return True
    font = span["font"].casefold()
    if "stix" in font and "math" not in font:
        # Publishers also set ordinary prose in STIX Regular, Bold and Italic.
        if any(word.lower() not in OPERATORS_AS_WORDS for word in WORDS.findall(text)):
            return False
        return bool(re.search(r"[=+−*/^<>\d]", text) or ("italic" in font and re.fullmatch(r"\s*[A-Za-z]\s*", text)))
    return bool(MATH_FONT.search(font))


def _span_styles(spans):
    visible = [(span, text) for span in spans if (text := _raw_span_text(span)).strip()]
    if not visible:
        return ()
    baseline, _ = max(visible, key=lambda item: (round(item[0]["size"], 1), len(item[1])))
    base_y, base_size = baseline["origin"][1], baseline["size"]
    styles = {}
    for index, (span, text) in enumerate(visible):
        shift = span["origin"][1] - base_y
        if span["size"] < base_size * .94 and .65 < abs(shift) < base_size * .75:
            styles[id(span)] = "sub" if shift > 0 else "sup"
        elif _is_math(span, text):
            # Some PDF fonts borrow the math hyphen glyph inside ordinary words.
            if (text == "-" and 0 < index < len(visible) - 1
                    and re.search(r"[A-Za-z]{2,}$", visible[index - 1][1])
                    and re.match(r"[A-Za-z]{2,}", visible[index + 1][1])):
                continue
            styles[id(span)] = "math"
    return styles


def _facts(block):
    text, prose, math_chars = [], [], 0
    for line in block["lines"]:
        for span in line["spans"]:
            value = _raw_span_text(span)
            text.append(value)
            if _is_math(span, value):
                math_chars += len(value.strip())
            else:
                prose.extend(word for word in WORDS.findall(value) if word.lower() not in OPERATORS_AS_WORDS)
        text.append(" ")
    return "".join(text).strip(), prose, math_chars


def _near(a, b, width, label=False):
    if a[2] < width * .51 < b[0] or b[2] < width * .51 < a[0]:
        return False
    horizontal = max(a[0] - b[2], b[0] - a[2])
    vertical = max(a[1] - b[3], b[1] - a[3])
    if label and abs((a[1] + a[3]) - (b[1] + b[3])) <= 14 and 0 <= b[0] - a[2] <= width * .48:
        return True
    return horizontal <= 64 and vertical <= 2.8


@lru_cache(maxsize=128)
def _detect(path, _modified, _size, number):
    with PDF_LAYOUT_LOCK, fitz.open(path) as document:
        page = document[number - 1]
        if page.rotation:
            return (), ()
        raw = [b for b in page.get_text("rawdict", flags=fitz.TEXTFLAGS_RAWDICT & ~fitz.TEXT_PRESERVE_IMAGES)["blocks"] if b["type"] == 0]
        recover_pdf_glyphs(page, raw)
        facts = [_facts(b) for b in raw]
        fragments = {i for i, (text, prose, math) in enumerate(facts) if len(text) <= 245 and len(prose) <= 2
                     and (math or OPERATOR.search(text) or NUMBER.search(text) or text.isdecimal())}
        groups = []
        for i, (text, _, math) in enumerate(facts):
            if i not in fragments or not OPERATOR.search(text) or (math < 2 and not re.search(r"\b[A-Za-z]\s*=\s*[^=]", text)):
                continue
            members, pending = {i}, [i]
            while pending:
                j = pending.pop()
                added = {k for k in fragments - members if _near(
                    raw[j]["bbox"], raw[k]["bbox"], page.rect.width, bool(NUMBER.fullmatch(facts[k][0])))}
                members.update(added)
                pending.extend(added)
            label = next((m[1] for k in sorted(members) if (m := NUMBER.search(facts[k][0]))), None)
            if not label and (len(members) == 1 and raw[i]["bbox"][2] - raw[i]["bbox"][0] > page.rect.width * .62):
                continue
            groups.append((members, label))
            fragments.difference_update(members)
        equations, excluded = [], set()
        for members, label in groups:
            rect = fitz.Rect(raw[min(members)]["bbox"])
            for k in members:
                rect |= fitz.Rect(raw[k]["bbox"])
            # Surrounding prose often overlaps a tall font's nominal bounding box.
            # Use the nearest actual baseline to avoid capturing the prose line.
            top, bottom = rect.y0 - 1, rect.y1 + 1
            for k, b in enumerate(raw):
                if k in members or not facts[k][1] or min(b["bbox"][2], rect.x1) <= max(b["bbox"][0], rect.x0):
                    continue
                for line in b["lines"]:
                    y = max((span["origin"][1] for span in line["spans"]), default=0)
                    if top - 4 <= y < rect.y0 + 2:
                        top = max(top, y + 3)
                    if rect.y1 - 2 < y <= bottom + 8 and line["bbox"][1] > top:
                        bottom = min(bottom, line["bbox"][1] + 1)
            crop = (fitz.Rect(rect.x0 - 4, top, rect.x1 + 4, bottom) & page.rect)
            if crop.height < 6 or crop.width < 12:
                continue
            boxes = tuple(tuple(round(v, 3) for v in raw[k]["bbox"]) for k in sorted(members))
            equations.append(Equation(tuple(crop), boxes, label))
            excluded.update(members)
        inline = []
        for i, block in enumerate(raw):
            if i in excluded or not any(_span_styles(line["spans"]) for line in block["lines"]):
                continue
            text, geom, _ = extract_block(block, page, span_styles=_span_styles)
            inline.append(StyledText(tuple(round(v, 3) for v in block["bbox"]), text, tuple(geom["styles"])))
        return tuple(equations), tuple(inline)


def notation_ranges(text, styles):
    """Join adjacent PDF glyphs into one immutable inline token."""
    runs = []
    for start, end, kind in styles:
        if runs and (runs[-1][1] == start or slice_text(text, runs[-1][1], start).isspace()):
            runs[-1][1] = end
        else:
            if start and kind in ("sub", "sup"):
                left = slice_text(text, 0, start)
                base = re.search(r"(?<![A-Za-z0-9_])[A-Za-z]{1,2} *$", left)
                if base:
                    start = max(runs[-1][1] if runs else 0, start - length(base[0]))
            runs.append([start, end])
    return runs


def mask_notation(block, text, offsets):
    if not block.get("notation") or TOKEN.search(text):
        return text, {}
    starts = {start: index for index, (start, _) in enumerate(offsets)}
    ends = {end: index + 1 for index, (_, end) in enumerate(offsets)}
    masked, tokens, cursor = [], {}, 0
    for start, end in notation_ranges(block["text"], block["notation"]):
        if start not in starts or end not in ends:
            continue
        a, b = starts[start], ends[end]
        marker = f"[[M{len(tokens) + 1}]]"
        masked.extend((text[cursor:a], marker))
        tokens[marker] = (start, end, text[a:b])
        cursor = b
    return "".join([*masked, text[cursor:]]), tokens


def keeps_notation(target, tokens):
    return not tokens or Counter(TOKEN.findall(target)) == Counter(tokens.keys())


def restore_notation(target, tokens):
    if not tokens:
        return target, []
    parts, spans, cursor, offset = [], [], 0, 0
    for match in TOKEN.finditer(target):
        prefix = target[cursor:match.start()]
        start, end, literal = tokens[match[0]]
        parts.extend((prefix, literal))
        offset += length(prefix)
        next_offset = offset + length(literal)
        spans.append({"source_start": start, "source_end": end, "target_start": offset, "target_end": next_offset, "notation": True})
        cursor, offset = match.end(), next_offset
    return "".join([*parts, target[cursor:]]), spans
