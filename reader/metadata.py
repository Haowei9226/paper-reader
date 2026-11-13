"""Conservative bibliography lookup for imported PDFs. Never upload the PDF."""

from __future__ import annotations

import html
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from difflib import SequenceMatcher
from urllib.parse import quote, unquote
from xml.etree import ElementTree

import httpx
import pymupdf

from .pdf import scalar_text
from .store import dumps, now, uid

PUBLICATION_COLUMNS = {"publication_date": "出版日期", "publisher": "出版社", "item_type": "条目类型"}
BIB_FIELDS = ("title", "authors", "year", "source", "doi", "abstract", *PUBLICATION_COLUMNS)
TYPE_MAP = {
    "journalarticle": "journal-article", "proceedingsarticle": "conference-paper", "conferencepaper": "conference-paper",
    "book": "book", "monograph": "book", "editedbook": "book", "bookchapter": "book-chapter",
    "preprint": "preprint", "postedcontent": "posted-content",
    "dissertation": "thesis", "thesis": "thesis", "report": "report", "dataset": "dataset", "other": "other",
}
ITEM_TYPES = frozenset(TYPE_MAP.values())
PUBLICATION_STATUSES = frozenset(("", "published", "unpublished", "internal"))
PUBLICATION_SKIP_MESSAGES = {
    "unpublished": "未发表手稿无需补全出版信息",
    "internal": "内部资料无需补全出版信息",
}
PUBLICATION_EXPORT_NOTES = {
    "published": "Published work",
    "unpublished": "Unpublished manuscript",
    "internal": "Internal material (not for publication)",
}
DOI_PATTERN = r"10\.\d{4,9}/[-._;()/:A-Z0-9]+"
CROSSREF = "https://api.crossref.org/works"
DATACITE = "https://api.datacite.org/dois"
REMOTE_SOURCES = {"Crossref", "DataCite"}
AUTHOR_PLACEHOLDER = re.compile(r"(?:unknown|author|administrator|admin|microsoft word|acrobat|user|windows user|"
                                r"(?:laptop|desktop)(?:[\s_-].*)?)", re.I)
AFFILIATION = re.compile(r"@|\b(?:universit\w*|college|institute|department|school|laborator\w*|"
                         r"research (?:lab|center|centre)|orcid|corresponding author)\b|大学|学院|研究所|实验室|作者单位", re.I)
AUTHOR_ROLE = re.compile(r",\s*(?:ph\.?\s*d\.?|m\.?\s*d\.?|(?:doctoral|graduate|master[’']?s?)\s+student|professor)\b.*$", re.I)
AFTER_BYLINE = re.compile(r"^(?:abstract\b|摘\s*要|(?:1[.、\s]+)?(?:introduction\b|引言)|keywords\b|ccs concepts\b)", re.I)
NAME_TOKEN = re.compile(r"[^\W\d_]+(?:[-’'][^\W\d_]+)*\.?", re.UNICODE)
NAME_PARTICLES = {"al", "bin", "da", "de", "del", "der", "di", "dos", "la", "le", "van", "von"}
# Recognizable UTF-8 punctuation bytes interpreted as PDFDocEncoding/Windows
# text. This is detection only: replacement requires an independent printed copy.
BROKEN_METADATA = re.compile(r"\ufffd|\u00e2(?:\u2022[\ufb01\ufb02]|\u20ac[\u02dc\u2122\u0153\u201c\u201d\u00a6\u2014\u2013])")


def plain(value, limit=2000):
    if not isinstance(value, str):
        return ""
    value = html.unescape(re.sub(r"</?(?:[a-z][\w-]*:)?[a-z][\w-]*(?:\s[^<>]*)?/?>", " ", value, flags=re.I))
    return " ".join(scalar_text(value).split())[:limit]


def identity(value):
    return "".join(ch for ch in unicodedata.normalize("NFKC", value).casefold() if ch.isalnum())


