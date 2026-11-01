"""任务状态流转：重试排队、失败回写、文档/版本/知识库状态同步。

这里有两条硬约束，都是踩过的真实缺陷：

1. **重试必须复用同一条任务记录**。旧实现每次点「重试」都 `INSERT` 一条新任务，
   用户点三次任务列表里就多三条，同一份文档还会被并发解析三遍；
   而「重试了几次」这件事本来就该是同一行上的一个计数，不是一堆重复行。
2. **任务状态变化必须同步到文档/版本/知识库**。任务列表显示「失败」、
   上面的文档却还停在「索引中」，是用户看到过的最典型的自相矛盾。
   任务表只是执行记录，文档状态才是用户判断「这份文档能不能用」的依据。

所以：任何改任务状态的地方（手动重试、中断恢复、执行失败）都走这里的函数，
不要各自 `task.status = ...` 之后只提交一半。
"""
from __future__ import annotations

import json
import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models

log = logging.getLogger("wrk.taskflow")


# ------------------------------------------------------------------ payload
def task_payload(task: models.Task) -> dict:
    """任务 payload 一律当 dict 用；老数据可能是空串或数组，容错。"""
    try:
        data = json.loads(task.payload or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def retry_history(task: models.Task) -> dict:
    hist = task_payload(task).get("retry")
    return hist if isinstance(hist, dict) else {}


def _write_payload(task: models.Task, data: dict) -> None:
    task.payload = json.dumps(data, ensure_ascii=False)


# ------------------------------------------------------------------ 重试
def requeue(db: Session, task: models.Task, *, reason: str = "已重新排队") -> dict:
    """把一条已结束的任务原地打回队列，返回更新后的重试历史。

    - 不新建任务行：任务列表里永远是「这份文档的一条记录」，重试次数记在
      `payload.retry.count` 上，`attempts` 归零后重新累计。
    - `attempts` 归零是给「中断自动恢复」重新发一份预算（见 worker
      MAX_RESUME_ATTEMPTS）；累计执行次数保留在 `payload.retry.previous_attempts`。
    - 上一次的错误不丢：挪进 `payload.retry.last_error`，前端作为「上次失败原因」
      展示，任务行的 error 清空才能正常显示「排队中」。
    """
    payload = task_payload(task)
    hist = payload.get("retry")
    hist = hist if isinstance(hist, dict) else {}

    hist["count"] = int(hist.get("count") or 0) + 1
    hist["previous_attempts"] = int(hist.get("previous_attempts") or 0) + int(task.attempts or 0)
    if task.error:
        hist["last_error"] = task.error[:500]
    hist["at"] = models.utcnow().isoformat(timespec="seconds")
    hist["reason"] = reason
    payload["retry"] = hist
    _write_payload(task, payload)

    task.status = models.TaskStatus.queued.value
    task.stage = "排队中"
    task.progress = 0
    task.error = ""
    task.result = "{}"
    task.attempts = 0
    task.started_at = None
    task.finished_at = None
    db.add(task)
    return hist


# ------------------------------------------------------------------ 状态同步
def sync_running(db: Session, task: models.Task, *, detail: str = "已重新排队，等待处理") -> None:
    """任务重新排队 / 开始执行：版本、文档、知识库一起回到「索引中」。"""
    if task.doc_version_id:
        ver = db.get(models.DocumentVersion, task.doc_version_id)
        if ver:
            ver.status = models.TaskStatus.queued.value
            ver.status_detail = detail
            db.add(ver)
    if task.doc_id:
        doc = db.get(models.Document, task.doc_id)
        if doc and not doc.deleted:
            doc.status = models.KbStatus.indexing.value
            doc.status_detail = detail
            db.add(doc)
    if task.kb_id:
        kb = db.get(models.KnowledgeBase, task.kb_id)
        if kb:
            kb.status = models.KbStatus.indexing.value
            kb.status_detail = "正在处理文档"
            db.add(kb)


def sync_failure(db: Session, task: models.Task, err: str) -> None:
    """任务失败：版本/文档标 failed，知识库按「还有没有可用索引」决定 partial/failed。

    知识库不能无条件标 failed：旧版本分块是保留的（新版本成功入库后才下线），
    检索仍能命中；标 failed 会让用户以为整个库不可用而停止提问。
    """
    detail = (err or "未知错误")[:500]
    if task.doc_version_id:
        ver = db.get(models.DocumentVersion, task.doc_version_id)
        if ver:
            ver.status = models.TaskStatus.failed.value
            ver.status_detail = detail
            db.add(ver)
    if task.doc_id:
        doc = db.get(models.Document, task.doc_id)
        if doc and not doc.deleted:
            doc.status = models.KbStatus.failed.value
            doc.status_detail = detail
            db.add(doc)
    if task.kb_id:
        kb = db.get(models.KnowledgeBase, task.kb_id)
        if kb:
            remaining = db.execute(
                select(func.count(models.Chunk.id)).where(models.Chunk.kb_id == kb.id)
            ).scalar() or 0
            if remaining:
                kb.status = models.KbStatus.partial.value
                kb.status_detail = (
                    f"部分文档处理失败，仍有 {remaining} 个索引单元可用；"
                    f"最近错误：{detail[:200]}"
                )
            else:
                kb.status = models.KbStatus.failed.value
                kb.status_detail = detail
            db.add(kb)


# ------------------------------------------------------------------ 查询
def latest_version(db: Session, doc_id: int) -> models.DocumentVersion | None:
    return db.execute(
        select(models.DocumentVersion)
        .where(models.DocumentVersion.doc_id == doc_id)
        .order_by(models.DocumentVersion.version_no.desc())
    ).scalars().first()


def ingest_tasks_for_version(db: Session, version_id: int) -> list[models.Task]:
    return list(db.execute(
        select(models.Task)
        .where(
            models.Task.doc_version_id == version_id,
            models.Task.kind == models.TaskKind.ingest_document.value,
        )
        .order_by(models.Task.id.desc())
    ).scalars().all())


def active_task(tasks: list[models.Task]) -> models.Task | None:
    """排队中 / 执行中的那条（同一版本最多允许一条）。"""
    for t in tasks:
        if t.status in (models.TaskStatus.queued.value, models.TaskStatus.running.value):
            return t
    return None


def collapse_queued_duplicates(db: Session, tasks: list[models.Task], keep: models.Task) -> int:
    """把同一版本多余的「排队中」任务合并进 keep，返回合并条数。

    旧版重试接口每点一次就插一条 queued 任务，一份文档可能排着三份一样的活。
    这些任务还没执行，标成 failed 并写明原因即可，不会丢任何已发生的工作。
    """
    merged = 0
    for t in tasks:
        if t.id == keep.id or t.status != models.TaskStatus.queued.value:
            continue
        t.status = models.TaskStatus.failed.value
        t.stage = "失败"
        t.error = f"重复的排队任务，已合并到 #{keep.id}"
        t.finished_at = models.utcnow()
        db.add(t)
        merged += 1
    if merged:
        log.info("合并同一版本的重复排队任务 %d 条 → #%s", merged, keep.id)
    return merged
