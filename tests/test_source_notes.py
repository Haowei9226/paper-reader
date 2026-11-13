import pymupdf as fitz
import pytest

from reader.pdf import slice_text

from .conftest import import_and_parse, sample_pdf


def add_body_marker(page, text, label, *, y=110, superscript=True):
    page.insert_text((48, y), text, fontsize=12)
    x = 48 + fitz.get_text_length(text, fontsize=12)
    page.insert_font(fontname="notefont", fontbuffer=fitz.Font("helv").buffer)
    page.insert_text((x, y - 4 if superscript else y), label, fontsize=7 if superscript else 12, fontname="notefont")
    page.insert_text((x + max(5, len(label) * 5), y), ". More prose follows here.", fontsize=12)
    return fitz.Rect(x - 1, y - 14, x + max(8, len(label) * 7), y + 2)


def footnote_pdf(*, symbol="1", normal_link=False, two_pages=False):
    with fitz.open() as doc:
        for number in range(2 if two_pages else 1):
            page = doc.new_page(width=610, height=792)
            page.insert_text((48, 60), "Source notes fixture", fontsize=16)
            marker = add_body_marker(page, "This claim uses a supporting source", symbol, superscript=not normal_link)
            add_body_marker(page, "This paragraph repeats the supporting source", symbol, y=160)
            add_body_marker(page, "The calculation contains the variable x", "2", y=220)
            add_body_marker(page, "The formula is energy = velocity", "2", y=250)
            page.insert_text((48, 280), "1 A normal numbered list item is ordinary body text.", fontsize=12)
            page.draw_line((48, 620), (195, 620))
            page.insert_text((48, 650), symbol + " A useful explanatory note refers to doi:10.1234/note-paper.", fontsize=9, fontname="notefont")
            page.insert_text((48, 669), "Its remaining sentence stays attached to the same note.", fontsize=9)
            page.insert_text((48, 700), "2 A second note must not make the algebraic exponent clickable.", fontsize=9)
            if normal_link:
                page.insert_link({"kind": fitz.LINK_GOTO, "from": marker, "page": number, "to": fitz.Point(48, 640)})
        doc.set_metadata({"title": "Source notes fixture"})
        return doc.tobytes()


def assert_source_anchors(client, pid, notes):
    for entry in notes["entries"]:
        for anchor in [entry["anchor"], *entry["fragments"]]:
            page = client.get(f"/api/papers/{pid}/pages/{anchor['page']}").json()
            block = next(block for block in page["blocks"] if block["id"] == anchor["block_id"])
            assert slice_text(block["text"], anchor["start"], anchor["end"]) == anchor["quote"]
    for marker in notes["markers"]:
        anchor = marker["anchor"]
        page = client.get(f"/api/papers/{pid}/pages/{anchor['page']}").json()
        block = next(block for block in page["blocks"] if block["id"] == anchor["block_id"])
        assert slice_text(block["text"], anchor["start"], anchor["end"]) == anchor["quote"]


def test_confirmed_footnotes_repeat_and_reset_on_each_page_without_formula_or_list_links(client):
    paper = import_and_parse(client, footnote_pdf(two_pages=True))
    result = client.get(f"/api/papers/{paper['id']}/notes")
    assert result.status_code == 200, result.text
    notes = result.json()
    assert len(notes["entries"]) == 2 and len(notes["markers"]) == 4
    first, second = notes["entries"]
    assert first["label"] == second["label"] == "1" and first["id"] != second["id"]
    assert "remaining sentence" in first["text"]
    assert "second note" not in first["text"]
    assert first["references"][0]["url"] == "https://doi.org/10.1234/note-paper"
    assert all(marker["anchor"]["quote"] == "1" for marker in notes["markers"])
    assert all(marker["evidence"] == "superscript" for marker in notes["markers"])
    assert_source_anchors(client, paper["id"], notes)


def test_native_internal_pdf_link_confirms_a_baseline_note_marker(client):
    paper = import_and_parse(client, footnote_pdf(normal_link=True))
    notes = client.get(f"/api/papers/{paper['id']}/notes").json()
    assert notes["entries"] and any(marker["evidence"] == "pdf_link" for marker in notes["markers"])
    assert_source_anchors(client, paper["id"], notes)


@pytest.mark.parametrize("symbol", ["*", "†", "‡", "§"])
def test_symbol_footnote_stays_separate_from_numeric_bibliography(client, symbol):
    paper = import_and_parse(client, footnote_pdf(symbol=symbol))
    notes = client.get(f"/api/papers/{paper['id']}/notes").json()
    entry, = notes["entries"]
    assert entry["label"] == symbol and entry["id"].startswith("note:footnote:")
    assert len(notes["markers"]) == 2


