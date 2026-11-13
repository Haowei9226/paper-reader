"""Map saved UTF-16 source ranges between two extractions of the same PDF block.

Text is never matched by semantic similarity. Stable block identity, monotone
physical glyphs and a small set of extraction-only changes provide the evidence.
"""
from __future__ import annotations

import json
import math
import unicodedata
import zlib
from collections import defaultdict
from dataclasses import dataclass


TOLERANCE = .04  # Both native extractors round PDF point coordinates to .001.
LIGATURES = {"f": {"ff", "fi", "fl", "ffi", "ffl", "ft"}, "s": {"st"}, "Q": {"Qu"}, "t": {"tt"}}


def _length(text):
    return len(text.encode("utf-16-le")) // 2


def _geometry(value):
    if isinstance(value, (bytes, bytearray, memoryview)):
        return json.loads(zlib.decompress(value))
    return value if isinstance(value, dict) else {}


def _close(left, right):
    return left is not None and right is not None and max(abs(a - b) for a, b in zip(left, right)) <= TOLERANCE


def _union(boxes):
    boxes = [box for box in boxes if box is not None]
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)) if boxes else None


def _covered(box, container):
    return box is not None and container is not None and all((container[0] - TOLERANCE <= box[0],
        container[1] - TOLERANCE <= box[1], container[2] + TOLERANCE >= box[2], container[3] + TOLERANCE >= box[3]))


@dataclass
class _Glyph:
    text: str
    start: int
    end: int
    box: tuple | None
    rows: set

    @property
    def spacing(self):
        return self.box is None and all(char.isspace() or char == "\u00ad" for char in self.text)


def _glyphs(text, geo):
    boxes, rows = geo.get("boxes", []), geo.get("lines", [])
    size = _length(text)
    if len(boxes) != size or len(rows) != size:
        raise ValueError("geometry_length_mismatch")
    result, boundaries, offset = [], {0}, 0
    for char in text:
        count = _length(char)
        cells = boxes[offset:offset + count]
        for box in cells:
            if box is not None and (len(box) != 4 or not all(type(v) in (int, float) and math.isfinite(v) for v in box) or box[2] < box[0] or box[3] < box[1]):
                raise ValueError("invalid_glyph_geometry")
        box = _union(cells)
        if any(cell is None for cell in cells) and box is not None:
            raise ValueError("partial_scalar_geometry")
        if box is not None and any(not _close(cell, box) for cell in cells):
            raise ValueError("split_scalar_geometry")
        glyph = _Glyph(char, offset, offset + count, box, set(rows[offset:offset + count]))
        previous = result[-1] if result else None
        same = previous and previous.rows == glyph.rows and (_close(previous.box, box) or previous.box is None and box is None)
        zero_tail = previous and previous.box is not None and box is not None and previous.rows == glyph.rows and (
            abs(box[2] - box[0]) <= .03 and abs(box[0] - previous.box[2]) <= TOLERANCE
            and abs(box[1] - previous.box[1]) <= TOLERANCE and abs(box[3] - previous.box[3]) <= TOLERANCE
            and (char.isalpha() or unicodedata.combining(char)) and previous.text[-1].isalpha())
        if same or zero_tail:
            previous.text += char
            previous.end = glyph.end
            previous.box = _union((previous.box, box))
        else:
            result.append(glyph)
        offset += count
        boundaries.add(offset)
    return result, boundaries


def _glyph_change(old, new):
    if (old and len(old) == len(new) and all(char == "\ufffd" for char in new)
            and all(char == "\ufffd" or (ord(char) < 32 or ord(char) == 127) and not char.isspace() for char in old)):
        return "unknown_placeholder"
    if old == new:
        return "equal"
    if unicodedata.normalize("NFKC", new) == unicodedata.normalize("NFKC", old):
        return "unicode"
    if new in LIGATURES.get(old, ()):
        return "ligature"
    if old and all(char == "\ufffd" or ord(char) < 32 or ord(char) == 127 for char in old) and new and all(char != "\ufffd" and ord(char) >= 32 for char in new):
        return "decoded_glyph"
    return None


def _glyph_kind(old, new):
    kind = _glyph_change(old.text, new.text)
    if kind == "unknown_placeholder" and (old.box is None or new.box is None or old.rows != new.rows):
        return None
    return kind


def _accent_change(old, new):
    """TeX spacing accent before a letter -> one composed Latin character."""
    def parts(text):
        decomposed = unicodedata.normalize("NFD", text.replace("`", "\u0300"))
        bases = "".join(char for char in decomposed if not unicodedata.combining(char) and not char.isspace())
        marks = sorted(char for char in decomposed if unicodedata.combining(char))
        return bases, marks

    left, right = parts(old), parts(new)
    return left == right and len(left[0]) == 1 and bool(left[1]) and "LATIN" in unicodedata.name(left[0], "")


