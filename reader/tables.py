"""Recover a table grid without replacing its original PDF text anchors."""
from __future__ import annotations

import bisect
import inspect
import re
from dataclasses import dataclass
from functools import lru_cache

import pymupdf as fitz

from .pdf import PDF_LAYOUT_LOCK

TABLE_LABEL = r"(?:table\s+s?\d+(?:\.\d+)*[a-z]?|表\s*[0-9一二三四五六七八九十]+)"
CAPTION = re.compile(r"^" + TABLE_LABEL + r"(?:\s*[:：.、–—-]|\s*$)", re.I)
LABELED_LINE = re.compile(r"^" + TABLE_LABEL + r"\s+", re.I)
IDENTIFIER_NUMBER = r"\d+(?:\.\d+)*[A-Za-z]?"
IDENTIFIER = r"[-_#]?" + IDENTIFIER_NUMBER
VALUE = re.compile(r"(?:[\d+−±×÷≤≥<>=%‰./,():\[\]–—*†‡\s-]+|[A-Za-z]{1,7}" + IDENTIFIER +
                   r"|[A-Za-z]{1,5}#|[A-Z]{1,4}|[pnrFtz]\s*[<>=≤≥]\s*[\d.]+)$")
NUMBERS = re.compile(r"(?<![A-Za-z0-9_])[A-Z]{1,7}(?:[_#]?|[ \t]*-[ \t]*)" + IDENTIFIER_NUMBER +
                     r"(?![A-Za-z0-9_])|[+−-]?(?:\d+(?:[.,]\d+)*|\.\d+)(?:[%‰])?|[±≤≥≠<>×÷†‡]")
ACADEMIC_YEAR = re.compile(r"\d+(?:st|nd|rd|th)\s+Year\s+(?:Undergrad(?:uate)?|PhD|Master(?:'s)?)", re.I)
CHINESE_YEARS = "一二三四五六七八九十"
CHINESE_YEAR = re.compile(rf"(?<![{CHINESE_YEARS}])([{CHINESE_YEARS}])(?=\s*(?:年级|学年))")
CHINESE_PHASE = re.compile(rf"第\s*([{CHINESE_YEARS}])\s*阶段")
_NATIVE_OPTIONS = {"use_layout": False} if "use_layout" in inspect.signature(fitz.Page.find_tables).parameters else {}


@dataclass(frozen=True)
class Cell:
    row: int
    column: int
    rowspan: int = 1
    colspan: int = 1


@dataclass(frozen=True)
class Table:
    bbox: tuple[float, ...]
    columns: tuple[float, ...]
    rows: tuple[float, ...]
    cells: tuple[Cell, ...]
    header_rows: int
    headings: tuple[str, ...]
    caption_id: str | None
    caption: str

    def contains(self, bbox):
        x0, y0, x1, y1 = self.bbox
        a, b, c, d = bbox
        overlap = max(0, min(c, x1 + 2) - max(a, x0 - 2)) * max(0, min(d, y1 + 2) - max(b, y0 - 2))
        return overlap >= .7 * max(1, (c - a) * (d - b))


def page_tables(path, number, blocks):
    captions = tuple((block["id"], tuple(block["bbox"]), block["text"].strip()[:450])
                     for block in blocks if CAPTION.match(block["text"].strip()) or LABELED_LINE.match(block["text"].strip()))
    try:
        stat = path.stat()
        return _detect(str(path), stat.st_mtime_ns, stat.st_size, number, captions)
    except (OSError, ValueError, RuntimeError, AttributeError, IndexError):
        return ()


def _rules(paths):
    horizontal, vertical = [], []
    for path in paths:
        for item in path["items"]:
            if item[0] == "l":
                a, b = item[1:]
                if abs(a.y - b.y) < .75 and abs(a.x - b.x) >= 45:
                    horizontal.append((min(a.x, b.x), max(a.x, b.x), (a.y + b.y) / 2))
                if abs(a.x - b.x) < .75 and abs(a.y - b.y) >= 12:
                    vertical.append((a.x, min(a.y, b.y), max(a.y, b.y)))
            elif item[0] == "re":
                box = item[1]
                if box.width >= 45 and box.height < 2:
                    horizontal.append((box.x0, box.x1, (box.y0 + box.y1) / 2))
                elif path["type"] != "f" and box.width >= 18 and box.height >= 12:
                    horizontal.extend((box.x0, box.x1, y) for y in (box.y0, box.y1))
                    vertical.extend((x, box.y0, box.y1) for x in (box.x0, box.x1))
    return sorted(horizontal), vertical


