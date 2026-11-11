"""The article layout used by the HTML reader and its standalone export."""

from __future__ import annotations

import base64
import html
import json
import re
import statistics
from pathlib import Path

from .metadata import identity
from .margins import is_running_section, page_margin_blocks

SECTION = re.compile(
    r"^(?:(?:\d+(?:\.\d+)*[.、]?\s+|[一二三四五六七八九十]+[、.]\s*)?"
    r"(?:abstract|introduction|background|methods?|results?|discussion|conclusions?|"
    r"references|bibliography|acknowledg(?:e)?ments|摘要|引言|绪论|背景|方法|结果|讨论|结论|参考文献|致谢))[\s:：.]*$",
    re.I,
)
REFERENCE_HEADING = re.compile(
    r"^(?:(?:\d+(?:\.\d+)*|[一二三四五六七八九十]+)(?:[.、．]\s*|\s+))?"
    r"(?:references|bibliography|works cited|literature cited|参考文献|参考书目)"
    r"(?:\s*[(（](?:continued|续)[)）])?\s*[:：.]?\s*$", re.I,
)
CAPTION = re.compile(
    r"^(?:(?:fig(?:ure)?\.?|table)\s+[sS]?\d+(?:\.\d+)*[a-z]?|[图表]\s*[0-9一二三四五六七八九十]+)"
    r"(?:\s*[:：.)、–—-]|\s{2,}|\s*$)", re.I,
)
PUBLICATION = re.compile(
    r"^(?:arxiv\s*:|copyright\b|©|provided proper attribution\b|all rights reserved\b|"
    r"this (?:work|article|paper) is (?:licensed|distributed|published) under\b|creative commons(?: attribution| license)\b|"
    r"acm (?:reference format|isbn)\b|"
    r"permission to (?:make|reproduce)\b|(?:https?://(?:dx\.)?doi\.org/|doi\s*:))",
    re.I,
)
PAGE_NUMBER = re.compile(r"^[-–—]?\s*(?:page\s+)?\d{1,4}(?:\s*(?:/|of)\s*\d{1,4})?\s*[-–—]?$", re.I)
FRONT_LABEL = re.compile(r"^(?:ccs concepts|(?:author )?keywords|key words|关键词|索引词)(?:\s*[:：].*)?$", re.I)
RUNNING_HEADER = re.compile(r"\b[A-Z][A-Z0-9&-]{1,15}\s+[’'‘]\d{2},.+,\s*\w+")
SOFT_WRAP = re.compile(r"\u00ad(?:[ \t\r\n]+(?=[^\W\d_]))?")
LIST_START = re.compile(r"^\s*(?:[•◦●▪▫‣]|(?:\(?\d{1,3}|\(?[A-Za-z])[).、])\s+")


def readable_text(text):
    return SOFT_WRAP.sub("", text)


def continues_sentence(left, right):
    previous, following = readable_text(left).rstrip(), readable_text(right).lstrip()
    if len(previous) < 18 or not following or re.search(r"[.!?。！？:：;；][\s\"'”’）)\]」』]*$", previous):
        return False
    if re.match(r"[a-z][a-z’'-]*\b", following):
        return True
    return bool(re.search(r"[\u3400-\u9fff，、]$", previous) and re.match(r"[\u3400-\u9fff]", following)
                and not re.match(r"^[ \u3000]{2,}|^\u3000", right))


def _title_indices(blocks, paper, page, height):
    if page != 1 or not (title := identity(paper["title"])):
        return [], False
    authors = [name for author in re.split("[;；]", paper["authors"]) if (name := identity(author))]
    # A short last line of a centered title can fall into a body column.
    # Match the title in page coordinates before trusting the reading order.
    candidates = sorted((i for i, b in enumerate(blocks)
                         if b["kind"] != "image" and b["bbox"][1] <= height * .35),
                        key=lambda i: (blocks[i]["bbox"][1], blocks[i]["bbox"][0]))[:12]
    for first in range(len(candidates)):
        candidate = ""
        for last in range(first, min(first + 4, len(candidates))):
            block = blocks[candidates[last]]
            if not (fragment := identity(block["text"])):
                break
            candidate += fragment
            if candidate == title:
                return candidates[first:last + 1], False
            if candidate.startswith(title) and len(block["text"]) <= len(paper["title"]) + 400:
                tail = candidate[len(title):]
                if "abstract" not in tail and "摘要" not in tail and any(author in tail for author in authors):
                    return candidates[first:last + 1], True
            if not title.startswith(candidate):
                break
    return [], False


