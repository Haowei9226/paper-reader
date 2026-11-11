import pymupdf as fitz
import pytest

from reader.pdf import length, slice_text
from .conftest import import_and_parse


@pytest.mark.parametrize(("embedded", "expected"), [(True, "ABZ"), (False, "\ufffdBZ")])
def test_import_survives_incomplete_pdf_unicode_mapping(client, embedded, expected):
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
            f"3 beginbfchar\n<{codes[0]}> <D835>\n<{codes[1]}> <0042>\n<{codes[2]}> <005A>\nendbfchar\nendcmap\n"
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
