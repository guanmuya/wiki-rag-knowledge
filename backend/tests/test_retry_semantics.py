"""重试语义：复用同一条任务记录 + 状态双向同步（真实缺陷回归）。

用户报的两个问题，根因是同一个：把「重试」实现成了「再插一条任务」，
任务表于是变成重试日志，而文档状态压根没人管。

1. 点一次重试任务列表就多一条记录，点三次变三条；同一份文档还会被重复解析。
2. 任务记录显示「失败」，上面的文档却永远停在「索引中」——
   `recover_interrupted()` 放弃恢复时只改任务和版本，漏了文档。

现在的口径：
- 重试 = 同一条任务记录打回队列，次数记在 payload.retry.count；
- 任何任务状态变化都同步到版本 / 文档 / 知识库（走 services/taskflow.py）。
"""
from __future__ import annotations

import uuid

from sqlalchemy import func, select

from app import models
from app.services import worker


def _make_doc(db, *, doc_status="failed", task_status="failed", attempts=2,
              with_task=True, filename="retry.md"):
    """造一份文档 + 一个（默认已失败的）入库任务。"""
    tag = uuid.uuid4().hex[:8]
    kb = models.KnowledgeBase(name=f"重试-{tag}", mode="basic", status="indexing")
    db.add(kb)
    db.commit()
    doc = models.Document(kb_id=kb.id, filename=filename, suffix=".md", size_bytes=12,
                          status=doc_status, status_detail="上次失败")
    db.add(doc)
    db.commit()
    ver = models.DocumentVersion(doc_id=doc.id, version_no=1, storage_path="/tmp/none",
                                 status=task_status)
    db.add(ver)
    db.commit()
    task = None
    if with_task:
        task = models.Task(
            kind=models.TaskKind.ingest_document.value, status=task_status,
            stage="失败" if task_status == "failed" else "处理中", progress=0,
            kb_id=kb.id, doc_id=doc.id, doc_version_id=ver.id,
            dedupe_key=f"ingest:{ver.id}", attempts=attempts,
            error="RuntimeError: 上次失败原因",
            payload=f'{{"doc": "{filename}"}}',
        )
        db.add(task)
        db.commit()
    return kb, doc, ver, task


def _task_count(db, version_id: int) -> int:
    return db.execute(
        select(func.count(models.Task.id)).where(models.Task.doc_version_id == version_id)
    ).scalar() or 0


