"""进程内后台 Worker。单机首版用数据库任务表 + 线程池，不引入外部队列。

- 任务抢占用 `BEGIN IMMEDIATE` 条件更新，避免同一任务被两个进程重复执行。
- 任务失败不丢失：状态、阶段、错误写回，可重试。
- 写锁（`database is locked`）是环境问题，不是业务失败：进度写撞锁只丢一次
  进度（见 db.WorkerSession），业务写撞锁则重新排队而不是判死。
"""
from __future__ import annotations

import json
import logging
import threading
import traceback
import uuid
from typing import Literal

from sqlalchemy import and_, exists, or_, select, text, update
from sqlalchemy.exc import OperationalError, TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.orm import aliased

from .. import config, models
from ..db import (apply_busy_timeout, is_lock_error, retry_on_locked,
                  run_with_lock_retry, session_scope)
from . import cost, ingest, llm, taskflow, wiki

log = logging.getLogger("wrk.worker")

_stop = threading.Event()
_threads: list[threading.Thread] = []
_started = False

# 业务写撞锁（等满 busy_timeout 仍拿不到）时自动重新排队的次数上限。
# 超过就按失败处理：说明不是「一时忙」，而是真有问题，再排队只会无限重跑。
LOCK_REQUEUE_MAX_ATTEMPTS = 3


def _claim(db, worker_id: str) -> models.Task | None:
    """抢一个排队任务；抢不到（没活/锁等不到）返回 None。

    这里包一层是为了「抢不到锁不要炸穿 worker 循环」：`_claim_locked` 内部
    有重试，重试完还是被锁挡着就当本轮没抢到，下一轮 poll 再来。
    """
    try:
        return _claim_locked(db, worker_id)
    except OperationalError as exc:
        if not is_lock_error(exc):
            raise
        log.warning("抢任务时写锁等待超时，本轮跳过：%s", getattr(exc, "orig", exc))
        db.rollback()
        return None


@retry_on_locked(attempts=2, base_delay=0.2)
def _claim_locked(db, worker_id: str) -> models.Task | None:
    """跨进程原子抢占一条 queued 任务。

    为什么必须 `BEGIN IMMEDIATE`：SQLite 默认是 DEFERRED 事务——先 SELECT 再
    UPDATE 时，两个 worker 可能同时读到同一条 queued 任务，然后先后拿到写锁
    各自 UPDATE；其中一个 rowcount 为 0 却已经白跑了一轮解析（重复向量化，
    真金白银的开销）。`BEGIN IMMEDIATE` 在事务一开始就拿写锁，读-改-写因此
    是原子的，抢不到就等（worker 档 busy_timeout=30s）。

    先做一次不加锁的 SELECT 是为了空队列不占锁：每秒 poll 一次都去抢写锁，
    会和交互请求无谓地碰撞。

    只重试 2 次（而不是 LOCK_RETRY_ATTEMPTS 次）：worker 引擎本身的 busy_timeout
    已经是 30s，再乘 5 次就是 150s 按在抢任务上；抢不到就返回 None，`_loop`
    一秒后还会再来，没必要在单次调用里耗光预算。
    """
    has_queued = db.execute(
        select(models.Task.id).where(models.Task.status == models.TaskStatus.queued.value).limit(1)
    ).first()
    if not has_queued:
        db.rollback()          # 结束这条只读事务，别把快照拖长
        return None

    db.execute(text("BEGIN IMMEDIATE"))
    # 首次 Wiki 生成等同库中先前任务结束；后续任务也等首次生成结束，
    # 避免多个 worker 同时写 Wiki。其他知识库及普通任务不受影响。
    other = aliased(models.Task)
    initial = models.Task.dedupe_key.like(f"{models.WIKI_INITIAL_TASK_PREFIX}%")
    other_initial = other.dedupe_key.like(f"{models.WIKI_INITIAL_TASK_PREFIX}%")
    blocked = exists(select(other.id).where(
        other.kb_id == models.Task.kb_id, other.id != models.Task.id,
        or_(initial, other_initial),
        or_(other.status == models.TaskStatus.running.value,
            and_(other.status == models.TaskStatus.queued.value, other.id < models.Task.id)),
    ))
    task = db.execute(
        select(models.Task).where(models.Task.status == models.TaskStatus.queued.value, ~blocked)
        .order_by(models.Task.id)
    ).scalars().first()
    if not task:
        # 另一个进程刚抢走了：立刻放锁，别占着库
        db.rollback()
        return None
    res = db.execute(
        update(models.Task)
        .where(models.Task.id == task.id, models.Task.status == models.TaskStatus.queued.value)
        .values(status=models.TaskStatus.running.value, stage="处理中", progress=5,
                started_at=models.utcnow(), attempts=models.Task.attempts + 1,
                run_token=uuid.uuid4().hex)
    )
    db.commit()
    if res.rowcount != 1:
        return None
    return db.get(models.Task, task.id)


