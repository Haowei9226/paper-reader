"""Read numbered bibliography entries from the PDF's existing text anchors."""
from __future__ import annotations

import json
import re
import unicodedata
from itertools import groupby
from urllib.parse import quote, urlsplit

from .metadata import NAME_PARTICLES, doi_name, first_author, identity, valid_title, year_of
from .pdf import geometry, length
from .reading import REFERENCE_HEADING, classify_reading_blocks, readable_text


REFERENCE_NUMBER = r"[0-9０-９]{1,4}|[〇零一二三四五六七八九十百千]{1,8}"
LABEL = re.compile(
    rf"^(?:[\[［【〔]\s*(?P<square>{REFERENCE_NUMBER})\s*[\]］】〕]|"
    rf"[(（]\s*(?P<round>{REFERENCE_NUMBER})\s*[)）]|"
    rf"(?P<punct>{REFERENCE_NUMBER})[.．、)）](?=\s|[^\W\d_]|$)|"
    r"(?P<circled>[①-⑳])|(?P<bare>[0-9０-９]{1,4})\s+(?=[^\W\d_]))"
)
AFTER_REFERENCES = re.compile(r"^(?:(?i:appendix\b|supplement(?:al|ary)?\b|acknowledg(?:e)?ments?\b)|附录|补充材料|致谢|[A-Z][.．]?\s+\S)")
HTTP_URL = re.compile(r"https?://[^\s<>\"“”]+", re.I)
ARXIV_ID = re.compile(r"\barxiv(?:\s*:\s*|\s+(?:abs/)?)(\d{4}\.\d{4,5}(?:v\d+)?|[a-z][a-z.\-]*/\d{7}(?:v\d+)?)\b", re.I)
YEAR = re.compile(r"(?<![\w/])((?:18|19|20|21)\d{2})[a-z]?(?!\w)", re.I)
DATE = re.compile(r"(?<![\w/])(?P<year>(?:18|19|20|21)\d{2}[a-z]?|n\s*\.\s*d\s*\.|无日期)(?!\w)", re.I)
AUTHOR_WORD = re.compile(r"[^\W\d_]+(?:['’\-][^\W\d_]+)*", re.UNICODE)
AUTHOR_CONNECTORS = NAME_PARTICLES | {"and", "of", "the", "for", "on", "in", "at"}
ORGANIZATION = re.compile(r"\b(?:organi[sz]ation|association|institute|agency|council|ministry|university|committee|department|foundation|corporation)\b", re.I)


def _reference_number(value):
    value = unicodedata.normalize("NFKC", value)
    if value.isdecimal():
        return int(value)
    digits = {char: index for index, char in enumerate("零一二三四五六七八九")}
    digits["〇"] = 0
    result, digit = 0, 0
    for char in value:
        if char in digits:
            digit = digit * 10 + digits[char]
        else:
            result += (digit or 1) * {"十": 10, "百": 100, "千": 1000}[char]
            digit = 0
    return result + digit


def _block_lines(block):
    """Keep visual lines tied to the original block and UTF-16 character offsets."""
    text = block["text"]
    geo = geometry(block["geometry"])
    groups, start, offset, previous, fallback_line = [], 0, 0, None, 0
    for index, char in enumerate(text):
        line = geo["lines"][offset] if offset < len(geo["lines"]) else fallback_line
        if previous is not None and line != previous:
            groups.append((start, index))
            start = index
        previous = line
        offset += length(char)
        fallback_line += char == "\n"
    groups.append((start, len(text)))
    for start, end in groups:
        raw = text[start:end]
        leading = len(raw) - len(raw.lstrip())
        content = raw.strip()
        if content:
            start += leading
            offset = length(text[:start])
            rectangles = [box for box in geo["boxes"][offset:offset + length(content)] if box]
            left = min(box[0] for box in rectangles) if rectangles else None
            top = min(box[1] for box in rectangles) if rectangles else None
            yield {"block": block, "text": content, "offset": offset, "left": left, "top": top}


def _surname(name):
    words = AUTHOR_WORD.findall(name)
    if not words:
        return ""
    if len(words) > 1 and len(words[-1]) == 1:
        return " ".join(words[:-1])
    first = len(words) - 1
    while first > 0 and words[first - 1].casefold() in NAME_PARTICLES:
        first -= 1
    return " ".join(words[first:])


def _given_aliases(given, family):
    words = AUTHOR_WORD.findall(given)
    if not words:
        return []
    return [words[0][0] + ". " + family, "".join(word[0] for word in words) + family,
            " ".join(words) + " " + family]


