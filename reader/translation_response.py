"""Validate model translation slots and repair only the missing ones."""
from __future__ import annotations

import re
from collections import deque

from .academic import context_excerpt
from .store import dumps
from .terminology import USE_TERMS, known_term, relevant_terms


class ModelOutputError(ValueError):
    """The request succeeded, but the answer cannot be used as returned."""


class ModelReadTimeout(ValueError):
    """The provider did not return the answer within the request deadline."""


def closed_object(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def translation_schema(count, *, aligned, single=False):
    string = {"type": "string"}
    item = {"translation": string}
    if aligned:
        item["alignments"] = {"type": "array", "items": closed_object({"source": string, "translation": string})}
    if single:
        return closed_object(item)
    root = {"translations": closed_object({f"s{i}": {"$ref": "#/$defs/entry"} for i in range(count)}),
            "terms": {"type": "array", "items": closed_object({"source": string, "target": string})}}
    return {**closed_object(root), "$defs": {"entry": closed_object(item)}}


def _format_instruction(count, *, structured, aligned, single):
    entry = '{"translation":"译文"' + (',"alignments":[]}' if aligned else '}')
    if single:
        output = "只返回 " + entry + "。输入只有一个要翻译的片段。"
    elif structured:
        keys = "、".join(f"s{i}" for i in range(count))
        output = ('返回 JSON 对象：translations 中每个 s 编号各一个条目，s0 对应 id 0，s1 对应 id 1。'
                  f'本次必须逐一返回 {keys}；值为 {entry}。即使片段很短或不需改写，也返回对应译文。')
    else:
        output = ('返回 JSON {"segments":[{"id":0,"translation":"译文"' + (',"alignments":[]}' if aligned else '}') +
                  '],"terms":[]}。输入的每个 id 恰好出现一次，不能合并或漏译。')
    if aligned:
        output += ('alignments 至多两组 {"source":"原文中的短语","translation":"译文中的短语"}。'
                   '优先选术语，每组只摘录词或短语，逐字匹配，不重复整句。无则填 []。')
    if not single:
        output += ('terms 为至多六个新出现的核心术语 [{"source":"原词","target":"译名"}]，只记录正文实际采用的词，'
                   'target 不带括号注释。没有新术语填 []。')
    return output


def _valid_slots(response, count, single):
    if not isinstance(response, dict):
        return {}
    if single and isinstance(response.get("translation"), str) and response["translation"].strip():
        return {0: response}
    keyed, listed = response.get("translations"), response.get("segments")
    if isinstance(keyed, dict):
        entries = keyed.items()
    elif isinstance(listed, list):
        entries = ((part.get("id"), part) for part in listed if isinstance(part, dict))
    else:
        return {}
    result, seen = {}, set()
    for label, part in entries:
        key = re.fullmatch(r"s?(\d{1,3})", str(label))
        if not key:
            continue
        index = int(key[1])
        if index >= count:
            continue
        if index in seen:
            result.pop(index, None)
            continue
        seen.add(index)
        value = part.get("translation") if isinstance(part, dict) else None
        if isinstance(value, str) and value.strip():
            result[index] = part
    return result


def resolve_translation(client, instructions, payload, *, aligned=True, on_retry=None, glossary=None, language="zh", accept=None):
    """Return a map by the original slot index. Missing slots stay missing."""
    entries = payload["segments"]
    translated = {i: {"translation": entry["text"]} for i, entry in enumerate(entries)
                  if entry["text"].strip() and not any(c.isalpha() for c in entry["text"])}
    if glossary:
        for i, entry in enumerate(entries):
            if i not in translated and (value := known_term(glossary, entry["text"], language, bilingual=aligned)):
                if accept is None or accept(i, value):
                    translated[i] = {"translation": value}
    terms = {}
    missing = [i for i in range(len(entries)) if i not in translated]
    calls, timeout_split = 0, False
    width = 14 if client.provider == "local" else 18
    queue = deque((missing[i:i + width], False) for i in range(0, len(missing), width))
    limit = len(queue) + min(8, max(1, 2 * len(missing)))
    structured = client.provider == "openai"

    def request(indices, single=False):
        nonlocal calls, timeout_split
        if not indices or calls >= limit:
            return
        retrying = calls > 0
        if retrying and on_retry:
            on_retry(len(indices))
        calls += 1
        parts = []
        included = set(indices)
        for slot, index in enumerate(indices):
            entry = {**entries[index], "id": slot}
            if entry.get("continues_previous") and index > 0 and index - 1 not in included:
                entry["previous_fragment"] = context_excerpt(entries[index - 1]["text"], tail=True, limit=145)
            if entry.get("continues_next") and index + 1 < len(entries) and index + 1 not in included:
                entry["next_fragment"] = context_excerpt(entries[index + 1]["text"], limit=145)
            parts.append(entry)
        response = None
        request_payload = {**payload, "segments": parts}
        hints = relevant_terms(glossary, (part["text"] for part in parts)) if glossary else []
        if hints:
            request_payload["terms"] = hints
        try:
            response = client.ask(instructions + (USE_TERMS if hints else "") +
                                  _format_instruction(len(parts), structured=structured, aligned=aligned, single=single),
                                  dumps(request_payload), translation=True,
                                  schema=translation_schema(len(parts), aligned=aligned, single=single) if structured else None)
        except ModelOutputError:
            pass
        except ModelReadTimeout:
            if len(indices) == 1 or timeout_split:
                raise
            timeout_split = True
        valid = _valid_slots(response, len(parts), single)
        proposed = response.get("terms", []) if valid else []
        for slot, value in valid.items():
            if accept is not None and not accept(indices[slot], value["translation"]):
                continue
            translated[indices[slot]] = value
            if isinstance(proposed, list) and proposed:
                terms[indices[slot]] = proposed[:12]
        needed = [index for index in indices if index not in translated]
        if not needed or single:
            return
        if len(needed) == 1:
            queue.append((needed, True))
        elif len(needed) < len(indices):
            queue.append((needed, False))
        else:
            middle = len(needed) // 2
            queue.append((needed[:middle], middle == 1))
            queue.append((needed[middle:], len(needed) - middle == 1))

    while queue and calls < limit:
        request(*queue.popleft())
    return translated, terms