def test_retry_reuses_same_task_row(app_client, admin_headers, db_session):
    """重试后任务行数不变、id 不变，次数记在 retry_count 上。"""
    kb, doc, ver, task = _make_doc(db_session)
    before = _task_count(db_session, ver.id)
    assert before == 1

    r = app_client.post(f"/api/tasks/{task.id}/retry", headers=admin_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["task_id"] == task.id, "重试必须复用同一条任务记录"
    assert body["reused"] is True
    assert body["retry_count"] == 1

    db_session.expire_all()
    assert _task_count(db_session, ver.id) == before, "重试不允许新增任务行"

    t = db_session.get(models.Task, task.id)
    assert t.status == "queued"
    assert t.stage == "排队中"
    assert t.attempts == 0, "attempts 归零，重新给中断自动恢复一份预算"
    assert t.error == "", "error 清空，否则列表还显示失败"
    assert t.started_at is None and t.finished_at is None

    # 状态同步：文档 / 版本 / 知识库都回到「索引中」
    assert db_session.get(models.Document, doc.id).status == "indexing"
    assert db_session.get(models.DocumentVersion, ver.id).status == "queued"
    assert db_session.get(models.KnowledgeBase, kb.id).status == "indexing"


def test_retry_twice_still_one_row(app_client, admin_headers, db_session):
    """连点两次重试：仍然只有一条任务记录，次数累加。"""
    kb, doc, ver, task = _make_doc(db_session)

    first = app_client.post(f"/api/tasks/{task.id}/retry", headers=admin_headers)
    assert first.status_code == 200

    # 模拟这次又失败了
    db_session.expire_all()
    t = db_session.get(models.Task, task.id)
    t.status = "failed"
    t.stage = "失败"
    t.error = "RuntimeError: 第二次失败"
    db_session.add(t)
    db_session.commit()

    second = app_client.post(f"/api/tasks/{task.id}/retry", headers=admin_headers)
    assert second.status_code == 200, second.text
    assert second.json()["retry_count"] == 2
    assert _task_count(db_session, ver.id) == 1, "点几次都只能有一条记录"

    db_session.expire_all()
    t = db_session.get(models.Task, task.id)
    assert "第二次失败" in t.payload, "上次失败原因要保留在 payload.retry.last_error"
    assert t.status == "queued"


def test_retry_rejects_already_active_task(app_client, admin_headers, db_session):
    """已经在排队/执行的任务再点重试 → 409，不再插一条重复任务。"""
    kb, doc, ver, task = _make_doc(db_session, doc_status="indexing",
                                   task_status="queued", attempts=0)
    r = app_client.post(f"/api/tasks/{task.id}/retry", headers=admin_headers)
    assert r.status_code == 409
    assert _task_count(db_session, ver.id) == 1


def test_document_reindex_endpoint_reuses_task(app_client, admin_headers, db_session):
    """文档级重试：入口在文档上，落到的还是那条任务记录。"""
    kb, doc, ver, task = _make_doc(db_session)

    r = app_client.post(f"/api/documents/{doc.id}/reindex", headers=admin_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["task_id"] == task.id
    assert body["reused"] is True
    assert _task_count(db_session, ver.id) == 1

    db_session.expire_all()
    assert db_session.get(models.Document, doc.id).status == "indexing"

    # 再点一次：已经在处理中，直接 409，不会堆出第二条
    again = app_client.post(f"/api/documents/{doc.id}/reindex", headers=admin_headers)
    assert again.status_code == 409
    assert _task_count(db_session, ver.id) == 1


def test_document_reindex_creates_task_when_history_missing(app_client, admin_headers, db_session):
    """没有任务记录的历史文档，重试补建一条（也只有一条）。"""
    kb, doc, ver, _ = _make_doc(db_session, with_task=False, doc_status="indexing")
    assert _task_count(db_session, ver.id) == 0

    r = app_client.post(f"/api/documents/{doc.id}/reindex", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["reused"] is False
    assert _task_count(db_session, ver.id) == 1


def test_document_reindex_merges_legacy_duplicate_tasks(app_client, admin_headers, db_session):
    """旧版重试留下的多条排队任务：点一次重试就合并成一条，不会重复解析。"""
    kb, doc, ver, first = _make_doc(db_session, doc_status="indexing",
                                    task_status="queued", attempts=0)
    for _ in range(2):
        db_session.add(models.Task(
            kind=models.TaskKind.ingest_document.value, status="queued", stage="排队中",
            kb_id=kb.id, doc_id=doc.id, doc_version_id=ver.id,
            dedupe_key=f"retry:{uuid.uuid4().hex[:6]}"))
    db_session.commit()
    assert _task_count(db_session, ver.id) == 3

    r = app_client.post(f"/api/documents/{doc.id}/reindex", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["merged"] == 2

    db_session.expire_all()
    queued = db_session.execute(
        select(func.count(models.Task.id)).where(
            models.Task.doc_version_id == ver.id,
            models.Task.status == models.TaskStatus.queued.value,
        )
    ).scalar()
    assert queued == 1, "同一版本只能有一条排队任务"


def test_recover_gives_up_syncs_document_status(db_session):
    """中断次数到上限后，任务标失败的同时文档也必须离开「索引中」。"""
    kb, doc, ver, task = _make_doc(db_session, doc_status="indexing",
                                   task_status="running", attempts=worker.MAX_RESUME_ATTEMPTS)

    worker.recover_interrupted()

    db_session.expire_all()
    t = db_session.get(models.Task, task.id)
    assert t.status == "failed"
    assert "中断" in t.error

    d = db_session.get(models.Document, doc.id)
    assert d.status == "failed", "任务已放弃恢复，文档不能还停在索引中"
    assert d.status_detail, "失败必须写明原因"
    assert db_session.get(models.DocumentVersion, ver.id).status == "failed"


def test_recover_requeue_keeps_document_indexing(db_session):
    """还能自动恢复时，文档保持「索引中」，与排队中的任务一致。"""
    kb, doc, ver, task = _make_doc(db_session, doc_status="indexing",
                                   task_status="running", attempts=1)

    worker.recover_interrupted()

    db_session.expire_all()
    assert db_session.get(models.Task, task.id).status == "queued"
    assert db_session.get(models.Document, doc.id).status == "indexing"
    assert db_session.get(models.DocumentVersion, ver.id).status == "queued"


def test_worker_failure_syncs_document_status(db_session, tmp_path):
    """真实执行失败（文件缺失）时，文档也要跟着标失败。"""
    tag = uuid.uuid4().hex[:8]
    kb = models.KnowledgeBase(name=f"执行失败-{tag}", mode="basic", status="indexing")
    db_session.add(kb)
    db_session.commit()
    doc = models.Document(kb_id=kb.id, filename="missing.md", suffix=".md", status="indexing")
    db_session.add(doc)
    db_session.commit()
    ver = models.DocumentVersion(doc_id=doc.id, version_no=1,
                                 storage_path=str(tmp_path / "not-there.md"), status="queued")
    db_session.add(ver)
    db_session.commit()
    task = models.Task(kind=models.TaskKind.ingest_document.value, status="queued",
                       kb_id=kb.id, doc_id=doc.id, doc_version_id=ver.id,
                       dedupe_key=f"ingest:{ver.id}")
    db_session.add(task)
    db_session.commit()

    while worker.run_once("retry-test"):
        pass

    db_session.expire_all()
    assert db_session.get(models.Task, task.id).status == "failed"
    assert db_session.get(models.Document, doc.id).status == "failed"
    assert db_session.get(models.DocumentVersion, ver.id).status == "failed"
