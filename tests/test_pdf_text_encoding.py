import pymupdf as fitz
import pytest

from reader.pdf import extract_block, length, recover_pdf_glyphs, scalar_text, slice_text
from reader.pdf_text import explicit_ligature
from .conftest import import_and_parse


@pytest.mark.parametrize(("embedded", "bad_code", "expected"), [
    (True, "D835", "ABZ"), (False, "D835", "\ufffdBZ"),
])
def test_import_survives_incomplete_pdf_unicode_mapping(client, embedded, expected, bad_code):
    with fitz.open() as doc:
        page = doc.new_page()
        name = "helv"
        codes = [f"{ord(char):02X}" for char in "ABZ"]
        if embedded:
            font = fitz.Font("tibo")
            name = "embedded"
            page.insert_font(fontname=name, fontbuffer=font.buffer)
            codes = [f"{font.has_glyph(ord(char)):04X}" for char in "ABZ"]
        page.insert_text((45, 55), "ABZ", fontname=name)
        ref = page.get_fonts()[0][0]
        cmap = doc.get_new_xref()
        doc.update_object(cmap, "<<>>")
        width = len(codes[0])
        doc.update_stream(cmap, (
            f"begincmap\n1 begincodespacerange\n<{'0' * width}> <{'F' * width}>\nendcodespacerange\n"
            f"3 beginbfchar\n<{codes[0]}> <{bad_code}>\n<{codes[1]}> <0042>\n<{codes[2]}> <005A>\nendbfchar\nendcmap\n"
        ).encode("ascii"))
        doc.xref_set_key(ref, "ToUnicode", f"{cmap} 0 R")
        doc.new_page().insert_text((45, 55), "Import continues on this page.")
        pdf = doc.tobytes()

    paper = import_and_parse(client, pdf)
    assert paper["parse_state"] == "ready"
    first = client.get(f"/api/papers/{paper['id']}/pages/1").json()["blocks"]
    assert "".join(block["text"] for block in first) == expected
    for block in first:
        assert len(block["geometry"]["boxes"]) == length(block["text"])
        assert slice_text(block["text"], 0, length(block["text"])) == block["text"]
    second = client.get(f"/api/papers/{paper['id']}/pages/2").json()["blocks"]
    assert second[0]["text"] == "Import continues on this page."


@pytest.mark.parametrize("broken", ["\ufffd", "\x00", "\x08", "\n"])
def test_raw_invalid_scalars_use_painted_glyph_trace(broken):
    # MuPDF versions differ in whether malformed ToUnicode values become a
    # replacement/control scalar or an encoding fallback; exercise the raw form.
    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_text((45, 55), "ABZ")
        blocks = page.get_text("rawdict")["blocks"]
        blocks[0]["lines"][0]["spans"][0]["chars"][0]["c"] = broken
        recover_pdf_glyphs(page, blocks)
        text, geo, _ = extract_block(blocks[0], page)
        assert text == "ABZ"
        assert len(geo["boxes"]) == length(text)


def _encoded_pdf(text, replacements):
    """A real embedded font with a deliberately incomplete/expanded ToUnicode map."""
    with fitz.open() as doc:
        page = doc.new_page()
        font = fitz.Font("tibo")
        page.insert_font(fontname="embedded", fontbuffer=font.buffer)
        page.insert_text((45, 55), text, fontname="embedded")
        ref = page.get_fonts()[0][0]
        cmap = doc.get_new_xref()
        doc.update_object(cmap, "<<>>")
        mappings = []
        for char in set(text):
            encoded = replacements.get(char, char).encode("utf-16-be").hex()
            mappings.append(f"<{font.has_glyph(ord(char)):04x}> <{encoded}>")
        doc.update_stream(cmap, ("begincmap\n1 begincodespacerange\n<0000> <ffff>\nendcodespacerange\n"
                                f"{len(mappings)} beginbfchar\n" + "\n".join(mappings) + "\nendbfchar\nendcmap").encode())
        doc.xref_set_key(ref, "ToUnicode", f"{cmap} 0 R")
        return doc.tobytes()


@pytest.mark.parametrize("mapped", ["f", "fi", "\ufb01"])
def test_embedded_ligature_recovers_truncated_mapping_without_doubling(mapped):
    with fitz.open(stream=_encoded_pdf("\ufb01rst \ufb02ow", {"\ufb01": mapped, "\ufb02": "f"})) as doc:
        page = doc[0]
        blocks = page.get_text("rawdict")["blocks"]
        recover_pdf_glyphs(page, blocks)
        text, geo, _ = extract_block(blocks[0], page)
        assert text == "first flow"
        assert len(geo["boxes"]) == length(text)
        assert geo["boxes"][0] == geo["boxes"][1]
        assert geo["boxes"][6] == geo["boxes"][7]
    assert explicit_ligature("Q_u") == "Qu"
    assert explicit_ligature("f_f_i") == "ffi"
    assert explicit_ligature("unknown_glyph") is None


@pytest.mark.parametrize("mapped", ["f", "fi"])
def test_trace_expansion_tail_with_fallback_width_does_not_duplicate_or_swallow_painted_letter(mapped):
    with fitz.open(stream=_encoded_pdf("\ufb01irst", {"\ufb01": mapped})) as doc:
        page = doc[0]
        blocks = page.get_text("rawdict")["blocks"]
        chars = blocks[0]["lines"][0]["spans"][0]["chars"]
        if mapped == "fi":
            # Some embedded fonts give this unpainted ToUnicode suffix a
            # nonzero fallback bbox, though the following real i starts here too.
            assert chars[1]["c"] == "i"
            box = chars[1]["bbox"]
            chars[1]["bbox"] = (*box[:2], box[0] + 2, box[3])
        recover_pdf_glyphs(page, blocks)
        text, geo, _ = extract_block(blocks[0], page)
        assert text == "fiirst"
        assert geo["boxes"][0] == geo["boxes"][1]
        assert geo["boxes"][2] != geo["boxes"][1]
        recover_pdf_glyphs(page, blocks)
        assert extract_block(blocks[0], page)[0] == text


