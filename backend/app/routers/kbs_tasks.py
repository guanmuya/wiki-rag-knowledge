"""任务路由：任务列表、详情、Wiki 停止、重试。

从 `routers/kbs.py` 按职责拆出（P1-4）。本模块不反向 import `kbs.py`；
`kbs.py` 聚合本模块的 router 并重导出处理函数，保持 `kbs.retry_task`
等名字可被测试直接调用。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from .. import models, serializers
from ..db import get_db
from ..deps import audit, get_current_user, grant_level
from ..services import taskflow

router = APIRouter(tags=["knowledge-base"])


# ------------------------------------------------------------------ 任务
@router.get("/tasks")
def list_tasks(status: str | None = None, kb_id: int | None = None, limit: int = 50,
               include_running: bool = False,
               db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    stmt = select(models.Task)
    wanted = [s.strip() for s in status.split(",") if s.strip()] if status else []
    if wanted:
        stmt = stmt.where(models.Task.status.in_(wanted))
    if kb_id:
        stmt = stmt.where(models.Task.kb_id == kb_id)
    if user.role != models.Role.admin.value:
        allowed = select(models.KbGrant.kb_id).where(models.KbGrant.user_id == user.id)
        stmt = stmt.where(models.Task.kb_id.in_(allowed))
    tasks = list(db.execute(stmt.order_by(models.Task.id.desc()).limit(min(limit, 200))).scalars())
    # 正在运行的旧任务不能被大量较新的排队任务挤出停止入口。
    if include_running and (not wanted or models.TaskStatus.running.value in wanted):
        running = db.execute(stmt.where(
            models.Task.status == models.TaskStatus.running.value
        )).scalars().all()
        tasks = sorted({t.id: t for t in [*tasks, *running]}.values(), key=lambda t: -t.id)
    return {"items": [serializers.task_out(db, t) for t in tasks], "total": len(tasks)}


@router.get("/tasks/{task_id}")
def get_task(task_id: int, db: Session = Depends(get_db),
             user: models.User = Depends(get_current_user)):
    task = db.get(models.Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.kb_id and grant_level(db, user, task.kb_id) is None:
        raise HTTPException(status_code=403, detail="没有该任务的访问权限")
    return serializers.task_out(db, task)


@router.post("/tasks/{task_id}/stop-wiki")
def stop_task_wiki(task_id: int, run_token: str = Query(..., max_length=32),
                   db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    """停止单个任务的 Wiki 阶段；当前阻塞调用结束后停止。"""
    task = db.get(models.Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not task.kb_id or grant_level(db, user, task.kb_id) != models.GrantLevel.manage.value:
        raise HTTPException(status_code=403, detail="需要该知识库的管理权限")
    if (task.run_token or "") != run_token:
        raise HTTPException(status_code=409, detail="任务已重新执行，请刷新后重试")
    if task.status == models.TaskStatus.stopped.value:
        return {"ok": True}

    queued = db.execute(
        update(models.Task).where(
            models.Task.id == task_id,
            models.Task.run_token == run_token,
            models.Task.kind == models.TaskKind.rebuild_wiki.value,
            models.Task.status == models.TaskStatus.queued.value,
        ).values(status=models.TaskStatus.stopped.value, stage="已停止",
                 wiki_stop_requested=True, wiki_stop_requested_at=models.utcnow(),
                 wiki_active=False, finished_at=models.utcnow())
    )
    if queued.rowcount:
        db.refresh(task)
    else:
        running = db.execute(
            update(models.Task).where(
                models.Task.id == task_id,
                models.Task.run_token == run_token,
                models.Task.status == models.TaskStatus.running.value,
                models.Task.wiki_active.is_(True),
                models.Task.wiki_stop_requested.is_(False),
            ).values(wiki_stop_requested=True, wiki_stop_requested_at=models.utcnow())
        )
        if not running.rowcount:
            db.refresh(task)
            if (task.run_token or "") != run_token:
                raise HTTPException(status_code=409, detail="任务已重新执行，请刷新后重试")
            if task.status == models.TaskStatus.stopped.value or (
                task.status == models.TaskStatus.running.value and task.wiki_stop_requested
            ):
                return {"ok": True}
            raise HTTPException(status_code=409, detail="该任务当前没有可停止的 Wiki 生成")
        # 领取或页面发布可能发生在最初读取之后；用持写锁后的最新任务状态。
        db.refresh(task)
    # 重试会把 attempts 归零；累计历史用于识别此前已运行过的队列任务。
    try:
        previous_attempts = int(taskflow.retry_history(task).get("previous_attempts") or 0)
    except (TypeError, ValueError, OverflowError):
        previous_attempts = 1  # 历史数据损坏时保守回退。
    previously_run = task.attempts > 0 or previous_attempts > 0
    # 从未开始的普通重建没有改变 Wiki；恢复队列可能已有部分页面发布，
    # 删除后的重建也可能需要回退，直到新的完整生成完成。
    if not queued.rowcount or previously_run or task.doc_id:
        stmt = update(models.KnowledgeBase).where(models.KnowledgeBase.id == task.kb_id)
        # 一个成功构建必须在该任务最近一次 Wiki 启动和页面发布之后开始，
        # 才能确认其结果不再受本次停止影响。旧库缺少时间时保守回退。
        if queued.rowcount and not previously_run:
            boundary = task.created_at  # 删除任务在排队时已改变来源。
        else:
            times = [t for t in (task.wiki_started_at, task.wiki_last_changed_at) if t is not None]
            boundary = max(times) if times else None
        if boundary is not None:
            stmt = stmt.where(or_(models.KnowledgeBase.wiki_ready_started_at.is_(None),
                                  models.KnowledgeBase.wiki_ready_started_at <= boundary))
        db.execute(stmt.values(
            wiki_incomplete=True, wiki_stop_epoch=models.KnowledgeBase.wiki_stop_epoch + 1,
        ).execution_options(synchronize_session=False))
    if queued.rowcount:
        taskflow.sync_wiki_stopped(db, task)
    audit(db, user, "stop_wiki", target=str(task_id))
    db.commit()
    return {"ok": True}


@router.post("/tasks/{task_id}/retry")
def retry_task(task_id: int, db: Session = Depends(get_db),
               user: models.User = Depends(get_current_user)):
    """把一条已结束的任务原地打回队列（不新建任务行）。

    旧实现每次都 `INSERT` 一条新任务：用户点三次重试，任务列表就多三条，
    同一份文档还会被并发解析三遍。现在重试 = 同一条记录的状态回退，
    「重试了几次」记在 payload.retry.count 上，同时把文档/版本/知识库
    状态同步回「索引中」——任务列表与文档列表不再各说各话。
    """
    task = db.get(models.Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.kb_id and grant_level(db, user, task.kb_id) != models.GrantLevel.manage.value:
        raise HTTPException(status_code=403, detail="需要该知识库的管理权限")
    if task.status in (models.TaskStatus.queued.value, models.TaskStatus.running.value):
        raise HTTPException(status_code=409, detail="任务已在队列中或正在执行，无需重试")
    if task.status == models.TaskStatus.stopped.value and task.kind != models.TaskKind.rebuild_wiki.value:
        raise HTTPException(status_code=409, detail="文档或分块索引已完成，请在知识库设置中重建 Wiki")
    if (task.dedupe_key or "").startswith(models.WIKI_INITIAL_TASK_PREFIX):
        # 先持写锁，再检查同库的首次生成任务；与切换模式时的排队互斥。
        claimed = db.execute(update(models.Task).where(
            models.Task.id == task.id,
            models.Task.status.notin_([models.TaskStatus.queued.value, models.TaskStatus.running.value]),
        ).values(status=models.TaskStatus.queued.value))
        if not claimed.rowcount:
            db.rollback()
            raise HTTPException(status_code=409, detail="任务已在队列中或正在执行，无需重试")
        other = db.execute(select(models.Task.id).where(
            models.Task.kb_id == task.kb_id, models.Task.id != task.id,
            models.Task.dedupe_key == task.dedupe_key,
            models.Task.status.in_([models.TaskStatus.queued.value, models.TaskStatus.running.value]),
        ).limit(1)).first()
        if other:
            db.rollback()
            raise HTTPException(status_code=409, detail="该库已有首次生成 Wiki 任务，请等待完成")
    hist = taskflow.requeue(db, task)
    taskflow.sync_running(db, task)
    audit(db, user, "retry_task", target=str(task.id),
          detail=f"kind={task.kind} retry={hist.get('count', 0)}")
    db.commit()
    return {"task_id": task.id, "reused": True, "retry_count": hist.get("count", 0)}