def _byline_indices(blocks, title_indices, authors):
    if not title_indices:
        return set()
    title_bottom = max(blocks[index]["bbox"][3] for index in title_indices)
    body_top = min(
        (block["bbox"][1] for block in blocks if block["bbox"][1] >= title_bottom - 2 and (
            SECTION.fullmatch(block["text"].strip()) or re.match(r"^(?:abstract|摘\s*要)\s*[:：]", block["text"].strip(), re.I)
        )), default=None,
    )
    if body_top is None:
        return set()
    # ACM manuscripts may start their abstract directly, without an Abstract
    # heading. A long prose line is not part of the author/affiliation band.
    def prose(text):
        words = re.findall(r"[A-Za-z]+", text)
        return (len(words) >= 17 and sum(word[:1].islower() for word in words) >= len(words) * .75
                or len(re.findall(r"[\u3400-\u9fff]", text)) >= 60)

    prose_top = min((block["bbox"][1] for block in blocks
                     if title_bottom - 2 <= block["bbox"][1] < body_top and prose(block["text"])), default=body_top)
    indices = {index for index, block in enumerate(blocks)
               if title_bottom - 2 <= block["bbox"][1] < prose_top and block["bbox"][3] <= prose_top + 2
               and len(block["text"].strip()) < 280}
    credit = " ".join(blocks[index]["text"] for index in sorted(indices))
    names = [name for author in re.split("[;；]", authors) if (name := identity(author))]
    normalized = identity(credit)
    if any(name in normalized for name in names) or re.search(r"\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b", credit, re.I):
        return indices
    return set()


def _wrapped_line(left, right):
    """Some PDFs expose each typeset line as a separate text block."""
    a, b = left["bbox"], right["bbox"]
    line = a[3] - a[1]
    if LIST_START.match(right["text"]):
        return False
    indent = b[0] - a[0]
    aligned = abs(indent) <= max(3, line * .35) or -1.5 * line <= indent < 0
    if LIST_START.match(left["text"]):
        aligned = aligned or 0 <= indent <= line * 1.5
    return (6 <= line <= 24 and .78 * line <= b[3] - b[1] <= 1.25 * line
            and -.15 * line <= b[1] - a[3] <= .85 * line
            and aligned
            and a[2] - a[0] >= max(75, (b[2] - b[0]) * .9))


def _line_height(block):
    data = block.get("geometry") or {}
    if isinstance(data, str):
        data = json.loads(data)
    boxes = data.get("boxes", ())
    heights = [box[3] - box[1] for box in boxes if box and box[3] > box[1]]
    return statistics.median(heights) if heights else block["bbox"][3] - block["bbox"][1]


def _starts_paragraph(left, right):
    """Indentation and note/list typography beat a lowercase-word guess."""
    if LIST_START.match(right["text"]):
        return True
    size, other = _line_height(left), _line_height(right)
    if size and other and min(size, other) < max(size, other) * .76:
        return True
    return (not LIST_START.match(left["text"]) and right["bbox"][0] - left["bbox"][0] > max(4, size * .55)
            and right["bbox"][1] >= left["bbox"][3] - 2)


def page_continuation(left, right, left_width, right_width):
    """Return a conservative cross-page prose bridge without changing anchors."""
    flow = lambda blocks: [b for b in blocks if b["reading_role"] not in ("extra", "image", "caption", "embedded")]
    before, after = flow(left), flow(right)
    if not before or not after:
        return None
    a, b = before[-1], after[0]
    if (a["reading_role"] != "text" or b["reading_role"] != "text"
            or a["section"] != b["section"] or LIST_START.match(b["text"])):
        return None
    size, other = _line_height(a), _line_height(b)
    if not size or not .85 <= other / size <= 1.18:
        return None

    def body_left(blocks, edge, width):
        # Work within the endpoint's column. Mirrored odd/even margins are fine;
        # an indented list/quotation must not become the normal body margin.
        col = int((edge["bbox"][0] + edge["bbox"][2]) / 2 > width / 2)
        aligned = [x for x in blocks if x["reading_role"] == "text" and len(x["text"]) > 60
                   and int((x["bbox"][0] + x["bbox"][2]) / 2 > width / 2) == col
                   and .85 <= _line_height(x) / size <= 1.18]
        return min((x["bbox"][0] for x in aligned), default=edge["bbox"][0])

    if any(abs(edge["bbox"][0] - body_left(blocks, edge, width)) > max(3, size * .45)
           for blocks, edge, width in ((before, a, left_width), (after, b, right_width))):
        return None
    previous, following = readable_text(a["text"]).rstrip(), readable_text(b["text"]).lstrip()
    if (len(previous) < 18 or not following or re.search(r"[.!?。！？:：;；][\s\"'”’）)\]」』]*$", previous)
            or not re.match(r"[\w\u3400-\u9fff]", following)):
        return None
    return {"left": a, "right": b, "join_word": bool(re.search(r"[-\u00ad]\s*$", a["text"]) and following[:1].islower())}


