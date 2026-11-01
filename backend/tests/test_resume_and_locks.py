"""中断恢复 + 慢速向量化不占写锁（真实缺陷回归）。

两个缺陷都出在「本地 Ollama 很慢」这个场景：

1. 关闭应用时 worker 是 daemon 线程，不会走 finally 把任务标成 failed。
   下次启动如果没人回收 `running` 任务，它就永远停在「处理中」，
   文档也永远解析不完 —— 用户看到「传完了但一直不动」。
2. `ingest_version()` 原来在向量化**之前**就 `delete(Chunk)`，SQLite 写事务
   因此一直开到整个向量化结束。本地 Ollama 一批几十秒、一份文档几分钟，
   这期间所有写请求都卡在写锁上直到 busy_timeout(15s)，表现就是「整个软件卡住」。
"""
from __future__ import annotations

import threading
import time
import uuid

from sqlalchemy import func, select, text

from app import models
from app.db import SessionLocal
from app.services import llm, worker

DOC = """# 差旅报销办法

## 住宿标准

一线城市住宿上限 600 元每晚，其他城市 400 元每晚。

## 交通标准

高铁二等座为标准配置，市内交通实报实销。

## 审批流程

提交人提交单据后，依次经过直属主管、部门负责人、财务复核，最后出纳付款。
"""


def _upload(app_client, admin_headers) -> tuple[int, int, int]:
    """建库 + 上传一份文档，返回 (kb_id, task_id, version_id)。"""
    kb = app_client.post("/api/kbs", headers=admin_headers,
                         json={"name": f"恢复-{uuid.uuid4().hex[:6]}", "description": "",
                               "mode": "basic"}).json()
    up = app_client.post(f"/api/kbs/{kb['id']}/documents", headers=admin_headers,
                         files=[("files", ("resume.md", DOC.encode(), "text/markdown"))])
    assert up.status_code == 200, up.text
    item = up.json()["items"][0]
    return kb["id"], item["task_id"], item["version_id"]


def _simulate_crash(db, task_id: int, version_id: int, attempts: int) -> None:
    """把任务改成「进程被强杀」时的样子：running + 已经涨过的 attempts。"""
    task = db.get(models.Task, task_id)
    task.status = models.TaskStatus.running.value
    task.stage = "向量化"
    task.progress = 55
    task.attempts = attempts
    task.started_at = models.utcnow()
    ver = db.get(models.DocumentVersion, version_id)
    ver.status = models.TaskStatus.running.value
    db.add_all([task, ver])
    db.commit()


def test_recover_requeues_and_finishes_interrupted_task(app_client, admin_headers, db_session):
    """被中断一次 → 启动回收 → 重新排队 → 能跑完并落分块。"""
    _kb_id, task_id, version_id = _upload(app_client, admin_headers)
    _simulate_crash(db_session, task_id, version_id, attempts=1)

    assert worker.recover_interrupted() >= 1

    db_session.expire_all()
    task = db_session.get(models.Task, task_id)
    assert task.status == models.TaskStatus.queued.value
    assert task.stage == "等待恢复"
    assert task.started_at is None
    ver = db_session.get(models.DocumentVersion, version_id)
    assert ver.status == models.TaskStatus.queued.value
    assert "重新排队" in ver.status_detail

    # worker 会重新领取并跑完；入库是幂等的，重跑不会产生重复分块
    while worker.run_once("test-recover"):
        pass
    db_session.expire_all()
    task = db_session.get(models.Task, task_id)
    assert task.status == models.TaskStatus.success.value, task.error
    chunks = db_session.execute(
        select(func.count(models.Chunk.id)).where(models.Chunk.doc_version_id == version_id)
    ).scalar()
    assert chunks > 0


def test_recover_gives_up_after_max_attempts(app_client, admin_headers, db_session):
    """中断次数到上限就不再自动重试，标 failed 并写明原因（避免每次开机都重跑）。"""
    _kb_id, task_id, version_id = _upload(app_client, admin_headers)
    _simulate_crash(db_session, task_id, version_id, attempts=worker.MAX_RESUME_ATTEMPTS)

    worker.recover_interrupted()

    db_session.expire_all()
    task = db_session.get(models.Task, task_id)
    assert task.status == models.TaskStatus.failed.value
    assert "中断" in task.error
    assert task.finished_at is not None


def test_slow_embedding_does_not_hold_write_lock(app_client, admin_headers, monkeypatch):
    """向量化期间必须能正常写库（否则本地 Ollama 一慢，整个应用卡住）。

    判据：embedding 进行中，另一个会话的写入要在 1.2s 内完成。
    修复前 `delete(Chunk)` 已经开了写事务，这个写入会卡到 busy_timeout。
    """
    _upload(app_client, admin_headers)

    in_embed = threading.Event()

    def slow_embed(db, texts, **kwargs):
        in_embed.set()
        time.sleep(1.5)          # 模拟本地 Ollama 一批要一秒多
        return [llm.hash_embed(t) for t in texts]

    monkeypatch.setattr(llm, "embed", slow_embed)

    errors: list[BaseException] = []

    def run_worker():
        try:
            while worker.run_once("lock-test"):
                pass
        except BaseException as exc:  # pragma: no cover - 只是把异常带回主线程
            errors.append(exc)

    thread = threading.Thread(target=run_worker, name="lock-test-worker")
    thread.start()
    try:
        assert in_embed.wait(timeout=20), "worker 没有进入向量化阶段"
        other = SessionLocal()
        try:
            # 把探测连接的等待压到 800ms：真被写锁挡住时会快速失败，不会拖 15s
            other.execute(text("PRAGMA busy_timeout=800"))
            started = time.monotonic()
            other.add(models.AppSetting(key=f"lock-probe-{uuid.uuid4().hex[:8]}", value="1"))
            other.commit()
            elapsed = time.monotonic() - started
        finally:
            other.close()
    finally:
        thread.join(timeout=120)

    assert not errors, errors
    assert elapsed < 1.2, f"向量化期间写入被写锁挡住 {elapsed:.2f}s"