def matching_titles(left, right):
    return SequenceMatcher(None, identity(left), identity(right)).ratio() >= .82


def valid_title(value):
    title = plain(value, 350)
    if len(identity(title)) < 5 or re.match(r"^(?:microsoft word\b|untitled\b|document\d*$|download(?:\s*\d+)?$|full[- ]?text$|https?://)", title, re.I):
        return ""
    if identity(title) in {"abstract", "introduction", "references", "bibliography", "tableofcontents", "contents"}:
        return ""
    return title


def doi_name(value):
    match = re.search(DOI_PATTERN, unquote(value or ""), re.I)
    if not match:
        return ""
    doi = match.group().rstrip(".,;:")
    while doi.endswith(")") and doi.count(")") > doi.count("("):
        doi = doi[:-1]
    return doi if len(doi) <= 250 else ""


def year_of(value):
    match = re.search(r"\b(?:18|19|20|21)\d{2}\b", str(value or ""))
    return match.group() if match else ""


def publication_date(value):
    value = plain(value, 80).split("T", 1)[0]
    if not re.fullmatch(r"\d{4}(?:-\d{1,2}(?:-\d{1,2})?)?", value):
        return ""
    parts = [int(part) for part in value.split("-")]
    try:
        validated = date(*(parts + [1] * (3 - len(parts))))
    except ValueError:
        return ""
    return validated.isoformat()[:(4, 7, 10)[len(parts) - 1]]


def pdf_filename(value):
    stem = re.sub(r"\.pdf$", "", str(value), flags=re.I)
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", stem)
    stem = " ".join(stem.split())[:150].strip(" .") or "document"
    if re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³]", stem.split(".", 1)[0], re.I):
        stem = "_" + stem
    return stem + ".pdf"


def first_author(authors):
    return re.split(r"[;；]", authors or "", maxsplit=1)[0].strip()


def credible_authors(value):
    value = plain(value)
    return "" if AUTHOR_PLACEHOLDER.fullmatch(value) else value


def byline_names(text):
    if AFFILIATION.search(text) or re.search(r"\b(?:study|analysis|framework|approach|dataset|experiment)\b", text, re.I):
        return []
    text = AUTHOR_ROLE.sub("", text)
    text = unicodedata.normalize("NFC", re.sub(r"´([^\W\d_])", lambda m: m[1] + "\u0301", text))
    text = re.sub(r"(?<=[^\W\d_])[\d¹²³⁰⁴-⁹]+(?=$|[\s,，、;；*∗†‡])", "", text)
    parts = [part.strip(" .,*∗†‡") for part in re.split(r"\band\b|[,，、;；]", text) if part.strip(" .,*∗†‡")]

    def is_name(part):
        if re.fullmatch(r"[\u3400-\u9fff]{2,4}(?:[·・][\u3400-\u9fff]{1,4})?", part):
            return True
        words = part.replace("(", "").replace(")", "").replace("（", "").replace("）", "").split()
        return 2 <= len(words) <= 7 and all(
            NAME_TOKEN.fullmatch(word) and (word[0].isupper() or word.casefold() in NAME_PARTICLES
            or (index == len(words) - 1 and words[0][0].isupper() and 2 <= len(word) <= 5 and word.islower()))
            for index, word in enumerate(words)
        )

    return parts if parts and all(is_name(part) for part in parts) else []


