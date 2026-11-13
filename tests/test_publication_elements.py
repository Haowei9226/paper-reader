"""Publisher layouts must not change source anchors or consume article prose."""
import copy
from collections import Counter

import pymupdf as fitz

from reader.frontmatter import frontmatter_elements
from reader.margins import document_furniture, page_margin_elements
from reader.reading import classify_reading_blocks


def block(key, text, box, size=10, kind="paragraph"):
    units = len(text.encode("utf-16-le")) // 2
    return {"id": key, "kind": kind, "text": text, "bbox": box, "section": "",
            "geometry": {"boxes": [[box[0], box[1], box[0] + 5, box[1] + size]] * units,
                         "lines": [0] * units}}


def paper(title, authors="", source=""):
    return {"title": title, "authors": authors, "source": source, "outline": []}


def test_shuffled_multicolumn_credits_and_metadata_title_tail_keep_abstract():
    title = "Shared work: Understanding collaboration with language models"
    blocks = [
        block("title1", "Shared work: Understanding collaboration", [60, 70, 520, 90], 20, "heading"),
        block("left", "Ada Smith", [70, 122, 190, 135]),
        block("left-aff", "School of Computing", [70, 140, 210, 151]),
        block("abstract", "This study asks how teams coordinate around shared AI tools.", [55, 215, 550, 228]),
        block("right", "Bo Jones", [370, 122, 490, 135]),
        block("right-aff", "Example University", [360, 140, 510, 151]),
        block("title2", "with language models", [180, 92, 430, 112], 20, "heading"),
        block("city", "Example City, Country", [360, 154, 510, 165]),
    ]
    original = copy.deepcopy(blocks)
    metadata = paper(title + " Ada Smith Bo Jones School of Computing", "Smith, Ada; Jones, Bo")
    classify_reading_blocks(blocks, metadata, 1, 792)
    by_id = {b["id"]: b for b in blocks}
    assert [by_id[key]["reading_role"] for key in ("title1", "title2")] == ["title", "title"]
    assert by_id["title1"]["reading_title"] == title
    assert by_id["left"]["reading_element"] == by_id["right"]["reading_element"] == "author"
    assert by_id["city"]["reading_element"] == "affiliation"
    assert by_id["abstract"]["reading_role"] == "text"
    for old in original:
        assert all(by_id[old["id"]][field] == old[field] for field in ("id", "text", "bbox", "geometry"))


def test_single_dropped_title_glyph_is_tolerated_but_different_title_is_not():
    expected = "Collaborative Qualitative Analysis with Large Language Models"
    blocks = [block("title", "Collaborative Qalitative Analysis with Large Language Models", [50, 80, 550, 105], 20)]
    elements, _, _, _ = frontmatter_elements(blocks, paper(expected), 1, 792)
    assert elements == {0: "title"}
    assert frontmatter_elements(blocks, paper("Completely different research about statistical inference"), 1, 792)[1] == []


def test_publisher_sidebar_and_unlabelled_abstract_are_separate():
    title = "Human creativity in engineering education"
    abstract = "The growing integration of AI raises questions about how teams learn and create new ideas together."
    blocks = [
        block("title", title, [220, 120, 540, 175], 21),
        block("author", "Ada Smith and Bo Jones", [220, 200, 540, 220]),
        block("aff", "Department of Engineering, Example University", [220, 230, 540, 240]),
        block("edited", "EDITED BY\nAn Editor, Example University", [50, 175, 190, 205], 8),
        block("correspond", "*CORRESPONDENCE Ada Smith", [50, 255, 190, 272], 8),
        block("mail", "ada.smith@example.org", [50, 274, 180, 282], 8),
        block("received", "RECEIVED 27 September 2025", [50, 290, 190, 300], 8),
        block("copyright", "COPYRIGHT © 2026 Smith. This is an open-access article.", [50, 390, 190, 450], 8),
        block("abstract", abstract, [220, 270, 545, 650]),
        block("body", "Received feedback changed the way participants chose their next steps.", [220, 680, 545, 705]),
    ]
    classify_reading_blocks(blocks, paper(title, "Ada Smith; Bo Jones"), 1, 842)
    by_id = {b["id"]: b for b in blocks}
    assert all(by_id[key]["reading_role"] == "extra" for key in ("edited", "correspond", "mail", "received", "copyright"))
    assert by_id["abstract"]["reading_role"] == by_id["body"]["reading_role"] == "text"
    assert by_id["abstract"]["text"] == abstract


