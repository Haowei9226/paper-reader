import copy
import json
import sqlite3
import zlib

import pytest

from reader.anchor_migration import apply_anchor_migration, plan_anchor_migration
from reader.pdf_text import canonical_glyph


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def units(text):
    return len(text.encode("utf-16-le")) // 2


def interval(text, quote):
    start = text.index(quote)
    return units(text[:start]), units(text[:start + len(quote)])


def source(bid, text, page=1):
    boxes, x = [], 40
    for char in text:
        rect = None if char.isspace() else [x, 100, x + (8 if char == "ﬁ" else 5), 110]
        boxes.extend([rect] * units(char))
        x += 9 if char == "ﬁ" else 6
    return {"id": bid, "paper_id": "paper", "page": page, "position": 0, "kind": "paragraph",
            "text": text, "bbox": dumps([40, 100, x, 110]),
            "geometry": zlib.compress(dumps({"boxes": boxes, "lines": [0] * len(boxes)}).encode()),
            "asset": None, "section": "Methods"}


def repaired(block):
    result = dict(block)
    geo, boxes, offset = json.loads(zlib.decompress(block["geometry"])), [], 0
    for char in block["text"]:
        boxes.extend([geo["boxes"][offset]] * units(canonical_glyph(char)))
        offset += units(char)
    result["text"] = canonical_glyph(block["text"])
    result["geometry"] = zlib.compress(dumps({"boxes": boxes, "lines": [0] * len(boxes)}).encode())
    return result


def library(blocks):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript("""
        PRAGMA foreign_keys=ON;
        CREATE TABLE papers(id TEXT PRIMARY KEY,locator TEXT NOT NULL);
        CREATE TABLE blocks(id TEXT PRIMARY KEY,paper_id TEXT,page INTEGER,position INTEGER,kind TEXT,
            text TEXT,bbox TEXT,geometry BLOB,asset TEXT,section TEXT);
        CREATE TABLE translations(id TEXT PRIMARY KEY,block_id TEXT REFERENCES blocks(id),paper_id TEXT,
            text TEXT,segments TEXT,provider TEXT,model TEXT,manual INTEGER,active INTEGER,created REAL);
        CREATE TABLE annotations(id TEXT PRIMARY KEY,paper_id TEXT,targets TEXT,translated_targets TEXT,
            quote TEXT,note TEXT,color TEXT,alignment TEXT,created REAL,updated REAL);
        CREATE TABLE ai_results(id TEXT PRIMARY KEY,paper_id TEXT,kind TEXT,question TEXT,answer TEXT,
            citations TEXT,provider TEXT,model TEXT,scope TEXT,created REAL);
        CREATE TABLE jobs(id TEXT PRIMARY KEY,paper_id TEXT,kind TEXT,state TEXT,payload TEXT,
            progress INTEGER,total INTEGER,message TEXT,result TEXT,created REAL,updated REAL);
    """)
    locator = {"page": 1, "block_id": blocks[0]["id"], "mode": "html", "side": "translation",
               "offset": .53708, "scroll": .56989, "page_fraction": .41805, "saved_at": 1794417671981}
    db.execute("INSERT INTO papers VALUES(?,?)", ("paper", dumps(locator)))
    for block in blocks:
        db.execute("INSERT INTO blocks VALUES(?,?,?,?,?,?,?,?,?,?)", tuple(block.values()))
    db.commit()
    return db


def translation(db, tid, bid, text, spans, manual=0, active=1):
    db.execute("INSERT INTO translations VALUES(?,?,?,?,?,?,?,?,?,?)",
               (tid, bid, "paper", text, dumps(spans), "deepseek", "historical-model", manual, active, 1234.5))


def target(block, quote):
    start, end = interval(block["text"], quote)
    return {"block_id": block["id"], "start": start, "end": end, "quote": quote,
            "prefix": block["text"].encode("utf-16-le")[max(0, start - 30) * 2:start * 2].decode("utf-16-le", errors="ignore"),
            "page": block["page"]}


def annotation(db, aid, targets, translated=(), quote=None):
    quote = "\n".join(t["quote"] for t in targets) if quote is None else quote
    db.execute("INSERT INTO annotations VALUES(?,?,?,?,?,?,?,?,?,?)",
               (aid, "paper", dumps(targets), dumps(list(translated)), quote,
                "A note written by the reader", "green", "manual", 10.0, 11.0))