def classify_reading_blocks(blocks, paper, page, height, table_blocks=(), equation_blocks=(), table_captions=()):
    """Add display roles without changing the text or its annotation coordinates."""
    hidden = page_margin_blocks(blocks, height, page, paper["title"])
    blocks[:] = [block for index, block in enumerate(blocks) if index not in hidden]
    title = identity(paper["title"])
    title_indices, mixed_byline = _title_indices(blocks, paper, page, height)
    byline_indices = _byline_indices(blocks, title_indices, paper["authors"])
    outline = [entry for entry in paper["outline"] if not is_running_section(entry["title"], paper["title"])]
    outline_levels = {identity(entry["title"]): entry["level"] for entry in outline if entry["page"] == page}
    section = next((entry["title"] for entry in reversed(outline) if entry["page"] < page), "")

    front_value = False
    front_label = None
    previous = None
    for index, block in enumerate(blocks):
        block.pop("continues_previous", None)
        text = block["text"].strip()
        top, bottom = block["bbox"][1], block["bbox"][3]
        near_edge = height > 0 and (top < height * .075 or bottom > height * .93)
        if block["kind"] == "image":
            role = "image"
        elif block.get("id") in table_blocks:
            role = "table"
        elif block.get("id") in equation_blocks:
            role = "formula"
        elif block.get("id") in table_captions:
            role = "caption"
        elif index in title_indices:
            # A title block can also contain the byline. Show the clean title in the
            # cover and keep the original block selectable in the publication details.
            role = "extra" if mixed_byline else "title"
        elif near_edge and (PAGE_NUMBER.fullmatch(text) or (title and identity(text) == title)):
            role = "extra"
        elif (page == 1 or near_edge or re.match(r"^(?:this (?:work|article|paper) is licensed under|acm (?:isbn|reference format))\b", text, re.I)) and PUBLICATION.match(text):
            role = "extra"
        elif page > 1 and top < height * .105 and len(text) < 160 and RUNNING_HEADER.search(text):
            role = "extra"
        elif page == 1 and FRONT_LABEL.fullmatch(text):
            role = "extra"
            front_label = block
            front_value = not re.search(r"[:：]\s*\S", text)
        elif front_label and block["kind"] != "heading" and len(text) < 500 and (front_value or _wrapped_line(front_label, block)):
            role = "extra"
            front_label = block
            front_value = False
        elif page == 1 and top > height * .68 and (
            re.match(r"^[∗*†‡]\s*\w", text) or re.match(r"^\d+(?:st|nd|rd|th)\s+Conference\b", text, re.I)
        ):
            role = "extra"
        elif (caption := CAPTION.match(text)) and not re.match(r"(?:shows?|illustrates?|summari[sz]es?|depicts?|presents?|lists?|reports?)\b", text[caption.end():].lstrip(), re.I):
            role = "caption"
        elif index in byline_indices:
            role = "extra"
        elif SECTION.fullmatch(text) or REFERENCE_HEADING.fullmatch(text) or block["kind"] == "heading":
            is_subheading = outline_levels.get(identity(text), 1) > 1 or re.match(r"^\d+\.\d+(?:\.\d+)*\s", text)
            role = "subheading" if is_subheading else "heading"
        elif REFERENCE_HEADING.fullmatch(block["section"]):
            role = "reference"
        else:
            role = "text"
        if role in ("heading", "subheading"):
            section = text
        if is_running_section(block["section"], paper["title"]):
            block["section"] = section
        elif block["section"]:
            section = block["section"]
        block["reading_role"] = role
        if role not in ("extra", "image", "caption", "table"):
            front_value = False
            front_label = None
            if role == "text" and previous and previous["reading_role"] == "text":
                if not _starts_paragraph(previous, block) and (continues_sentence(previous["text"], block["text"]) or _wrapped_line(previous, block)):
                    block["continues_previous"] = True
            previous = block
        elif role in ("table", "embedded"):
            previous = None


def reading_runs(blocks):
    runs = []
    for block in blocks:
        if block["reading_role"] in ("title", "extra", "image", "caption", "table", "embedded") or (
            block["reading_role"] == "formula" and not block.get("equation")
        ):
            continue
        if block.get("continues_previous") and runs:
            runs[-1].append(block)
        else:
            runs.append([block])
    return runs


