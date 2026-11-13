from copy import deepcopy

from reader.flow import refine_reading_flow
from reader.layout import figure_text_blocks
from reader.reading import classify_reading_blocks


def block(name, lines, *, role="text", kind="paragraph"):
    text, boxes, rows = "", [], []
    for number, (value, x, y, size) in enumerate(lines):
        if text:
            text += " "
            boxes.append(None)
            rows.append(number)
        for index, char in enumerate(value):
            rectangle = [x + index * size * .45, y, x + (index + 1) * size * .45, y + size]
            text += char
            boxes.extend([rectangle] * (2 if ord(char) > 0xffff else 1))
            rows.extend([number] * (2 if ord(char) > 0xffff else 1))
    visible = [box for box in boxes if box]
    return {"id": name, "kind": kind, "reading_role": role, "text": text, "page": 2, "section": "Introduction",
            "bbox": [min(b[0] for b in visible), min(b[1] for b in visible), max(b[2] for b in visible), max(b[3] for b in visible)],
            "geometry": {"boxes": boxes, "lines": rows}, "translation": None}


def test_paragraphs_inside_one_canonical_block_use_utf16_boundaries():
    source = block("source", [("An initial sentence discusses 🧭 attention.", 50, 100, 10),
                              ("This line ends the first paragraph.", 50, 113, 10),
                              ("An indented paragraph starts here.", 61, 126, 10),
                              ("Its next line remains in that paragraph.", 50, 139, 10)])
    original = deepcopy(source)
    refine_reading_flow([source], 792)
    expected = len((original["text"].split("An indented")[0]).encode("utf-16-le")) // 2
    assert source["reading_breaks"] == [expected]
    assert {k: source[k] for k in original} == original


def test_research_questions_and_hanging_lines_are_separate_items():
    questions = block("questions", [("RQ1. Is the first condition effective?", 48, 100, 10),
                                   ("RQ2. Does the next condition improve participation", 48, 113, 10)])
    hanging = block("hanging", [("during the activity?", 73, 126, 10)])
    refine_reading_flow([questions, hanging], 792)
    assert questions["reading_breaks"] == [len("RQ1. Is the first condition effective? ")]
    assert hanging["continues_previous"] is True


def test_parenthesized_acronyms_are_not_list_labels():
    source = block("source", [("We studied collaborative learning", 48, 100, 10),
                              ("(CCL) in a single ordinary paragraph.", 48, 113, 10),
                              ("(ii) A separate enumerated item follows.", 48, 126, 10)])
    refine_reading_flow([source], 792)
    assert source["reading_breaks"] == [len("We studied collaborative learning (CCL) in a single ordinary paragraph. ")]


def test_capital_column_continuations_skip_figure_labels_and_small_notes():
    left = block("left", [("The result is artificial creativity (Ivcevic and", 48, 650, 10)])
    label = block("label", [("Diagram annotation", 330, 90, 8)], role="embedded")
    caption = block("caption", [("Figure 1: Original figure", 315, 120, 9)], role="caption")
    note = block("note", [("1 A source note in a smaller font below the column.", 48, 700, 7)])
    right = block("right", [("Grandinetti, 2024), with further context.", 315, 160, 10)])
    refine_reading_flow([left, label, caption, note, right], 792)
    assert note["reading_role"] == "extra" and note["reading_element"] == "footnote"
    assert right["continues_previous"] is True


def test_paragraph_gaps_indents_headings_and_tables_are_boundaries():
    first = block("first", [("A complete paragraph ends at this location.", 48, 100, 10)])
    separate = block("separate", [("a new paragraph starts after a large blank gap.", 48, 140, 10)])
    separate["continues_previous"] = True
    heading = block("heading", [("2 Methods", 48, 165, 12)], role="heading")
    next_block = block("next", [("another source fragment follows this heading.", 48, 181, 10)])
    table = {"id": "table", "kind": "image", "reading_role": "table", "text": "", "bbox": [48, 195, 550, 260]}
    after = block("after", [("with independent body text after the table.", 48, 275, 10)])
    refine_reading_flow([first, separate, heading, next_block, table, after], 792)
    assert all(not b.get("continues_previous") for b in (separate, next_block, after))


