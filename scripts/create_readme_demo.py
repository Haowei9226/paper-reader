"""Create a completely synthetic, offline library for documentation screenshots.

This is optional developer tooling. It never reads a personal library or API key.
Run from a configured source checkout. The default output must not already exist.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pymupdf

ROOT = Path(__file__).resolve().parent.parent
PARAGRAPHS = [
    ("Read with the source in view", "对照原文，保留证据"),
    ("A reading workspace can keep a document, a translation and a note close together. "
     "The reader can return to the original passage when a term needs another look. "
     "This synthetic document illustrates the workflow; it reports no research findings.",
     "阅读工作区把文档、译文和笔记放在一起。遇到需要再次核对的术语时，可以随时回到原始段落。"
     "本文是用于展示操作流程的合成文档，不报告任何研究结果。"),
    ("Keep meaning connected", "让原文与译文保持联系"),
    ("Parallel reading places the source and translation side by side. "
     "Annotations remain linked to a passage, so a short observation can retain its context. "
     "A translation is a reading aid and should always be checked against the source.",
     "双栏阅读让原文与译文并排显示。标注与相应段落关联，因此简短的想法也能保留上下文。"
     "译文是一种阅读辅助，仍需与原文核对。"),
    ("From a passage to a note", "从一段原文到一条笔记"),
    ("A useful note can separate an excerpt from a reader's own interpretation. "
     "Source locations make it easier to revisit an idea, compare wording and revise a conclusion. "
     "Exporting a copy keeps these observations available beyond this workspace.",
     "有用的笔记能够区分原文摘录与读者自己的理解。来源位置便于重访观点、比较措辞和修订结论。"
     "导出副本后，这些思考也能在工作区之外继续使用。"),
    ("Local by default", "默认保存在本机"),
    ("The sample library contains only generated documents and manually written example translations. "
     "No published papers, personal notes or API credentials are used in these screenshots. "
     "A real library starts empty and is built from the documents its user chooses to import.",
     "示例文献库只包含生成的文档和人工编写的演示译文。截图不使用任何已发表论文、私人笔记或 API 密钥。"
     "实际使用时文献库从空库开始，由使用者导入自己的文献。"),
]


def make_pdf(title: str, subtitle: str) -> bytes:
    document = pymupdf.open()
    page = document.new_page(width=595, height=842)
    page.draw_rect((0, 0, 595, 12), color=None, fill=(.19, .29, .49))
    page.insert_text((48, 60), title, fontsize=23, color=(.12, .20, .35))
    page.insert_text((48, 83), "SYNTHETIC DEMO  |  NOT A PUBLISHED STUDY", fontsize=9, color=(.52, .35, .14))
    page.insert_text((48, 105), subtitle, fontsize=10, color=(.4, .45, .52))
    y = 152
    for index, (source, _) in enumerate(PARAGRAPHS[:6]):
        if index % 2 == 0:
            page.insert_text((48, y), f"{index // 2 + 1}  {source}", fontsize=14, color=(.18, .29, .45))
            y += 20
        else:
            page.insert_textbox((48, y, 547, y + 88), source, fontsize=11.5, lineheight=1.45)
            y += 135
    page.insert_text((48, 792), "Paper Reader / synthetic documentation sample", fontsize=8, color=(.5, .55, .62))
    page.insert_text((535, 792), "1", fontsize=9, color=(.5, .55, .62))
    page2 = document.new_page(width=595, height=842)
    page2.insert_text((48, 67), "4  " + PARAGRAPHS[6][0], fontsize=17, color=(.18, .29, .45))
    page2.insert_textbox((48, 95, 547, 210), PARAGRAPHS[7][0], fontsize=12, lineheight=1.5)
    page2.insert_text((48, 260), "A simple reading workflow", fontsize=16, color=(.18, .29, .45))
    for x, label in ((48, "Import PDF"), (222, "Read & annotate"), (396, "Export notes")):
        page2.draw_rect((x, 294, x + 150, 360), color=(.5, .65, .78), fill=(.92, .96, .98))
        page2.insert_text((x + 15, 332), label, fontsize=12, color=(.2, .33, .45))
    page2.insert_text((48, 405), "Figure 1. An illustrative workflow, not an empirical result.", fontsize=10)
    document.set_metadata({"title": title + " [DEMO]", "author": "Demo Research Group", "subject": "Synthetic documentation sample"})
    document.set_toc([[1, "Read with the source in view", 1], [1, "Local by default", 2]])
    value = document.tobytes()
    document.close()
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "output" / "readme-demo")
    parser.add_argument("--port", type=int, default=18867)
    parser.add_argument("--start", action="store_true", help="Start this synthetic library on loopback with no browser")
    args = parser.parse_args()
    demo = args.output.resolve()
    if not demo.is_relative_to((ROOT / "output").resolve()):
        raise SystemExit("Documentation samples must stay under this checkout's output directory.")
    demo.mkdir(parents=True, exist_ok=False)
    os.environ["READER_DATA_DIR"] = str(demo / "data")
    os.environ["READER_SHARED_BUILD"] = "1"
    sys.path.insert(0, str(ROOT))
    from reader.store import Store, dumps, now, uid
    from reader.worker import run
    from reader.pdf import length, rects_for_range

    store = Store(demo / "data")
    store.set_settings({"metadata_lookup": False, "auto_rename": False, "provider": "local", "font_size": 17, "layout": "parallel"})
    collection = uid()
    with store.connect() as db:
        db.execute("INSERT INTO collections VALUES(?,?,?,?)", (collection, "DEMO · 合成演示", None, now()))
    papers = []
    for index, title in enumerate(("Reading across languages", "Source-linked notes", "A local-first research library", "Revisiting an idea")):
        pid = uid()
        content = make_pdf(title, "Manual examples for a local reading workflow / 2026")
        store.original(pid).write_bytes(content)
        with store.connect() as db:
            db.execute("INSERT INTO papers(id,sha256,filename,original_filename,title,authors,year,source,status,starred,metadata_status,created,updated,locator) VALUES(?,?,?,?,?,?,?,?,?,?,'local',?,?,?)",
                       (pid, hashlib.sha256(content).hexdigest(), title + " [DEMO].pdf", title + " [DEMO].pdf", title + " [DEMO]", "Demo Research Group", "2026", "Synthetic examples · 合成示例", ("reading", "read", "unread", "unread")[index], int(index == 0), now(), now(), dumps({"mode": "html", "page": 1})))
            db.execute("INSERT INTO paper_collections VALUES(?,?)", (pid, collection))
            for tag in ("合成演示", ("双语阅读", "来源笔记", "本地文献库", "回顾与检索")[index]):
                db.execute("INSERT INTO tags VALUES(?,?)", (pid, tag))
        job = store.create_job("parse", pid)
        with store.connect() as db:
            db.execute("UPDATE jobs SET state='running' WHERE id=?", (job["id"],))
        run(store, job["id"])
        if store.job(job["id"])["state"] != "done":
            raise RuntimeError("Synthetic PDF parsing failed")
        with store.connect() as db:
            db.execute("UPDATE papers SET title=?,authors=?,year=?,source=?,locator=? WHERE id=?", (title + " [DEMO]", "Demo Research Group", "2026", "Synthetic examples · 合成示例", dumps({"mode": "html", "page": 1}), pid))
            blocks = db.execute("SELECT id,text FROM blocks WHERE paper_id=? AND kind!='image' ORDER BY page,position", (pid,)).fetchall()
            for block in blocks:
                source = " ".join(block["text"].split())
                target = ""
                for english, chinese in PARAGRAPHS:
                    if english[:30] in source:
                        target = chinese
                        break
                if "SYNTHETIC DEMO" in source:
                    target = "合成演示 · 非已发表论文 · 译文为人工编写的示例"
                elif source == title:
                    target = {"Reading across languages": "跨语言阅读", "Source-linked notes": "关联原文的笔记", "A local-first research library": "本地优先的文献库", "Revisiting an idea": "重新审视一个观点"}[title]
                if target:
                    tid = uid()
                    segments = [{"source_start": 0, "source_end": length(block["text"]), "target_start": 0, "target_end": length(target)}]
                    db.execute("INSERT INTO translations VALUES(?,?,?,?,?,?,?,?,?,?)", (tid, block["id"], pid, target, dumps(segments), "local", "manual-demo", 1, 1, now()))
                    store.reindex_block(db, block["id"])
            store.reindex_paper(db, pid)
        papers.append(pid)
    with store.connect() as db:
        pid = papers[0]
        block = db.execute("SELECT * FROM blocks WHERE paper_id=? AND text LIKE '%Annotations remain linked%'", (pid,)).fetchone()
        quote = "Annotations remain linked"
        start = block["text"].index(quote)
        end = start + len(quote)
        target = {"block_id": block["id"], "start": start, "end": end, "quote": quote,
                  "prefix": block["text"][max(0, start - 32):start], "page": block["page"],
                  "rects": rects_for_range(block, start, end)}
        translation = db.execute("SELECT id,text FROM translations WHERE block_id=? AND active=1", (block["id"],)).fetchone()
        counterpart = {"block_id": block["id"], "translation_id": translation["id"], "start": 0,
                       "end": length(translation["text"]), "quote": translation["text"]}
        db.execute("INSERT INTO annotations VALUES(?,?,?,?,?,?,?,?,?,?)", (
            uid(), pid, dumps([target]), dumps([counterpart]), quote,
            "演示笔记：保留原文出处，让阅读中的想法可以回到证据。此文献与译文均为合成示例。",
            "yellow", "review", now(), now()))
        db.execute("UPDATE papers SET updated=? WHERE id=?", (now(), pid))
        store.reindex_paper(db, pid)
    manifest = {"synthetic_only": True, "manual_example_translations": True, "personal_data_used": False, "api_calls": 0, "paper_ids": papers, "url": f"http://127.0.0.1:{args.port}"}
    if args.start:
        # Serve a clean source snapshot, so project-local .env files are never
        # consulted by the demo server's normal credential-status endpoint.
        program = demo / "program"
        shutil.copytree(ROOT / "reader", program / "reader", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copytree(ROOT / "web" / "dist", program / "web" / "dist")
        environment = {key: value for key, value in os.environ.items() if key.upper() not in {"OPENAI_API_KEY", "DEEPSEEK_API_KEY"}}
        log = (demo / "server.log").open("ab")
        try:
            process = subprocess.Popen([sys.executable, "-B", "-m", "reader", "--no-browser", "--port", str(args.port)], cwd=program, env=environment, stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        finally:
            log.close()
        manifest["server_pid"] = process.pid
    (demo / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False))


if __name__ == "__main__":
    main()
