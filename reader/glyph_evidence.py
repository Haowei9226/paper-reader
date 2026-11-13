"""Read-only proof for a legacy ToUnicode suffix with misleading fallback boxes."""
from __future__ import annotations

import math
import re
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

import pymupdf as fitz

from .pdf_text import canonical_glyph, explicit_ligature


def _font_key(value):
    return re.sub(r"[\s-]+", "", value.split("+", 1)[-1]).casefold()


def _box(value):
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    if not all(type(v) in (int, float) and math.isfinite(v) for v in value):
        return None
    return tuple(value) if value[2] > value[0] and value[3] > value[1] else None


def _same_box(trace, source):
    if abs(trace[0] - source[0]) > .04 or abs(trace[2] - source[2]) > .04:
        return False
    overlap = min(trace[3], source[3]) - max(trace[1], source[1])
    return overlap >= max(trace[3] - trace[1], source[3] - source[1]) * .8


def _unicode(code):
    return chr(code) if 0 <= code <= 0x10ffff and not 0xd800 <= code <= 0xdfff else ""


@lru_cache(maxsize=64)
def _page_evidence(path, mtime_ns, size, number):
    """Cache by file identity and page; no live document or font handle escapes."""
    with fitz.open(path) as doc:
        if doc.needs_pass or number > len(doc):
            return (), ()
        page = doc[number - 1]
        records = []
        for span in page.get_texttrace():
            font = _font_key(span["font"])
            painted = span.get("type", 0) in (0, 1, 2) and span.get("opacity", 1) > 0
            for code, glyph, origin, box in span["chars"]:
                records.append((font, code, glyph, tuple(origin), tuple(box), painted))
        groups = []
        for record in records:
            font, code, glyph, origin, box, painted = record
            if glyph >= 0:
                groups.append({"font": font, "code": code, "glyph": glyph, "origin": origin, "box": box,
                               "painted": painted and glyph > 0, "suffix": []})
            elif groups and font == groups[-1]["font"] and max(abs(a - b) for a, b in zip(origin, groups[-1]["origin"])) <= .001:
                groups[-1]["suffix"].append(code)
            elif groups:
                # An intervening non-glyph record at another physical point
                # breaks the required immediately following suffix sequence.
                groups.append({"font": font, "code": code, "glyph": glyph, "origin": origin, "box": box,
                               "painted": False, "suffix": []})
        refs = defaultdict(set)
        for xref, _, _, name, *_ in page.get_fonts():
            refs[_font_key(name)].add(xref)
        fonts, names = {}, {}

        def named(font_key, glyph):
            cache_key = font_key, glyph
            if cache_key in names:
                return names[cache_key]
            candidates = set()
            for xref in refs[font_key]:
                if xref not in fonts:
                    data = doc.extract_font(xref)[3]
                    fonts[xref] = fitz.Font(fontbuffer=data) if data else None
                if not fonts[xref]:
                    candidates.add(None)
                    continue
                name = fitz.mupdf.fz_get_glyph_name2(fonts[xref].this, glyph)
                explicit = explicit_ligature(name)
                code = fitz.mupdf.fz_unicode_from_glyph_name(name)
                candidates.add((explicit, canonical_glyph(_unicode(code))))
            names[cache_key] = next(iter(candidates)) if len(candidates) == 1 and None not in candidates else None
            return names[cache_key]

        def displayed(group):
            if not group["painted"]:
                return ""
            info = named(group["font"], group["glyph"])
            if not info:
                return ""  # Require embedded, unambiguous font evidence.
            explicit, recovered = info
            logical = canonical_glyph("".join(_unicode(code) for code in [group["code"], *group["suffix"]]))
            if explicit and (logical == explicit or not group["suffix"] and logical == explicit[0]):
                return explicit
            if not logical or any(ord(char) < 32 or char == "\ufffd" for char in logical):
                return recovered
            return logical

        proofs = []
        for index, group in enumerate(groups[:-1]):
            if not group["painted"] or not group["suffix"]:
                continue
            info = named(group["font"], group["glyph"])
            if not info or not info[0]:
                continue
            letters = info[0]
            logical = canonical_glyph("".join(_unicode(code) for code in [group["code"], *group["suffix"]]))
            following = groups[index + 1]
            if logical != letters or not following["painted"]:
                continue
            text = displayed(following)
            if text:
                proofs.append((group["box"], letters, following["box"], text))
        return tuple(proofs), tuple(page.derotation_matrix)