def _author_names(prefix):
    prefix = unicodedata.normalize("NFKC", prefix).strip(" .,:;([（")
    abbreviated = bool(re.search(r"(?:\bet\s+al\.?|等)\s*$", prefix, re.I))
    prefix = re.sub(r"(?:\bet\s+al\.?|等)\s*$", "", prefix, flags=re.I).strip(" .,")
    aliases = []
    alias = re.search(r"\s*[(\[]([A-Z][A-Z0-9-]{1,14})[)\]]$", prefix)
    if alias:
        aliases = [alias[1]]
        prefix = prefix[:alias.start()].rstrip(" .,")
    if not prefix or len(prefix) > 700 or re.search(r"[\d\"“”:：!?。]", prefix):
        return None
    words = AUTHOR_WORD.findall(prefix)
    if not words:
        return None
    chinese = bool(re.search(r"[\u3400-\u9fff]", prefix))
    if chinese:
        if re.search(r"组织|协会|学院|大学|委员会|研究院|公司|部门|联合国", prefix):
            return [prefix], abbreviated, aliases
        names = [part.strip() for part in re.split(r"[,，、;；]|和|与", prefix) if part.strip()]
        return (names, abbreviated, aliases) if all(re.fullmatch(r"[\u3400-\u9fff]{2,6}", name) for name in names) else None
    if any(not (word[0].isupper() or word.casefold() in AUTHOR_CONNECTORS) for word in words):
        return None
    if words[-1].casefold() in AUTHOR_CONNECTORS:
        return None
    # A continuation containing title/venue sentences is not an author byline.
    if re.search(r"\b(?!Jr\b|Sr\b|Dept\b|Univ\b|Inst\b|Inc\b|Co\b)[^\W\d_]{2,}\.\s+\S", prefix):
        return None
    if ORGANIZATION.search(prefix) and ("," not in prefix or re.match(r"^(?:National|World|United|International|Ministry)\b", prefix)):
        return [prefix], abbreviated, aliases
    names = []
    for group in re.split(r"\s*(?:;|&|\band\b)\s*", prefix):
        parts = [part.strip(" .,") for part in group.split(",") if part.strip(" .,")]
        index = 0
        while index < len(parts):
            tokens = AUTHOR_WORD.findall(parts[index])
            family_first = len(tokens) == 1 or all(word.casefold() in NAME_PARTICLES for word in tokens[:-1])
            if family_first and index + 1 < len(parts):
                if not names:
                    aliases.extend(_given_aliases(parts[index + 1], parts[index]))
                names.append(parts[index])
                index += 2
            else:
                family = _surname(parts[index])
                if not names and len(tokens) > 1:
                    given = tokens[-1:] if len(tokens[-1]) == 1 else tokens[:len(tokens) - len(AUTHOR_WORD.findall(family))]
                    aliases.extend(_given_aliases(" ".join(given), family))
                names.append(family)
                index += 1
    return (names, abbreviated, aliases) if names and all(names) else None


def _author_date(text):
    text = unicodedata.normalize("NFKC", text)
    date = DATE.search(text[:750])
    if date and (parsed := _author_names(text[:date.start()])):
        prefix = text[:date.start()].rstrip()
        if (not prefix.endswith((".", "(", "[", ",", ";")) and len(AUTHOR_WORD.findall(prefix)) > 1
                and not ORGANIZATION.search(prefix) and not re.search(r"[\u3400-\u9fff]", prefix)):
            return None
        names, abbreviated, aliases = parsed
        year = re.sub(r"\s+", "", date["year"].lower())
        return names, year, abbreviated, aliases
    # GB/T bibliography puts the publication year after the title and venue.
    author = re.match(r"^([^。.]{2,180})[。.]\s*", text)
    genre = re.search(r"\[(?:J|M|C|D|R|EB/OL|J/OL|M/OL)\]", text, re.I)
    author_tokens = AUTHOR_WORD.findall(author[1]) if author else []
    gb_author = author and (re.search(r"[\u3400-\u9fff]", author[1]) or
                            len(author_tokens) > 1 and any(len(word) == 1 for word in author_tokens))
    if gb_author and genre and genre.start() < 400 and (parsed := _author_names(author[1])):
        if date := DATE.search(HTTP_URL.sub("", text[genre.end():])):
            names, abbreviated, aliases = parsed
            return names, re.sub(r"\s+", "", date["year"].lower()), abbreviated, aliases
    return None