def _refresh_kb_after_rebuild(db, task: models.Task, result: dict) -> None:
    """重建成功后重算知识库状态（不提前提交）。

    换模型 / 手动重建排队时把 kb.status 置为「索引中」，任务成功却只改任务行，
    用户会一直看到「任务成功 + 知识库索引中」。状态必须和 task.result 在同一次
    提交里落库，避免「库已就绪但任务还没标成功」的半截状态。
    """
    if not task.kb_id:
        return
    kb = db.get(models.KnowledgeBase, task.kb_id)
    if kb is None:
        return
    ingest._refresh_kb_status(db, kb, commit=False)
    if result.get("wiki_error"):
        # Wiki 重建失败不影响分块索引可用，但不能把状态说成完全就绪。
        kb.status = models.KbStatus.partial.value
        kb.status_detail = f"分块索引已重建，Wiki 生成失败：{result['wiki_error']}"
    db.add(kb)


def _retain_index_failures(db, kb: models.KnowledgeBase) -> None:
    """首次生成 Wiki 不代表失败的文档或全库索引重建已恢复。"""
    failed_doc = db.execute(select(models.Document).where(
        models.Document.kb_id == kb.id, models.Document.deleted.is_(False),
        models.Document.status == models.KbStatus.failed.value,
    ).order_by(models.Document.id.desc()).limit(1)).scalars().first()
    last_rebuild = db.execute(select(models.Task).where(
        models.Task.kb_id == kb.id,
        models.Task.kind.in_([models.TaskKind.reindex_kb.value, models.TaskKind.embedding_rebuild.value]),
        models.Task.status.in_([models.TaskStatus.success.value, models.TaskStatus.failed.value]),
    ).order_by(models.Task.finished_at.desc().nullslast(), models.Task.id.desc()).limit(1)).scalars().first()
    if failed_doc:
        kb.status = models.KbStatus.partial.value
        kb.status_detail = f"Wiki 已生成；文档 {failed_doc.filename} 处理失败：{failed_doc.status_detail[:200]}"
    elif last_rebuild and last_rebuild.status == models.TaskStatus.failed.value:
        kb.status = models.KbStatus.partial.value
        kb.status_detail = f"Wiki 已生成；分块索引重建失败：{(last_rebuild.error or '请重试')[:200]}"