def visible_authors(lines, title):
    """Extract a byline only when it is anchored to the paper's visible title."""
    key = identity(title)
    end = None
    for start, line in enumerate(lines):
        if not key or not key.startswith(identity(line[0])):
            continue
        found = ""
        for index in range(start, min(start + 6, len(lines))):
            found += identity(lines[index][0])
            if found == key:
                end = index
                break
            if not key.startswith(found):
                break
        if end is not None:
            break
    if end is None:
        return ""

    front = []
    for line in lines[end + 1:]:
        if AFTER_BYLINE.match(line[0]) or line[1][1] > lines[end][1][3] + 230:
            break
        front.append(line)
    first = next((line for line in front if line[1][1] - lines[end][1][3] <= 48 and byline_names(line[0])), None)
    if not first:
        return ""
    support = any(AFFILIATION.search(line[0]) or AUTHOR_ROLE.search(line[0]) for line in front)
    authors, seen = [], set()
    for text, _, size, baseline in front:
        if baseline < first[3] - 2 or abs(size - first[2]) > max(.75, first[2] * .085):
            continue
        for author in byline_names(text):
            if (name := identity(author)) not in seen:
                authors.append(author)
                seen.add(name)
    if not authors or (not support and len(authors) < 2):
        return ""
    return "; ".join(authors)[:2000]


def same_first_author(left, right):
    def parts(value):
        return [identity(part) for part in re.findall(r"[^\W\d_]+", first_author(value))]

    left, right = parts(left), parts(right)
    return bool(left and right and (sorted(left) == sorted(right) or (
        len(left) > 1 and len(right) > 1 and left[-1] == right[-1]
        and (len(left[0]) == 1 or len(right[0]) == 1) and left[0][0] == right[0][0]
    )))


def bibliography_filename(paper):
    title = valid_title(paper["title"])
    if not title:
        return pdf_filename(paper["filename"])
    pieces = [first_author(paper.get("authors"))[:38], year_of(paper.get("year")), title[:105]]
    return pdf_filename(" - ".join(piece for piece in pieces if piece))


@dataclass
class PdfClues:
    fields: dict
    candidate_doi: str = ""
    cover_title: str = ""


def xmp_fields(doc):
    try:
        xml = doc.get_xml_metadata() or ""
    except (RuntimeError, UnicodeError):
        return {}
    if not xml or len(xml) > 524288 or re.search(r"<!DOCTYPE|<!ENTITY", xml, re.I):
        return {}
    try:
        tree = ElementTree.fromstring(scalar_text(xml))
    except ElementTree.ParseError:
        return {}
    values = {}
    names = {"title", "creator", "description", "doi", "identifier", "publicationname", "publicationdate", "publisher", "type"}
    for node in tree.iter():
        key = node.tag.rsplit("}", 1)[-1].lower()
        if key not in names:
            continue
        entries = [plain("".join(item.itertext()), 6000) for item in node.iter() if item.tag.endswith("}li")]
        value = ("; ".join(entries) if key == "creator" else entries[0]) if entries else plain(node.text, 6000)
        if value:
            values.setdefault(key, value)
    return values


def visible_publication_source(doc, source):
    """Return a printed source only for a corrupt field with exact textual support."""
    if not BROKEN_METADATA.search(source or ""):
        return ""
    # Remove only the recognized bad punctuation sequence from the comparison
    # key. Every remaining letter and number must occur in the printed copy.
    expected = identity(BROKEN_METADATA.sub("", source))
    if len(expected) < 24:
        return ""
    from .pdf import extract_block, recover_pdf_glyphs

    candidates = set()
    for page_number in range(min(3, len(doc))):
        page = doc[page_number]
        blocks = page.get_text("rawdict", flags=pymupdf.TEXTFLAGS_RAWDICT & ~pymupdf.TEXT_PRESERVE_IMAGES)["blocks"]
        recover_pdf_glyphs(page, blocks)
        text = "\n".join(extract_block(block, page)[0] for block in blocks if block.get("type") == 0)
        key, offsets = [], []
        for index, char in enumerate(text):
            folded = identity(char)
            key.extend(folded)
            offsets.extend([index] * len(folded))
        key = "".join(key)
        start = 0
        while (found := key.find(expected, start)) >= 0:
            value = plain(text[offsets[found]:offsets[found + len(expected) - 1] + 1])
            if value and not BROKEN_METADATA.search(value):
                candidates.add(value)
            start = found + 1
    return next(iter(candidates)) if len(candidates) == 1 else ""


