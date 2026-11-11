import json
from types import SimpleNamespace

import pymupdf as fitz

from reader import ai
from reader.academic import source_with_offsets
from reader.formulas import _span_styles
from reader.layout import classify_page
from reader.pdf import length, slice_text
from reader.store import dumps, now, uid
from reader.tables import same_numbers
from .conftest import import_and_parse
from .test_translation_recovery import start


def layout_sample():
    with fitz.open() as doc:
        page = doc.new_page(width=610, height=792)
        page.insert_text((54, 55), "Structured reading fixture", fontsize=16)
        for y in (105, 142, 190, 217):
            page.draw_line((54, y), (555, y))
        page.draw_line((175, 123), (555, 123))
        for x, y, text in [
            (63, 118, "Category"), (255, 118, "Measurements"),
            (63, 136, "Type"), (185, 136, "Condition"), (407, 136, "Count"),
            (63, 156, "G1"), (185, 156, "Week 2"), (407, 156, "18"),
            (185, 177, "Week 4"), (407, 177, "22"),
            (63, 207, "G2"), (185, 207, "Week 6"), (407, 207, "15"),
        ]:
            page.insert_text((x, y), text, fontsize=10)
        page.insert_text((54, 235), "Table 1: Fixture measurements.", fontsize=10)
        page.insert_text((54, 290), "Let x", fontsize=12)
        x = 54 + fitz.get_text_length("Let x", fontsize=12)
        page.insert_text((x + .3, 285.6), "2", fontsize=8)
        page.insert_text((x + 6, 290), " denote a squared value.", fontsize=12)
        page.insert_text((200, 332), "y = a + b", fontsize=12)
        page.insert_text((505, 332), "(1)", fontsize=12)
        page.insert_text((54, 370), "The measurement was repeated in a second trial.", fontsize=12)
        doc.set_metadata({"title": "Structured reading fixture"})
        return doc.tobytes()


def test_table_validation_accepts_a_wrapped_model_name_beside_chinese():
    source = "Team 4 used a GPT- 3-Generated idea."
    assert same_numbers(source, "第4组使用了由GPT-3生成的创意。")
    assert not same_numbers(source, "第4组使用了由GPT-4生成的创意。")
    assert not same_numbers("The change was -3.5%", "变动为3.5%")


def test_table_validation_preserves_the_year_when_translated_as_a_chinese_ordinal():
    assert same_numbers("3rd Year Undergrad", "本科三年级")
    assert same_numbers("1st Year PhD", "博士第一学年")
    assert not same_numbers("2nd Year Undergrad", "本科三年级")
    assert not same_numbers("Monthly / < monthly", "每月 / 少于每月")
    assert same_numbers("Suggestions Acceptance in Phase 1 (%)", "第一阶段建议接受率（%）")
    assert not same_numbers("Suggestions Acceptance in Phase 1 (%)", "第二阶段建议接受率（%）")


def test_math_font_hyphen_in_a_word_is_not_a_translation_placeholder():
    def span(text, font="Times-Roman"):
        return {"font": font, "size": 10, "origin": (0, 10), "chars": [{"c": ch} for ch in text]}

    hyphen = span("-", "Symbol")
    assert not _span_styles([span("open"), hyphen, span("ended")])
    assert _span_styles([span("x"), hyphen, span("y")]) == {id(hyphen): "math"}


def test_stix_prose_and_pdf_control_glyphs_do_not_block_translation():
    prose = {"font": "STIX-Bold", "size": 10, "origin": (0, 10), "chars": [{"c": ch} for ch in "2  \x07Related Work"]}
    variable = {"font": "STIX-Italic", "size": 10, "origin": (0, 10), "chars": [{"c": "x"}]}
    assert _span_styles([prose, variable]) == {id(variable): "math"}
    clean, offsets = source_with_offsets("A\x07B")
    assert clean == "AB" and offsets == [(0, 1), (2, 3)]


def test_numeric_table_cell_keeps_styled_decimals_without_a_model_request():
    class Model:
        provider, model = "deepseek", "fixture"

        def ask(self, *_args, **_options):
            raise AssertionError("Numeric table cell should be kept verbatim")

    value = "4.44 ± 0.77"
    block = {"id": "scores", "text": value, "bbox": (20, 20, 100, 30),
             "table_spans": [(0, len(value), 1, 0)], "notation": [(1, 2, "math"), (4, 6, "math"), (8, 9, "math")]}
    table = SimpleNamespace(caption="Summary statistics", headings=("Mean ± SD",))
    translated, incomplete = ai.translate_table(Model(), {"language": "en"}, table, [block], {}, lambda _: None)
    assert not incomplete and "".join(translated["scores"]["text"]) == value
    assert sum(bool(span.get("notation")) for span in translated["scores"]["segments"]) == 3


