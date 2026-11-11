"""Verified terminology shared by the translation routes for one paper."""
from __future__ import annotations

import json
import re
from functools import lru_cache

from .pdf import length, slice_text
from .reading import readable_text


USE_TERMS = "terms 是本文已有的译名。相同术语沿用 target，区分不同概念。按语境翻译其余文字。"
LIMIT = 240
DASHES = str.maketrans("‐‑‒–—−", "------")
CJK = re.compile(r"[\u3400-\u9fff]")
LATIN = re.compile(r"[a-z]", re.I)
SEPARATOR = re.compile(r"[。！？；，、：!?;,:()（）\n\r]")
MARKED = re.compile(r"(.+?)\s*[(（]([^()（）]+)[)）]")
ADJACENT_MARK = re.compile(r"\s*[(（][^()（）]{2,85}[)）]")
INLINE_TERM = re.compile(r"〔([^〔〕\n]{2,85})〕(?:\s*[(（]([^()（）]{2,85})[)）])?")


def term_key(text):
    text = readable_text(text).translate(DASHES).casefold()
    return re.sub(r"\s*-\s*", "-", " ".join(text.split()))


@lru_cache(maxsize=1024)
def term_pattern(key):
    body = re.escape(key).replace(r"\ ", r"\s+").replace(r"\-", r"\s*[-‐‑‒–—−]\s*")
    # Chinese terms can be adjacent to other words without spaces.
    return re.compile(body if CJK.search(key) else r"(?<![\w-])" + body + r"(?![\w-])", re.I)


def term_pair(source, target, language):
    if not isinstance(source, str) or not isinstance(target, str):
        return None
    source, target = (" ".join(value.split()) for value in (source, target))
    if not (2 <= len(source) <= 85 and 2 <= len(target) <= 85) or SEPARATOR.search(source + target):
        return None
    english, chinese = (target, source) if language == "en" else (source, target)
    if not LATIN.search(english) or CJK.search(english) or not CJK.search(chinese):
        return None
    return {"source": source, "target": target}


def add_term(terms, pair):
    if len(terms) < LIMIT:
        terms.setdefault(term_key(pair["source"]), pair)


def relevant_terms(terms, texts):
    # Use only terms in the pieces being sent, including when repairing missing slots.
    text = "\n".join(texts)
    return [pair for key, pair in terms.items() if term_pattern(key).search(text)][:10]


def known_term(terms, source, language, *, bilingual=True):
    pair = terms.get(term_key(source))
    if pair:
        return pair["target"] + (f"（{source}）" if bilingual and language == "zh" else "")
    return None


def _stored_pairs(db, paper_id, language):
    for row in db.execute("""SELECT json_extract(result,'$.terms') FROM jobs
        WHERE paper_id=? AND kind IN ('translate','reading') AND json_type(result,'$.terms')='array'
        AND coalesce(json_extract(result,'$.term_language'),'zh')=? ORDER BY created,id""",
                          (paper_id, language)):
        for value in json.loads(row[0]):
            if isinstance(value, dict) and (pair := term_pair(value.get("source"), value.get("target"), language)):
                yield pair


def _marked_pairs(db, paper_id, *, manual):
    # Older translations may have an exact phrase alignment but no saved term list.
    for row in db.execute("""SELECT b.text AS source,t.text,t.segments FROM translations t
        JOIN blocks b ON b.id=t.block_id WHERE t.paper_id=? AND t.active=1 AND t.manual=?
        AND (instr(t.text,'（') OR instr(t.text,'(')) ORDER BY b.page,b.position""", (paper_id, int(manual))):
        if manual:
            mark = MARKED.fullmatch(row["text"].strip())
            if mark and term_key(mark[2]) == term_key(row["source"]) and (pair := term_pair(mark[2], mark[1], "zh")):
                yield pair
            continue
        target_length = length(row["text"])
        for span in json.loads(row["segments"]):
            if span.get("source_block_id") or span.get("target_block_id"):
                continue
            if span["source_end"] - span["source_start"] > 95 or span["target_end"] - span["target_start"] > 175:
                continue
            try:
                source = readable_text(slice_text(row["source"], span["source_start"], span["source_end"]))
                end = span["target_end"]
                target = slice_text(row["text"], span["target_start"], end).strip()
                if end < target_length and not MARKED.fullmatch(target):
                    tail = slice_text(row["text"], end, target_length)[:88]
                    if adjacent := ADJACENT_MARK.match(tail):
                        target += adjacent[0]
            except ValueError:
                continue
            mark = MARKED.fullmatch(target)
            if mark and term_key(source) == term_key(mark[2]) and (pair := term_pair(mark[2], mark[1], "zh")):
                yield pair


