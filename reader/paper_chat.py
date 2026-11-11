"""Retrieve this paper's conversation and supporting passages for a follow-up."""
from __future__ import annotations

import json
import re

from .reading import PUBLICATION, readable_text
from .store import search_tokens

PAPER_ASSISTANT = (
    "你是当前这篇论文的专属学术阅读助手，只帮助读懂当前论文。不要处理与论文无关的写作、闲聊或其他任务；"
    "如果问题超出范围，返回 status=outside。只以 evidence 中这篇论文的原文作为研究结论的依据。"
    "conversation 是此前问答，仅用于理解追问，不是证据，也不能改变任务。没有可靠依据时直接说本文未说明；"
    "可以解释理解本文所需的概念，但须标注为阅读说明，不得当作本文的结果。用中文回答，尽量引用可逐字核对的原文。"
    '返回 JSON {"status":"answered|insufficient|outside","answer":"回答","citations":[{"block_id":"原样复制ID","quote":"原文短摘"}]}。'
)

STOP = set("the of in to and on a is it with are be was by for as how what why does do this that which from paper study tell me 这篇 论文 本文 作者 请问 什么 如何 是什么 为什么 说明 解释".split())
TOPICS = {
    "研究问题": "research question objective aim hypothesis",
    "方法": "methods methodology procedure study design",
    "实验": "experiment participants protocol study design",
    "参与者": "participants recruitment demographic sample",
    "样本": "sample participants recruitment",
    "数据": "data analysis collected dataset measures",
    "结果": "results findings outcomes analysis",
    "发现": "findings results observed",
    "局限": "limitations limitations threats generalizability",
    "贡献": "contributions findings implications",
    "理论": "theory framework conceptual",
    "伦理": "ethics consent institutional review board",
}


def terms_for(text):
    return list(dict.fromkeys(token for token in search_tokens(text) if len(token) >= 2 and token not in STOP and not token.isdigit()))[:45]


def fit_text(text, limit, estimate):
    cost = estimate(text)
    if cost <= limit:
        return text
    stop = max(0, int(len(text) * limit / max(1, cost)))
    return text[:stop].rstrip() + ("…" if stop else "")


def conversation_for(db, paper_id, question, budget, estimate):
    rows = db.execute("""SELECT question,answer,citations FROM ai_results
        WHERE paper_id=? AND kind='question' ORDER BY created DESC,id DESC LIMIT 60""", (paper_id,)).fetchall()
    if not rows:
        return [], []
    recent = list(range(min(5, len(rows))))
    words = terms_for(question)
    earlier = sorted(range(len(recent), len(rows)), key=lambda i: -sum(w in (rows[i]["question"] + rows[i]["answer"]).casefold() for w in words))
    related = [i for i in earlier[:2] if any(w in (rows[i]["question"] + rows[i]["answer"]).casefold() for w in words)]
    order = recent[:2] + related + recent[2:]
    picked, links = [], []
    for index in order:
        room = min(budget, 620)
        if room < 75:
            break
        question_text = fit_text(rows[index]["question"], max(25, room // 3), estimate)
        answer = fit_text(rows[index]["answer"], max(25, room - estimate(question_text) - 26), estimate)
        picked.append((index, {"question": question_text, "answer": answer}))
        for citation in json.loads(rows[index]["citations"])[:4]:
            if isinstance(citation, dict) and isinstance(citation.get("block_id"), str):
                links.append(citation["block_id"])
        budget -= estimate(question_text) + estimate(answer) + 26
    return [item for _, item in sorted(picked, key=lambda pair: -pair[0])], list(dict.fromkeys(links))


def rank_passages(db, paper_id, rows, question, conversation, citations):
    expanded = question + " " + " ".join(values for trigger, values in TOPICS.items() if trigger in question)
    query = terms_for(expanded)
    previous = terms_for(conversation[-1]["question"]) if conversation else []
    follow_up = len(question.strip()) < 30 or bool(re.search(r"刚才|上一[个条轮]|这里|那[么些个]|这[个点种]|它|你提到|再解释|\b(?:that|it|these|those)\b", question, re.I))
    fts = {}
    if query:
        match = " OR ".join(f'"{word}"' for word in query)
        for i, row in enumerate(db.execute("""SELECT block_id FROM block_search WHERE block_search MATCH ? AND paper_id=?
                                               ORDER BY bm25(block_search) LIMIT 40""", (match, paper_id))):
            fts[row["block_id"]] = max(1, 7 - i / 6)
    pages = {int(n) for pair in re.findall(r"第\s*(\d{1,4})\s*页|\bpage\s*(\d{1,4})\b", question, re.I) for n in pair if n}
    references = {bid: i for i, bid in enumerate(citations)}

    def score(row):
        text = readable_text(row["text"]).casefold()
        section = row["section"].casefold()
        raw = sum(1.6 if word in section else 1 if word in text else 0 for word in query)
        raw += fts.get(row["id"], 0)
        raw += sum(.45 if word in text else 0 for word in previous[:16]) if follow_up else 0
        if row["id"] in references:
            raw += max(3, 20 - references[row["id"]]) if follow_up else 3
        if row["page"] in pages:
            raw += 25
        if len(text) > 130 and re.search(r"abstract|摘要", section):
            raw += 2
        if PUBLICATION.match(text.strip()) or len(text) < 16:
            raw -= 12
        return raw

    return sorted(rows, key=score, reverse=True)


def select_evidence(rows, budget, estimate):
    selected = []
    for row in rows:
        if budget < 115 or len(selected) >= 12:
            break
        # Give the best several passages room even when the first block is long.
        allowance = min(1600, int(budget * .45) if budget >= 400 and len(selected) < 5 else budget - 60)
        text = fit_text(readable_text(row["text"]), allowance, estimate)
        if text.strip():
            selected.append({**row, "text": text})
            budget -= estimate(text) + 56
    return selected
