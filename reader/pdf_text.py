"""Conservative text repairs backed by PDF glyph names and line geometry."""
from __future__ import annotations

import re
import statistics
import unicodedata


# Compatibility normalization would also erase mathematical alphabets, exponents,
# units and nonbreaking spaces. Only these presentation ligatures are text aliases.
LATIN_LIGATURES = dict(zip("\ufb00\ufb01\ufb02\ufb03\ufb04\ufb05\ufb06", ("ff", "fi", "fl", "ffi", "ffl", "st", "st")))
ACCENTS = {"\u00b4": "\u0301", "\u0060": "\u0300", "\u00a8": "\u0308", "\u02c6": "\u0302",
           "\u02dc": "\u0303", "\u02c7": "\u030c", "\u02d8": "\u0306", "\u02da": "\u030a"}


def canonical_glyph(text):
    return "".join(LATIN_LIGATURES.get(char, char) for char in text)


def explicit_ligature(name):
    """An embedded glyph named Q_u or f_f_i states its letters explicitly."""
    if re.fullmatch(r"[A-Za-z](?:_[A-Za-z]){1,3}", name or ""):
        return name.replace("_", "")
    return {"ff": "ff", "fi": "fi", "fl": "fl", "ffi": "ffi", "ffl": "ffl"}.get(name)


def recover_overprinted_accents(blocks):
    """Compose a spacing accent only when it is painted over the next Latin letter."""
    for block in blocks:
        for line in block.get("lines", ()):
            if line.get("dir", (1, 0))[0] < .995:
                continue
            chars = [(char, span) for span in line["spans"] for char in span.get("chars", ())]
            for (accent, a_span), (base, b_span) in zip(chars, chars[1:]):
                mark, letter = ACCENTS.get(accent["c"]), base["c"]
                if not mark or len(letter) != 1 or "LATIN" not in unicodedata.name(letter, ""):
                    continue
                if a_span["font"] != b_span["font"] or abs(a_span["size"] - b_span["size"]) > b_span["size"] * .05:
                    continue
                a, b = accent["bbox"], base["bbox"]
                # A separate apostrophe/acute before a word has no horizontal
                # overlap. TeX accents are centered over the base and may precede
                # it in the content stream, despite having the same baseline.
                center = (a[0] + a[2]) / 2
                if not b[0] <= center <= b[2] or min(a[2], b[2]) - max(a[0], b[0]) < (a[2] - a[0]) * .6:
                    continue
                if abs(accent["origin"][1] - base["origin"][1]) > b_span["size"] * .35:
                    continue
                composed = unicodedata.normalize("NFC", letter + mark)
                if len(composed) != 1:
                    continue
                base["c"] = composed
                base["bbox"] = (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))
                accent["c"] = ""


def word_gap_starts(line):
    """Recover omitted Latin word spaces from a distinct, font-scaled gap cluster."""
    if line.get("dir", (1, 0))[0] < .995:
        return set()
    chars = [(char, span) for span in line["spans"] for char in span.get("chars", ()) if char["c"]]
    text = "".join(char["c"] for char, _ in chars)
    if len(re.findall(r"[A-Za-z]{3,}", text)) < 1 or re.search(r"https?://|www\.|doi\s*:|\b10\.\d{4,9}/|\S+@\S+|[=+\u00d7\u00f7\u2211\u222b]", text, re.I):
        return set()
    pairs = []
    for (left, a), (right, b) in zip(chars, chars[1:]):
        if a["font"] != b["font"] or re.search(r"math|symbol|cmmi|cmsy|cmex", a["font"], re.I):
            continue
        size = a["size"]
        if size <= 0 or abs(size - b["size"]) > size * .05 or abs(left["origin"][1] - right["origin"][1]) > size * .08:
            continue
        pair = left["c"][-1] + right["c"][0]
        if not re.fullmatch(r"(?:[A-Za-z\u00c0-\u024f\ufb00-\ufb06,.;:!?][A-Za-z\u00c0-\u024f\ufb00-\ufb06]|[,;:!?][0-9])", pair):
            continue
        pairs.append((right, (right["bbox"][0] - left["bbox"][2]) / size))
    if len(pairs) < 5:
        return set()
    baseline = statistics.median(gap for _, gap in pairs)
    # Dense letter pairs must dominate. Uniform tracking in headings is not a
    # word boundary, nor is a loose gap between two isolated formula variables.
    if baseline > .045:
        return set()
    threshold = max(.11, baseline + .10)
    return {id(char) for char, gap in pairs if threshold <= gap <= .8}