def test_endnote_continuation_crosses_pages_and_multiple_references_are_resolved(client):
    target = import_and_parse(client, sample_pdf(pages=1))
    client.patch(f"/api/papers/{target['id']}", json={"doi": "10.1234/note-paper"})
    with fitz.open() as doc:
        page = doc.new_page(width=610, height=792)
        page.insert_text((48, 60), "Endnotes fixture", fontsize=16)
        add_body_marker(page, "An explanation appears in the final notes", "1")
        page = doc.new_page(width=610, height=792)
        page.insert_text((48, 80), "Endnotes", fontsize=15)
        page.insert_text((48, 620), "1 An explanatory note cites doi:10.1234/note-paper and", fontsize=10)
        page = doc.new_page(width=610, height=792)
        page.insert_text((48, 90), "also cites https://example.org/another-work on the next page.", fontsize=10)
        page.insert_text((48, 140), "2 Ibid., p. 12. This relation remains unspecified.", fontsize=10)
        page.insert_text((48, 230), "References", fontsize=15)
        page.insert_text((48, 260), "[1] Ada Baker. 2021. A bibliography entry, separate from note one.", fontsize=10)
        doc.set_metadata({"title": "Endnotes fixture"})
        paper = import_and_parse(client, doc.tobytes())
    notes = client.get(f"/api/papers/{paper['id']}/notes").json()
    first, second = notes["entries"]
    assert first["kind"] == second["kind"] == "endnote"
    assert {fragment["page"] for fragment in first["fragments"]} == {2, 3}
    assert len(first["references"]) == 2 and first["references"][0]["library_match"]["paper_id"] == target["id"]
    assert second["references"] == []
    assert notes["markers"][0]["note_id"] == first["id"]
    assert "bibliography entry" not in second["text"]
    assert_source_anchors(client, paper["id"], notes)


def test_plain_body_numbers_and_ordinary_pdf_have_no_source_note_links(client):
    paper = import_and_parse(client)
    notes = client.get(f"/api/papers/{paper['id']}/notes").json()
    assert notes == {"entries": [], "markers": []}


def test_explicit_footnote_continuation_keeps_one_id_across_pages(client):
    with fitz.open() as doc:
        page = doc.new_page(width=610, height=792)
        page.insert_text((48, 60), "Continued footnote fixture", fontsize=16)
        add_body_marker(page, "This passage has a long explanatory note", "1")
        page.draw_line((48, 620), (195, 620))
        page.insert_text((48, 650), "1 The note begins on this page and carries on", fontsize=9)
        page = doc.new_page(width=610, height=792)
        page.insert_text((48, 110), "Ordinary body text continues without another note marker.", fontsize=12)
        page.draw_line((48, 620), (195, 620))
        page.insert_text((48, 650), "1 (continued) The same note finishes on the next page.", fontsize=9)
        page.insert_text((48, 669), "A final explanatory sentence belongs to that note.", fontsize=9)
        doc.set_metadata({"title": "Continued footnote fixture"})
        paper = import_and_parse(client, doc.tobytes())
    notes = client.get(f"/api/papers/{paper['id']}/notes").json()
    entry, = notes["entries"]
    assert {fragment["page"] for fragment in entry["fragments"]} == {1, 2}
    assert "same note finishes" in entry["text"] and len(notes["markers"]) == 1
    assert_source_anchors(client, paper["id"], notes)