def rows(db, table):
    return [dict(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY id")]


def test_all_translation_versions_cross_block_ranges_and_target_annotations_survive():
    old = [source("left", "😀 The ﬁsh form teams."), source("right", "Another ﬁsh follows.", 2)]
    new = [repaired(block) for block in old]
    db = library(old)
    peer_start, peer_end = interval(old[1]["text"], "ﬁsh")
    cross = {"source_start": peer_start, "source_end": peer_end, "target_start": 3, "target_end": 6,
             "source_block_id": "right", "sentence_join": "joined", "notation": True,
             "notation_styles": [[3, 6, "math"]], "cell_group": "table-cell"}
    spans = [{"source_start": 0, "source_end": units(old[0]["text"]), "target_start": 0, "target_end": 9}, cross]
    translation(db, "historical", "left", "原有旧版译文不会改变", spans, active=0)
    translation(db, "manual", "left", "人工译文不会改变", spans, manual=1)
    mirror = {key: value for key, value in cross.items() if key != "source_block_id"}
    mirror["target_block_id"] = "left"
    translation(db, "placeholder", "right", "", [mirror])
    selected = target(old[0], "ﬁsh")
    selected["rects"] = [[1, 2, 3, 4]]
    translated = [{"block_id": "left", "translation_id": "historical", "start": 2, "end": 4, "quote": "旧版"}]
    annotation(db, "note", [selected, target(old[1], "follows")], translated)
    before = {table: rows(db, table) for table in ("translations", "annotations", "papers")}
    db.commit()
    changed = db.total_changes
    plan = plan_anchor_migration(db, "paper", new)
    assert plan.safe, plan.issues
    assert db.total_changes == changed  # Planning is read-only.
    db.execute("BEGIN IMMEDIATE")
    apply_anchor_migration(db, plan)
    assert db.in_transaction
    for previous, current in zip(before["translations"], rows(db, "translations")):
        assert {key: value for key, value in current.items() if key != "segments"} == {
            key: value for key, value in previous.items() if key != "segments"}
        for a, b in zip(json.loads(previous["segments"]), json.loads(current["segments"])):
            assert {key: value for key, value in a.items() if key not in ("source_start", "source_end")} == {
                key: value for key, value in b.items() if key not in ("source_start", "source_end")}
            assert b["source_end"] == a["source_end"] + 1
    ann = rows(db, "annotations")[0]
    assert ann["translated_targets"] == before["annotations"][0]["translated_targets"]
    assert ann["quote"] == "fish\nfollows"
    migrated = json.loads(ann["targets"])
    assert migrated[0]["quote"] == "fish" and migrated[0]["end"] == selected["end"] + 1
    assert migrated[0]["rects"] != selected["rects"]
    assert migrated[1]["start"] == target(old[1], "follows")["start"] + 1
    for key in ("id", "note", "color", "alignment", "created", "updated"):
        assert ann[key] == before["annotations"][0][key]
    assert rows(db, "papers") == before["papers"]
    db.rollback()
    assert rows(db, "annotations") == before["annotations"]


def test_quote_only_ai_and_known_job_paths_change_but_answers_and_arbitrary_json_do_not():
    old = source("body", "The ﬁsh form teams.")
    db = library([old])
    citation = {"block_id": "body", "quote": "ﬁsh", "page": 1}
    db.execute("INSERT INTO ai_results VALUES(?,?,?,?,?,?,?,?,?,?)",
               ("ai", "paper", "summary", "What about ﬁsh?", "An existing answer about ﬁsh.", dumps([citation]), "local", "old", "all", 9))
    payload = {"block_ids": ["body"], "question": "Explain ﬁsh", "source_targets": [target(old, "ﬁsh")],
               "custom": {"block_id": "body", "start": 4, "end": 7, "quote": "ﬁsh"}}
    result = {"parts": [{"answer": "Answer about ﬁsh", "citations": [citation]}],
              "terms": [{"source": "ﬁsh", "target": "鱼"}], "completed": ["body"]}
    db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
               ("job", "paper", "reading", "cancelled", dumps(payload), 1, 3, "Old message", dumps(result), 5, 6))
    db.commit()
    before_ai, before_job = rows(db, "ai_results")[0], rows(db, "jobs")[0]
    plan = plan_anchor_migration(db, "paper", [repaired(old)])
    assert plan.safe, plan.issues
    db.execute("BEGIN")
    apply_anchor_migration(db, plan)
    ai, job = rows(db, "ai_results")[0], rows(db, "jobs")[0]
    assert json.loads(ai["citations"]) == [{"block_id": "body", "quote": "fish", "page": 1}]
    assert {k: v for k, v in ai.items() if k != "citations"} == {k: v for k, v in before_ai.items() if k != "citations"}
    p, r = json.loads(job["payload"]), json.loads(job["result"])
    assert p["source_targets"][0]["quote"] == "fish"
    assert p["question"] == payload["question"] and p["custom"] == payload["custom"]
    assert r["parts"][0]["citations"][0]["quote"] == "fish"
    assert r["parts"][0]["answer"] == result["parts"][0]["answer"] and r["terms"] == result["terms"]
    assert {k: v for k, v in job.items() if k not in ("payload", "result")} == {
        k: v for k, v in before_job.items() if k not in ("payload", "result")}


def test_custom_annotation_quote_prefix_and_historical_target_are_opaque():
    old = source("body", "The ﬁsh form teams.")
    db = library([old])
    value = target(old, "teams")
    value["prefix"] = "A custom context snippet"
    annotation(db, "custom", [value], [{"translation_id": "inactive", "block_id": "body", "start": 7, "end": 8, "quote": "译"}],
               quote="A custom translated display quote")
    db.commit()
    before = rows(db, "annotations")[0]
    plan = plan_anchor_migration(db, "paper", [repaired(old)])
    assert plan.safe
    db.execute("BEGIN")
    apply_anchor_migration(db, plan)
    after = rows(db, "annotations")[0]
    assert after["quote"] == before["quote"] and after["translated_targets"] == before["translated_targets"]
    assert json.loads(after["targets"])[0]["prefix"] == "A custom context snippet"


def test_ambiguous_quote_reports_only_its_page_and_replanning_preserves_it():
    old = [source("safe", "A ﬁsh swims."), source("ambiguous", "A ﬁsh meets another ﬁsh.", 2)]
    db = library(old)
    annotation(db, "note", [target(old[0], "ﬁsh")])
    db.execute("INSERT INTO ai_results VALUES(?,?,?,?,?,?,?,?,?,?)",
               ("ai", "paper", "question", "q", "Unchanged answer", dumps([{"block_id": "ambiguous", "quote": "ﬁsh", "page": 2}]),
                "local", "old", "all", 1))
    db.commit()
    new = [repaired(block) for block in old]
    plan = plan_anchor_migration(db, "paper", new)
    assert not plan.safe and plan.unsafe_blocks == {"ambiguous"} and plan.unsafe_pages == {2}
    with pytest.raises(ValueError, match="Unsafe"):
        apply_anchor_migration(db, plan)
    safe = plan_anchor_migration(db, "paper", new, preserved_pages=plan.unsafe_pages)
    assert safe.safe and safe.mapped_blocks == {"safe"}
    db.execute("BEGIN")
    apply_anchor_migration(db, safe)
    assert json.loads(rows(db, "ai_results")[0]["citations"])[0]["quote"] == "ﬁsh"
    assert rows(db, "annotations")[0]["quote"] == "fish"


@pytest.mark.parametrize("kind", ["deleted", "moved", "bad_quote", "half_surrogate"])
def test_unprovable_source_anchor_is_never_partially_published(kind):
    old = source("body", "😀 The ﬁsh form teams.")
    new = repaired(old)
    db = library([old])
    value = target(old, "ﬁsh")
    if kind == "bad_quote":
        value["quote"] = "fish"
    elif kind == "half_surrogate":
        value.update(start=1, end=2, quote="😀")
    elif kind == "moved":
        new["page"] = 2
    annotation(db, "note", [value])
    db.commit()
    plan = plan_anchor_migration(db, "paper", [] if kind == "deleted" else [new])
    assert not plan.safe and plan.unsafe_blocks == {"body"}
    assert plan.unsafe_pages == ({1, 2} if kind == "moved" else {1})
    before = rows(db, "annotations")
    with pytest.raises(ValueError):
        apply_anchor_migration(db, plan)
    assert rows(db, "annotations") == before


def test_apply_requires_transaction_and_detects_stale_source_json_before_any_update():
    old = source("body", "The ﬁsh form teams.")
    db = library([old])
    translation(db, "tr", "body", "Original translation", [{"source_start": 0, "source_end": units(old["text"]), "target_start": 0, "target_end": 20}])
    annotation(db, "note", [target(old, "ﬁsh")])
    db.commit()
    plan = plan_anchor_migration(db, "paper", [repaired(old)])
    with pytest.raises(ValueError, match="transaction"):
        apply_anchor_migration(db, plan)
    previous = rows(db, "translations")
    db.execute("UPDATE annotations SET targets='[]' WHERE id='note'")
    with pytest.raises(ValueError, match="changed while rebuilding"):
        apply_anchor_migration(db, plan)
    assert rows(db, "translations") == previous


def test_geometry_only_replacement_preserves_every_locator_fraction():
    old = source("body", "The original text remains unchanged.")
    db = library([old])
    new = copy.deepcopy(old)
    new["bbox"] = dumps([40, 80, 320, 135])
    before = rows(db, "papers")
    plan = plan_anchor_migration(db, "paper", [new])
    assert plan.safe and not plan.updates
    assert rows(db, "papers") == before


def test_image_only_locator_cannot_move_without_text_geometry_evidence():
    old = source("body", "")
    old["kind"] = "image"
    db = library([old])
    new = copy.deepcopy(old)
    new["bbox"] = dumps([40, 120, 320, 210])
    plan = plan_anchor_migration(db, "paper", [new])
    assert not plan.safe and plan.unsafe_pages == {1}


@pytest.mark.parametrize("reference", ["translation_without_segments", "locator_only"])
def test_whole_block_semantics_are_proven_even_without_saved_source_ranges(reference):
    old = source("body", "Teams coordinate their work.")
    new = source("body", "Teams coordinate different tasks.")
    db = library([old])
    if reference == "translation_without_segments":
        translation(db, "manual", "body", "必须保留且不能接到不同原文的人工译文", [], manual=1)
        db.execute("UPDATE papers SET locator='{}'")
    db.commit()
    plan = plan_anchor_migration(db, "paper", [new])
    assert not plan.safe and plan.unsafe_blocks == {"body"} and plan.unsafe_pages == {1}