def test_table_translation_preserves_numbered_examples_and_decades():
    class Model:
        provider, model = "deepseek", "fixture"

        def ask(self, _instructions, data, **_options):
            segment, = json.loads(data)["segments"]
            assert segment["math"] == {"[[M1]]": "[Apple ($N1)]", "[[M2]]": "mid-1800s"}
            return {"segments": [{"id": segment["id"], "translation": "示例 [[M1]] 最早见于 [[M2]]。"}]}

    value = "The [Apple ($N1)] example dates to the mid-1800s."
    block = {"id": "example", "text": value, "bbox": (20, 20, 100, 30), "table_spans": [(0, len(value), 1, 0)]}
    table = SimpleNamespace(caption="Examples", headings=("Description",))
    translated, incomplete = ai.translate_table(Model(), {"language": "en"}, table, [block], {}, lambda _: None)
    assert not incomplete and "".join(translated["example"]["text"]) == "示例 [Apple ($N1)] 最早见于 mid-1800s。"


def test_table_translation_preserves_quantities_next_to_participant_ids():
    class Model:
        provider, model = "deepseek", "fixture"

        def ask(self, _instructions, data, **_options):
            segment, = json.loads(data)["segments"]
            assert segment["math"] == {"[[M1]]": "P1", "[[M2]]": "5", "[[M3]]": "30"}
            return {"segments": [{"id": segment["id"], "translation": "[[M1]] 完成了 [[M2]] 个编码，用时 [[M3]] 分钟。"}]}

    value = "P1 completed 5 codes in 30 minutes."
    block = {"id": "observation", "text": value, "bbox": (20, 20, 100, 30), "table_spans": [(0, len(value), 1, 0)]}
    table = SimpleNamespace(caption="Participant outcomes", headings=("Observation",))
    translated, incomplete = ai.translate_table(Model(), {"language": "en"}, table, [block], {}, lambda _: None)
    assert not incomplete and "".join(translated["observation"]["text"]) == "P1 完成了 5 个编码，用时 30 分钟。"


def test_original_table_and_equation_preserve_geometry_in_reader_and_export(client):
    pid = import_and_parse(client, layout_sample())["id"]
    page = client.get(f"/api/papers/{pid}/pages/1?geometry=false").json()
    table, = page["tables"]
    assert (table["rows"], table["columns"], table["header_rows"]) == (5, 3, 2)
    assert {(c["row"], c["column"], c["rowspan"], c["colspan"]) for c in table["cells"] if c["rowspan"] > 1 or c["colspan"] > 1} == {
        (0, 0, 2, 1), (0, 1, 1, 2),
    }
    blocks = {b["id"]: b for b in page["blocks"]}
    value = next(c for c in table["cells"] if c["row"] == 3 and c["column"] == 1)
    part, = value["parts"]
    assert slice_text(blocks[part["block_id"]]["text"], part["start"], part["end"]) == "Week 4"
    assert not blocks[part["block_id"]]["geometry"]["boxes"]
    equation, = [b["equation"] for b in blocks.values() if b.get("equation")]
    assert equation["number"] == "1"
    for asset in (equation["asset"], table["asset"]):
        image = client.get("/api/assets/" + asset)
        assert image.status_code == 200 and image.content.startswith(b"\x89PNG\r\n\x1a\n")
    exported = client.get(f"/api/papers/{pid}/export/html").text
    assert '<th rowspan="2" colspan="1">' in exported
    assert '<th rowspan="1" colspan="2">' in exported
    assert "<sup>2</sup>" in exported
    assert 'alt="公式 (1)"' in exported


