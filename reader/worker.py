import sys
import traceback

from .ai import alignment_job, reading_job, translation_job
from .backup import create_backup
from .metadata import PUBLICATION_COLUMNS, lookup_job, schedule_lookup
from .ocr import ocr_job
from .pdf import parse_pdf, rebuild_pdf
from .store import Store, dumps, now


def run(store, job_id):
    job = store.job(job_id)
    rebuild = job["kind"] == "parse" and job["payload"].get("rebuild") is True
    fill_publication = job["kind"] == "metadata" and job["payload"].get("fill_publication_fields") is True
    try:
        if rebuild:
            rebuild_pdf(store, job_id, job["paper_id"])
            return
        if job["kind"] == "parse":
            result = parse_pdf(store, job_id, job["paper_id"])
            if store.settings()["metadata_lookup"] and store.job(job_id)["state"] == "running":
                schedule_lookup(store, job["paper_id"])
        elif job["kind"] == "metadata":
            result = lookup_job(store, job)
        elif job["kind"] == "ocr":
            result = ocr_job(store, job)
        elif job["kind"] == "translate":
            result = translation_job(store, job)
        elif job["kind"] == "reading":
            result = reading_job(store, job)
        elif job["kind"] == "align":
            result = alignment_job(store, job)
        elif job["kind"] == "backup":
            output = create_backup(store, lambda i, t, m: store.progress(job_id, i, t, m))
            result = {"filename": output.name}
        else:
            raise ValueError("未知任务类型")
        state, message = "done", "已完成"
        if job["kind"] == "metadata" and result.get("skipped_reason"):
            message = result["skipped_reason"]
        elif fill_publication:
            filled = "、".join(PUBLICATION_COLUMNS[key] for key in result["filled_fields"])
            missing = "、".join(PUBLICATION_COLUMNS[key] for key in result["missing_fields"])
            message = f"已补全{filled}" if filled else "没有待自动补全的出版信息"
            if missing:
                reason = {
                    "review": "未确认书目匹配", "offline": "暂未连上书目服务",
                }.get(result["status"], "书目来源未提供全部字段")
                if result.get("cover_mismatch"):
                    reason = "DOI 返回的标题与 PDF 首页不符"
                message = "，".join(part for part in (f"已补全{filled}" if filled else "", reason, f"待补{missing}") if part)
                if result["status"] == "offline":
                    state = "partial" if filled else "failed"
        elif job["kind"] == "metadata":
            message = {"review": "未确认匹配，保留已有信息", "offline": "暂未连上书目服务，保留已有信息"}.get(result["status"], message)
            if result.get("cover_mismatch"):
                message = "DOI 返回的标题与 PDF 首页不符，保留已有书目信息"
        elif job["kind"] == "ocr":
            recognized, empty, failed = result["recognized_pages"], result["empty_pages"], len(result["failed_pages"])
            message = f"识别到文字 {recognized} 页"
            if empty:
                message += f"，{empty} 页未识别到文字"
            if failed:
                state = "partial" if recognized or empty else "failed"
                message += f"，{failed} 页处理失败，可重试"
                if state == "failed" and result["error"]:
                    message = result["error"]
        elif job["kind"] == "translate" and result.get("failed_blocks"):
            state = "partial" if result["processed"] else "failed"
            message = f"还有 {len(result['failed_blocks'])} 段没有译完，可补译剩余部分"
        elif job["kind"] == "translate" and result.get("preserved_cross_page"):
            message = "已完成，保留了含人工修改的跨页译文"
        with store.connect() as db:
            db.execute("UPDATE jobs SET state=?,progress=CASE WHEN ?='done' THEN total ELSE progress END,message=?,result=?,updated=? WHERE id=? AND state='running'",
                       (state, state, message, dumps(result), now(), job_id))
    except InterruptedError:
        pass
    except Exception as exc:
        traceback.print_exc()
        message = str(exc) if isinstance(exc, ValueError) else (
            "重新转换失败，已保留原阅读版，可重试" if rebuild else "处理失败，已保留完成的部分，可以重试")
        with store.connect() as db:
            db.execute("UPDATE jobs SET state='failed',message=?,updated=? WHERE id=? AND state='running'", (message, now(), job_id))
            if job["kind"] == "parse" and not rebuild:
                db.execute("UPDATE papers SET parse_state='failed',parse_error=? WHERE id=?", (message, job["paper_id"]))
            elif job["kind"] == "metadata" and not fill_publication:
                db.execute("UPDATE papers SET metadata_status='offline' WHERE id=?", (job["paper_id"],))


if __name__ == "__main__":
    run(Store(), sys.argv[1])