def test_second_page_article_credits_and_inline_abstract_after_publisher_cover():
    title = "Human collaboration and creative design"
    blocks = [
        block("title", title, [72, 110, 500, 155], 14),
        block("author", "Ada Smith and Bo Jones", [72, 170, 300, 180]),
        block("aff", "Example University", [72, 188, 380, 198], 9),
        block("abstract", "ABSTRACT  Advances in AI bring new questions about shared creative work.", [84, 220, 417, 360], 9),
        block("keywords", "KEYWORDS  Human-AI collaboration; creative design", [433, 220, 520, 263], 9),
        block("intro", "1. Introduction", [72, 418, 200, 430]),
    ]
    classify_reading_blocks(blocks, paper(title, "Ada Smith; Bo Jones"), 2, 794)
    by_id = {b["id"]: b for b in blocks}
    assert by_id["title"]["reading_role"] == "title"
    assert by_id["author"]["reading_element"] == "author"
    assert by_id["aff"]["reading_element"] == "affiliation"
    assert by_id["keywords"]["reading_element"] == "keywords"
    assert by_id["abstract"]["reading_role"] == "text"


def test_keywords_do_not_consume_roman_heading_or_drop_cap():
    blocks = [
        block("keywords", "Index Terms—Artificial intelligence, learning performance.", [42, 476, 293, 505]),
        block("heading", "I. INTRODUCTION A", [42, 528, 203, 574]),
        block("body", "NEW wave of technological innovation is emerging across education.", [42, 546, 293, 591]),
    ]
    classify_reading_blocks(blocks, paper("An article"), 1, 792)
    assert blocks[0]["reading_role"] == "extra"
    assert blocks[1]["reading_role"] == "text"
    assert blocks[1]["reading_heading"] == [0, 15]
    assert blocks[1]["reading_breaks"] == [16]
    assert blocks[1]["text"] == "I. INTRODUCTION A"
    assert blocks[2]["text"].startswith("NEW wave")


def test_repeated_journal_furniture_and_offset_folios_preserve_body_and_notes(tmp_path):
    path = tmp_path / "publisher.pdf"
    with fitz.open() as doc:
        for number in range(1, 7):
            page = doc.new_page(width=612, height=792)
            header = f"{379 + number} JOURNAL OF COLLABORATIVE RESEARCH" if number % 2 else f"SMITH ET AL.: SHARED WORK {379 + number}"
            page.insert_text((50, 40), header, fontsize=8)
            page.insert_text((50, 84), "II. METHODS", fontsize=11)
            page.insert_text((50, 150), "Ordinary article prose remains part of the main reading flow.", fontsize=10)
            page.insert_text((50, 200), "JOURNAL OF COLLABORATIVE RESEARCH", fontsize=10)
            page.insert_text((50, 680), "1 This is a genuine note explaining the article's method.", fontsize=8)
            page.insert_text((300, 735), str(379 + number), fontsize=8)
            page.insert_text((50, 770), "Authorized licensed use limited to: Example University.", fontsize=7)
        doc.save(path)
    furniture = document_furniture(path)
    assert len(furniture) == 6
    assert Counter(item["element"] for rows in furniture.values() for item in rows) == {"header": 6, "footer": 6, "page_number": 6}
    with fitz.open(path) as doc:
        page = doc[2]
        blocks = [block(str(index), raw[4].strip(), list(raw[:4])) for index, raw in enumerate(page.get_text("blocks")) if raw[6] == 0]
    before = copy.deepcopy(blocks)
    elements = page_margin_elements(blocks, 792, 3, repeated=furniture)
    matched = {blocks[index]["text"]: element for index, element in elements.items()}
    assert matched["382"] == "page_number"
    assert matched["382 JOURNAL OF COLLABORATIVE RESEARCH"] == "header"
    assert "II. METHODS" not in matched
    assert "JOURNAL OF COLLABORATIVE RESEARCH" not in matched
    assert not any("genuine note" in text for text in matched)
    assert blocks == before
    assert document_furniture(None) == document_furniture(tmp_path / "absent.pdf") == {}


def test_page_of_total_and_edge_numeric_table_are_not_confused(tmp_path):
    path = tmp_path / "pagination.pdf"
    with fitz.open() as doc:
        for number in range(1, 4):
            page = doc.new_page(width=612, height=792)
            page.insert_text((45, 40), "16", fontsize=8)
            page.insert_text((80, 40), f"Page {number} of 3", fontsize=8)
            page.insert_text((120, 90), str(number + 20), fontsize=10)
            page.insert_text((220, 90), str(number + 30), fontsize=10)
            page.insert_text((50, 150), "A paragraph explaining the numeric table above this text.", fontsize=10)
        doc.save(path)
    result = document_furniture(path)
    assert all(len(rows) == 1 and rows[0]["element"] == "page_number" for rows in result.values())
    with fitz.open(path) as doc:
        raw = doc[1].get_text("dict")["blocks"]
        assert len(raw[0]["lines"]) == 2  # Springer article number + folio.
        source = [block("folio", "16 Page 2 of 3", list(raw[0]["bbox"]))]
        assert page_margin_elements(source, 792, 2, repeated=result) == {0: "page_number"}