def pdf_clues(doc):
    from .pdf import front_lines, inferred_title

    if doc.needs_pass or not len(doc):
        return PdfClues({})
    embedded, xmp = doc.metadata or {}, xmp_fields(doc)
    page = doc[0]
    first_page = page.get_text("text", sort=True)[:16000]
    front = re.split(r"(?im)^\s*(?:references|bibliography|参考文献)\s*[:：]?\s*$", first_page, maxsplit=1)[0]
    header = re.split(r"(?im)^\s*(?:abstract\b|摘\s*要)", front, maxsplit=1)[0]
    fields = {}
    lines = front_lines(page)
    inferred = valid_title(inferred_title(lines))
    title = valid_title(xmp.get("title")) or valid_title(embedded.get("title")) or inferred
    byline = visible_authors(lines, title)
    if not byline and inferred != title and (visible := visible_authors(lines, inferred)):
        title, byline = inferred, visible
    if title:
        fields["title"] = title
    labelled = re.search(r"(?im)^[ \t]*(?:Authors?|作\s*者)\s*[:：]\s*([^\r\n]+)", header)
    authors = (credible_authors(labelled[1]) if labelled else "") or byline
    authors = authors or credible_authors(xmp.get("creator")) or credible_authors(embedded.get("author"))
    if authors:
        fields["authors"] = authors
    published = re.search(r"(?im)^[ \t]*(?:published(?:\s+(?:online|on))?|publication year|出版年份|出版日期)\s*[:：]\s*([^\r\n]+)", header)
    raw_date = xmp.get("publicationdate") or (published.group(1) if published else "")
    year = year_of(raw_date)
    if year:
        fields["year"] = year
    if published_on := publication_date(raw_date):
        fields["publication_date"] = published_on
    if xmp.get("publisher"):
        fields["publisher"] = xmp["publisher"][:2000]
    if item_type := TYPE_MAP.get(identity(xmp.get("type", ""))):
        fields["item_type"] = item_type
    if xmp.get("publicationname"):
        source = xmp["publicationname"][:2000]
        fields["source"] = visible_publication_source(doc, source) or source
    abstract = re.search(
        r"(?ims)^[ \t]*(?:Abstract\b|摘\s*要)[ \t]*[:：.—-]?\s*(.+?)(?=^[ \t]*(?:Keywords?\b|Index Terms\b|关键词|Abstract\b|(?:\d+[. \t]+)?Introduction\b|[一1][、. \t]+引言|CCS CONCEPTS\b|ACM Reference Format\b)|\Z)",
        front,
    )
    description = xmp.get("description") or (plain(abstract.group(1), 6000) if abstract else "")
    if len(description) >= 30:
        fields["abstract"] = description
    doi = doi_name(xmp.get("doi") or xmp.get("identifier"))
    if not doi:
        # Layout sorting may insert a sideways arXiv stamp into a line of prose.
        native = page.get_text("text", sort=False)[:16000]
        arxiv_ids = set(re.findall(r"(?im)^[ \t]*arxiv:\s*(\d{4}\.\d{4,5}|[a-z.-]+/\d{7})(?:v\d+)?\b", native))
        if len(arxiv_ids) == 1:
            doi = "10.48550/arXiv." + arxiv_ids.pop()
    if not doi:
        labelled_dois = {doi_name(m.group(1)) for m in re.finditer(
            r"(?im)^[ \t]*(?:doi|digital object identifier)[ \t]*[:：][ \t]*(?:https?://(?:dx\.)?doi\.org/)?(" + DOI_PATTERN + ")", header)}
        labelled_dois.discard("")
        if len(labelled_dois) == 1:
            doi = labelled_dois.pop()
    if doi:
        fields["doi"] = doi
    printed = set()
    if not doi:
        for match in re.finditer(r"(?i)(?:\bdoi\s*[:：]\s*|https?://(?:dx\.)?doi\.org/|(?m:^[ \t]*))(" + DOI_PATTERN + ")", front):
            candidate = doi_name(match.group(1))
            if candidate:
                printed.add(candidate)
    return PdfClues(fields, doi or (next(iter(printed)) if len(printed) == 1 else ""), title if byline else "")