def export_reading_html(store, paper):
    from .pdf import length, page_data, slice_text

    with store.connect() as db:
        numbers = [r[0] for r in db.execute("SELECT number FROM pages WHERE paper_id=? ORDER BY number", (paper["id"],))]
    pages = {number: page_data(store, paper, number, include_geometry=False) for number in numbers}

    source_language = "zh-CN" if paper["language"] == "zh" else "en"
    target_language = "en" if source_language == "zh-CN" else "zh-CN"
    asset_dir = (store.root / "assets").resolve()
    escape = html.escape

    def render_image(asset, description, width=None):
        image = (asset_dir / (asset or "")).resolve()
        if not image.is_relative_to(asset_dir) or not image.is_file() or image.suffix.lower() != ".png":
            return ""
        png = base64.b64encode(image.read_bytes()).decode("ascii")
        size = f' width="{width}"' if width else ""
        return f'<img alt="{escape(description)}" loading="lazy"{size} src="data:image/png;base64,{png}">'

    def formatted(block, translated=False, bounds=None):
        translation = block["translation"]
        text = translation["text"] if translated else block["text"]
        styles = block.get("notation", ())
        if translated:
            mapped = []
            for span in translation["segments"]:
                if not span.get("notation") or span.get("target_block_id"):
                    continue
                if span.get("source_block_id"):
                    mapped.extend(span.get("notation_styles", ()))
                else:
                    offset = span["target_start"] - span["source_start"]
                    mapped.extend((start + offset, end + offset, kind) for start, end, kind in styles
                                  if span["source_start"] <= start and end <= span["source_end"])
            styles = mapped
        start, end = bounds or (0, length(text))

        def chunk(a, b):
            value = escape(readable_text(slice_text(text, a, b))) if a < b else ""
            return re.sub(r"\s+(?=[•◦●▪▫])", "<br>", value) if bounds else value

        pieces, cursor = [], start
        for left, right, style in sorted(styles):
            a, b = max(start, left), min(end, right)
            if cursor <= a < b:
                pieces.append(chunk(cursor, a))
                value = chunk(a, b)
                pieces.append(f'<{style}>{value}</{style}>' if style in ("sup", "sub") else f'<span class="math-inline">{value}</span>')
                cursor = b
        return "".join([*pieces, chunk(cursor, end)])

    def render_block(block):
        role = block["reading_role"]
        anchor = f'block-{escape(block["id"])}'
        if role == "image":
            image = render_image(block["asset"], f'第 {block["page"]} 页图表')
            return f'<figure class="reading-figure" id="{anchor}">{image}</figure>' if image else ""
        if equation := block.get("equation"):
            label = f'公式 ({equation["number"]})' if equation["number"] else "原文公式"
            image = render_image(equation["asset"], label, round(equation["width"] * 1.65))
            aliases = "".join(f'<span id="block-{escape(bid)}"></span>' for bid in equation["block_ids"][1:])
            return f'<figure class="reading-equation" id="{anchor}">{aliases}{image}</figure>'
        tag = {"heading": "h2", "subheading": "h3"}.get(role, "p")
        content = f'<{tag} class="block-text">{formatted(block)}</{tag}>'
        if block["translation"] and block["translation"]["text"]:
            content += (
                f'<div class="paired-translation" lang="{target_language}">'
                f'<p class="block-text">{formatted(block, True)}</p></div>'
            )
        return f'<div id="{anchor}" class="reading-block reading-{role}">{content}</div>'

    def render_table(table, blocks):
        caption = blocks.get(table["caption_id"])
        original = render_image(table["asset"], readable_text(caption["text"]).strip() if caption else "原表")
        content = ['<figure class="reading-table">']
        if caption:
            content.append(f'<figcaption>{render_block(caption)}</figcaption>')
        if not table["cells"]:
            return "".join([*content, '<div class="table-original-image">', original, '</div></figure>'])
        anchored = set()
        for translated in (False, True):
            if translated and not any(part["target"] for cell in table["cells"] for part in cell["parts"]):
                continue
            language = target_language if translated else source_language
            content.append(f'<div class="reading-table-scroll" lang="{language}" tabindex="0"><table><colgroup>')
            content.extend(f'<col style="width:{width:.3f}%">' for width in table["widths"])
            content.append("</colgroup><thead>")
            for row in range(table["rows"]):
                if row == table["header_rows"]:
                    content.append("</thead><tbody>")
                content.append("<tr>")
                tag = "th" if row < table["header_rows"] else "td"
                for cell in table["cells"]:
                    if cell["row"] != row:
                        continue
                    content.append(f'<{tag} rowspan="{cell["rowspan"]}" colspan="{cell["colspan"]}">')
                    for part in cell["parts"][:1] if translated and cell["joined"] else cell["parts"]:
                        bid = part["block_id"]
                        target = part["target"] if translated else None
                        attributes = f' id="block-{escape(bid)}"' if bid not in anchored else ""
                        anchored.add(bid)
                        pending = " table-pending" if translated and not target else ""
                        value = formatted(blocks[bid], bool(target), target or (part["start"], part["end"]))
                        content.append(f'<span class="table-text-part{pending}"{attributes}>{value}</span>')
                    content.append(f"</{tag}>")
                content.append("</tr>")
            content.append("</tbody></table></div>")
        content.append(f'<details class="table-original"><summary>查看原表</summary>{original}</details></figure>')
        return "".join(content)

    title_blocks = [b for b in (pages.get(1) or {}).get("blocks", []) if b["reading_role"] == "title"]
    title_text = " ".join(f'<span id="block-{escape(b["id"])}">{escape(readable_text(b["text"]).strip())}</span>' for b in title_blocks) if title_blocks else escape(paper["title"])
    title_translation = " ".join(b["translation"]["text"] for b in title_blocks) if title_blocks and all(b["translation"] for b in title_blocks) else ""
    publication = " · ".join(part for part in (paper["source"], paper["year"]) if part)
    authors = " · ".join(name.strip() for name in re.split("[;；]", paper["authors"]) if name.strip())
    header = ['<header class="article-cover">']
    if publication:
        header.append(f'<p class="article-publication">{escape(publication)}</p>')
    header.append(f'<h1 class="article-title">{title_text}</h1>')
    if title_translation:
        header.append(f'<p class="article-title-translation" lang="{target_language}">{escape(title_translation)}</p>')
    if authors:
        header.append(f'<p class="article-authors">{escape(authors)}</p>')
    header.append("</header>")

    headings = [block for data in pages.values() for block in data["blocks"] if block["reading_role"] in ("heading", "subheading")]
    contents = []
    if headings:
        contents = ['<nav aria-label="章节目录"><details class="article-extras article-contents"><summary>章节目录</summary><ol>']
        for block in headings:
            contents.append(f'<li><a href="#block-{escape(block["id"])}">{escape(block["text"])}</a></li>')
        contents.append("</ol></details></nav>")

    body = []
    for page, data in pages.items():
        blocks = data["blocks"]
        tables = data["tables"]
        body.append(f'<section id="page-{page}" class="export-page" aria-label="第 {page} 页">')
        body.append(f'<p class="article-page-number">原文第 {page} 页</p>')
        runs = reading_runs(blocks)
        flow = []
        for run in runs:
            if len(run) == 1:
                flow.append((run[0]["position"], render_block(run[0])))
                continue
            content = ['<p class="reading-block reading-joined block-text">']
            for index, block in enumerate(run):
                if index and not (run[index - 1]["text"].rstrip().endswith(("-", "\u00ad")) and block["text"][:1].islower()):
                    content.append(" ")
                content.append(f'<span id="block-{escape(block["id"])}">{formatted(block).strip()}</span>')
            content.append("</p>")
            for block in run:
                if block["translation"] and block["translation"]["text"]:
                    content.append(f'<div class="paired-translation" lang="{target_language}"><p class="block-text">{formatted(block, True)}</p></div>')
            flow.append((run[0]["position"], "".join(content)))
        by_id = {b["id"]: b for b in blocks}
        flow.extend((table["position"], render_table(table, by_id)) for table in tables)
        body.extend(content for _, content in sorted(flow, key=lambda item: item[0]))
        caption_ids = {table["caption_id"] for table in tables}
        figures = [block for block in blocks if block["reading_role"] in ("image", "caption") and block["id"] not in caption_ids]
        if figures:
            body.append('<details class="article-extras article-figures"' + (' open' if not runs else '') + '><summary>本页图表</summary>')
            body.extend(render_block(block) for block in figures)
            body.append("</details>")
        extras = [block for block in blocks if block["reading_role"] == "extra"]
        if extras:
            body.append('<details class="article-extras"><summary>页边与出版信息</summary>')
            body.extend(render_block(block) for block in extras)
            body.append("</details>")
        body.append("</section>")
    if not pages:
        body.append('<p class="reading-block">暂无可显示的正文。</p>')

    styles = Path(__file__).with_name("reading.css").read_text(encoding="utf-8")
    return (
        f'<!doctype html><html lang="{source_language}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{escape(paper["title"])}</title><style>{styles}</style></head>'
        '<body class="reading-export"><main class="article-view">'
        + "".join(header + contents + body)
        + "</main></body></html>"
    )
