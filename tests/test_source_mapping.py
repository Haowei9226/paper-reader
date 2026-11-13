import zlib
import json

import pytest

from reader.source_mapping import BlockMap


def _block(parts, **fields):
    text, boxes, rows = [], [], []
    for content, box, row in parts:
        text.append(content)
        count = len(content.encode("utf-16-le")) // 2
        boxes.extend([box] * count)
        rows.extend([row] * count)
    return {"id": "stable-source-block", "paper_id": "paper", "page": 1,
            "text": "".join(text), "geometry": {"boxes": boxes, "lines": rows}, **fields}


def _letters(text, *, x=10, row=0):
    return [(char, [x + index * 5, row * 20 + 10, x + index * 5 + 5, row * 20 + 22], row) for index, char in enumerate(text)]


@pytest.mark.parametrize(("old", "new"), [("f", "fi"), ("f", "fl"), ("f", "ffi"), ("Q", "Qu"), ("ﬁ", "fi"), ("m", "𝑚"), ("2", "²"), ("ħ", "ℏ"), ("t", "tt"), ("f", "ft")])
def test_explicit_same_glyph_repairs_map_utf16_and_keep_physical_range(old, new):
    box = [10, 10, 17, 22]
    before = _block([(old, box, 0), *_letters("tail", x=17)])
    after = _block([(new, box, 0), *_letters("tail", x=17)])
    after["geometry"] = zlib.compress(json.dumps(after["geometry"]).encode())
    mapping = BlockMap(before, after)
    assert mapping.safe, mapping.reason
    result = mapping.map_range(0, len(old.encode("utf-16-le")) // 2, old)
    assert result == {"start": 0, "end": len(new.encode("utf-16-le")) // 2, "quote": new, "rects": [box]}
    assert mapping.map_range(len(old.encode("utf-16-le")) // 2, len(before["text"].encode("utf-16-le")) // 2)["quote"] == "tail"


def test_inserted_word_space_has_geometric_evidence_and_boundary_bias():
    left, right = _letters("alpha"), _letters("beta", x=36.5)
    before = _block(left + right)
    after = _block(left + [(" ", None, 0)] + right)
    mapping = BlockMap(before, after)
    assert mapping.safe, mapping.reason
    assert mapping.map_range(0, 5)["quote"] == "alpha"
    assert mapping.map_range(5, 9)["quote"] == "beta"
    assert mapping.map_range(0, 9)["quote"] == "alpha beta"
    assert mapping.map_boundary(5, "left") == 5
    assert mapping.map_boundary(5, "right") == 6
    # A fabricated space inside tightly adjacent glyphs has no PDF evidence.
    assert not BlockMap(_block(left + _letters("beta", x=35)), _block(left + [(" ", None, 0)] + _letters("beta", x=35))).safe


def test_line_end_literal_hyphen_can_be_reintroduced_without_changing_other_ranges():
    left, right = _letters("semi"), _letters("structured", row=1)
    before = _block(left + right)
    # The painted hyphen can kern slightly into the preceding letter's advance.
    after = _block(left + [("-", [29.8, 10, 33.8, 22], 0)] + right)
    mapping = BlockMap(before, after)
    assert mapping.safe, mapping.reason
    assert mapping.map_range(0, 4)["quote"] == "semi"
    assert mapping.map_range(4, 14)["quote"] == "structured"
    assert mapping.map_range(0, 14)["quote"] == "semi-structured"
    assert any(edit["kind"] == "line_end_hyphen" for edit in mapping.edits)
    # A newly inserted ordinary inline hyphen is not the old line-join bug.
    assert not BlockMap(_block(left + _letters("structured", x=34)), _block(left + [("-", [30, 10, 34, 22], 0)] + _letters("structured", x=34))).safe


def test_soft_hyphen_and_deleted_synthetic_line_space_remain_addressable():
    left = _letters("en") + [("\u00ad", [20, 10, 24, 22], 0)]
    right = _letters("gage", row=1)
    mapping = BlockMap(_block(left + [(" ", None, 1)] + right), _block(left + right))
    assert mapping.safe, mapping.reason
    assert mapping.map_range(0, 8)["quote"] == "en\u00adgage"
    assert mapping.map_range(2, 3)["quote"] == "\u00ad"
    assert mapping.map_range(3, 4) is None


def test_overprinted_accent_can_cover_a_partial_old_glyph_cluster():
    j = [("J", [10, 10, 14, 22], 0)]
    accent = [(" \u0301", [14.5, 9, 18.5, 21], 0)]
    base = [("e", [14, 10, 20, 22], 0)]
    tail = _letters("remy", x=20)
    mapping = BlockMap(_block(j + accent + base + tail), _block(j + [("é", [14, 9, 20, 22], 0)] + tail))
    assert mapping.safe, mapping.reason
    assert mapping.map_range(3, 4, "e")["quote"] == "é"
    assert mapping.map_range(1, 3, " \u0301")["quote"] == "é"
    assert mapping.map_range(0, 8)["quote"] == "Jéremy"


def test_spacing_ascii_grave_requires_a_real_overprint_not_literal_code():
    mark = [386.435, 582.589, 389.089, 590.559]
    base = [385.992, 582.629, 389.531, 590.599]
    merged = [385.992, 582.589, 389.531, 590.599]
    mapping = BlockMap(_block([("`", mark, 0), ("e", base, 0)]), _block([("è", merged, 0)]))
    assert mapping.safe, mapping.reason
    assert mapping.map_range(0, 2, "`e") == {"start": 0, "end": 1, "quote": "è", "rects": [merged]}
    assert mapping.map_range(1, 2, "e")["quote"] == "è"
    adjacent = _block([("`", [10, 10, 13, 22], 0), ("e", [13, 10, 18, 22], 0)])
    assert not BlockMap(adjacent, _block([("è", [10, 10, 18, 22], 0)])).safe
    assert not BlockMap(adjacent, _block([("`è", [10, 10, 18, 22], 0)])).safe


def test_restored_leading_hyphen_requires_native_pdf_evidence_and_keeps_old_ranges(monkeypatch):
    from reader import glyph_evidence

    tail = _letters("comple", row=1)
    first = ("-", [15, 10, 19, 22], 0)
    bounds = [10, 10, 40, 42]
    old = _block(tail, bbox=bounds)
    fresh = _block([first, *tail], bbox=bounds, _source_pdf="a-real-source.pdf")
    monkeypatch.setattr(glyph_evidence, "confirmed_leading_hyphen", lambda *_: True)
    mapping = BlockMap(old, fresh)
    assert mapping.safe, mapping.reason
    assert mapping.map_range(0, 6, "comple")["start"] == 1
    assert mapping.map_range(0, 6, "comple")["quote"] == "comple"
    assert any(item["kind"] == "leading_hyphen_restore" for item in mapping.edits)
    assert not BlockMap(old, {**fresh, "_source_pdf": None}).safe
    assert not BlockMap(old, {**fresh, "bbox": [10, 9, 40, 42]}).safe
    monkeypatch.setattr(glyph_evidence, "confirmed_leading_hyphen", lambda *_: False)
    assert not BlockMap(old, fresh).safe


def test_repeated_words_use_their_physical_position_not_find_first():
    first, second = _letters("first"), _letters("frst", row=1)
    replacement = [("fi", second[0][1], 1)] + second[1:]
    mapping = BlockMap(_block(first + [(" ", None, 1)] + second), _block(first + [(" ", None, 1)] + replacement))
    assert mapping.safe, mapping.reason
    result = mapping.map_range(6, 10, "frst")
    assert result["quote"] == "first" and result["start"] == 6
    assert result["rects"] == [[10, 30, 30, 42]]


def test_legacy_unpainted_suffix_fallback_geometry_requires_exact_text_and_range_coverage():
    before = _block([("f", [10, 10, 20, 22], 0), ("t", [20, 10, 24, 22], 0), ("M", [20, 10, 30, 22], 0)])
    after = _block([("ft", [10, 10, 20, 22], 0), ("M", [20, 10, 30, 22], 0)])
    mapping = BlockMap(before, after)
    assert mapping.safe, mapping.reason
    assert mapping.map_range(0, 3, "ftM")["quote"] == "ftM"
    # A synthetic suffix had no separate painted t. Its honest physical anchor
    # is the complete ft glyph, never an invented box for an individual letter.
    assert mapping.map_range(1, 2, "t") == {"start": 0, "end": 2, "quote": "ft", "rects": [[10, 10, 20, 22]]}
    assert not BlockMap(before, _block([("ftt", [10, 10, 20, 22], 0), ("M", [20, 10, 30, 22], 0)])).safe


@pytest.mark.parametrize(("first", "suffix", "next_box"), [
    ([205.306, 97.811, 210.427, 106.867], [210.427, 97.811, 212.881, 106.867], [210.427, 97.811, 215.384, 106.867]),
    ([394.548, 312.804, 401.540, 323.713], [401.540, 312.804, 405.053, 323.713], [401.540, 312.804, 407.224, 323.713]),
])
def test_known_ligature_anchor_cannot_strand_its_proven_legacy_suffix(first, suffix, next_box):
    # Physical samples from GraftMind findings and SeeSawBot Configurations.
    before = _block([("f", first, 0), ("i", suffix, 0), ("n", next_box, 0)])
    after = _block([("fi", first, 0), ("n", next_box, 0)])
    mapping = BlockMap(before, after)
    assert mapping.safe, mapping.reason
    assert mapping.map_range(0, 3, "fin")["quote"] == "fin"
    assert any(edit["kind"] == "expanded_glyph_geometry" for edit in mapping.edits)
    # An individual suffix expands to its confirmed physical ligature, rather
    # than borrowing the old incorrect box belonging to the next letter.
    assert mapping.map_range(1, 2, "i") == {"start": 0, "end": 2, "quote": "fi", "rects": [first]}
    assert mapping.map_range(0, 2, "fi")["quote"] == "fi"
    assert not BlockMap(before, _block([("f", first, 0), ("n", next_box, 0)])).safe
    # A real, separately painted i before the next glyph is a different case.
    distinct = _block([("f", first, 0), ("i", suffix, 0),
                       ("n", [suffix[2], next_box[1], suffix[2] + 5, next_box[3]], 0)])
    assert not BlockMap(distinct, _block([("fi", first, 0),
        ("n", [suffix[2], next_box[1], suffix[2] + 5, next_box[3]], 0)])).safe


def test_alias_suffix_may_share_the_following_real_letter_box():
    first, shared, end = [10, 10, 15, 22], [15, 10, 17, 22], [17, 10, 22, 22]
    before = _block([("f", first, 0), ("li", shared, 0), ("e", end, 0)])
    after = _block([("fl", first, 0), ("i", shared, 0), ("e", end, 0)])
    mapping = BlockMap(before, after)
    assert mapping.safe, mapping.reason
    assert mapping.map_range(0, 4, "flie")["quote"] == "flie"
    assert mapping.map_range(1, 2, "l")["quote"] == "fl"
    assert mapping.map_range(2, 3, "i") == {"start": 2, "end": 3, "quote": "i", "rects": [shared]}
    assert not BlockMap(before, _block([("fl", first, 0), ("e", end, 0)])).safe


@pytest.mark.parametrize("following", [".", " "])
def test_alias_fallback_width_can_exceed_the_following_punctuation_advance(following):
    first, tail, next_box = [10, 10, 15, 22], [15, 10, 18, 22], [15, 10, 16.5, 22]
    before = _block([("f", first, 0), ("f", tail, 0), (following, next_box, 0)])
    after = _block([("ff", first, 0), (following, next_box, 0)])
    mapping = BlockMap(before, after)
    assert mapping.safe, mapping.reason
    assert mapping.map_range(0, 2, "ff") == {"start": 0, "end": 2, "quote": "ff", "rects": [first]}
    assert not BlockMap(before, _block([("f", first, 0), (following, next_box, 0)])).safe


def test_alias_repair_and_restored_line_end_hyphen_use_independent_physical_evidence():
    first, tail, hyphen = [10, 10, 15, 22], [15, 10, 18, 22], [15, 10, 18.2, 22]
    following = ("t", [10, 30, 15, 42], 1)
    before = _block([("f", first, 0), ("f", tail, 0), following])
    after = _block([("ff", first, 0), ("-", hyphen, 0), following])
    mapping = BlockMap(before, after)
    assert mapping.safe, mapping.reason
    assert mapping.map_range(0, 3, "fft")["quote"] == "ff-t"
    assert mapping.map_range(0, 2, "ff")["quote"] == "ff"
    inline = ("t", [18.2, 10, 23.2, 22], 0)
    assert not BlockMap(_block([("f", first, 0), ("f", tail, 0), inline]),
                        _block([("ff", first, 0), ("-", hyphen, 0), inline])).safe


def test_negative_kerning_requires_complete_tail_coverage_not_a_larger_tolerance():
    first = [208.735, 533.873, 214.275, 543.836]
    tail = [214.275, 533.873, 217.044, 543.836]
    following = [214.026, 533.873, 219.007, 543.836]
    before = _block([("f", first, 0), ("i", tail, 0), ("v", following, 0)])
    after = _block([("fi", first, 0), ("v", following, 0)])
    assert BlockMap(before, after).map_range(0, 3, "fiv")["quote"] == "fiv"
    narrow = [214.026, 533.873, 216.0, 543.836]
    assert not BlockMap(_block([("f", first, 0), ("i", tail, 0), ("v", narrow, 0)]),
                        _block([("fi", first, 0), ("v", narrow, 0)])).safe


@pytest.mark.parametrize(("old", "new"), [("abc", "ac"), ("ac", "abc"), ("abc", "adc")])
def test_unexplained_real_letter_deletion_addition_or_substitution_is_rejected(old, new):
    positions = {"a": [10, 10, 15, 22], "b": [15, 10, 20, 22], "c": [20, 10, 25, 22], "d": [15, 10, 20, 22]}
    mapping = BlockMap(_block([(char, positions[char], 0) for char in old]), _block([(char, positions[char], 0) for char in new]))
    assert not mapping.safe
    assert mapping.map_range(0, 1) is None


def test_same_text_at_different_physical_location_or_nonmonotone_order_is_rejected():
    assert not BlockMap(_block(_letters("same")), _block(_letters("same", row=1))).safe
    old = _letters("abab")
    assert not BlockMap(_block(old), _block(old[2:] + old[:2])).safe
    assert not BlockMap(_block(old), _block(old, id="another-block")).safe
    assert not BlockMap(_block(old), _block(old, page=2)).safe


def test_invalid_quotes_boundaries_and_half_surrogates_never_map():
    old = _block(_letters("𝑚x"))
    mapping = BlockMap(old, old)
    assert mapping.safe, mapping.reason
    for start, end in [(None, 2), (True, 2), (-1, 2), (0, 0), (2, 1), (0, 1), (1, 2), (0, 99)]:
        assert mapping.map_range(start, end) is None
    assert mapping.map_range(0, 2, "m") is None
    assert mapping.map_range(0, 2, "𝑚")["quote"] == "𝑚"
    assert mapping.map_boundary(1) is None
    assert mapping.map_boundary(0, "guess") is None


def test_missing_or_inconsistent_geometry_fails_closed():
    block = _block(_letters("word"))
    assert not BlockMap(block, {**block, "geometry": {"boxes": [], "lines": []}}).safe
    assert not BlockMap(block, {**block, "geometry": None}).safe
    assert not BlockMap(None, block).safe


@pytest.mark.parametrize("control", ["\x03", "\x13", "\x15", "\ufffd"])
def test_unknown_placeholder_normalization_keeps_the_same_painted_unknown_glyph(control):
    box = [10, 10, 15, 22]
    before = _block([(control, box, 2)])
    after = _block([("\ufffd", box, 2)])
    mapping = BlockMap(before, after)
    assert mapping.safe, mapping.reason
    assert mapping.map_range(0, 1, control) == {"start": 0, "end": 1, "quote": "\ufffd", "rects": [box]}
    assert mapping.edits[0]["kind"] == "unknown_placeholder"
    assert not BlockMap(before, _block([("\ufffd", box, 3)])).safe
    assert not BlockMap(_block([(control, None, 2)]), _block([("\ufffd", None, 2)])).safe


@pytest.mark.parametrize("visible", ["a", "t", "2", "α", "\t", "\n", "\r"])
def test_visible_letters_and_spacing_cannot_be_disguised_as_unknown_placeholders(visible):
    box = [10, 10, 15, 22]
    assert not BlockMap(_block([(visible, box, 0)]), _block([("\ufffd", box, 0)])).safe
    assert not BlockMap(_block([("t", box, 0)]), _block([("arbitrary", box, 0)])).safe
