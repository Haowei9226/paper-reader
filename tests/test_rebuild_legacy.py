import copy
import json
import zlib

import pymupdf as fitz

from reader.anchor_migration import plan_anchor_migration
from reader.rebuild_legacy import legacy_candidates, retain_legacy_blocks
from .test_anchor_migration import library, repaired, source, translation, units


PAPER = {"title": "Shared Systems", "authors": "", "outline": []}
PAGES = {1: {"number": 1, "width": 612, "height": 792},
         2: {"number": 2, "width": 612, "height": 792},
         3: {"number": 3, "width": 612, "height": 792}}


def raw_block(bid, text, bbox, page=1, kind="paragraph"):
    return {"id": bid, "paper_id": "paper", "page": page, "position": 0, "kind": kind,
            "text": text, "bbox": json.dumps(bbox), "geometry": None, "asset": None, "section": ""}


def publisher_pdf(path):
    with fitz.open() as doc:
        for _ in range(3):
            page = doc.new_page(width=612, height=792)
            page.insert_text((50, 40), "JOURNAL OF SHARED SYSTEMS", fontsize=8)
            page.insert_text((50, 160), "The body remains ordinary article prose.", fontsize=10)
        doc.save(path)


def test_missing_header_translation_can_survive_while_page_body_is_updated(tmp_path):
    path = tmp_path / "publisher.pdf"
    publisher_pdf(path)
    with fitz.open(path) as doc:
        header_raw = doc[0].get_text("blocks")[0]
    header = raw_block("header", "JOURNAL OF SHARED SYSTEMS", list(header_raw[:4]))
    body = source("body", "The ﬁsh form teams.")
    db = library([header, body])
    translation(db, "existing", "header", "必须保留的原有译文", [
        {"source_start": 0, "source_end": units(header["text"]), "target_start": 0, "target_end": 12}])
    db.commit()
    new = [repaired(body)]
    assert plan_anchor_migration(db, "paper", new).unsafe_pages == {1}
    snapshot = copy.deepcopy([header, body])
    updated, retained = retain_legacy_blocks(path, PAPER, snapshot, new, PAGES, {"header"})
    assert retained == {"header": "header"}
    assert next(b for b in updated if b["id"] == "body")["text"] == "The fish form teams."
    retained_header = next(b for b in updated if b["id"] == "header")
    assert retained_header == {**header, "kind": "legacy"}
    assert plan_anchor_migration(db, "paper", updated).safe
    assert snapshot == [header, body] and new == [repaired(body)]
    assert db.execute("SELECT text FROM translations WHERE id='existing'").fetchone()[0] == "必须保留的原有译文"


def test_plain_body_is_not_a_legacy_candidate_or_copied_as_duplicate():
    old = [raw_block("body", "Teams coordinate their work.", [50, 200, 500, 220]),
           raw_block("long", "This is ordinary paragraph prose. " * 12, [50, 250, 550, 420])]
    new = [{**old[0], "text": "Teams coordinate different tasks."}]
    assert legacy_candidates(None, PAPER, old, PAGES) == {}
    updated, retained = retain_legacy_blocks(None, PAPER, old, new, PAGES, {"body", "long"})
    assert updated == new and retained == {}


def test_only_short_fully_contained_figure_labels_qualify_not_captions_or_expansion():
    blocks = [
        raw_block("image", "", [50, 100, 450, 250], kind="image"),
        raw_block("label", "Coder 1", [80, 120, 140, 135]),
        raw_block("caption", "Figure 1. The comparison.", [60, 220, 300, 235]),
        raw_block("unpunctuated-caption", "Fig. 3 An unpunctuated caption", [60, 205, 300, 218]),
        raw_block("below", "Ordinary article words", [60, 270, 300, 285]),
        raw_block("actual-caption", "Figure 2. A caption below the image", [50, 300, 440, 315]),
        raw_block("body", "This sentence explains the methods used in the study.", [80, 160, 400, 180]),
        raw_block("long", "Several participants discussed these results and explained their own perspectives " * 3, [80, 185, 400, 210]),
        raw_block("formula", "x = y + z", [180, 120, 250, 135]),
        raw_block("sum", "x+y", [270, 120, 300, 135]),
        raw_block("table", "Group A", [180, 150, 250, 165], kind="table"),
    ]
    candidates = legacy_candidates(None, PAPER, blocks, PAGES)
    assert candidates == {"label": "figure_text"}
    updated, retained = retain_legacy_blocks(None, PAPER, blocks, [], PAGES, set(candidates) | {"caption", "below", "body", "formula", "table"})
    assert retained == {"label": "figure_text"} and len(updated) == 1