def _caption_distance(box, caption):
    x0, y0, x1, y1 = box
    a, b, c, d = caption[1]
    if min(x1, c) - max(x0, a) < .55 * min(x1 - x0, c - a):
        return float("inf")
    above, below = y0 - d, b - y1
    gap = max(above, below)
    return abs(gap) if -3 <= gap <= 24 else float("inf")


def _rule_bands(horizontal, captions):
    groups = []
    for left, right, y in horizontal:
        match = next((g for g in groups if abs(left - g[0]) <= 3 and abs(right - g[1]) <= 3), None)
        if match:
            match[2].append(y)
        else:
            groups.append((left, right, [y]))
    candidates = []
    for left, right, ys in groups:
        if len(ys) < 2:
            continue
        neighboring = [c for c in captions if min(right, c[1][2]) - max(left, c[1][0]) >= .55 * min(right - left, c[1][2] - c[1][0])]
        # Separate tables in the same column at their intervening caption.
        cuts = sorted((c[1][1] + c[1][3]) / 2 for c in neighboring)
        groups_by_caption = {}
        for y in sorted(ys):
            band = groups_by_caption.setdefault(bisect.bisect_right(cuts, y), [])
            if not band or y - band[-1] >= 3:
                band.append(y)
        for band in groups_by_caption.values():
            if len(band) >= 2 and 13 <= band[-1] - band[0] <= 620:
                box = (left, band[0], right, band[-1])
                choices = [c for c in neighboring if _caption_distance(box, c) < float("inf")]
                if choices:
                    candidates.append((box, band, sorted(choices, key=lambda c: _caption_distance(box, c))))
    # A caption between two tables may be closer to the wrong one. Give bands
    # with only one possible caption priority, and use each caption only once.
    used = set()
    for box, band, choices in sorted(candidates, key=lambda item: len(item[2])):
        if caption := next((c for c in choices if c[0] not in used), None):
            used.add(caption[0])
            yield box, band, caption


def _line_words(words):
    lines = []
    for word in sorted(words, key=lambda w: ((w[1] + w[3]) / 2, w[0])):
        y = (word[1] + word[3]) / 2
        if not lines or y - lines[-1][0] > 3.8:
            lines.append((y, []))
        lines[-1][1].append(word)
    return [(y, sorted(line)) for y, line in lines]


def _intervals(ranges, gap):
    merged = []
    for a, b in sorted(ranges):
        if not merged or a > merged[-1][1] + gap:
            merged.append([a, b])
        else:
            merged[-1][1] = max(merged[-1][1], b)
    return merged


def _header_groups(words):
    ranges = []
    for _, line in _line_words(words):
        chunks = []
        for word in line:
            a, _, b, _, text = word[:5]
            if chunks and a - chunks[-1][1] <= (18 if text in ("→", "←", "↔") else 3.4):
                chunks[-1][1] = max(chunks[-1][1], b)
            else:
                chunks.append([a, b])
        ranges.extend(chunks)
    return _intervals(ranges, 3.4)


def _column_edges(box, headers, body, fixed=None):
    groups = _header_groups(headers)
    if not 2 <= len(groups) <= 18:
        return ()
    occupied = _intervals(((w[0], w[2]) for w in body), .25)
    edges = [box[0]]
    for (_, left), (right, _) in zip(groups, groups[1:]):
        if fixed is not None and left <= fixed <= right and not any(a < fixed < b for a, b in occupied):
            edges.append(fixed)
            continue
        gaps = [(max(a[1], left), min(b[0], right)) for a, b in zip(occupied, occupied[1:])]
        gaps += [(left, min(occupied[0][0], right)), (max(occupied[-1][1], left), right)]
        a, b = max(gaps, key=lambda g: g[1] - g[0])
        if b - a < .8:
            return ()
        edges.append((a + b) / 2)
    return tuple(edges + [box[2]])


def _word_column(word, columns):
    return min(len(columns) - 2, max(0, bisect.bisect_right(columns, (word[0] + word[2]) / 2) - 1))


def _row_cuts(words, columns):
    lines = _line_words(words)
    if len(lines) < 2:
        return []
    cells = []
    for _, line in lines:
        by_col = {}
        for word in line:
            by_col.setdefault(_word_column(word, columns), []).append(word)
        cells.append(by_col)

    def is_value(words):
        return preserve_value(" ".join(word[4] for word in words))

    # Open tables often print a code or label only at the start of each row.
    # Later text lines in the other columns belong to the same row.
    starts = [i for i, row in enumerate(cells) if 0 in row]
    anchored = len(starts) >= 2 and starts[0] == 0 and all(len(cells[i]) >= 2 for i in starts)
    if anchored and not all(is_value(cells[i][0]) for i in starts):
        # For text labels, require aligned starts in every column and distinct
        # PDF text blocks so wrapped labels do not become additional rows.
        labels = [frozenset(w[5] for w in cells[i][0]) for i in starts]
        anchored = (all(len(label) == 1 for label in labels) and len(set(labels)) == len(starts)
                    and all(len(cells[i]) == len(columns) - 1 for i in starts))
    if not anchored:
        numeric_columns = [{col for col, values in row.items() if is_value(values)} for row in cells]
        if any(len(row) < 2 for row in cells) or not set.intersection(*numeric_columns):
            return []
        starts = range(len(lines))
    return [(max(w[3] for w in lines[i - 1][1]) + min(w[1] for w in lines[i][1])) / 2 for i in starts[1:]]


