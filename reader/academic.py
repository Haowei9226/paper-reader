"""Context and canonical text spans for academic translation."""
from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from itertools import groupby

from .layout import classify_page
from .reading import SOFT_WRAP, continues_sentence, readable_text


def translation_style(language=None, *, inline_terms=False):
    direction = f"翻译成{language}。" if language else "按要求的目标语言翻译。"
    first_use = "译为中文时，核心概念、理论、方法和重要专名首次出现用中文（英文原词）"
    if language == "简体中文" and inline_terms:
        first_use += "。用〔〕框住中文译名，如〔技术探针〕（technology probe），阅读器会去掉框号。"
    else:
        first_use += "，如技术探针（technology probe）。"
    return (direction + "使用准确、通顺的学术书面语。保留论证关系、否定、限定、数字、单位、引文和公式，不增删。"
            "保留‘结果提示’和‘可能’等证据语气。相关性不译成因果。避免逐词硬译。"
            + first_use +
            "优先使用本领域通行译名，区分不同概念。无通行译名的专名、缩写和符号保留原样。"
            "译为英文时采用该领域惯用的学术表达。")


SEGMENT_GUIDANCE = (
    '按 segments 的顺序一起理解。带 page_end 的跨页句已经拼成完整的一句，直接翻译整个句子。'
    '每个 id 只译自己的 text，完整保留引文编号及其出现次数。'
    '标记 continues_next 的片段保留未完句状态，不补句号、省略号或续页提示；其余按正常标点。'
    'context、previous_fragment、next_fragment 只用于理解，terms 是已有译名，均不作为正文输出。'
)
SEGMENT_INSTRUCTION = SEGMENT_GUIDANCE + (
    '仅返回 {"segments":[{"id":0,"translation":"译文","alignments":[{"source":"原文原样短语","translation":"译文原样短语"}]}],'
    '"terms":[{"source":"英文术语","target":"中文译名"}]}。每个 id 恰好一次；每段至多两条确信的短语对应。'
    'terms 可省略，只收录译文中确实以中文（英文）标注的术语。'
)

NUMERIC_CITATION = re.compile(r"(?:\[|【)(\s*\d{1,4}(?:\s*[,，、;；‐‑–—−-]\s*\d{1,4})*\s*)(?:\]|】)")


def citation_counts(text):
    normal = unicodedata.normalize("NFKC", text).translate(str.maketrans("，、；‐‑–—−", ",,;-----"))
    return Counter("".join(match.group(1).split()).replace(";", ",") for match in NUMERIC_CITATION.finditer(normal))

TERM_PARENTHESIS = re.compile(r"(?<=[\u3400-\u9fff])[（(]([A-Za-z][A-Za-z0-9 ’'./\-–]{1,84})[）)]")


def retain_bilingual_terms(target, source, alignments):
    """Carry a supported English term into the main text when its wording agrees exactly."""
    if not isinstance(alignments, list):
        return target
    for alignment in alignments[:12]:
        if not isinstance(alignment, dict):
            continue
        quote, variant = alignment.get("source"), alignment.get("translation")
        if not isinstance(quote, str) or not quote or source.count(quote) != 1 or not isinstance(variant, str):
            continue
        terms = {}
        for mark in TERM_PARENTHESIS.finditer(variant):
            original = re.search(r"(?<!\w)" + re.escape(mark[1]) + r"(?!\w)", quote, re.I)
            if original and not re.search(r"[（(]\s*" + re.escape(mark[1]) + r"\s*[）)]", target, re.I):
                terms[mark.start()] = "（" + original[0] + "）"
        if terms:
            plain = TERM_PARENTHESIS.sub(lambda m: "" if m.start() in terms else m[0], variant)
            if plain and target.count(plain) == 1:
                expanded = TERM_PARENTHESIS.sub(lambda m: terms.get(m.start(), m[0]), variant)
                target = target.replace(plain, expanded, 1)
    return target


def source_with_offsets(raw):
    """Remove PDF control glyphs and discretionary breaks while retaining UTF-16 spans."""
    visible, offsets = [], []
    hidden = iter(SOFT_WRAP.finditer(raw))
    marker = next(hidden, None)
    offset = 0
    for index, char in enumerate(raw):
        if marker and index >= marker.end():
            marker = next(hidden, None)
        width = 2 if ord(char) > 0xffff else 1
        control = (ord(char) < 32 or 127 <= ord(char) <= 159) and not char.isspace()
        if not control and (not marker or index < marker.start()):
            visible.append(char)
            offsets.append((offset, offset + width))
        offset += width
    return "".join(visible), offsets


def translation_blocks(db, paper, path, scope):
    rows = db.execute("""SELECT b.id,b.page,b.kind,b.text,b.bbox,b.geometry,b.section,p.height,p.width,p.ocr_job_id!='' AS ocr
        FROM blocks b JOIN pages p ON p.paper_id=b.paper_id AND p.number=b.page
        WHERE b.paper_id=? ORDER BY b.page,b.position""", (paper["id"],)).fetchall()
    blocks, table_by_block = [], {}
    explicit = set(scope.get("block_ids", []))
    adjacent = scope.get("neighbor_context") is True
    selected = {row["page"] for row in rows if (not explicit or row["id"] in explicit) and (
        not scope.get("page_start") or scope["page_start"] <= row["page"] <= scope.get("page_end", row["page"]))}
    detected = selected | {page + delta for page in selected for delta in (-1, 1)} if adjacent else selected
    for page, group in groupby(rows, key=lambda row: row["page"]):
        items = [dict(row) for row in group]
        for item in items:
            item["bbox"] = json.loads(item["bbox"])
            item["layout_checked"] = page in detected
        _, matches, _ = classify_page(path, paper, page, items[0]["height"], items,
                                      width=items[0]["width"], detect=page in detected, ocr=bool(items[0]["ocr"]))
        table_by_block.update(matches)
        blocks.extend(item for item in items if item["kind"] not in ("image", "legacy"))
    before, after = {}, {}
    for order, neighbors in ((blocks, before), (reversed(blocks), after)):
        previous = None
        for block in order:
            if previous and abs(previous["page"] - block["page"]) <= 1:
                neighbors[block["id"]] = previous
            if block["reading_role"] == "formula":
                previous = None
            elif block["reading_role"] not in ("caption", "extra", "table", "embedded") and block["text"].strip():
                previous = block
    return blocks, before, after, table_by_block


def joined(left, right):
    return (left and right and left["reading_role"] == right["reading_role"] == "text"
            and continues_sentence(left["text"], right["text"]))


def page_joined(left, right):
    return (joined(left, right) and right["page"] == left["page"] + 1
            and left["section"] == right["section"])


def context_excerpt(text, *, tail=False, limit=205):
    clean = " ".join(readable_text(text).split())
    if len(clean) <= limit:
        return clean
    excerpt = clean[-limit:] if tail else clean[:limit]
    if " " in excerpt:
        excerpt = excerpt.split(" ", 1)[-1] if tail else excerpt.rsplit(" ", 1)[0]
    return ("…" + excerpt) if tail else (excerpt + "…")