def _spacing_accent_overprint(glyphs):
    marks = [glyph for glyph in glyphs if "`" in glyph.text]
    if not marks:
        return True
    base = glyphs[-1]
    if (len(base.text) != 1 or "LATIN" not in unicodedata.name(base.text, "") or base.box is None):
        return False
    for mark in marks:
        if mark.text.strip() != "`" or mark.box is None or mark.rows != base.rows:
            return False
        a, b = mark.box, base.box
        overlap = min(a[2], b[2]) - max(a[0], b[0])
        vertical = min(a[3], b[3]) - max(a[1], b[1])
        if not (b[0] <= (a[0] + a[2]) / 2 <= b[2] and overlap >= (a[2] - a[0]) * .6
                and vertical >= min(a[3] - a[1], b[3] - b[1]) * .7):
            return False
    return True


def _expanded_alias(old, new, following, replacement):
    """Recognize old unpainted ToUnicode tails using the next real glyph."""
    if (not 2 <= len(old) <= 4 or "".join(g.text for g in old) != new.text or not new.text.isalpha()
            or not _close(old[0].box, new.box) or not _close(following.box, replacement.box)
            or following.text != replacement.text or following.box is None):
        return False
    return all(glyph.box is not None and glyph.rows == new.rows
               and abs(glyph.box[0] - old[0].box[2]) <= TOLERANCE
               and abs(glyph.box[0] - following.box[0]) <= TOLERANCE
               and glyph.box[1] >= following.box[1] - TOLERANCE
               and glyph.box[3] <= following.box[3] + TOLERANCE
               and glyph.box[2] <= following.box[2] + TOLERANCE for glyph in old[1:])