def _citation_identity(text):
    parsed = _author_date(text)
    if not parsed:
        return [], ""
    names, year, abbreviated, aliases = parsed
    authors = [names[0], *aliases]
    if len(names) == 2 and not abbreviated:
        authors.append("".join(names))
    keys = [{"author": author, "year": year} for author in dict.fromkeys(identity(name) for name in authors) if author]
    label = names[0] + (" et al." if abbreviated or len(names) > 2 else " & " + names[1] if len(names) == 2 else "")
    return keys, label + ", " + year


def _http_urls(text):
    text = re.sub(r"(https://api\.semanticscholar\.org/CorpusID:)\s+(?=\d)", r"\1", text, flags=re.I)
    for match in HTTP_URL.finditer(text):
        url = match[0].rstrip(".,;:!?，。；")
        while url.endswith(")") and url.count(")") > url.count("("):
            url = url[:-1]
        if "\\" in url or re.search(r"[\x00-\x20\x7f]|%(?:0[0-9a-f]|1[0-9a-f]|7f)", url, re.I):
            continue
        try:
            parsed = urlsplit(url)
            if parsed.scheme.lower() in ("http", "https") and parsed.hostname and parsed.port != 0 and not (parsed.username or parsed.password):
                yield url
        except ValueError:
            continue


def _external_target(text, doi):
    if doi:
        return "https://doi.org/" + quote(doi, safe="/"), "doi"
    urls = list(_http_urls(text))
    for url in urls:
        parsed = urlsplit(url)
        if parsed.hostname in ("arxiv.org", "www.arxiv.org", "export.arxiv.org") and re.match(r"/(?:abs|pdf|html)/", parsed.path):
            return url, "url"
    if arxiv := ARXIV_ID.search(text):
        return "https://arxiv.org/abs/" + quote(arxiv[1], safe="/"), "url"
    if urls:
        return urls[0], "url"
    return "https://scholar.google.com/scholar?q=" + quote(text[:2000], safe=""), "search"


def _title_span(text, key, reference_key, positions):
    # Short titles are often topic or venue names, even when an author agrees.
    if len(key) < (6 if re.search(r"[\u3400-\u9fff]", key) else 12):
        return None
    found = reference_key.find(key)
    if found < 0:
        return None
    start, end = positions[found], positions[found + len(key) - 1] + 1
    before, after = text[:start].rstrip(), text[end:].lstrip()
    if not before or before[-1] not in ".,;:!?)]}\"'“‘。；：":
        return None
    if before[-1] in "\"'“‘":
        if not re.match(r"[.,!?。]*[\"'”’]", after):
            return None
    elif after and not re.match(r"[.!?。]|\[(?:[a-z]{1,3}(?:/[a-z]{1,3})?)\]", after, re.I):
        return None
    return start, end


def _first_author_matches(prefix, authors):
    author = unicodedata.normalize("NFKC", first_author(authors)).casefold()
    if re.fullmatch(r"[\u3400-\u9fff]{2,6}", author):
        return bool(re.match(re.escape(author) + r"(?:等|[\s,，.。;；、(]|$)", prefix.lstrip()))
    words = lambda value: [identity(word) for word in re.findall(r"[^\W\d_]+(?:['’\-][^\W\d_]+)*", value)]
    name, cited = words(author), words(YEAR.sub("", prefix))
    if len(name) < 2 or not cited:
        return False
    if "," in author:
        family_text, given_text = author.split(",", 1)
        family, given = words(family_text), words(given_text)
    elif len(name[-1]) == 1:
        family, given = name[:-1], name[-1:]
    else:
        boundary = len(name) - 1
        while boundary > 0 and name[boundary - 1] in NAME_PARTICLES:
            boundary -= 1
        given, family = name[:boundary], name[boundary:]
    if not family or not given:
        return False
    if cited[:len(family)] == family and cited[len(family):len(family) + 2] == ["et", "al"]:
        return True
    for parts, surname_start in ((family + given, 0), (given + family, len(given))):
        if len(cited) < len(parts):
            continue
        if all(observed == expected or (
            not surname_start <= index < surname_start + len(family)
            and (len(observed) == 1 or len(expected) == 1) and observed[0] == expected[0]
        ) for index, (observed, expected) in enumerate(zip(cited, parts))):
            return True
    return False


def _bibliographic_match(text, row, span):
    start, end = span
    outside = HTTP_URL.sub("", text[:start] + " " + text[end:])
    years = {match[1] for match in YEAR.finditer(outside)}
    return years == {year_of(row["year"])} and _first_author_matches(text[:start], row["authors"])