def confirmed_alias(path, page, main_bbox, ligature_text, following_bbox, following_text):
    """Require embedded glyph spelling, unpainted suffix and the next real glyph."""
    main, following = _box(main_bbox), _box(following_bbox)
    if not main or not following or type(page) is not int or page < 1:
        return False
    if not isinstance(ligature_text, str) or not ligature_text or not isinstance(following_text, str) or not following_text:
        return False
    try:
        resolved = Path(path).resolve(strict=True)
        stat = resolved.stat()
        proofs, derotation = _page_evidence(str(resolved), stat.st_mtime_ns, stat.st_size, page)
        if not derotation:
            return False
        matrix = fitz.Matrix(*derotation)
        main, following = tuple(fitz.Rect(main) * matrix), tuple(fitz.Rect(following) * matrix)
        matches = [proof for proof in proofs if proof[1] == ligature_text and proof[3] == following_text
                   and _same_box(proof[0], main) and _same_box(proof[2], following)]
        return len(matches) == 1
    except (OSError, ValueError, RuntimeError, AttributeError, TypeError, IndexError):
        return False


@lru_cache(maxsize=64)
def _leading_evidence(path, mtime_ns, size, number):
    with fitz.open(path) as doc:
        if doc.needs_pass or number > len(doc):
            return (), ()
        page = doc[number - 1]
        painted = []
        for span in page.get_texttrace():
            if span.get("type", 0) not in (0, 1, 2) or span.get("opacity", 1) <= 0:
                continue
            for code, glyph, _, box in span["chars"]:
                if glyph > 0:
                    painted.append((_font_key(span["font"]), canonical_glyph(_unicode(code)), tuple(box)))
        proofs = []
        raw = page.get_text("rawdict", flags=fitz.TEXTFLAGS_RAWDICT & ~fitz.TEXT_PRESERVE_IMAGES)
        for block in raw["blocks"]:
            lines = []
            for line in block.get("lines", ()):
                chars = [(char, _font_key(span["font"])) for span in line["spans"] for char in span.get("chars", ())]
                if "".join(char["c"] for char, _ in chars).strip():
                    lines.append(chars)
            if len(lines) < 2 or "".join(char["c"] for char, _ in lines[0]).strip() not in ("-", "\u00ad"):
                continue
            first = [(char, font) for char, font in lines[0] if not char["c"].isspace()]
            second = [(char, font) for char, font in lines[1] if not char["c"].isspace()]
            if len(first) != 1 or not second:
                continue
            hyphen, hyphen_font = first[0]
            following, following_font = second[0]
            text = canonical_glyph(following["c"])
            # The next line returns left of a line-end dash. A bullet/list dash
            # to the left of an item cannot justify this legacy parser repair.
            if not text or not text[0].islower() or hyphen["bbox"][0] <= following["bbox"][0] + .04:
                continue
            main_matches = [item for item in painted if item[0] == hyphen_font and item[1] == hyphen["c"]
                            and _same_box(item[2], hyphen["bbox"])]
            next_matches = [item for item in painted if item[0] == following_font and item[1] == text
                            and _same_box(item[2], following["bbox"])]
            if len(main_matches) == len(next_matches) == 1:
                proofs.append((tuple(block["bbox"]), tuple(hyphen["bbox"]), tuple(following["bbox"]), text))
        return tuple(proofs), tuple(page.derotation_matrix)


def confirmed_leading_hyphen(path, page, block_bbox, hyphen_bbox, following_bbox, following_text):
    """Prove the old line-join bug removed a standalone first-line PDF hyphen."""
    block, hyphen, following = _box(block_bbox), _box(hyphen_bbox), _box(following_bbox)
    if not block or not hyphen or not following or type(page) is not int or page < 1:
        return False
    if not isinstance(following_text, str) or not following_text:
        return False
    try:
        resolved = Path(path).resolve(strict=True)
        stat = resolved.stat()
        proofs, derotation = _leading_evidence(str(resolved), stat.st_mtime_ns, stat.st_size, page)
        if not derotation:
            return False
        matrix = fitz.Matrix(*derotation)
        block, hyphen, following = (tuple(fitz.Rect(box) * matrix) for box in (block, hyphen, following))
        matches = [proof for proof in proofs if max(abs(a - b) for a, b in zip(proof[0], block)) <= .04
                   and proof[3] == following_text and _same_box(proof[1], hyphen) and _same_box(proof[2], following)]
        return len(matches) == 1
    except (OSError, ValueError, RuntimeError, AttributeError, TypeError, IndexError):
        return False