def test_chinese_endnotes_resolve_multiple_bibliographic_citations_without_numeric_namespace_mix(client):
    first = import_and_parse(client, sample_pdf(pages=1))
    second = import_and_parse(client, sample_pdf(pages=3))
    client.patch(f"/api/papers/{first['id']}", json={"doi": "10.1234/baker"})
    client.patch(f"/api/papers/{second['id']}", json={"doi": "10.1234/chen"})
    with fitz.open() as doc:
        page = doc.new_page(width=610, height=792)
        page.insert_text((48, 60), "Chinese endnotes fixture", fontsize=16)
        add_body_marker(page, "This argument has an explanatory source note", "1")
        add_body_marker(page, "Another argument discusses two published studies", "2", y=160)
        page.insert_text((48, 330), "注释", fontsize=15, fontname="china-s")
        page.insert_text((48, 365), "1 这里是纯说明，不指定任何文献。", fontsize=10, fontname="china-s")
        page.insert_text((48, 400), "2 See Baker (2021); Chen and Diaz (2022).", fontsize=10)
        page.insert_text((48, 480), "References", fontsize=15)
        page.insert_text((48, 515), "[1] Ada Baker. 2021. First work. doi:10.1234/baker", fontsize=10)
        page.insert_text((48, 550), "[2] Bo Chen and Cara Diaz. 2022. Second work. doi:10.1234/chen", fontsize=10)
        doc.set_metadata({"title": "Chinese endnotes fixture"})
        paper = import_and_parse(client, doc.tobytes())
    notes = client.get(f"/api/papers/{paper['id']}/notes").json()
    plain, cited = notes["entries"]
    assert plain["references"] == []
    assert len(cited["references"]) == 2
    assert {reference["library_match"]["paper_id"] for reference in cited["references"]} == {first["id"], second["id"]}
    assert {marker["note_id"] for marker in notes["markers"]} == {plain["id"], cited["id"]}
    assert_source_anchors(client, paper["id"], notes)


def test_endnote_numeric_bibliography_ranges_and_explicit_note_references_use_distinct_namespaces(client):
    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_text((48, 60), "Note namespaces fixture", fontsize=16)
        add_body_marker(page, "This argument has a detailed explanatory note", "1")
        page.insert_text((48, 300), "Notes", fontsize=15)
        page.insert_text((48, 335), "1 See [1-2]. The phrase note [2] identifies an explanatory note.", fontsize=10)
        page.insert_text((48, 380), "References", fontsize=15)
        page.insert_text((48, 415), "[1] A. Baker. 2021. First work. doi:10.1234/first", fontsize=10)
        page.insert_text((48, 450), "[2] B. Chen. 2022. Second work. doi:10.1234/second", fontsize=10)
        doc.set_metadata({"title": "Note namespaces fixture"})
        paper = import_and_parse(client, doc.tobytes())
    notes = client.get(f"/api/papers/{paper['id']}/notes").json()
    entry, = notes["entries"]
    assert len(entry["references"]) == 2 and entry["id"].startswith("note:endnote:")
    assert {reference["url"] for reference in entry["references"]} == {"https://doi.org/10.1234/first", "https://doi.org/10.1234/second"}


@pytest.mark.parametrize("model", ["GPT-4", "ChatGPT4", "Llama-3.1", "GPT–4"])
def test_model_names_ending_in_digits_can_have_real_footnote_markers(client, model):
    with fitz.open() as doc:
        page = doc.new_page(width=610, height=792)
        page.insert_text((48, 60), "Model name note fixture", fontsize=16)
        # Embed the body font so the Unicode dash remains in the source text.
        body_font = fitz.Font("helv")
        page.insert_font(fontname="body", fontbuffer=body_font.buffer)
        text = "This paragraph discusses the model " + model
        page.insert_text((48, 110), text, fontsize=12, fontname="body")
        x = 48 + body_font.text_length(text, fontsize=12)
        page.insert_text((x, 106), "2", fontsize=7, fontname="body")
        page.insert_text((x + 5, 110), ". More context follows.", fontsize=12, fontname="body")
        add_body_marker(page, "The calculation contains the variable x", "2", y=170)
        add_body_marker(page, "The equation is energy = Llama-3.1", "2", y=230)
        page.draw_line((48, 620), (195, 620))
        page.insert_text((48, 650), "2 The model's explanatory note has an actual source target.", fontsize=9)
        doc.set_metadata({"title": "Model name note fixture"})
        paper = import_and_parse(client, doc.tobytes())
    notes = client.get(f"/api/papers/{paper['id']}/notes").json()
    entry, = notes["entries"]
    marker, = notes["markers"]
    assert entry["label"] == "2" and marker["note_id"] == entry["id"]
    assert marker["anchor"]["quote"] == "2"
    assert_source_anchors(client, paper["id"], notes)


def test_a_model_name_superscript_without_its_note_still_has_no_link(client):
    with fitz.open() as doc:
        page = doc.new_page(width=610, height=792)
        page.insert_text((48, 60), "Missing model note fixture", fontsize=16)
        add_body_marker(page, "This paragraph discusses the model GPT-4", "2")
        page.draw_line((48, 620), (195, 620))
        page.insert_text((48, 650), "1 A different explanatory note is present on this page.", fontsize=9)
        doc.set_metadata({"title": "Missing model note fixture"})
        paper = import_and_parse(client, doc.tobytes())
    assert client.get(f"/api/papers/{paper['id']}/notes").json() == {"entries": [], "markers": []}