def merge_bibliography(paper, fields, *, online, auto_rename, allow_name):
    manual = set(json.loads(paper["manual_fields"]))
    if not online and paper.get("publication_status") in PUBLICATION_SKIP_MESSAGES:
        fields = {key: value for key, value in fields.items() if key not in ("publisher", "publication_date", "source")}
    updates = {}
    for key in BIB_FIELDS:
        if fields.get(key) and key not in manual and (online or paper["metadata_source"] not in REMOTE_SOURCES or not paper.get(key)):
            updates[key] = fields[key]
    merged = {**paper, **updates}
    if auto_rename and allow_name and "filename" not in manual and (online or (merged["authors"] and merged["year"] and fields.get("title"))):
        updates["filename"] = bibliography_filename(merged)
    return updates


def missing_publication_fields(paper):
    manual = set(json.loads(paper["manual_fields"]))
    return [key for key in PUBLICATION_COLUMNS if not paper[key] and key not in manual]


def write_bibliography(store, db, paper_id, updates):
    db.execute("UPDATE papers SET " + ", ".join(k + "=?" for k in updates) + ", updated=? WHERE id=?", (*updates.values(), now(), paper_id))
    store.reindex_paper(db, paper_id)


def refresh_pdf_authors(store, paper_id):
    with pymupdf.open(store.original(paper_id)) as doc:
        clues = pdf_clues(doc)
    if not clues.cover_title or not clues.fields.get("authors"):
        raise ValueError("未能从 PDF 首页确认署名，可在编辑窗口手动填写。")
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        paper = store.paper(paper_id, db)
        if not paper or paper["deleted_at"] is not None:
            raise ValueError("请先恢复这篇文献")
        if "authors" in json.loads(paper["manual_fields"]):
            raise ValueError("作者已手动填写，可在编辑窗口修改。")
        if db.execute("SELECT 1 FROM jobs WHERE paper_id=? AND kind='metadata' AND state IN ('queued','running')", (paper_id,)).fetchone():
            raise ValueError("书目信息正在查找，请完成后再重读署名。")
        updates = {}
        if paper["authors"] != clues.fields["authors"]:
            updates["authors"] = clues.fields["authors"]
        if paper["metadata_status"] == "matched" and not matching_titles(paper["title"], clues.cover_title):
            updates["metadata_status"] = "review"
        if updates:
            write_bibliography(store, db, paper_id, updates)
    return store.paper(paper_id)


def schedule_lookup(store, paper_id, *, requested=False):
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        paper = db.execute("SELECT publication_status FROM papers WHERE id=?", (paper_id,)).fetchone()
        if paper and not requested and paper["publication_status"] in PUBLICATION_SKIP_MESSAGES:
            return None
        current = db.execute("SELECT id,state,payload FROM jobs WHERE paper_id=? AND kind='metadata' AND state IN ('queued','running')", (paper_id,)).fetchone()
        if current:
            job_id = current[0]
            if requested and current["state"] == "queued":
                payload = json.loads(current["payload"])
                payload["requested"] = True
                payload.pop("fill_publication_fields", None)
                db.execute("UPDATE jobs SET payload=? WHERE id=?", (dumps(payload), job_id))
        else:
            job_id = uid()
            stamp = now()
            db.execute("INSERT INTO jobs(id,paper_id,kind,state,payload,created,updated) VALUES(?,?,'metadata','queued',?,?,?)",
                       (job_id, paper_id, dumps({"requested": requested}), stamp, stamp))
            db.execute("UPDATE papers SET metadata_status='queued' WHERE id=?", (paper_id,))
    return store.job(job_id)


def first_string(items):
    return next((plain(value) for value in items if isinstance(value, str) and value.strip()), "")


