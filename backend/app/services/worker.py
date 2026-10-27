"""进程内后台 Worker。单机首版用数据库任务表 + 线程池，不引入外部队列。

- 任务抢占用条件更新，避免同一任务被两个线程重复执行。
- 任务失败不丢失：状态、阶段、错误写回，可重试。
"""
from __future__ import annotations

import json
import logging
import threading
import time
import traceback

from sqlalchemy import select, update

from .. import config, models
from ..db import session_scope
from . import cost, ingest, llm, wiki

log = logging.getLogger("wrk.worker")

_stop = threading.Event()
_threads: list[threading.Thread] = []
_started = False


def _claim(db, worker_id: str) -> models.Task | None:
    task = db.execute(
        select(models.Task).where(models.Task.status == models.TaskStatus.queued.value)
        .order_by(models.Task.id)
    ).scalars().first()
    if not task:
        return None
    res = db.execute(
        update(models.Task)
        .where(models.Task.id == task.id, models.Task.status == models.TaskStatus.queued.value)
        .values(status=models.TaskStatus.running.value, stage="处理中", progress=5,
                started_at=models.utcnow(), attempts=models.Task.attempts + 1)
    )
    db.commit()
    if res.rowcount != 1:
        return None
    return db.get(models.Task, task.id)


def run_once(worker_id: str = "inline") -> bool:
    """执行一个任务；返回是否真的处理了任务（供测试同步驱动）。"""
    with session_scope() as db:
        task = _claim(db, worker_id)
        if not task:
            return False
        stats = llm.CallStats()
        try:
            if task.kind == models.TaskKind.ingest_document.value:
                result = ingest.ingest_version(db, task, stats=stats)
            elif task.kind == models.TaskKind.rebuild_wiki.value:
                kb = db.get(models.KnowledgeBase, task.kb_id)
                if not kb:
                    raise RuntimeError("知识库不存在")
                wres = wiki.build_wiki(db, kb, stats=stats)
                result = {"created": wres.pages_created, "updated": wres.pages_updated,
                          "skipped": wres.pages_skipped, "sections": wres.sections,
                          "notes": wres.notes or []}
                # build_wiki 的阶段统计就是任务本身，不需要再拆
                result["usage_by_phase"] = {task.kind: stats.as_dict()}
            elif task.kind == models.TaskKind.reindex_kb.value:
                result = ingest.reindex_kb(db, task, stats=stats)
            elif task.kind == models.TaskKind.embedding_rebuild.value:
                result = ingest.reindex_kb(db, task, stats=stats)
            else:
                raise RuntimeError(f"未知任务类型：{task.kind}")

            result["usage"] = stats.as_dict()
            # 成本流水：把本任务的用量按阶段投影进 cost_records。
            # Task.result["usage"] 保持原样不动（前端与历史接口仍读它），
            # 这里只是让历史成本能被 SQL 聚合，而不是每次查询都反序列化 JSON。
            cost.record_phases(
                db, kb_id=task.kb_id, task_kind=task.kind, task_id=task.id,
                doc_id=task.doc_id,
                phases=result.get("usage_by_phase"),
                total=stats,
            )
            task.status = models.TaskStatus.success.value
            task.stage = "完成"
            task.progress = 100
            task.result = json.dumps(result, ensure_ascii=False)
            task.finished_at = models.utcnow()
            if task.doc_version_id:
                ver = db.get(models.DocumentVersion, task.doc_version_id)
                if ver:
                    ver.status = models.TaskStatus.success.value
                    db.add(ver)
            db.add(task)
            db.commit()
        except Exception as exc:
            db.rollback()
            err = f"{exc.__class__.__name__}: {exc}"
            log.warning("任务 %s 失败：%s", task.id, err)
            task = db.get(models.Task, task.id)
            if task:
                task.status = models.TaskStatus.failed.value
                task.stage = "失败"
                task.error = err[:2000]
                task.finished_at = models.utcnow()
                # 失败前已经发生的模型调用是真金白银的开销，必须留痕。
                # 否则「失败重试」这条路径的成本永远是 0，实验数据会失真。
                if stats.calls or stats.embed_texts:
                    cost.record(db, kb_id=task.kb_id, kind="failed_attempt", stats=stats,
                                source="task", source_id=task.id, doc_id=task.doc_id)
                    task.result = json.dumps({"usage": stats.as_dict()}, ensure_ascii=False)
                db.add(task)
            if task and task.doc_version_id:
                ver = db.get(models.DocumentVersion, task.doc_version_id)
                if ver:
                    ver.status = models.TaskStatus.failed.value
                    ver.status_detail = err[:500]
                    db.add(ver)
            if task and task.doc_id:
                doc = db.get(models.Document, task.doc_id)
                if doc:
                    doc.status = models.KbStatus.failed.value
                    doc.status_detail = err[:500]
                    db.add(doc)
            if task and task.kb_id:
                kb = db.get(models.KnowledgeBase, task.kb_id)
                if kb:
                    # 状态必须与事实一致：旧版本分块是保留的（见 ingest.py
                    # "新版本成功入库后才下线旧版本分块"），检索仍能命中。
                    # 若无条件置 failed，用户会以为整个库不可用而停止提问。
                    from sqlalchemy import func, select as _select
                    remaining = db.execute(
                        _select(func.count(models.Chunk.id)).where(
                            models.Chunk.kb_id == kb.id)
                    ).scalar() or 0
                    if remaining:
                        kb.status = models.KbStatus.partial.value
                        kb.status_detail = (
                            f"部分文档处理失败，仍有 {remaining} 个索引单元可用；"
                            f"最近错误：{err[:200]}"
                        )
                    else:
                        kb.status = models.KbStatus.failed.value
                        kb.status_detail = err[:500]
                    db.add(kb)
            db.commit()
            log.debug("traceback: %s", traceback.format_exc())
    return True


def _loop(worker_id: str) -> None:
    log.info("worker %s 启动", worker_id)
    while not _stop.is_set():
        try:
            worked = run_once(worker_id)
        except Exception:  # pragma: no cover
            log.exception("worker 循环异常")
            worked = False
        if not worked:
            _stop.wait(config.WORKER_POLL_SECONDS)


def start() -> None:
    global _started
    if _started or not config.WORKER_ENABLED:
        return
    _started = True
    for i in range(max(1, config.WORKER_CONCURRENCY)):
        t = threading.Thread(target=_loop, args=(f"w{i+1}",), daemon=True, name=f"wrk-worker-{i+1}")
        t.start()
        _threads.append(t)


def stop() -> None:
    _stop.set()
    for t in _threads:
        t.join(timeout=3)
    _threads.clear()