def _grid(box, columns, rows, caption, words, *, header_rows=1, split=None, spans=()):
    rows = tuple(rows)
    covered, cells = set(), []
    if split is not None:
        spans = [*(Cell(0, c, 2) for c in range(split)), Cell(0, split, colspan=len(columns) - 1 - split)]
    for cell in spans:
        slots = {(r, c) for r in range(cell.row, cell.row + cell.rowspan) for c in range(cell.column, cell.column + cell.colspan)}
        if slots & covered:
            return None
        cells.append(cell)
        covered.update(slots)
    cells.extend(Cell(r, c) for r in range(len(rows) - 1) for c in range(len(columns) - 1) if (r, c) not in covered)
    headings = [[] for _ in columns[1:]]
    for _, line in _line_words(w for w in words if (w[1] + w[3]) / 2 < rows[header_rows]):
        for word in line:
            if split is None or (word[1] + word[3]) / 2 >= rows[1] or _word_column(word, columns) < split:
                headings[_word_column(word, columns)].append(word[4])
    return Table(tuple(box), tuple(columns), rows, tuple(sorted(cells, key=lambda c: (c.row, c.column))), header_rows,
                 tuple(" ".join(words)[:110] for words in headings), caption[0] if caption else None, caption[2] if caption else "")


def _ruled_grid(page, box, rules, caption, horizontal):
    words = page.get_text("words", clip=fitz.Rect(box) + (-1, -.4, 1, .4))
    if len(rules) < 3 or len(words) < 6:
        return None
    x0, y0, x1, _ = box
    partial = [(a, y) for a, b, y in horizontal if x0 + 10 < a < x1 - 25 and abs(b - x1) < 3 and y0 + 4 < y < rules[1] - 4]
    header_split, header_top = partial[0] if len(partial) == 1 else (None, y0)
    center = lambda w: (w[1] + w[3]) / 2
    headers = [w for w in words if center(w) < rules[1] and (
        center(w) >= header_top or (header_split is not None and w[2] < header_split))]
    body = [w for w in words if center(w) >= rules[1]]
    if not body or not (columns := _column_edges(box, headers, body, header_split)):
        return None
    rows = [y0]
    split = None
    if len(partial) == 1:
        x, y = partial[0]
        split = min(range(1, len(columns) - 1), key=lambda i: abs(columns[i] - x))
        if abs(columns[split] - x) > 9:
            return None
        rows.append(y)
    rows.append(rules[1])
    for top, bottom in zip(rules[1:], rules[2:]):
        rows.extend(_row_cuts([w for w in body if top <= center(w) < bottom], columns))
        rows.append(bottom)
    return _grid(box, columns, rows, caption, words, header_rows=2 if split is not None else 1, split=split)


def _native_grids(page, paths, captions):
    for native in page.find_tables(strategy="lines_strict", paths=paths, **_NATIVE_OPTIONS).tables:
        if not (2 <= native.row_count <= 120 and 2 <= native.col_count <= 20):
            continue
        rectangles = [cell for cell in native.cells if cell]
        columns = sorted({round(x, 2) for a, _, b, _ in rectangles for x in (a, b)})
        rows = sorted({round(y, 2) for _, a, _, b in rectangles for y in (a, b)})
        if len(columns) - 1 != native.col_count or len(rows) - 1 != native.row_count:
            continue
        spans = []
        for a, b, c, d in rectangles:
            r, col = rows.index(round(b, 2)), columns.index(round(a, 2))
            spans.append(Cell(r, col, rows.index(round(d, 2)) - r, columns.index(round(c, 2)) - col))
        words = page.get_text("words", clip=native.bbox)
        if len(words) < 3:
            continue
        caption = min(captions, key=lambda c: _caption_distance(native.bbox, c), default=None)
        if caption and _caption_distance(native.bbox, caption) == float("inf"):
            caption = None
        if grid := _grid(native.bbox, columns, rows, caption, words, spans=spans):
            yield grid


