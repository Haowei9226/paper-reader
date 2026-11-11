"""Find text in a paper and persist a reader's place within a page."""
from __future__ import annotations

import re

from .academic import source_with_offsets
from .pdf import length, slice_text


def reading_locator(value, pages: int):
    if not isinstance(value, dict):
        raise ValueError("阅读位置无效")
    page = value.get("page", 1)
    if type(page) is not int:
        raise ValueError("阅读页码无效")
    locator = {"page": min(max(1, pages), max(1, page)), "block_id": str(value.get("block_id") or "")[:100]}
    for key, options in (("mode", ("pdf", "html")), ("side", ("source", "translation"))):
        if value.get(key) in options:
            locator[key] = value[key]
    for key in ("offset", "scroll", "page_fraction"):
        number = value.get(key)
        if type(number) in (int, float) and 0 <= number <= 1:
            locator[key] = number
    stamp = value.get("saved_at")
    if type(stamp) in (int, float) and 0 < stamp < 10**14:
        locator["saved_at"] = stamp
    return locator


def find_in_paper(db, paper_id: str, query: str, scope: str, limit: int, offset: int):
    words = query.split()
    result = {"items": [], "next_offset": None}
    if not words:
        return result
    pattern = re.compile(r"\s+".join(re.escape(word) for word in words), re.IGNORECASE)
    # Only retrieve text, and use the same current translation as the page reader.
    translations = """LEFT JOIN translations t ON t.id=(
        SELECT id FROM translations WHERE block_id=b.id AND active=1 ORDER BY created DESC LIMIT 1
    )""" if scope != "source" else ""
    columns = "t.id AS translation_id,t.text AS translated" if translations else "NULL AS translation_id,NULL AS translated"
    rows = db.execute(f"""SELECT b.id,b.page,b.text,{columns}
        FROM blocks b {translations} WHERE b.paper_id=? AND b.kind!='image' ORDER BY b.page,b.position""", (paper_id,))
    skipped = 0
    for row in rows:
        for side, text, translation_id in (("source", row["text"], None), ("translation", row["translated"], row["translation_id"])):
            if not text or scope not in ("both", side):
                continue
            visible, spans = source_with_offsets(text) if "\u00ad" in text else (text, None)
            for match in pattern.finditer(visible):
                if skipped < offset:
                    skipped += 1
                    continue
                if len(result["items"]) == limit:
                    result["next_offset"] = offset + limit
                    return result
                start, end = match.span()
                before, after = max(0, start - 42), min(len(visible), end + 64)
                utf_start, utf_end = (spans[start][0], spans[end - 1][1]) if spans else (length(text[:start]), length(text[:end]))
                result["items"].append({
                    "block_id": row["id"], "page": row["page"], "side": side, "translation_id": translation_id,
                    "start": utf_start, "end": utf_end,
                    "before": ("…" if before else "") + visible[before:start], "match": slice_text(text, utf_start, utf_end) if spans else match[0],
                    "after": visible[end:after] + ("…" if after < len(visible) else ""),
                })
    return result