def paper_terms(db, paper, language):
    terms = {}
    forward = "en" if paper["language"] == "zh" else "zh"
    paper_id = paper["id"]
    if language == forward == "zh":
        for pair in _marked_pairs(db, paper_id, manual=True):
            add_term(terms, pair)
    for pair in _stored_pairs(db, paper_id, language):
        add_term(terms, pair)
    if language == forward == "zh":
        for pair in _marked_pairs(db, paper_id, manual=False):
            add_term(terms, pair)
    if language != forward:
        # Reverse translation can use a term only when its reverse is unambiguous.
        reverse = {}
        for pair in paper_terms(db, paper, forward).values():
            key = term_key(pair["target"])
            reverse[key] = None if key in reverse else {"source": pair["target"], "target": pair["source"]}
        for pair in reverse.values():
            if pair:
                add_term(terms, pair)
    return terms


def _replace_span(target, alignments, start, end, replacement):
    updated = []
    for item in alignments:
        quote = item["translation"]
        offset = target.find(quote)
        if offset < 0 or target.count(quote) != 1:
            updated.append(item)
        elif offset <= start and end <= offset + len(quote):
            quote = quote[:start - offset] + replacement + quote[end - offset:]
            updated.append({**item, "translation": quote})
        elif end <= offset or offset + len(quote) <= start:
            updated.append(item)
    return target[:start] + replacement + target[end:], updated


def synchronize_terms(source, target, alignments, proposed, terms, language):
    """Reuse a saved term only at a verified span, before the reader computes offsets."""
    alignments = alignments[:12] if isinstance(alignments, list) else []
    alignments = [item for item in alignments if isinstance(item, dict)
                  and all(isinstance(item.get(key), str) and item[key] for key in ("source", "translation"))]
    candidates = []
    if language == "zh":
        marks = list(INLINE_TERM.finditer(target))
        for mark in reversed(marks):
            if mark[0] in source:
                continue
            target, alignments = _replace_span(target, alignments, mark.end(1), mark.end(1) + 1, "")
            target, alignments = _replace_span(target, alignments, mark.start(), mark.start() + 1, "")
        candidates.extend((mark[2], mark[1], False) for mark in marks if mark[2])
    for item in alignments:
        quote = item["translation"]
        mark = MARKED.fullmatch(quote) if language == "zh" else None
        candidates.append((item["source"], mark[1] if mark and term_key(mark[2]) == term_key(item["source"]) else quote, True))
    suggested = [pair for item in (proposed[:12] if isinstance(proposed, list) else []) if isinstance(item, dict)
                 and (pair := term_pair(item.get("source"), item.get("target"), language))]
    suggested_keys = {(term_key(pair["source"]), term_key(pair["target"])) for pair in suggested}
    candidates.extend((pair["source"], pair["target"], False) for pair in suggested)
    if language == "zh" and (mark := MARKED.fullmatch(target)) and term_key(source) == term_key(mark[2]):
        candidates.append((mark[2], mark[1], False))

    learned = {}
    for src, dst, aligned in candidates:
        pair = term_pair(src, dst, language)
        if not pair:
            continue
        key = term_key(pair["source"])
        matches = list(term_pattern(key).finditer(source))
        if not matches:
            continue
        dst = pair["target"]
        mark = re.compile(re.escape(dst) + r"\s*[(（]" + term_pattern(key).pattern + r"[)）]") if language == "zh" else None
        marked = list(mark.finditer(target)) if mark else []
        spans = marked or list(term_pattern(term_key(dst)).finditer(target))
        if len(spans) != 1 or (not marked and len(matches) != 1):
            continue
        start, end = spans[0].span()
        if marked:
            end = start + len(dst)
        if not marked and not aligned and language == "zh":
            continue
        canonical = terms.get(key) or learned.get(key)
        if canonical:
            replacement = canonical["target"]
            if target[start:end] != replacement and target[max(0, end - len(replacement)):end] != replacement:
                target, alignments = _replace_span(target, alignments, start, end, replacement)
        elif language == "zh" and aligned and not marked and (key, term_key(dst)) in suggested_keys:
            target, alignments = _replace_span(target, alignments, start, end, f"{target[start:end]}（{matches[0][0]}）")
            add_term(learned, pair)
        elif marked or (language == "en" and not aligned):
            add_term(learned, pair)
    return target, alignments, learned