def test_unpainted_zero_width_layout_separator_in_unicode_expansion_is_preserved():
    with fitz.open(stream=_encoded_pdf("Chat Box", {"t": "t\n"})) as doc:
        page = doc[0]
        blocks = page.get_text("rawdict")["blocks"]
        recover_pdf_glyphs(page, blocks)
        text, geo, _ = extract_block(blocks[0], page)
        assert text == "Chat\n Box"
        assert len(geo["boxes"]) == length(text)


def test_scientific_unicode_and_utf16_geometry_are_not_compatibility_normalized():
    expected = "\U0001d45a\u210f\u00b2"
    with fitz.open(stream=_encoded_pdf("ABZ", dict(zip("ABZ", expected)))) as doc:
        page = doc[0]
        blocks = page.get_text("rawdict")["blocks"]
        recover_pdf_glyphs(page, blocks)
        text, geo, _ = extract_block(blocks[0], page)
        assert text == expected
        assert len(geo["boxes"]) == length(text) == 4
        assert geo["boxes"][0] == geo["boxes"][1]
        assert slice_text(text, 0, 2) == expected[0]
        assert slice_text(text, 2, 4) == expected[1:]
        with pytest.raises(ValueError):
            slice_text(text, 0, 1)
    assert scalar_text("\ud835\udc5a") == expected[0]


@pytest.mark.parametrize("hyphen", ["-", "\u00ad"])
def test_line_join_preserves_literal_hyphen_and_its_anchor(hyphen):
    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_text((45, 55), "semi-\nstructured")
        raw = page.get_text("rawdict")["blocks"][0]
        for line in raw["lines"]:
            for span in line["spans"]:
                for char in span["chars"]:
                    if char["c"] == "-":
                        char["c"] = hyphen
        text, geo, _ = extract_block(raw, page)
        assert text == f"semi{hyphen}structured"
        assert slice_text(text, 4, 5) == hyphen
        assert geo["boxes"][4] is not None
        assert len(geo["boxes"]) == length(text)


def _raw_line(text, *, size=10, gaps=(), gap_em=.145, tracking=0, font="Times-Roman", direction=(1, 0)):
    chars, x = [], 40
    for index, char in enumerate(text):
        x += (size * gap_em if index in gaps else size * tracking)
        chars.append({"c": char, "origin": (x, 50), "bbox": (x, 40, x + size * .45, 53)})
        x += size * .45
    return {"dir": direction, "spans": [{"font": font, "size": size, "chars": chars}]}


@pytest.mark.parametrize("size", [7, 10, 18])
def test_omitted_prose_spaces_use_font_scaled_gaps_and_null_geometry(size):
    with fitz.open() as doc:
        page = doc.new_page()
        x = 40
        for word in ("students", "that", "after", "eight", "weeks"):
            page.insert_text((x, 50), word, fontname="tiro", fontsize=size)
            x += fitz.get_text_length(word, fontname="tiro", fontsize=size) + size * .145
        raw = page.get_text("rawdict")["blocks"][0]
        assert "".join(c["c"] for line in raw["lines"] for span in line["spans"] for c in span["chars"]) == "studentsthataftereightweeks"
        text, geo, _ = extract_block(raw, page)
        assert text == "students that after eight weeks"
        assert len(geo["boxes"]) == length(text)
        assert all(box is None for char, box in zip(text, geo["boxes"]) if char == " ")


@pytest.mark.parametrize(("text", "options"), [
    ("Tracking", {"tracking": .145}),
    ("normalword", {"gaps": (5,), "gap_em": .07, "tracking": .03}),
    ("abcde=fghi", {"gaps": (7,)}),
    ("abcdeghijk", {"gaps": (5,), "font": "CambriaMath"}),
    ("https://example.test/longpath", {"gaps": (15,)}),
    ("doi:10.1145/1234567.1234568", {"gaps": (4, 19)}),
    ("score0.12345percent", {"gaps": (5, 7)}),
    ("中华人民共和国", {"gaps": (3,)}),
    ("verticaltext", {"gaps": (8,), "direction": (0, -1)}),
])
def test_space_recovery_does_not_split_tracking_math_urls_cjk_or_vertical(text, options):
    line = _raw_line(text, **options)
    with fitz.open() as doc:
        assert extract_block({"lines": [line]}, doc.new_page())[0] == text


def test_overprinted_accent_composes_only_when_geometry_supports_it():
    with fitz.open() as doc:
        page = doc.new_page()
        raw = {"lines": [_raw_line("J\u00b4eremy \u00b4quote")]}
        chars = raw["lines"][0]["spans"][0]["chars"]
        # The first acute is painted over e; the second is a separate literal mark.
        for char in chars[2:]:
            char["origin"] = (char["origin"][0] - 4.5, 50)
            char["bbox"] = (char["bbox"][0] - 4.5, 40, char["bbox"][2] - 4.5, 53)
        chars[1]["origin"] = (chars[2]["origin"][0] + .5, 49.8)
        chars[1]["bbox"] = (chars[2]["bbox"][0] + .5, 39, chars[2]["bbox"][2] - .5, 51)
        recover_pdf_glyphs(page, [raw])
        text, geo, _ = extract_block(raw, page)
        assert text == "Jéremy \u00b4quote"
        assert len(geo["boxes"]) == length(text)
