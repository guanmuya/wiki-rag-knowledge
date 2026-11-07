"""重建任务结束后的知识库状态同步（真实缺陷回归）。

用户现场：换 Embedding 模型后的全量重建任务已「成功」（分块 492、签名
`KZ:qwen3-embedding-8b:4096`），知识库卡片却一直显示「索引中 · 正在全量重建」。
根因是任务成功时只更新任务行，没重算 kb.status。
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models, serializers
from app.db import Base
from app.services import retrieval, worker


def _make_kb(db, name: str, *, mode: str = "basic", chunks: int = 3):
    kb = models.KnowledgeBase(
        name=f"{name}-{uuid.uuid4().hex[:6]}", mode=mode, status="indexing",
        status_detail="Embedding 切换中，正在全量重建", embed_signature="old:model:256",
    )
    db.add(kb)
    db.commit()
    doc = models.Document(kb_id=kb.id, filename="a.md", suffix=".md", status="ready")
    db.add(doc)
    db.commit()
    ver = models.DocumentVersion(doc_id=doc.id, version_no=1, storage_path="/tmp/a",
                                 status="success")
    db.add(ver)
    db.commit()
    for i in range(chunks):
        db.add(models.Chunk(kb_id=kb.id, doc_id=doc.id, doc_version_id=ver.id, ordinal=i,
                            text=f"第 {i} 段测试正文。", keywords="测试", chunk_hash=f"{kb.id}-{i}"))
    db.commit()
    return kb


def _run_queued(db, task_id: int, limit: int = 20) -> models.Task:
    for _ in range(limit):
        db.expire_all()
        task = db.get(models.Task, task_id)
        if task.status in (models.TaskStatus.success.value, models.TaskStatus.failed.value):
            return task
        assert worker.run_once("test") is True
    raise AssertionError(f"任务 #{task_id} 在 {limit} 轮内没有执行")


@pytest.mark.parametrize("kind", ["embedding_rebuild", "reindex_kb"])
def test_rebuild_success_clears_indexing_status(db_session, monkeypatch, kind):
    kb = _make_kb(db_session, f"重建-{kind}")
    task = models.Task(kind=kind, status=models.TaskStatus.queued.value, stage="排队中",
                       kb_id=kb.id, dedupe_key=f"{kind}:{kb.id}:{uuid.uuid4().hex[:6]}")
    db_session.add(task)
    db_session.commit()

    # 只验证 worker 的状态收尾；重建本身由 ingest 的单测覆盖，这里不连模型。
    monkeypatch.setattr(worker.ingest, "reindex_kb", lambda db, task, *, stats: {"chunks": 3})
    done = _run_queued(db_session, task.id)
    assert done.status == models.TaskStatus.success.value, done.error

    db_session.expire_all()
    kb = db_session.get(models.KnowledgeBase, kb.id)
    assert kb.status == models.KbStatus.ready.value, "任务成功，知识库不能还停在索引中"
    assert "正在全量重建" not in kb.status_detail
    assert "1 个文档 / 3 个索引单元" in kb.status_detail


def test_rebuild_wiki_failure_marks_partial(db_session, monkeypatch):
    kb = _make_kb(db_session, "重建-Wiki失败", mode="wiki")
    task = models.Task(kind=models.TaskKind.embedding_rebuild.value,
                       status=models.TaskStatus.queued.value, kb_id=kb.id)
    db_session.add(task)
    db_session.commit()
    monkeypatch.setattr(worker.ingest, "reindex_kb",
                        lambda db, task, *, stats: {"chunks": 3, "wiki_error": "模型超时"})

    assert _run_queued(db_session, task.id).status == models.TaskStatus.success.value
    db_session.expire_all()
    kb = db_session.get(models.KnowledgeBase, kb.id)
    assert kb.status == models.KbStatus.partial.value
    assert "Wiki 生成失败" in kb.status_detail


def test_reconcile_clears_only_stale_indexing_kbs():
    """启动校正：没有活动任务的「索引中」库恢复；有排队任务的库保持原样。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as db:
        ready = _make_kb(db, "状态陈旧")
        queued = _make_kb(db, "真的还在重建")
        empty = models.KnowledgeBase(name=f"空库-{uuid.uuid4().hex[:6]}", mode="basic",
                                     status="indexing", status_detail="Embedding 切换中")
        db.add_all([empty, models.Task(kind=models.TaskKind.embedding_rebuild.value,
                                       status=models.TaskStatus.queued.value, kb_id=queued.id)])
        db.commit()

        assert worker._reconcile_kb_statuses(db) == 2
        db.expire_all()
        assert db.get(models.KnowledgeBase, ready.id).status == models.KbStatus.ready.value
        assert db.get(models.KnowledgeBase, empty.id).status == models.KbStatus.empty.value
        assert db.get(models.KnowledgeBase, queued.id).status == models.KbStatus.indexing.value


