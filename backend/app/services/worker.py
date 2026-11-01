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

from sqlalchemy import select, text, update
from sqlalchemy.exc import OperationalError

from .. import config, models
from ..db import is_lock_error, retry_on_locked, session_scope
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
    task = db.execute(
        select(models.Task).where(models.Task.status == models.TaskStatus.queued.value)
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
                started_at=models.utcnow(), attempts=models.Task.attempts + 1)
    )
    db.commit()
    if res.rowcount != 1:
        return None
    return db.get(models.Task, task.id)


def run_once(worker_id: str = "inline") -> bool:
    """执行一个任务；返回是否真的处理了任务（供测试同步驱动）。"""
    # role="worker"：busy_timeout 提到 30s，且进度写撞锁不会让任务失败
    with session_scope(role="worker") as db:
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
                # 重建成功后必须刷新 status_detail：排队时写的是「重建任务已排队」，
                # 不刷新就会永远停在那一句（真实缺陷，实测重建完仍显示「已排队」）。
                ingest._refresh_kb_status(db, kb, commit=False)
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
            if task and is_lock_error(exc) and task.attempts <= LOCK_REQUEUE_MAX_ATTEMPTS:
                # 业务写等满 30s 还没拿到锁：这是「环境忙」，不是「任务坏」。
                # 判失败会让用户以为文档解析出问题、还白花一次重试预算；
                # 重新排队等下一轮，进度和文档状态都回到「等待处理」。
                task.status = models.TaskStatus.queued.value
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
        tasks = db.execute(
            select(models.Task).where(models.Task.status == models.TaskStatus.running.value)
        ).scalars().all()
        if not tasks:
            return 0
        for task in tasks:
            if task.attempts >= max_attempts:
                task.status = models.TaskStatus.failed.value
                task.stage = "失败"
                task.error = f"任务被中断 {task.attempts} 次，已停止自动恢复（可在文档列表点重试）"
                task.finished_at = models.utcnow()
                # 放弃恢复也必须同步文档状态：否则任务显示「失败」、
                # 文档永远停在「索引中」，用户既看不出问题也没有重试入口。
                taskflow.sync_failure(db, task, task.error)
            else:
                task.status = models.TaskStatus.queued.value
                task.stage = "等待恢复"
                task.progress = 0
                task.started_at = None
                taskflow.sync_running(db, task, detail="上次处理被中断，已重新排队")
            db.add(task)
        db.commit()
        log.info("启动恢复：%d 个被中断的任务已重新排队", len(tasks))
        return len(tasks)


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