def _reference_text(parts):
    text = ""
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if text and not (text.endswith("\u00ad") or re.search(r"\w-$", text) and part[0].islower()):
            text += " "
        text += part
    text = " ".join(readable_text(text).split())
    text = re.sub(r"(https?)\s*:\s*/\s*/\s*", r"\1://", text, flags=re.I)
    text = re.sub(r"((?:doi\.org/|doi:\s*)10\.)\s+(?=\d)", r"\1", text, flags=re.I)
    return re.sub(r"(10\.\d{4,9})\s*/\s*(?=\w)", r"\1/", text)


def _anchor(block, start, label):
    return {"block_id": block["id"], "page": block["page"], "start": start, "end": start + length(label), "quote": label}


def _entries(db, paper, heading):
    rows = db.execute("""SELECT b.id,b.page,b.position,b.kind,b.text,b.section,b.bbox,b.geometry,p.height
        FROM blocks b JOIN pages p ON p.paper_id=b.paper_id AND p.number=b.page
        WHERE b.paper_id=? AND b.kind!='image' AND (b.page>? OR (b.page=? AND b.position>=?))
        ORDER BY b.page,b.position""", (paper["id"], heading["page"], heading["page"], heading["position"]))
    outline = {(entry["page"], identity(entry["title"])) for entry in paper["outline"]}
    lines = []
    for page, group in groupby(rows, key=lambda row: row["page"]):
        blocks = [{**row, "bbox": json.loads(row["bbox"])} for row in group]
        classify_reading_blocks(blocks, paper, page, blocks[0]["height"])
        for block in blocks:
            if block["reading_role"] == "extra":
                continue
            for line in _block_lines(block):
                if block["id"] != heading["id"] or line["offset"] > heading["offset"]:
                    lines.append(line)
    entries, parts, current = [], [], None
    style, previous_number, unnumbered, entry_line = None, None, 0, None
    prelude_columns = []
    if heading.get("top") is not None and heading.get("left") is not None:
        for index, line in enumerate(lines):
            if (line["block"]["page"] == heading["page"] and line["top"] is not None and line["left"] is not None
                    and line["top"] < heading["top"] and abs(line["left"] - heading["left"]) > 80
                    and line["block"]["kind"] == "heading" and not REFERENCE_HEADING.fullmatch(line["text"])
                    and not _author_date(_reference_text(item["text"] for item in lines[index:index + 8])[:900])):
                prelude_columns.append(line["left"])

    def finish():
        if current and (text := _reference_text(parts)):
            doi = doi_name(text)
            url, kind = _external_target(text, doi)
            keys, label = _citation_identity(text)
            if not current["numbered"] and not label:
                return
            entries.append({**current, "display_label": current["display_label"] if current["numbered"] else label,
                            "citation_keys": keys, "text": text, "doi": doi, "url": url, "url_kind": kind})

    for index, line in enumerate(lines):
        block, text = line["block"], line["text"]
        if (block["page"] == heading["page"] and line["top"] is not None and heading.get("top") is not None
                and line["top"] < heading["top"] and line["left"] is not None
                and any(abs(line["left"] - left) < 40 for left in prelude_columns)):
            continue
        if REFERENCE_HEADING.fullmatch(text):
            continue
        if (block["kind"] == "heading" and re.match(r"^(?:appendix\b|supplement(?:al|ary)?\b|acknowledg(?:e)?ments?\b|附录|补充材料|致谢)", text, re.I)
                or re.fullmatch(r"(?:footnotes?|endnotes?|notes|注释|脚注|尾注)", text, re.I)):
            break
        marker = LABEL.match(text) if not unnumbered else None
        number = _reference_number(marker[marker.lastgroup]) if marker else None
        if marker and (not number or number > 9999 or
                       previous_number is None and marker.lastgroup in ("bare", "punct") and number != 1 or
                       previous_number is not None and (marker.lastgroup != style or not previous_number < number <= previous_number + 20)):
            marker = None
        if marker:
            finish()
            style, previous_number = marker.lastgroup, number
            label = marker[0].rstrip()
            current = {"number": number, "numbered": True, "display_label": label,
                       "anchor": _anchor(block, line["offset"], label)}
            entry_line = line
            parts = [text[marker.end():]]
            continue
        lookahead = _reference_text(item["text"] for item in lines[index:index + 8])[:900]
        parsed = _author_date(lookahead) if style is None else None
        if parsed:
            hanging = (entry_line and entry_line["block"]["page"] == block["page"]
                       and entry_line["left"] is not None and line["left"] is not None
                       and 3 < line["left"] - entry_line["left"] < 65)
            if current and hanging:
                parts.append(text)
                continue
            if current and not current["numbered"] and not _author_date(_reference_text(parts)):
                parts.append(text)
                continue
            finish()
            unnumbered += 1
            current = {"number": -unnumbered, "numbered": False, "display_label": "",
                       "anchor": _anchor(block, line["offset"], text[:100])}
            entry_line = line
            parts = [text]
        elif (AFTER_REFERENCES.match(text) and block["kind"] == "heading" or
              re.fullmatch(r"(?:footnotes?|endnotes?|notes|注释|脚注|尾注)", text, re.I) or
              block["kind"] == "heading" and (block["page"], identity(text)) in outline):
            break
        elif current:
            parts.append(text)
    finish()
    by_author_year = {}
    for entry in entries:
        if not entry["numbered"] and entry["citation_keys"]:
            first = entry["citation_keys"][0]
            by_author_year.setdefault((first["author"], first["year"]), []).append(entry)
    for group in by_author_year.values():
        if len(group) < 2:
            continue
        for entry in group:
            names, _, _, aliases = _author_date(entry["text"])
            qualified = next((alias for alias in aliases if re.match(r"^[^\W\d_]\. ", alias)), None)
            if qualified:
                entry["display_label"] = entry["display_label"].replace(names[0], qualified, 1)
    return entries