class BlockMap:
    """A conservative, all-or-nothing mapping for a stable canonical source block.

    ``old`` and ``new`` contain id, page, text and geometry (decoded or zlib JSON).
    map_range returns start/end/quote/rects, or None when a range is not reliable.
    map_boundary's bias chooses either side of an inserted or atomic glyph span.
    ``edits`` records UTF-16 spans and their physical/extraction justification.
    """

    def __init__(self, old, new):
        self.safe, self.reason, self.edits = False, "invalid_block", []
        if not isinstance(old, dict) or not isinstance(new, dict):
            return
        self.old_text, self.new_text = old.get("text", ""), new.get("text", "")
        self._bounds, self._interiors = defaultdict(set), {}
        self._aliases, self._alias_boxes = {}, {}
        self._source_pdf, self._page = new.get("_source_pdf"), new.get("page")
        self._old_bbox, self._new_bbox = old.get("bbox"), new.get("bbox")
        try:
            if not old.get("id") or old.get("id") != new.get("id") or old.get("page") != new.get("page"):
                raise ValueError("block_identity_mismatch")
            if old.get("paper_id") and new.get("paper_id") and old["paper_id"] != new["paper_id"]:
                raise ValueError("block_identity_mismatch")
            if not isinstance(self.old_text, str) or not isinstance(self.new_text, str):
                raise ValueError("invalid_source_text")
            self._old_bytes, self._new_bytes = self.old_text.encode("utf-16-le"), self.new_text.encode("utf-16-le")
            self.old_geometry, self.new_geometry = _geometry(old.get("geometry")), _geometry(new.get("geometry"))
            self._old, self._old_boundaries = _glyphs(self.old_text, self.old_geometry)
            self._new, self._new_boundaries = _glyphs(self.new_text, self.new_geometry)
            self._restore_alias_clusters()
            self._build()
            for alias in self._aliases.values():
                for offset in range(alias["old_start"] + 1, alias["old_end"]):
                    if offset in self._old_boundaries:
                        self._interiors[offset] = (alias["new_start"], alias["new_end"])
            self.safe, self.reason = True, ""
        except (ValueError, TypeError, KeyError, IndexError, UnicodeError, zlib.error, json.JSONDecodeError) as exc:
            self.reason = str(exc) if isinstance(exc, ValueError) else "invalid_geometry_or_source"
            self.edits.clear()

    def _piece(self, a, b, c, d, kind):
        alias = self._aliases.get((a, b))
        self.edits.append({"old_start": a, "old_end": b, "new_start": c, "new_end": d,
                           "kind": "expanded_glyph_geometry" if alias else kind,
                           **({"physical_bbox": alias["physical_bbox"], "legacy_suffix_start": alias["legacy_suffix_start"]} if alias else {})})
        self._bounds[a].add(c)
        self._bounds[b].add(d)
        if self._old_bytes[a * 2:b * 2] == self._new_bytes[c * 2:d * 2]:
            for offset in range(a + 1, b):
                if offset in self._old_boundaries:
                    self._bounds[offset].add(c + offset - a)
        elif a < b:
            for offset in range(a + 1, b):
                if offset in self._old_boundaries:
                    self._interiors[offset] = (c, d)

    def _restore_alias_clusters(self):
        """Reattach old synthetic suffix letters to their unchanged painted glyph.

        The suffix must complete the exact new ligature text, start at the old
        main glyph's advance, and share that position with a stable following
        glyph. A suffix may share its fallback box with that following letter.
        No source letter is removed and ordinary geometric coverage stays strict.
        """
        cells = defaultdict(list)
        for j, glyph in enumerate(self._new):
            if glyph.box is not None:
                cells[(math.floor(glyph.box[0] / .05), math.floor(glyph.box[1] / .05))].append(j)
        old, restored, i = list(self._old), [], 0
        while i < len(old):
            first = old[i]
            matches = []
            if first.box is not None:
                x, y = math.floor(first.box[0] / .05), math.floor(first.box[1] / .05)
                matches = [j for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                           for j in cells.get((x + dx, y + dy), ()) if _close(first.box, self._new[j].box)]
            target = self._new[matches[0]] if len(matches) == 1 else None
            if not (target and 2 <= len(target.text) <= 4 and target.text.isalpha()
                    and target.text.startswith(first.text) and len(first.text) < len(target.text)):
                restored.append(first)
                i += 1
                continue
            needed, cursor, tails = target.text[len(first.text):], i + 1, []
            remainder = None
            while needed and cursor < len(old) and len(tails) < 3:
                glyph = old[cursor]
                count = min(len(needed), len(glyph.text))
                if (glyph.text[:count] != needed[:count] or glyph.box is None or glyph.rows != target.rows
                        or abs(glyph.box[0] - first.box[2]) > TOLERANCE
                        or abs(glyph.box[1] - first.box[1]) > TOLERANCE or abs(glyph.box[3] - first.box[3]) > TOLERANCE
                        or glyph.box[2] - glyph.box[0] > (first.box[2] - first.box[0]) * 1.1):
                    break
                end = glyph.start + _length(glyph.text[:count])
                tails.append((glyph.start, end))
                needed = needed[count:]
                if count < len(glyph.text):
                    remainder = _Glyph(glyph.text[count:], end, glyph.end, glyph.box, glyph.rows)
                else:
                    cursor += 1
            following = remainder or (old[cursor] if cursor < len(old) else None)
            j = matches[0] + 1
            if following and j < len(self._new) and self._new[j].text in ("-", "\u00ad"):
                try:
                    if self._insert(j) == "line_end_hyphen":
                        j += 1
                except ValueError:
                    pass
            next_glyph = self._new[j] if j < len(self._new) else None
            # Native spaces have an explicit advance box too. They can prove
            # the shared following position, unlike inserted None-box spacing.
            # Negative kerning can put the next real glyph slightly before the
            # main glyph's advance. Its complete coverage of the fallback tail
            # supplies stronger evidence than merely relaxing a distance limit.
            tails_covered = following and all(_covered(self.old_geometry["boxes"][a], following.box) for a, _ in tails)
            shared_position = (following and next_glyph and following.box is not None
                               and following.text == next_glyph.text and _close(following.box, next_glyph.box)
                               and (abs(following.box[0] - first.box[2]) <= TOLERANCE
                                    or tails_covered
                                    or following.rows.isdisjoint(first.rows)
                                    and j > matches[0] + 1))
            if (not shared_position and not needed and tails and following and next_glyph
                    and following.text == next_glyph.text and _close(following.box, next_glyph.box)
                    and following.rows == first.rows and following.box[0] > first.box[2] + TOLERANCE
                    and self._source_pdf):
                from .glyph_evidence import confirmed_alias
                shared_position = confirmed_alias(self._source_pdf, self._page, first.box, target.text,
                                                  following.box, following.text)
            if needed or not tails or not shared_position:
                restored.append(first)
                i += 1
                continue
            end = tails[-1][1]
            alias = {"old_start": first.start, "old_end": end, "new_start": target.start, "new_end": target.end,
                     "physical_bbox": list(first.box), "legacy_suffix_start": first.end}
            self._aliases[(first.start, end)] = alias
            for a, b in tails:
                self._alias_boxes.update((offset, first.box) for offset in range(a, b))
            restored.append(_Glyph(target.text, first.start, end, first.box, first.rows))
            if remainder:
                old[cursor] = remainder
            i = cursor
        self._old = restored

    def _anchors(self):
        # Index only physical coordinates, never a text substring. A tolerance
        # match must be unique in both directions before becoming an anchor.
        cells = defaultdict(list)
        for j, glyph in enumerate(self._new):
            if glyph.box is not None:
                cells[(math.floor(glyph.box[0] / .05), math.floor(glyph.box[1] / .05))].append(j)
        forward, reverse = {}, defaultdict(list)
        for i, glyph in enumerate(self._old):
            if glyph.box is None:
                continue
            x, y = math.floor(glyph.box[0] / .05), math.floor(glyph.box[1] / .05)
            matches = [j for dx in (-1, 0, 1) for dy in (-1, 0, 1) for j in cells.get((x + dx, y + dy), ()) if _close(glyph.box, self._new[j].box)]
            forward[i] = matches
            for j in matches:
                reverse[j].append(i)
        anchors = []
        for i, matches in forward.items():
            if len(matches) != 1 or len(reverse[matches[0]]) != 1:
                continue
            j = matches[0]
            if not _glyph_kind(self._old[i], self._new[j]):
                continue
            # Matching just old f -> new fi would strand the old fallback i as
            # a deletion. Keep that proven alias cluster in one region instead.
            if self._old[i].text != self._new[j].text and j + 1 < len(self._new) and any(
                    _expanded_alias(self._old[i:end], self._new[j], self._old[end], self._new[j + 1])
                    for end in range(i + 2, min(i + 5, len(self._old)))):
                continue
            anchors.append((i, j))
        if any(a[1] >= b[1] for a, b in zip(anchors, anchors[1:])):
            raise ValueError("nonmonotone_glyph_order")
        return anchors

    def _insert(self, index):
        glyph = self._new[index]
        if glyph.spacing:
            # An invented word space must sit between distinct real glyphs with
            # a physical gap or a line transition. Edge whitespace has no text.
            before = next((g for g in reversed(self._new[:index]) if g.box is not None), None)
            after = next((g for g in self._new[index + 1:] if g.box is not None), None)
            if before and after and before.rows == after.rows:
                height = min(before.box[3] - before.box[1], after.box[3] - after.box[1])
                if after.box[0] - before.box[2] < max(.05, height * .05):
                    raise ValueError("unproven_inserted_space")
            return "spacing"
        if glyph.text in ("-", "\u00ad") and glyph.box is not None:
            before = next((g for g in reversed(self._new[:index]) if g.box is not None and not g.text.isspace()), None)
            after = next((g for g in self._new[index + 1:] if g.box is not None and not g.text.isspace()), None)
            if (before is None and index == 0 and after and after.text[0].islower()
                    and glyph.box[0] > after.box[0] + TOLERANCE
                    and glyph.rows.isdisjoint(after.rows) and self._source_pdf):
                from .glyph_evidence import confirmed_leading_hyphen
                old_box = json.loads(self._old_bbox) if isinstance(self._old_bbox, str) else self._old_bbox
                new_box = json.loads(self._new_bbox) if isinstance(self._new_bbox, str) else self._new_bbox
                if old_box and old_box == new_box and confirmed_leading_hyphen(
                        self._source_pdf, self._page, new_box, glyph.box, after.box, after.text):
                    return "leading_hyphen_restore"
            if before and after and glyph.rows == before.rows and glyph.rows.isdisjoint(after.rows) and after.text[0].islower() and (
                -min(glyph.box[2] - glyph.box[0], before.box[2] - before.box[0]) * .5 - TOLERANCE
                <= glyph.box[0] - before.box[2] <= max(1, glyph.box[3] - glyph.box[1])):
                return "line_end_hyphen"
        raise ValueError("unmatched_new_real_glyph")

    def _region(self, oi, oe, ni, ne):
        old, new = self._old[oi:oe], self._new[ni:ne]
        old_start = old[0].start if old else (self._old[oi].start if oi < len(self._old) else _length(self.old_text))
        new_start = new[0].start if new else (self._new[ni].start if ni < len(self._new) else _length(self.new_text))
        if not old:
            for j in range(ni, ne):
                glyph = self._new[j]
                self._piece(old_start, old_start, glyph.start, glyph.end, self._insert(j))
            return
        if not new:
            for glyph in old:
                if not glyph.spacing:
                    raise ValueError("deleted_real_source_glyph")
                self._piece(glyph.start, glyph.end, new_start, new_start, "spacing")
            return
        if old[0].spacing and new[0].spacing:
            self._piece(old[0].start, old[0].end, new[0].start, new[0].end, "spacing")
            return self._region(oi + 1, oe, ni + 1, ne)
        if old[0].spacing:
            self._piece(old[0].start, old[0].end, new_start, new_start, "spacing")
            return self._region(oi + 1, oe, ni, ne)
        if new[0].spacing:
            self._piece(old_start, old_start, new[0].start, new[0].end, self._insert(ni))
            return self._region(oi, oe, ni + 1, ne)
        if old[-1].spacing:
            self._region(oi, oe - 1, ni, ne)
            return self._piece(old[-1].start, old[-1].end, new[-1].end, new[-1].end, "spacing")
        if new[-1].spacing:
            self._region(oi, oe, ni, ne - 1)
            return self._piece(old[-1].end, old[-1].end, new[-1].start, new[-1].end, self._insert(ne - 1))
        if len(old) == len(new) and all(_close(a.box, b.box) and _glyph_kind(a, b) for a, b in zip(old, new)):
            for a, b in zip(old, new):
                self._piece(a.start, a.end, b.start, b.end, _glyph_kind(a, b))
            return
        old_text, new_text = "".join(g.text for g in old), "".join(g.text for g in new)
        if len(new) == 1 and oe < len(self._old) and ne < len(self._new):
            # Older MuPDF rawdict builds sometimes assigned a nonzero fallback
            # advance to an unpainted ToUnicode suffix. The following real glyph
            # starts at the same point. The new trace-confirmed ligature retains
            # the original first glyph, and no letter is inserted or removed.
            if _expanded_alias(old, new[0], self._old[oe], self._new[ne]):
                self._piece(old[0].start, old[-1].end, new[0].start, new[0].end, "expanded_glyph_geometry")
                return
        if (_accent_change(old_text, new_text) and _spacing_accent_overprint(old)
                and _close(_union(g.box for g in old), _union(g.box for g in new))):
            self._piece(old[0].start, old[-1].end, new[0].start, new[-1].end, "overprinted_accent")
            return
        raise ValueError("unproven_source_change")

    def _build(self):
        oi = ni = 0
        for i, j in self._anchors():
            self._region(oi, i, ni, j)
            old, new = self._old[i], self._new[j]
            self._piece(old.start, old.end, new.start, new.end, _glyph_kind(old, new))
            oi, ni = i + 1, j + 1
        self._region(oi, len(self._old), ni, len(self._new))
        self._bounds[0].add(0)
        self._bounds[_length(self.old_text)].add(_length(self.new_text))
        for offset in self._old_boundaries:
            if offset not in self._bounds and offset not in self._interiors:
                raise ValueError("unmapped_source_boundary")

    def map_boundary(self, offset, bias="left"):
        if not self.safe or type(offset) is not int or offset not in self._old_boundaries or bias not in ("left", "right"):
            return None
        values = self._interiors.get(offset) or self._bounds.get(offset)
        return (min(values) if bias == "left" else max(values)) if values else None

    def map_range(self, start, end, quote=None):
        if not self.safe or type(start) is not int or type(end) is not int or start >= end:
            return None
        if start not in self._old_boundaries or end not in self._old_boundaries:
            return None
        old_quote = self._old_bytes[start * 2:end * 2].decode("utf-16-le")
        if quote is not None and (not isinstance(quote, str) or quote != old_quote):
            return None
        # Interior boundaries cover the complete repaired glyph. Exact boundary
        # insertions stay outside a range that starts or ends at that boundary.
        a = self.map_boundary(start, "left" if start in self._interiors else "right")
        b = self.map_boundary(end, "right" if end in self._interiors else "left")
        if a is None or b is None or a >= b or a not in self._new_boundaries or b not in self._new_boundaries:
            return None
        new_boxes = [box for box in self.new_geometry["boxes"][a:b] if box is not None]
        exact_boxes = {tuple(box) for box in new_boxes}
        for offset, box in enumerate(self.old_geometry["boxes"][start:end], start):
            box = self._alias_boxes.get(offset, box)
            if box is not None and tuple(box) not in exact_boxes and not any(all((target[0] - TOLERANCE <= box[0], target[1] - TOLERANCE <= box[1], target[2] + TOLERANCE >= box[2], target[3] + TOLERANCE >= box[3])) for target in new_boxes):
                return None
        rows = defaultdict(list)
        for box, row in zip(self.new_geometry["boxes"][a:b], self.new_geometry["lines"][a:b]):
            if box is not None:
                rows[row].append(box)
        return {"start": a, "end": b, "quote": self._new_bytes[a * 2:b * 2].decode("utf-16-le"),
                "rects": [list(_union(boxes)) for boxes in rows.values()]}