def crossref_record(value):
    authors = []
    for author in value.get("author") or []:
        name = plain(" ".join(str(author.get(k) or "") for k in ("given", "family"))) or plain(author.get("name"))
        if name:
            authors.append(name)
    published = ""
    for field in ("published", "published-print", "issued", "published-online", "posted"):
        parts = (value.get(field) or {}).get("date-parts") or []
        if parts and parts[0] and (published := publication_date("-".join(map(str, parts[0])))):
            break
    publisher = plain(value.get("publisher"))
    return {"title": valid_title(first_string(value.get("title") or [])), "authors": "; ".join(authors)[:2000], "year": published[:4],
            "publication_date": published, "publisher": publisher, "item_type": TYPE_MAP.get(identity(plain(value.get("type"))), ""),
            "source": first_string(value.get("container-title") or []) or plain((value.get("event") or {}).get("name")) or publisher,
            "doi": doi_name(value.get("DOI")), "abstract": plain(value.get("abstract"), 20000)}


def datacite_record(value):
    title = next((title for x in (value.get("titles") or []) if (title := valid_title(x.get("title")))), "")
    authors = [plain(" ".join(str(a.get(k) or "") for k in ("givenName", "familyName"))) or plain(a.get("name")) for a in (value.get("creators") or [])]
    publisher = value.get("publisher")
    source = plain(publisher.get("name") if isinstance(publisher, dict) else publisher)
    abstract = next((plain(x.get("description"), 20000) for x in (value.get("descriptions") or []) if x.get("descriptionType") == "Abstract"), "")
    year = year_of(value.get("publicationYear"))
    item_type = TYPE_MAP.get(identity(plain((value.get("types") or {}).get("resourceTypeGeneral"))), "")
    return {"title": title, "authors": "; ".join(a for a in authors if a)[:2000], "year": year,
            "publication_date": publication_date(value.get("published")) or year, "publisher": source, "item_type": item_type,
            "source": source, "doi": doi_name(value.get("doi")), "abstract": abstract}


def fetch_json(client, endpoint, **kwargs):
    response = client.get(endpoint, **kwargs)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


def lookup_online(title, doi, authors, year, *, explicit_doi=False):
    normalized = identity(title)
    with httpx.Client(timeout=httpx.Timeout(5, connect=3), follow_redirects=False, trust_env=False,
                      headers={"User-Agent": "PaperReader/0.3 (personal bibliography lookup)", "Accept": "application/json"}) as client:
        if doi:
            choices = [(CROSSREF, "Crossref"), (DATACITE, "DataCite")]
            if doi.casefold().startswith("10.48550/arxiv."):
                choices.reverse()
            for endpoint, source in choices:
                value = fetch_json(client, endpoint + "/" + quote(doi, safe="/"))
                if value is None:
                    continue
                record = crossref_record(value["message"]) if source == "Crossref" else datacite_record(value["data"]["attributes"])
                related = not normalized or matching_titles(title, record["title"])
                if record["title"] and record["doi"].casefold() == doi.casefold() and (explicit_doi or related):
                    return record, source
            if explicit_doi:
                return None, ""
        if len(normalized) < 16 and len(re.findall(r"[\u3400-\u9fff]", normalized)) < 8:
            return None, ""
        value = fetch_json(client, CROSSREF, params={"query.title": title, "rows": 6,
                          "select": "DOI,title,author,container-title,published,issued,publisher,abstract,event,type"})
        matches = {}
        for item in value["message"]["items"] if value else []:
            record = crossref_record(item)
            if not record["doi"] or identity(record["title"]) != normalized:
                continue
            if year and record["year"] and record["year"] != year:
                continue
            if authors and record["authors"] and not same_first_author(authors, record["authors"]):
                continue
            matches[record["doi"].casefold()] = record
        if len(matches) == 1:
            return next(iter(matches.values())), "Crossref"
    return None, ""


