"""Plan source-anchor repairs while retaining every translated and authored value.

The caller owns staging, its write transaction, and the final block UPSERT. A
failed range makes its source page unsafe; callers can preserve that page and
plan again. No target-side offset, translated quote, or model answer is rewritten.
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field


@dataclass(frozen=True)
class MigrationIssue:
    block_id: str
    page: int | None
    table: str
    row_id: str
    field: str
    reason: str


@dataclass(frozen=True)
class AnchorUpdate:
    table: str
    row_id: str
    before: dict
    values: dict


@dataclass
class AnchorMigrationPlan:
    paper_id: str
    updates: list[AnchorUpdate] = field(default_factory=list)
    issues: list[MigrationIssue] = field(default_factory=list)
    unsafe_blocks: set[str] = field(default_factory=set)
    unsafe_pages: set[int] = field(default_factory=set)
    mapped_blocks: set[str] = field(default_factory=set)

    @property
    def safe(self):
        return not self.issues


def _blocks(values):
    return {block["id"]: dict(block) for block in (values.values() if isinstance(values, dict) else values)}


def _units(text):
    return len(text.encode("utf-16-le")) // 2


def _slice(text, start, end):
    return text.encode("utf-16-le")[start * 2:end * 2].decode("utf-16-le")


def _prefix(text, start):
    return text.encode("utf-16-le")[max(0, start - 30) * 2:start * 2].decode("utf-16-le", errors="ignore")


class _Planner:
    def __init__(self, db, paper_id, new_blocks, old_blocks, preserved_pages):
        self.db = db
        self.plan = AnchorMigrationPlan(paper_id)
        self.old = _blocks(old_blocks if old_blocks is not None else db.execute(
            "SELECT * FROM blocks WHERE paper_id=?", (paper_id,)))
        self.new = _blocks(new_blocks)
        kept = set(preserved_pages)
        self.new = {bid: block for bid, block in self.new.items() if block["page"] not in kept}
        self.new.update((bid, block) for bid, block in self.old.items() if block["page"] in kept)
        self.changed = {bid for bid, old in self.old.items() if bid not in self.new or any(
            old.get(key) != self.new[bid].get(key) for key in ("page", "text", "bbox", "geometry"))}
        self.maps = {}

    def issue(self, bid, table, row_id, field, reason):
        if bid not in self.changed:
            return
        page = self.old[bid]["page"]
        self.plan.unsafe_blocks.add(bid)
        self.plan.unsafe_pages.add(page)
        if bid in self.new and self.new[bid]["page"] != page:
            self.plan.unsafe_pages.add(self.new[bid]["page"])
        self.plan.issues.append(MigrationIssue(bid, page, table, row_id, field, reason))

    def present(self, bid, table, row_id, field):
        if bid not in self.changed:
            return True
        old, new = self.old[bid], self.new.get(bid)
        if not new or new["page"] != old["page"]:
            self.issue(bid, table, row_id, field, "Source block is absent or moved to another page")
            return False
        return True

    def trusted(self, bid, table, row_id, field):
        if bid not in self.changed:
            return True
        if not self.present(bid, table, row_id, field):
            return False
        old, new = self.old[bid], self.new[bid]
        old_box = json.loads(old["bbox"]) if isinstance(old["bbox"], str) else old["bbox"]
        new_box = json.loads(new["bbox"]) if isinstance(new["bbox"], str) else new["bbox"]
        if old.get("kind") == "image" and (new.get("kind") != "image" or old_box != new_box):
            self.issue(bid, table, row_id, field, "Referenced image bounds changed without text anchors")
            return False
        from .source_mapping import BlockMap

        if bid not in self.maps:
            self.maps[bid] = BlockMap(self.old[bid], self.new[bid])
        if not self.maps[bid].safe:
            self.issue(bid, table, row_id, field, self.maps[bid].reason or "Unproven source block change")
            return False
        return True

    def decode(self, raw, expected, table, row_id, field, owner=None):
        try:
            value = json.loads(raw)
            if not isinstance(value, expected):
                raise ValueError("Unexpected JSON shape")
            return value
        except (TypeError, ValueError):
            # An opaque malformed object cannot prove which changed source it
            # referred to. Preserve those candidate pages rather than guessing.
            for bid in ([owner] if owner else self.changed):
                self.issue(bid, table, row_id, field, "Invalid source-reference JSON")
            return None

    def update(self, table, row, values):
        values = {key: value for key, value in values.items() if value != row[key]}
        if values:
            self.plan.updates.append(AnchorUpdate(table, row["id"], {key: row[key] for key in values}, values))

    def map_range(self, bid, start, end, quote, table, row_id, field):
        if bid not in self.changed:
            return None
        if not self.trusted(bid, table, row_id, field):
            return None
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= _units(self.old[bid]["text"]):
            self.issue(bid, table, row_id, field, "Source range is not a valid UTF-16 interval")
            return None
        mapping = self.maps[bid]
        result = mapping.map_range(start, end, quote=quote)
        if result is None:
            self.issue(bid, table, row_id, field, mapping.reason or "Source range has no unambiguous mapping")
        else:
            self.plan.mapped_blocks.add(bid)
        return result

    def anchor(self, value, table, row_id, field, *, owner=None):
        """Only this known anchor shape is traversed; arbitrary JSON is opaque."""
        if not isinstance(value, dict):
            for bid in ([owner] if owner else self.changed):
                self.issue(bid, table, row_id, field, "Invalid source anchor")
            return value
        bid = value.get("block_id", owner)
        if not isinstance(bid, str) or bid not in self.changed:
            return value
        if value.get("translation_id") or value.get("side") == "translation":
            # These offsets address immutable translation text, even though its
            # block ID is the same as a source block being improved.
            self.trusted(bid, table, row_id, field)
            return value
        if not self.trusted(bid, table, row_id, field):
            return value
        old = self.old[bid]["text"]
        start, end, quote = value.get("start"), value.get("end"), value.get("quote")
        explicit = "start" in value or "end" in value
        if not explicit:
            if not isinstance(quote, str) or not quote:
                return value  # A block-only reference has no character range.
            if old == self.new[bid]["text"] and "rects" not in value:
                return value
            first = old.find(quote)
            if first < 0 or old.find(quote, first + 1) >= 0:
                self.issue(bid, table, row_id, field, "Quote-only source anchor is missing or repeated")
                return value
            start, end = _units(old[:first]), _units(old[:first + len(quote)])
        if type(start) is not int or type(end) is not int:
            self.issue(bid, table, row_id, field, "Source range is not an integer UTF-16 interval")
            return value
        result = self.map_range(bid, start, end, quote, table, row_id, field)
        if result is None:
            return value
        mapped = dict(value)
        if explicit:
            mapped.update(start=result["start"], end=result["end"])
        if "quote" in value:
            mapped["quote"] = result["quote"]
        if "prefix" in value and value["prefix"] == _prefix(old, start):
            mapped["prefix"] = _prefix(self.new[bid]["text"], result["start"])
        if "page" in value:
            mapped["page"] = self.new[bid]["page"]
        if "rects" in value:
            mapped["rects"] = result["rects"]
        return mapped

    def translations(self):
        for row in self.db.execute("SELECT * FROM translations WHERE paper_id=?", (self.plan.paper_id,)):
            row = dict(row)
            owner = row["block_id"]
            self.trusted(owner, "translations", row["id"], "block_id")
            spans = self.decode(row["segments"], list, "translations", row["id"], "segments", owner)
            if spans is None:
                continue
            migrated = []
            for index, span in enumerate(spans):
                if not isinstance(span, dict):
                    self.issue(owner, "translations", row["id"], "segments", "Invalid translation source segment")
                    migrated.append(span)
                    continue
                bid = span.get("source_block_id", owner)
                result = None
                if not isinstance(bid, str):
                    self.issue(owner, "translations", row["id"], f"segments[{index}]", "Invalid source block ID")
                elif bid in self.changed:
                    result = self.map_range(bid, span.get("source_start"), span.get("source_end"), None,
                                            "translations", row["id"], f"segments[{index}]")
                migrated.append({**span, "source_start": result["start"], "source_end": result["end"]} if result else span)
                if isinstance(peer := span.get("target_block_id"), str):
                    self.trusted(peer, "translations", row["id"], f"segments[{index}].target_block_id")
            self.update("translations", row, {"segments": _json(migrated) if migrated != spans else row["segments"]})

    def annotations(self):
        for row in self.db.execute("SELECT * FROM annotations WHERE paper_id=?", (self.plan.paper_id,)):
            row = dict(row)
            targets = self.decode(row["targets"], list, "annotations", row["id"], "targets")
            if targets is None:
                continue
            mapped = [self.anchor(value, "annotations", row["id"], f"targets[{i}]") for i, value in enumerate(targets)]
            values = {"targets": _json(mapped) if mapped != targets else row["targets"]}
            # Older editions may have a translated or custom display quote.
            # Only an exact aggregate of old source targets is derived data.
            if (all(isinstance(t, dict) and isinstance(t.get("quote"), str) for t in targets)
                    and row.get("selected_language") != "translation"
                    and row["quote"] == "\n".join(t["quote"] for t in targets)):
                values["quote"] = "\n".join(t["quote"] for t in mapped)
            self.update("annotations", row, values)
            translated = self.decode(row["translated_targets"], list, "annotations", row["id"], "translated_targets")
            for value in translated or ():
                if isinstance(value, dict) and isinstance(value.get("block_id"), str):
                    self.trusted(value["block_id"], "annotations", row["id"], "translated_targets.block_id")

    def citations(self, values, table, row_id, field):
        if not isinstance(values, list):
            for bid in self.changed:
                self.issue(bid, table, row_id, field, "Invalid source citation list")
            return values
        return [self.anchor(value, table, row_id, f"{field}[{i}]") for i, value in enumerate(values)]

    def ai_results(self):
        for row in self.db.execute("SELECT * FROM ai_results WHERE paper_id=?", (self.plan.paper_id,)):
            row = dict(row)
            values = self.decode(row["citations"], list, "ai_results", row["id"], "citations")
            if values is None:
                continue
            mapped = self.citations(values, "ai_results", row["id"], "citations")
            self.update("ai_results", row, {"citations": _json(mapped) if mapped != values else row["citations"]})

    def jobs(self):
        for row in self.db.execute("SELECT * FROM jobs WHERE paper_id=? AND kind IN ('reading','translate','align')", (self.plan.paper_id,)):
            row = dict(row)
            values = {}
            for column in ("payload", "result"):
                source = self.decode(row[column], dict, "jobs", row["id"], column)
                if source is None:
                    continue
                mapped = copy.deepcopy(source)
                # Fixed, versioned paths only. In particular question, answer,
                # terms and model output text can contain arbitrary JSON/quotes.
                for key in ("targets", "source_targets", "citations"):
                    if key in mapped:
                        mapped[key] = self.citations(mapped[key], "jobs", row["id"], f"{column}.{key}")
                if column == "payload" and isinstance(mapped.get("block_ids"), list):
                    for bid in mapped["block_ids"]:
                        if isinstance(bid, str):
                            self.trusted(bid, "jobs", row["id"], "payload.block_ids")
                if column == "result":
                    completed = mapped.get("completed", [])
                    failed = mapped.get("failed_blocks", [])
                    parts = mapped.get("parts", [])
                    for key, items in (("completed", completed), ("failed_blocks", failed), ("parts", parts)):
                        if not isinstance(items, list):
                            for bid in self.changed:
                                self.issue(bid, "jobs", row["id"], f"result.{key}", "Invalid source-reference list")
                    for bid in completed if isinstance(completed, list) else ():
                        if isinstance(bid, str):
                            self.trusted(bid, "jobs", row["id"], "result.completed")
                    for value in failed if isinstance(failed, list) else ():
                        if isinstance(value, dict) and isinstance(value.get("block_id"), str):
                            self.trusted(value["block_id"], "jobs", row["id"], "result.failed_blocks")
                    for index, part in enumerate(parts if isinstance(parts, list) else ()):
                        if isinstance(part, dict) and "citations" in part:
                            part["citations"] = self.citations(part["citations"], "jobs", row["id"], f"result.parts[{index}].citations")
                values[column] = _json(mapped) if mapped != source else row[column]
            self.update("jobs", row, values)

    def locator(self):
        row = self.db.execute("SELECT locator FROM papers WHERE id=?", (self.plan.paper_id,)).fetchone()
        if row:
            locator = self.decode(row[0], dict, "papers", self.plan.paper_id, "locator")
            if locator and isinstance(locator.get("block_id"), str):
                # offset is a rendered-height fraction, not a UTF-16 offset.
                # With the same PDF/page/block it retains its existing meaning.
                self.trusted(locator["block_id"], "papers", self.plan.paper_id, "locator.block_id")


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def plan_anchor_migration(db, paper_id, new_blocks, *, old_blocks=None, preserved_pages=()):
    """Read only. Candidate blocks must cover the paper's staged replacement.

    Retry with ``preserved_pages=plan.unsafe_pages`` (plus already kept pages)
    until safe. Replanning uses the original blocks/anchors; never chain an
    approximate migration through an intermediate extraction.
    """
    planner = _Planner(db, paper_id, new_blocks, old_blocks, preserved_pages)
    if not planner.changed:
        return planner.plan
    planner.translations()
    planner.annotations()
    planner.ai_results()
    planner.jobs()
    planner.locator()
    return planner.plan


def apply_anchor_migration(db, plan):
    """Apply inside the caller's transaction; never commit or alter target text."""
    if not plan.safe:
        raise ValueError("Unsafe source anchors must be preserved and planned again")
    if not db.in_transaction:
        raise ValueError("Source anchors must be published inside the block replacement transaction")
    allowed = {"translations": {"segments"}, "annotations": {"targets", "quote"},
               "ai_results": {"citations"}, "jobs": {"payload", "result"}}
    for update in plan.updates:
        if update.table not in allowed or not update.values.keys() <= allowed[update.table]:
            raise ValueError("Unexpected source-anchor update")
        keys = list(update.values)
        where = " AND ".join(f"{key} IS ?" for key in keys)
        exists = db.execute(f"SELECT 1 FROM {update.table} WHERE id=? AND paper_id=? AND {where}",
                            [update.row_id, plan.paper_id, *(update.before[key] for key in keys)]).fetchone()
        if not exists:
            raise ValueError("Source anchors changed while rebuilding; replan before publication")
    for update in plan.updates:
        keys = list(update.values)
        where = " AND ".join(f"{key} IS ?" for key in keys)
        cursor = db.execute(f"UPDATE {update.table} SET {','.join(key + '=?' for key in keys)} WHERE id=? AND paper_id=? AND {where}",
                            [*(update.values[key] for key in keys), update.row_id, plan.paper_id,
                             *(update.before[key] for key in keys)])
        if cursor.rowcount != 1:
            raise ValueError("Source anchors changed while rebuilding; replan before publication")