def test_table_and_inline_notation_repair_only_invalid_model_output(client, monkeypatch):
    pid = import_and_parse(client, layout_sample())["id"]
    cell_calls = []
    notation_attempts = 0

    class Model:
        provider, model, settings = "deepseek", "fixture", {"context_tokens": 4096}

        def __init__(self, *args):
            pass

        def ask(self, instructions, data, **options):
            nonlocal notation_attempts
            payload = json.loads(data)
            result = []
            if "table" in payload:
                cell_calls.append([part["text"] for part in payload["segments"]])
            for part in payload["segments"]:
                text = part["text"]
                assert "y = a + b" not in text and text not in ("(1)", "18", "22", "15", "G1", "G2")
                if part.get("math"):
                    notation_attempts += 1
                    assert set(part["math"].values()) == {"x2"}
                    value = "令其表示平方值。" if notation_attempts == 1 else "令 [[M1]] 表示平方值。"
                elif text == "Category Type":
                    value = "类别"
                elif text.startswith("Week "):
                    week = text.split()[-1]
                    value = f"第 {week} 周" if week != "4" or len(cell_calls) > 1 else "第 9 周"
                else:
                    value = "已翻译"
                result.append({"id": part["id"], "translation": value})
            return {"segments": result}

    monkeypatch.setattr(ai, "Client", Model)
    store = client.store
    result = start(store, store.create_job("translate", pid, {"provider": "deepseek"}))
    assert result["state"] == "done", result["message"]
    assert cell_calls[-1] == ["Week 4"] and notation_attempts == 2
    assert sum(batch.count("Category Type") for batch in cell_calls) == 1
    page = client.get(f"/api/papers/{pid}/pages/1?geometry=false").json()
    table, = page["tables"]
    wrapped = next(c for c in table["cells"] if c["row"] == 0 and c["column"] == 0)
    assert wrapped["joined"] and len(wrapped["parts"]) == 2
    assert client.get(f"/api/papers/{pid}/export/html").text.count(">类别</span>") == 1
    cell = next(c for c in table["cells"] if c["row"] == 3 and c["column"] == 1)
    part, = cell["parts"]
    blocks = {b["id"]: b for b in page["blocks"]}
    assert slice_text(blocks[part["block_id"]]["translation"]["text"], *part["target"]) == "第 4 周"
    paragraph = next(b for b in blocks.values() if "squared value" in b["text"])
    span, = [s for s in paragraph["translation"]["segments"] if s.get("notation")]
    assert slice_text(paragraph["translation"]["text"], span["target_start"], span["target_end"]) == "x2"
    assert not any(b["translation"] for b in blocks.values() if b["reading_role"] == "formula")


def test_centered_title_and_both_author_columns_precede_the_abstract():
    first = "A Study of Generative AI in Small Group"
    last = "Collaborative Dynamics"
    contents = [
        ("title-first", first, (61, 76, 551, 118), "heading"),
        ("title-last", last, (210, 117, 405, 141), "heading"),
        ("left-author", "Alice Example", (155, 149, 240, 163), "heading"),
        ("right-author", "Bob Example", (379, 149, 464, 163), "heading"),
        ("left-org", "Example University", (139, 165, 254, 186), "paragraph"),
        ("right-org", "Example Research Lab", (370, 165, 473, 186), "paragraph"),
        ("abstract-label", "Abstract", (54, 205, 101, 220), "heading"),
        ("abstract", "This study examines how a group of researchers uses an AI system during its collaborative work.", (54, 224, 293, 275), "paragraph"),
        ("intro-label", "1 Introduction", (317, 205, 404, 220), "heading"),
        ("intro", "Prior studies have explored the influence of AI tools on decision-making in individual and group work.", (317, 224, 556, 300), "paragraph"),
    ]
    blocks = [{"id": bid, "text": text, "bbox": box, "kind": kind, "section": ""}
              for bid, text, box, kind in contents]
    paper = {"title": first + " " + last, "authors": "Alice Example; Bob Example", "outline": []}
    classify_page(None, paper, 1, 792, blocks, width=612, detect=False)
    assert [b["id"] for b in blocks if b["reading_role"] == "title"] == ["title-first", "title-last"]
    assert {b["id"] for b in blocks if b["reading_role"] == "extra"} == {"left-author", "right-author", "left-org", "right-org"}
    assert [b["id"] for b in blocks if b["reading_role"] in ("heading", "text")] == [
        "abstract-label", "abstract", "intro-label", "intro",
    ]