def test_table_image_and_main_title_are_protected_even_inside_painted_regions():
    blocks = [
        raw_block("image", "", [50, 90, 450, 250], kind="image"),
        raw_block("cell", "27", [80, 120, 100, 135]),
        raw_block("table-caption", "Table 1. Participant data", [50, 265, 440, 280]),
        raw_block("title-image", "", [50, 300, 500, 420], kind="image"),
        raw_block("title", "Shared Systems", [100, 315, 430, 340], kind="heading"),
    ]
    assert legacy_candidates(None, PAPER, blocks, PAGES) == {}


def test_existing_legacy_is_inherited_exactly_without_new_reference_or_reclassification():
    old = [raw_block("previous", "An old retained source label", [90, 140, 250, 160], kind="legacy")]
    before = copy.deepcopy(old)
    result, retained = retain_legacy_blocks(None, PAPER, old, [], PAGES)
    assert result == old and retained == {"previous": "existing_legacy"}
    staged = [{**old[0], "kind": "paragraph", "text": "A different re-extraction"}]
    result, retained = retain_legacy_blocks(None, PAPER, old, staged, PAGES)
    assert result == old and retained == {"previous": "existing_legacy"}
    assert old == before and staged[0]["text"] == "A different re-extraction"


def test_same_id_furniture_replacement_requires_exact_page_and_decoded_bbox():
    old = [raw_block("footer", "Manuscript submitted to ACM", [50, 660, 400, 668])]
    candidate = {**old[0], "text": "Manuscript submitted to ACM 1", "bbox": [50, 660, 400, 668]}
    result, retained = retain_legacy_blocks(None, PAPER, old, [candidate], PAGES, {"footer"})
    assert result == [{**old[0], "kind": "legacy"}] and retained == {"footer": "footer"}
    for changed in ({**candidate, "page": 2}, {**candidate, "bbox": [50, 660, 401, 668]}):
        result, retained = retain_legacy_blocks(None, PAPER, old, [changed], PAGES, {"footer"})
        assert result == [changed] and retained == {}


def test_confirmed_margin_fragment_requires_same_position_glyph_subset():
    # Explicit ACM submission furniture needs no external document fixture.
    old = source("header", "Manuscript submitted to ACM 3", page=3)
    old_geo = json.loads(zlib.decompress(old["geometry"]))
    old["bbox"] = json.dumps([40, 660, 230, 670])
    old_geo["boxes"] = [[b[0], 660, b[2], 670] if b else None for b in old_geo["boxes"]]
    old["geometry"] = zlib.compress(json.dumps(old_geo).encode())
    box = old_geo["boxes"][-1]
    staged = {**old, "text": "3", "bbox": list(box),
              "geometry": zlib.compress(json.dumps({"boxes": [box], "lines": [0]}).encode())}
    result, retained = retain_legacy_blocks(None, PAPER, [old], [staged], PAGES, {"header"})
    assert result == [{**old, "kind": "legacy"}] and retained == {"header": "footer"}
    shifted = {**staged, "geometry": zlib.compress(json.dumps({"boxes": [[box[0] - 1, 660, box[2] - 1, 670]], "lines": [0]}).encode())}
    assert retain_legacy_blocks(None, PAPER, [old], [shifted], PAGES, {"header"}) == ([shifted], {})
    # The same containment shortcut is never applied to a figure label.
    figure = raw_block("image", "", [30, 640, 300, 700], page=3, kind="image")
    label = {**old, "text": "Coder 1"}
    assert retain_legacy_blocks(None, PAPER, [figure, label], [staged], PAGES, {"header"}) == ([staged], {})