def _link_library_entries(db, paper, entries):
    own_doi = doi_name(paper["doi"]).casefold()
    library = [dict(row) for row in db.execute(
        "SELECT id,title,doi,authors,year,source FROM papers WHERE deleted_at IS NULL AND id<>?", (paper["id"],))]
    doi_matches = {}
    for row in library:
        row["title_key"] = identity(valid_title(row["title"]))
        if doi := doi_name(row["doi"]).casefold():
            doi_matches[doi] = None if doi in doi_matches else row
    for entry in entries:
        match, method = None, None
        doi = entry["doi"].casefold()
        if doi:
            if doi != own_doi:
                match = doi_matches.get(doi)
                method = "doi" if match else None
        else:
            text = unicodedata.normalize("NFKC", entry["text"]).casefold()
            positions = [index for index, char in enumerate(text) if char.isalnum()]
            reference_key = "".join(text[index] for index in positions)
            own_span = _title_span(text, identity(valid_title(paper["title"])), reference_key, positions)
            if not own_span or not _bibliographic_match(text, paper, own_span):
                spans = [(row, span) for row in library if (span := _title_span(text, row["title_key"], reference_key, positions))]
                candidates = [row for row, span in spans if _bibliographic_match(text, row, span) and not any(
                    other_span != span and other_span[0] <= span[0] and other_span[1] >= span[1]
                    for _, other_span in spans
                )]
                if len(candidates) == 1:
                    candidate = candidates[0]
                    candidate_doi = doi_name(candidate["doi"]).casefold()
                    if not candidate_doi or (candidate_doi != own_doi and doi_matches[candidate_doi] is candidate):
                        match, method = candidate, "title_author_year"
        entry["library_match"] = {"paper_id": match["id"], "title": match["title"]} if match else None
        entry["library_match_method"] = method
        if match and entry["url_kind"] == "search":
            if matched_doi := doi_name(match["doi"]):
                entry["url"], entry["url_kind"] = _external_target("", matched_doi)
            elif url := next(_http_urls(match["source"]), ""):
                entry["url"], entry["url_kind"] = url, "url"


def paper_references(store, paper):
    with store.connect() as db:
        headings = []
        for row in db.execute("""SELECT id,page,position,text,geometry FROM blocks
            WHERE paper_id=? AND kind!='image' ORDER BY page,position""", (paper["id"],)):
            for line in _block_lines(row):
                if REFERENCE_HEADING.fullmatch(line["text"]):
                    headings.append({**row, "offset": line["offset"], "label": line["text"], "left": line["left"], "top": line["top"]})
        for heading in headings:
            if entries := _entries(db, paper, heading):
                _link_library_entries(db, paper, entries)
                return {"entries": entries, "heading": _anchor(heading, heading["offset"], heading["label"])}
        if headings:
            heading = headings[-1]
            return {"entries": [], "heading": _anchor(heading, heading["offset"], heading["label"])}
    return {"entries": [], "heading": None}