def consecutive_tables_sample():
    with fitz.open() as doc:
        page = doc.new_page(width=612, height=792)
        for y in (94, 113, 169, 196, 217, 324):
            page.draw_line((54, y), (558, y))
        page.insert_text((150, 82), "Table 1: Observation participants", fontsize=10)
        page.insert_text((150, 182), "Table 2: Collaborative dimensions", fontsize=10)
        for y, row in [
            (107, ("Participant ID", "Mode", "Discipline", "Frequency")),
            (128, ("P1.1", "In-person", "Computing", "Daily")),
            (145, ("P1.2", "Online", "Design", "Weekly")),
            (162, ("P2.1", "Online", "Computing", "Daily")),
            (211, ("Dimension", "Role", "Description", "Analytical Focus")),
            (232, ("Awareness", "Observer", "Recognise updates", "Visibility")),
            (268, ("Negotiation", "Facilitator", "Discuss alternatives", "Consensus")),
            (304, ("Information Flow", "Coordinator", "Share outputs", "Timing")),
        ]:
            for x, value in zip((61, 160, 260, 413), row):
                page.insert_text((x, y), value, fontsize=10)
        page.insert_text((260, 243), "across the team.", fontsize=10)
        page.insert_text((260, 279), "with the whole team.", fontsize=10)
        page.insert_text((260, 315), "before deciding.", fontsize=10)
        doc.set_metadata({"title": "Consecutive open tables fixture"})
        return doc.tobytes()


def test_consecutive_tables_restore_rows_and_retranslate_old_combined_cells(client, monkeypatch):
    pid = import_and_parse(client, consecutive_tables_sample())["id"]
    path = f"/api/papers/{pid}/pages/1?geometry=false"
    data = client.get(path).json()
    first, second = data["tables"]
    blocks = {b["id"]: b for b in data["blocks"]}
    assert blocks[first["caption_id"]]["text"].startswith("Table 1:")
    assert blocks[second["caption_id"]]["text"].startswith("Table 2:")
    assert [(table["rows"], table["columns"]) for table in (first, second)] == [(4, 4), (4, 4)]
    codes = [part for cell in first["cells"] if cell["column"] == 0 and cell["row"] > 0 for part in cell["parts"]]
    assert [slice_text(blocks[p["block_id"]]["text"], p["start"], p["end"]) for p in codes] == ["P1.1", "P1.2", "P2.1"]
    description = next(c for c in second["cells"] if (c["row"], c["column"]) == (1, 2))
    assert " ".join(slice_text(blocks[p["block_id"]]["text"], p["start"], p["end"]) for p in description["parts"]) == (
        "Recognise updates across the team."
    )
    segments = {}
    old_text = "P1.1、P1.2、P2.1"
    for part in codes:
        segments.setdefault(part["block_id"], []).append({
            "source_start": part["start"], "source_end": part["end"],
            "target_start": 0, "target_end": length(old_text), "cell_group": "old-column-group",
        })
    old_ids = []
    with client.store.connect() as db:
        for bid, spans in segments.items():
            tid = uid()
            old_ids.append(tid)
            db.execute("INSERT INTO translations VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (tid, bid, pid, old_text, dumps(spans), "deepseek", "fixture", 0, 1, now()))
    stale = client.get(path).json()["tables"][0]
    assert all(p["target"] is None for c in stale["cells"] if c["column"] == 0 and c["row"] > 0 for p in c["parts"])

    class Model:
        provider, model, settings = "deepseek", "fixture", {"context_tokens": 4096}

        def __init__(self, *args):
            pass

        def ask(self, instructions, data, **options):
            payload = json.loads(data)
            if "table" in payload:
                assert all("P1.1" not in part["text"] for part in payload["segments"])
            return {"segments": [{"id": p["id"], "translation": p["text"]} for p in payload["segments"]]}

    monkeypatch.setattr(ai, "Client", Model)
    job = client.store.create_job("translate", pid, {"provider": "deepseek", "page_start": 1, "page_end": 1})
    result = start(client.store, job)
    assert result["state"] == "done", result["message"]
    data = client.get(path).json()
    repaired = data["tables"][0]
    blocks = {b["id"]: b for b in data["blocks"]}
    assert [slice_text(blocks[p["block_id"]]["translation"]["text"], *p["target"])
            for c in repaired["cells"] if c["column"] == 0 and c["row"] > 0 for p in c["parts"]] == ["P1.1", "P1.2", "P2.1"]
    with client.store.connect() as db:
        assert all(db.execute("SELECT active FROM translations WHERE id=?", (tid,)).fetchone()[0] == 0 for tid in old_ids)