@pytest.mark.parametrize(("status", "label"), [
    ("queued", "Wiki 排队中"),
    ("running", "Wiki 生成中"),
])
def test_wiki_rebuild_status_reports_available_chunks(db_session, app_client, admin_headers, status, label):
    kb = _make_kb(db_session, "Wiki状态", mode="wiki")
    task = models.Task(kind="rebuild_wiki", status=status, kb_id=kb.id)
    db_session.add(task)
    db_session.commit()

    # 和真实检索路由对照，而不是把「索引中」误当作库不可用。
    assert retrieval.plan_route(db_session, kb)[0] == "chunk"
    detail = app_client.get(f"/api/kbs/{kb.id}", headers=admin_headers).json()
    listed = next(item for item in app_client.get("/api/kbs", headers=admin_headers).json()["items"]
                  if item["id"] == kb.id)
    for result in (detail, listed):
        assert result["status"] == "indexing"  # 原有 API 状态保持兼容
        assert result["status_label"] == label
        assert result["status_detail"] == "分块检索可用"
    db_session.delete(task)
    db_session.commit()


def test_auto_wiki_rebuild_reports_activity_on_ready_kb(db_session):
    kb = _make_kb(db_session, "删除文档后", mode="wiki")
    kb.status = models.KbStatus.ready.value
    task = models.Task(kind="rebuild_wiki", status="running", kb_id=kb.id)
    db_session.add_all([kb, task])
    db_session.commit()
    result = serializers.kb_out(db_session, kb)
    assert result["status_label"] == "可用"
    assert result["status_detail"] == "Wiki 生成中 · 分块检索可用"
    db_session.delete(task)
    db_session.commit()


def test_auto_wiki_rebuild_preserves_partial_error(db_session):
    kb = _make_kb(db_session, "部分可用", mode="wiki")
    kb.status, kb.status_detail = "partial", "一个文档处理失败"
    task = models.Task(kind="rebuild_wiki", status="queued", kb_id=kb.id)
    db_session.add_all([kb, task])
    db_session.commit()
    result = serializers.kb_out(db_session, kb)
    assert result["status_label"] == "部分可用"
    assert result["status_detail"] == "一个文档处理失败；Wiki 排队中"
    db_session.delete(task)
    db_session.commit()


def test_wiki_rebuild_does_not_mask_other_active_work(db_session):
    kb = _make_kb(db_session, "混合状态", mode="wiki")
    tasks = [
        models.Task(kind="rebuild_wiki", status="queued", kb_id=kb.id),
        models.Task(kind="reindex_kb", status="running", kb_id=kb.id),
    ]
    db_session.add_all(tasks)
    db_session.commit()
    result = serializers.kb_out(db_session, kb)
    assert result["status_label"] == "索引中"
    assert result["status_detail"] == kb.status_detail
    for task in tasks:
        db_session.delete(task)
    db_session.commit()


def test_wiki_rebuild_without_chunks_does_not_claim_availability(db_session):
    kb = _make_kb(db_session, "无分块", mode="wiki", chunks=0)
    task = models.Task(kind="rebuild_wiki", status="running", kb_id=kb.id)
    db_session.add(task)
    db_session.commit()
    result = serializers.kb_out(db_session, kb)
    assert result["status_label"] == "Wiki 生成中"
    assert result["status_detail"] == "尚无分块索引"
    db_session.delete(task)
    db_session.commit()