def _unruled(page, caption):
    _, (a, top, b, bottom), _ = caption
    w, h = page.rect.width, page.rect.height
    wide = a < w * .25 and b > w * .58
    lo, hi = (0, w) if wide else (0, w * .51) if (a + b) / 2 < w / 2 else (w * .49, w)
    fallback = None
    for start, end in ((max(0, top - h * .28), top), (bottom, min(h, bottom + h * .28))):
        if end - start < 24:
            continue
        clip = fitz.Rect(max(lo, a - 22), start, min(hi, max(b + 18, a + 170)), end)
        for native in page.find_tables(clip=clip, strategy="text", min_words_horizontal=2, **_NATIVE_OPTIONS).tables:
            box = native.bbox
            if _caption_distance(box, caption) > 28:
                continue
            words = page.get_text("words", clip=fitz.Rect(box) + (-1, -1, 1, 1))
            lines = _line_words(words)
            if not 3 <= len(lines) <= 60:
                continue
            if native.col_count >= 2 and _caption_distance(box, caption) <= 12:
                fallback = Table(tuple(box), (), (), (), 0, (), caption[0], caption[2])
            header, body = lines[0][1], [w for _, line in lines[1:] for w in line]
            columns = _column_edges(box, header, body)
            if not columns or any(len({_word_column(w, columns) for w in line}) < 2 for _, line in lines):
                continue
            boundaries = [(max(w[3] for w in left) + min(w[1] for w in right)) / 2 for (_, left), (_, right) in zip(lines, lines[1:])]
            return _grid(box, columns, [box[1], *boundaries, box[3]], caption, words)
    return fallback


@lru_cache(maxsize=128)
def _detect(path, _modified, _size, number, captions):
    with PDF_LAYOUT_LOCK, fitz.open(path) as doc:
        page = doc[number - 1]
        if page.rotation:
            return ()
        paths = page.get_drawings()
        horizontal, vertical = _rules(paths)
        found = list(_native_grids(page, paths, captions)) if len(vertical) >= 2 and len(horizontal) >= 2 else []
        for box, rules, caption in _rule_bands(horizontal, captions):
            if any(t.caption_id == caption[0] or t.contains(box) for t in found):
                continue
            table = _ruled_grid(page, box, rules, caption, horizontal)
            found.append(table or Table(box, (), (), (), 0, (), caption[0], caption[2]))
        for caption in captions:
            if CAPTION.match(caption[2]) and not any(t.caption_id == caption[0] for t in found) and (table := _unruled(page, caption)):
                found.append(table)
    return tuple(sorted(found, key=lambda table: (table.bbox[1], table.bbox[0])))


def cell_ranges(raw, geometry, table):
    """Map grid cells to canonical UTF-16 ranges, only for fully covered blocks."""
    if not table.cells:
        return []
    slots = {(r, c): (cell.row, cell.column) for cell in table.cells
             for r in range(cell.row, cell.row + cell.rowspan)
             for c in range(cell.column, cell.column + cell.colspan)}
    result, offset = [], 0
    boxes = geometry["boxes"]
    for char in raw:
        end = offset + (2 if ord(char) > 0xffff else 1)
        if not (char.isspace() or char == "\u00ad"):
            box = boxes[offset] if offset < len(boxes) else None
            if not box:
                return []
            x, y = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            if not (table.columns[0] - 2 <= x <= table.columns[-1] + 2 and table.rows[0] - 2 <= y <= table.rows[-1] + 2):
                return []
            col = min(len(table.columns) - 2, max(0, bisect.bisect_right(table.columns, x) - 1))
            row = min(len(table.rows) - 2, max(0, bisect.bisect_right(table.rows, y) - 1))
            row, col = slots[row, col]
            if result and result[-1][2:] == [row, col]:
                result[-1][1] = end
            else:
                result.append([offset, end, row, col])
        offset = end
    return result


def preserve_value(text):
    return bool(VALUE.fullmatch(text))


def same_numbers(source, translated):
    # A wrapped PDF can split an identifier such as GPT-3 across lines.
    # CJK text may also touch the identifier without an intervening space.
    if ACADEMIC_YEAR.fullmatch(source):
        translated = CHINESE_YEAR.sub(lambda match: str(CHINESE_YEARS.index(match[1]) + 1), translated)
    if re.search(r"\bPhase\s+\d+\b", source, re.I):
        translated = CHINESE_PHASE.sub(lambda match: str(CHINESE_YEARS.index(match[1]) + 1), translated)
    return [re.sub(r"[ \t]", "", token) for token in NUMBERS.findall(source)] == [
        re.sub(r"[ \t]", "", token) for token in NUMBERS.findall(translated)]