def _report_wiki_progress(task_id: int, phase: Literal["topics", "pages"],
                          done: int, total: int, *, run_token: str | None = None) -> None:
    """独立任务的阶段计数单独提交，不提交 Wiki 会话中尚未落库的业务修改。"""
    if phase == "topics":
        stage = f"Wiki 主题归纳 {done}/{total}"
        progress = 5 + (35 * done // total if total else 0)
    else:
        stage = f"Wiki 页面处理 {done}/{total}"
        progress = 40 + (55 * done // total if total else 0)
    # 进度值为兼容已有任务契约的阶段权重，并非总耗时的精确百分比；
    # 已完成工作以 stage 中的计数为准，100 只在任务成功提交时写入。
    def write_progress() -> None:
        # 外层 Wiki 会话占用 worker 连接；进度使用独立池，池满也只短等一次。
        with session_scope(role="progress") as progress_db:
            apply_busy_timeout(progress_db, min(config.BUSY_TIMEOUT_INTERACTIVE,
                                                config.BUSY_TIMEOUT_PROGRESS))
            # 单条条件更新与恢复、重试及停止请求竞争；旧执行不能改写新执行的进度。
            statement = update(models.Task).where(
                models.Task.id == task_id,
                models.Task.status == models.TaskStatus.running.value,
                models.Task.wiki_stop_requested.is_(False),
            )
            if run_token is not None:
                statement = statement.where(models.Task.run_token == run_token,
                                            models.Task.wiki_active.is_(True))
            progress_db.execute(statement.values(stage=stage, progress=progress))

    try:
        # 和 WorkerSession 的进度提交一致，最多短暂重试一次写锁冲突。
        run_with_lock_retry(write_progress, attempts=min(config.LOCK_RETRY_ATTEMPTS, 2))
    except OperationalError as exc:
        if not is_lock_error(exc):
            raise
        log.warning("Wiki 进度写入被锁挡住，跳过本次更新：%s", getattr(exc, "orig", exc))
    except SQLAlchemyTimeoutError as exc:
        log.warning("Wiki 进度连接池等待超时，跳过本次更新：%s", exc)


def run_once(worker_id: str = "inline") -> bool:
    """执行一个任务；返回是否真的处理了任务（供测试同步驱动）。"""
    # role="worker"：busy_timeout 提到 30s，且进度写撞锁不会让任务失败
    with session_scope(role="worker") as db:
        task = _claim(db, worker_id)
        if not task:
            return False
        stats = llm.CallStats()
        run_token = task.run_token
        task_id, kb_id, doc_id = task.id, task.kb_id, task.doc_id
        try:
            if task.kind == models.TaskKind.ingest_document.value:
                result = ingest.ingest_version(db, task, stats=stats)
            elif task.kind == models.TaskKind.rebuild_wiki.value:
                kb = db.get(models.KnowledgeBase, task.kb_id)
                if not kb:
                    raise RuntimeError("知识库不存在")
                initial = (task.dedupe_key or "").startswith(models.WIKI_INITIAL_TASK_PREFIX)
                payload = taskflow.task_payload(task)
                has_pages = db.execute(select(models.WikiPage.id).where(
                    models.WikiPage.kb_id == kb.id).limit(1)).first() if initial else None
                has_chunks = db.execute(select(models.Chunk.id).where(
                    models.Chunk.kb_id == kb.id).limit(1)).first() if initial else True
                skip_initial = initial and (
                    kb.mode != models.KbMode.wiki.value or not has_chunks or
                    (has_pages and not payload.get("initial_wiki_started")
                     and not taskflow.retry_history(task).get("count") and not kb.wiki_incomplete)
                )
                if skip_initial:
                    result = {"notes": ["无需首次生成 Wiki"]}
                    # 保留前一任务的失败状态；仅清除没有其他活动任务的陈旧索引状态。
                    pending = db.execute(select(models.Task.id).where(
                        models.Task.kb_id == kb.id, models.Task.id != task.id,
                        models.Task.status.in_([models.TaskStatus.queued.value, models.TaskStatus.running.value]),
                    ).limit(1)).first()
                    if not pending and kb.status == models.KbStatus.indexing.value:
                        ingest._refresh_kb_status(db, kb, commit=False)
                else:
                    if initial and not payload.get("initial_wiki_started"):
                        # 已发布部分页面后重试必须继续；标记与开始阶段同事务提交，
                        # 且旧执行不能把标记写到恢复或重试后的新执行上。
                        payload["initial_wiki_started"] = True
                        marked = db.execute(update(models.Task).where(
                            models.Task.id == task_id,
                            models.Task.status == models.TaskStatus.running.value,
                            models.Task.run_token == run_token,
                        ).values(payload=json.dumps(payload, ensure_ascii=False)))
                        if marked.rowcount != 1:
                            raise taskflow.StaleRun()
                    wiki_epoch = taskflow.start_wiki(db, task, run_token)
                    try:
                        wres = wiki.build_wiki(
                            db, kb, stats=stats,
                            on_progress=lambda phase, done, total: _report_wiki_progress(
                                task_id, phase, done, total, run_token=run_token),
                            check_cancel=lambda: taskflow.check_wiki_stop(task_id, run_token, db),
                            on_publish=lambda: taskflow.record_wiki_publication(db, task_id, run_token),
                        )
                        taskflow.finish_wiki(db, task, run_token, wiki_epoch, complete=True)
                        result = {"created": wres.pages_created, "updated": wres.pages_updated,
                                  "skipped": wres.pages_skipped, "sections": wres.sections,
                                  "notes": wres.notes or []}
                        ingest._refresh_kb_status(db, kb, commit=False)
                        if initial:
                            _retain_index_failures(db, kb)
                            pending = db.execute(select(models.Task.id).where(
                                models.Task.kb_id == kb.id, models.Task.id != task.id,
                                models.Task.status.in_([models.TaskStatus.queued.value, models.TaskStatus.running.value]),
                            ).limit(1)).first()
                            if pending and kb.status == models.KbStatus.ready.value:
                                kb.status = models.KbStatus.indexing.value
                                kb.status_detail = "还有任务排队或处理中"
                    except taskflow.WikiStopped:
                        db.rollback()
                        result = {"wiki_stopped": True}
                result["usage_by_phase"] = {task.kind: stats.as_dict()}
            elif task.kind == models.TaskKind.reindex_kb.value:
                result = ingest.reindex_kb(db, task, stats=stats)
                if not result.get("wiki_stopped"):
                    _refresh_kb_after_rebuild(db, task, result)
            elif task.kind == models.TaskKind.embedding_rebuild.value:
                result = ingest.reindex_kb(db, task, stats=stats)
                if not result.get("wiki_stopped"):
                    _refresh_kb_after_rebuild(db, task, result)
            else:
                raise RuntimeError(f"未知任务类型：{task.kind}")

            result["usage"] = stats.as_dict()
            stopped = bool(result.get("wiki_stopped"))
            values = {
                "status": models.TaskStatus.stopped.value if stopped else models.TaskStatus.success.value,
                "stage": "已停止" if stopped else "完成",
                "wiki_active": False,
                "result": json.dumps(result, ensure_ascii=False),
                "finished_at": models.utcnow(),
            }
            if not stopped:
                values["progress"] = 100
            changed = db.execute(update(models.Task).where(
                models.Task.id == task_id, models.Task.status == models.TaskStatus.running.value,
                models.Task.run_token == run_token,
            ).values(**values))
            if changed.rowcount != 1:
                raise taskflow.StaleRun()
            cost.record_phases(
                db, kb_id=kb_id, task_kind=task.kind, task_id=task_id,
                doc_id=doc_id, phases=result.get("usage_by_phase"), total=stats,
            )
            if stopped:
                taskflow.sync_wiki_stopped(db, task)
            if task.kind != models.TaskKind.rebuild_wiki.value and task.kb_id:
                kb = db.get(models.KnowledgeBase, task.kb_id)
                if (kb and kb.status == models.KbStatus.ready.value and
                        ingest._initial_wiki_pending(db, task.kb_id)):
                    kb.status = models.KbStatus.indexing.value
                    kb.status_detail = "首次生成 Wiki 页面已排队"
            if task.doc_version_id:
                ver = db.get(models.DocumentVersion, task.doc_version_id)
                if ver:
                    ver.status = models.TaskStatus.success.value
                    db.add(ver)
            db.commit()
        except taskflow.StaleRun:
            db.rollback()
            # 旧执行已经产生的调用仍需留账，但不能覆盖重试后的任务状态。
            cost.record(db, kb_id=kb_id, kind="failed_attempt", stats=stats,
                        source="task", source_id=task_id, doc_id=doc_id)
            db.commit()
            return True
        except Exception as exc:
            db.rollback()
            err = f"{exc.__class__.__name__}: {exc}"
            log.warning("任务 %s 失败：%s", task_id, err)
            owned = db.execute(update(models.Task).where(
                models.Task.id == task_id, models.Task.status == models.TaskStatus.running.value,
                models.Task.run_token == run_token,
            ).values(run_token=run_token))
            if owned.rowcount != 1:
                db.rollback()
                cost.record(db, kb_id=kb_id, kind="failed_attempt", stats=stats,
                            source="task", source_id=task_id, doc_id=doc_id)
                db.commit()
                return True
            task = db.get(models.Task, task_id, populate_existing=True)
            if task and task.wiki_stop_requested and task.wiki_active:
                task.status = models.TaskStatus.stopped.value
                task.stage = "已停止"
                task.finished_at = models.utcnow()
                task.result = json.dumps({"wiki_stopped": True, "usage": stats.as_dict()}, ensure_ascii=False)
                # 异常路径可能同时包含入库和 Wiki 成本，不能把总量都归给 Wiki。
                cost.record(db, kb_id=task.kb_id, kind="failed_attempt",
                            stats=stats, source="task", source_id=task.id, doc_id=task.doc_id)
                taskflow.sync_wiki_stopped(db, task)
                db.add(task)
                db.commit()
                return True
            if task and is_lock_error(exc) and task.attempts <= LOCK_REQUEUE_MAX_ATTEMPTS:
                # 业务写等满 30s 还没拿到锁：这是「环境忙」，不是「任务坏」。
                # 判失败会让用户以为文档解析出问题、还白花一次重试预算；
                # 重新排队等下一轮，进度和文档状态都回到「等待处理」。
                task.status = models.TaskStatus.queued.value
                task.wiki_active = False
                task.run_token = uuid.uuid4().hex
                task.stage = "等待重试（数据库繁忙）"
                task.progress = 0
                task.started_at = None
                task.error = f"数据库写入繁忙，已自动重新排队：{err}"[:2000]
                db.add(task)
                taskflow.sync_running(db, task, detail="数据库繁忙，已重新排队")
                db.commit()
                return True
            if task:
                task.status = models.TaskStatus.failed.value
                task.wiki_active = False
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
            # 版本 / 文档 / 知识库的状态回写统一走 taskflow：
            # 任务失败而文档还停在「索引中」，是用户看到过的最典型的自相矛盾。
            if task:
                taskflow.sync_failure(db, task, err)
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


# 被中断几次后不再自动恢复：避免一个「必崩」的任务每次开机都重跑一遍
MAX_RESUME_ATTEMPTS = 3


def recover_interrupted(max_attempts: int = MAX_RESUME_ATTEMPTS) -> int:
    """启动时把上次进程被强杀留下的 `running` 任务重新排队，返回处理条数。

    为什么必须有：桌面版没有守护进程，用户关窗/任务管理器结束进程时，worker 是
    daemon 线程，不会走 finally 把任务标成 failed。任务会永远停在「处理中」，
    文档状态也永远不更新——用户看到的就是「上传完了，但一直不解析」。
    启动时统一回收：

    - `attempts < max_attempts`：改回 `queued`，worker 重新解析（入库是幂等的：
      先按文档版本删旧块再重建，重复执行不会产生重复分块）；
    - 超过上限：标 `failed` 并写明原因，避免每次开机都重跑一个必崩的任务。

    用 worker 档会话：启动瞬间常常正好赶上上一个进程还没退干净（桌面版强关后
    残留的 sidecar 就是这种状态），此时短等待会让回收本身失败，任务继续卡在
    「处理中」——等 30 秒拿到锁，比报错退出有用得多。
    """
    with session_scope(role="worker") as db:
        # 恢复与停止/完成/重试必须读取同一份受写锁保护的状态；否则旧快照
        # 可能在用户确认停止后把任务重新排队。SQLite 的锁需在 SELECT 前取得。
        if db.get_bind().dialect.name == "sqlite":
            db.rollback()
            db.execute(text("BEGIN IMMEDIATE"))
        tasks = db.execute(
            select(models.Task).where(models.Task.status == models.TaskStatus.running.value)
            .with_for_update()
        ).scalars().all()
        if not tasks:
            return 0
        for task in tasks:
            if task.wiki_stop_requested and task.wiki_active:
                task.status = models.TaskStatus.stopped.value
                task.stage = "已停止"
                task.finished_at = models.utcnow()
                task.result = json.dumps({"wiki_stopped": True}, ensure_ascii=False)
                taskflow.sync_wiki_stopped(db, task)
            elif task.attempts >= max_attempts:
                task.wiki_active = False
                task.status = models.TaskStatus.failed.value
                task.stage = "失败"
                task.error = f"任务被中断 {task.attempts} 次，已停止自动恢复（可在文档列表点重试）"
                task.finished_at = models.utcnow()
                # 放弃恢复也必须同步文档状态：否则任务显示「失败」、
                # 文档永远停在「索引中」，用户既看不出问题也没有重试入口。
                taskflow.sync_failure(db, task, task.error)
            else:
                task.wiki_active = False
                task.status = models.TaskStatus.queued.value
                task.run_token = uuid.uuid4().hex
                task.stage = "等待恢复"
                task.progress = 0
                task.started_at = None
                taskflow.sync_running(db, task, detail="上次处理被中断，已重新排队")
            db.add(task)
        db.commit()
        log.info("启动恢复：%d 个被中断的任务已重新排队", len(tasks))
        return len(tasks)


def reconcile_kb_statuses() -> int:
    """启动时修复「任务已结束、知识库仍显示索引中」的陈旧状态。

    旧版本在 embedding_rebuild / reindex_kb 成功后不刷新知识库，升级前卡住的库
    不会自己恢复。没有排队/执行中任务的 indexing 库按实际文档/分块重算；
    有活动任务的库跳过——那是真的在重建，不能提前标成就绪。
    """
    with session_scope(role="worker") as db:
        return _reconcile_kb_statuses(db)


def _reconcile_kb_statuses(db) -> int:
    """`reconcile_kb_statuses` 的实现；db 参数让测试能用独立会话验证。"""
    active_kb_ids = {
        row[0] for row in db.execute(
            select(models.Task.kb_id).where(
                models.Task.status.in_([models.TaskStatus.queued.value,
                                        models.TaskStatus.running.value]),
                models.Task.kb_id.is_not(None),
            )
        ).all()
    }
    stale = db.execute(
        select(models.KnowledgeBase).where(
            models.KnowledgeBase.status == models.KbStatus.indexing.value)
    ).scalars().all()
    fixed = 0
    for kb in stale:
        if kb.id in active_kb_ids:
            continue
        ingest._refresh_kb_status(db, kb, commit=False)
        fixed += 1
    if fixed:
        db.commit()
        log.info("启动校正：%d 个知识库的陈旧「索引中」状态已按实际索引重算", fixed)
    return fixed


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
