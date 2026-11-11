"""Resolve a temporary selection against saved alignments without creating a note."""
from __future__ import annotations

import json

from .ai import mapped_ranges, matching_sentence
from .pdf import length, rects_for_range, slice_text


def selection_counterpart(db, paper_id, side, targets, *, pdf_rects=False):
    if side not in ("source", "translation") or not isinstance(targets, list) or len(targets) > 50:
        raise ValueError("选中的文字范围无效")
    opposite = "translation" if side == "source" else "source"
    blocks, translations = {}, {}

    def lookup(block_id):
        if block_id not in blocks:
            columns = "id,page,text,geometry" if pdf_rects else "id,page,text"
            blocks[block_id] = db.execute(f"SELECT {columns} FROM blocks WHERE id=? AND paper_id=? AND kind!='image'",
                                          (block_id, paper_id)).fetchone()
        if block_id not in translations:
            translations[block_id] = db.execute("""SELECT id,text,segments FROM translations
                WHERE block_id=? AND paper_id=? AND active=1 ORDER BY created DESC LIMIT 1""",
                                               (block_id, paper_id)).fetchone()
        return blocks[block_id], translations[block_id]

    def make_range(block_id, start, end, quality, translation_id=None):
        block, tr = lookup(block_id)
        record = block if opposite == "source" else tr
        if not block or not record or (translation_id and translation_id != record["id"]):
            return None
        try:
            quote = slice_text(record["text"], start, end)
        except (TypeError, ValueError):
            return None
        result = {"block_id": block_id, "page": block["page"], "start": start, "end": end, "quote": quote, "quality": quality}
        if opposite == "translation":
            result["translation_id"] = record["id"]
        elif pdf_rects:
            result["rects"] = rects_for_range(block, start, end)
        return result

    manual = []
    for row in db.execute("SELECT targets,translated_targets FROM annotations WHERE paper_id=? AND alignment='manual' ORDER BY updated DESC",
                          (paper_id,)):
        original, translated = json.loads(row["targets"]), json.loads(row["translated_targets"])
        manual.append((original, translated) if side == "source" else (translated, original))

    ranges, seen, missing = [], set(), 0
    for target in targets:
        if not isinstance(target, dict) or not isinstance(target.get("block_id"), str):
            raise ValueError("选中的文字范围无效")
        bid, start, end = target["block_id"], target.get("start"), target.get("end")
        if type(start) is not int or type(end) is not int:
            raise ValueError("选中的文字范围无效")
        block, tr = lookup(bid)
        if not block or (side == "translation" and (not tr or target.get("translation_id") != tr["id"])):
            missing += 1
            continue
        slice_text((tr if side == "translation" else block)["text"], start, end)
        choices = []
        for anchors, linked in manual:
            widths = [a["end"] - a["start"] for a in anchors if a["block_id"] == bid and a["start"] <= start and a["end"] >= end
                      and (side == "source" or a.get("translation_id") == tr["id"])]
            if widths and linked:
                choices.append((min(widths), linked))
        resolved = []
        for _, linked in sorted(choices, key=lambda item: item[0]):
            resolved = [make_range(a["block_id"], a["start"], a["end"], "manual", a.get("translation_id")) for a in linked]
            if all(resolved):
                break
            resolved = []
        if not resolved and tr:
            ranges_for_selection = list(mapped_ranges(json.loads(tr["segments"]), bid, start, end,
                                                        "target" if side == "translation" else "source"))
            for destination, a, b, link in ranges_for_selection:
                if link:
                    _, linked = lookup(destination)
                    if not linked or not matching_sentence(json.loads(linked["segments"]), link, a, b):
                        continue
                if item := make_range(destination, a, b, "mapped"):
                    resolved.append(item)
            if not resolved and not ranges_for_selection and (text := (block if opposite == "source" else tr)["text"]):
                resolved = [make_range(bid, 0, length(text), "paragraph")]
        if not resolved:
            missing += 1
        for item in resolved:
            key = (item["block_id"], item["start"], item["end"])
            if key not in seen:
                seen.add(key)
                ranges.append(item)
    return {"side": opposite, "ranges": ranges[:24], "missing": missing}