def lookup_job(store, job):
    paper_id = job["paper_id"]
    columns_only = job["payload"].get("fill_publication_fields") is True
    paper = store.paper(paper_id)
    if not paper or paper["deleted_at"] is not None:
        if columns_only:
            raise ValueError("请恢复文献后再补全出版信息")
        return {"status": "local"}
    restricted = paper["publication_status"] in PUBLICATION_SKIP_MESSAGES
    if restricted and (columns_only or not job["payload"].get("requested", False)):
        reason = PUBLICATION_SKIP_MESSAGES[paper["publication_status"]]
        with store.connect() as db:
            db.execute("UPDATE papers SET metadata_status='local' WHERE id=? AND metadata_status='queued'", (paper_id,))
        return {"status": "local", "filled_fields": [], "missing_fields": [], "skipped_reason": reason}
    if columns_only and not missing_publication_fields(paper):
        return {"status": "local", "filled_fields": [], "missing_fields": []}
    settings = store.settings()
    enabled = settings["metadata_lookup"] or job["payload"].get("requested", False)
    manual = set(json.loads(paper["manual_fields"]))
    with pymupdf.open(store.original(paper_id)) as doc:
        clues = pdf_clues(doc)
    doi = doi_name(paper["doi"]) if "doi" in manual or paper["metadata_source"] in REMOTE_SOURCES else clues.candidate_doi or doi_name(paper["doi"])
    explicit = "doi" in manual and bool(doi)
    title = paper["title"] if "title" in manual or paper["metadata_source"] in REMOTE_SOURCES else clues.fields.get("title", "")
    record, source, status = None, "", "local"
    cover_mismatch = False
    store.progress(job["id"], 0, 1, "正在查找出版信息" if columns_only
                   else "正在查找公开文献信息" if enabled else "正在读取 PDF 信息")
    if enabled and (doi or title):
        try:
            authors = paper["authors"] if "authors" in manual else clues.fields.get("authors") or credible_authors(paper["authors"])
            record, source = lookup_online(title, doi, authors, paper["year"] or clues.fields.get("year", ""), explicit_doi=explicit)
            status = "matched" if record else "review"
            if record and clues.cover_title and not matching_titles(clues.cover_title, record["title"]):
                record, source, status, cover_mismatch = None, "", "review", True
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
            status = "offline"
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        current = store.paper(paper_id, db)
        state = db.execute("SELECT state FROM jobs WHERE id=?", (job["id"],)).fetchone()
        if not state or state[0] != "running":
            raise InterruptedError("任务已暂停或取消")
        if not current or current["deleted_at"] is not None:
            if columns_only:
                raise ValueError("请恢复文献后再补全出版信息")
            return {"status": "local"}
        if current["publication_status"] != paper["publication_status"]:
            db.execute("UPDATE papers SET metadata_status='local' WHERE id=? AND metadata_status='queued'", (paper_id,))
            return {"status": "local", "filled_fields": [], "missing_fields": [],
                    "skipped_reason": "发表状态已变化，已忽略本次书目查询结果"}
        if (current["title"], current["doi"]) != (paper["title"], paper["doi"]):
            record, status = None, "review"
        if columns_only:
            missing = missing_publication_fields(current)
            updates = {key: value for key in missing if (value := (record or {}).get(key) or clues.fields.get(key))}
            if updates:
                write_bibliography(store, db, paper_id, updates)
            return {"status": status, "filled_fields": list(updates), "missing_fields": [k for k in missing if k not in updates],
                    "cover_mismatch": cover_mismatch}
        updates = merge_bibliography(current, record or clues.fields, online=bool(record),
                                     auto_rename=settings["auto_rename"], allow_name=bool(record) or status == "local")
        updates["metadata_status"] = status
        updates["metadata_source"] = source if record else current["metadata_source"] or ("PDF" if clues.fields else "")
        write_bibliography(store, db, paper_id, updates)
    return {"status": status, "source": updates["metadata_source"], "cover_mismatch": cover_mismatch}