def test_mixed_heading_retains_a_large_drop_cap_and_joins_its_body():
    mixed = block("mixed", [("I. INTRODUCTION", 130, 530, 8), ("A", 42, 545, 30)])
    mixed.update(reading_heading=[0, 15], reading_breaks=[16])
    body = block("body", [("NEW wave of technology is emerging.", 66, 546, 10)])
    refine_reading_flow([mixed, body], 792)
    assert mixed["text"] == "I. INTRODUCTION A" and mixed["reading_breaks"] == [16]
    assert body["continues_previous"] is True


def test_figure_masks_retain_source_labels_and_protect_long_body_and_captions():
    image = {"id": "image", "kind": "image", "text": "", "bbox": [335, 95, 505, 175]}
    label = block("label", [("Our focus", 465, 83, 10)])
    caption = block("caption", [("Figure 2: Collaborative analysis", 318, 188, 10)])
    body = block("body", [("This full paragraph has more than one hundred and sixty characters and must stay outside the figure mask. " * 2, 48, 90, 10)])
    assert figure_text_blocks([image, label, caption, body], 612, 792) == {"label"}
    assert figure_text_blocks([image, label, caption], 612, 792, protected={"label"}) == set()


def test_plain_classification_without_database_ids_is_supported():
    source = {"text": "A plain source paragraph is still readable.", "kind": "paragraph", "bbox": [40, 100, 470, 112], "section": ""}
    classify_reading_blocks([source], {"title": "Example", "authors": "", "outline": []}, 1, 792)
    assert source["reading_role"] == "text"


def test_real_figure_labels_keep_export_anchors_but_do_not_enter_translation(client, monkeypatch):
    import json
    import pymupdf as fitz
    from reader import ai
    from .conftest import import_and_parse
    from .test_academic_reader import finish

    with fitz.open() as doc:
        page = doc.new_page(width=612, height=792)
        page.insert_text((48, 65), "Figure mask example", fontsize=18)
        page.draw_rect(fitz.Rect(325, 110, 520, 180))
        page.insert_text((350, 140), "Internal Diagram Label", fontsize=9)
        page.insert_text((315, 200), "Figure 1: A diagram of the study.", fontsize=10)
        page.insert_text((48, 300), "The study provides independent body evidence for the main research question.", fontsize=11)
        page.insert_text((48, 760), "Copyright 2026 Example Journal", fontsize=8)
        doc.set_metadata({"title": "Figure mask example"})
        paper = import_and_parse(client, doc.tobytes())
    data = client.get(f"/api/papers/{paper['id']}/pages/1").json()
    label = next(b for b in data["blocks"] if b["text"] == "Internal Diagram Label")
    assert label["reading_role"] == "embedded" and label["reading_element"] == "figure_text"
    exported = client.get(f"/api/papers/{paper['id']}/export/html").text
    assert exported.count(f'id="block-{label["id"]}"') == 1 and "图内文字" in exported
    calls = []

    class Model:
        provider, model = "local", "isolated-fixture"
        settings = {"context_tokens": 4096}

        def __init__(self, *_args):
            pass

        def ask(self, _instruction, text, **_kwargs):
            payload = json.loads(text)
            calls.extend(segment["text"] for segment in payload["segments"])
            return {"segments": [{"id": segment["id"], "translation": "研究内容。", "alignments": []}
                                 for segment in payload["segments"]]}

    monkeypatch.setattr(ai, "Client", Model)
    finish(client.store, "translate", paper["id"], {"provider": "local"})
    assert any("independent body evidence" in text for text in calls)
    assert all("Internal Diagram Label" not in text and "Copyright" not in text for text in calls)
