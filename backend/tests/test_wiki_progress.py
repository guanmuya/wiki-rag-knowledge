"""独立 Wiki 重建的阶段计数与进度事务回归。"""
from __future__ import annotations

import json
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app import config, models
from app.db import Base, WorkerSession
from app.services import llm, wiki, worker


@pytest.fixture
def wiki_db(tmp_path, monkeypatch):
    """独立文件库允许观察另一会话已提交的进度和未提交的业务数据。"""
    engine = create_engine(f"sqlite:///{(tmp_path / 'wiki-progress.sqlite3').as_posix()}")
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA journal_mode=WAL")
    Base.metadata.create_all(engine)
    observer = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    worker_sessions = sessionmaker(bind=engine, class_=WorkerSession,
                                   autoflush=False, expire_on_commit=False)

    @contextmanager
    def session_scope(*, role):
        assert role == "worker"
        with worker_sessions() as db:
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise

    monkeypatch.setattr(worker, "session_scope", session_scope)
    yield observer
    engine.dispose()


def _prepare(session_factory, *, locked: bool = False) -> tuple[int, int, int | None]:
    with session_factory() as db:
        kb = models.KnowledgeBase(name="进度测试", mode="wiki", status="indexing")
        db.add(kb)
        db.flush()
        doc = models.Document(kb_id=kb.id, filename="a.md", suffix=".md", status="ready")
        db.add(doc)
        db.flush()
        version = models.DocumentVersion(doc_id=doc.id, version_no=1,
                                         storage_path="/tmp/a.md", status="success")
        db.add(version)
        db.flush()
        for i in range(2):
            db.add(models.Chunk(kb_id=kb.id, doc_id=doc.id, doc_version_id=version.id,
                                ordinal=i, text=f"第 {i + 1} 段有依据的 Wiki 正文。" * 5,
                                heading=f"主题{i + 1}", locator=f"第{i + 1}段",
                                keywords="Wiki 正文", chunk_hash=f"hash-{i}"))
        page_id = None
        if locked:
            page = models.WikiPage(kb_id=kb.id, slug="locked", title="Locked", summary="原摘要",
                                   status=models.WikiPageStatus.published.value, human_locked=True)
            db.add(page)
            db.flush()
            page_version = models.WikiPageVersion(page_id=page.id, version_no=1,
                                                  content_md="人工校对的原文", origin="human")
            db.add(page_version)
            db.flush()
            page.current_version_id = page_version.id
            page_id = page.id
        task = models.Task(kind=models.TaskKind.rebuild_wiki.value,
                           status=models.TaskStatus.queued.value, kb_id=kb.id)
        db.add(task)
        db.commit()
        return kb.id, task.id, page_id


def _snapshot(session_factory, task_id):
    with session_factory() as db:
        task = db.get(models.Task, task_id)
        return task.status, task.stage, task.progress


def _two_shards(monkeypatch):
    monkeypatch.setattr(wiki, "_plan_shards", lambda chunks: (
        [[(i, chunk)] for i, chunk in enumerate(chunks, start=1)], 120))


def _model_text():
    return "## 内容\n\n" + "这是根据资料整理的正文。" * 8


def test_rebuild_reports_committed_stage_counts_without_early_page_commit(wiki_db, monkeypatch):
    kb_id, task_id, page_id = _prepare(wiki_db, locked=True)
    _two_shards(monkeypatch)
    observed = []

    def map_topics(db, entries, *, shard_no, shard_total, **kwargs):
        snapshot = _snapshot(wiki_db, task_id)
        observed.append(snapshot)
        assert snapshot[0] == "running"
        assert snapshot[1] == f"Wiki 主题归纳 {shard_no - 1}/{shard_total}"
        assert snapshot[2] < 100
        return [{"title": "Locked" if shard_no == 1 else "New",
                 "summary": "依据原文", "chunk_ids": [entries[0][0]]}]

    def page_chat(db, messages, **kwargs):
        snapshot = _snapshot(wiki_db, task_id)
        observed.append(snapshot)
        assert snapshot[0:2] == ("running", "Wiki 页面处理 1/2")
        # 人工锁定页面的待审状态仍在 Wiki 会话里，不因进度写而提前提交。
        with wiki_db() as other:
            assert other.get(models.WikiPage, page_id).status == "published"
            other.add(models.AppSetting(key="during-page-chat", value="可写"))
            other.commit()
        return _model_text()

    def section_embed(db, texts, **kwargs):
        assert _snapshot(wiki_db, task_id)[1] == "Wiki 页面处理 1/2"
        # 章节向量化这段长调用不应持有 Wiki 的写锁。
        with wiki_db() as other:
            other.add(models.AppSetting(key="during-section-embed", value="可写"))
            other.commit()
        return []

    refresh = worker.ingest._refresh_kb_status

    def check_before_final_commit(db, kb, *, commit=True):
        snapshot = _snapshot(wiki_db, task_id)
        observed.append(snapshot)
        assert snapshot[0:2] == ("running", "Wiki 页面处理 2/2")
        assert snapshot[2] < 100
        with wiki_db() as other:
            assert other.get(models.KnowledgeBase, kb_id).wiki_ready_at is None
        return refresh(db, kb, commit=commit)

    monkeypatch.setattr(wiki, "_map_topics", map_topics)
    monkeypatch.setattr(llm, "chat", page_chat)
    monkeypatch.setattr(llm, "embed", section_embed)
    monkeypatch.setattr(worker.ingest, "_refresh_kb_status", check_before_final_commit)

    assert worker.run_once("wiki-progress-test")
    with wiki_db() as db:
        task = db.get(models.Task, task_id)
        assert (task.status, task.stage, task.progress) == ("success", "完成", 100), task.error
        result = json.loads(task.result)
        assert (result["created"], result["skipped"]) == (1, 1)
        assert any("人工校对" in note for note in result["notes"])
        page = db.get(models.WikiPage, page_id)
        assert page.status == "needs_review"
        assert db.get(models.WikiPageVersion, page.current_version_id).content_md == "人工校对的原文"
        assert db.get(models.KnowledgeBase, kb_id).wiki_ready_at is not None
        assert db.execute(select(models.WikiSection).where(models.WikiSection.kb_id == kb_id)).first()
    assert [item[1] for item in observed] == [
        "Wiki 主题归纳 0/2", "Wiki 主题归纳 1/2",
        "Wiki 页面处理 1/2", "Wiki 页面处理 2/2",
    ]
    assert [item[2] for item in observed] == sorted(item[2] for item in observed)


def test_failed_page_does_not_report_completed_or_100(wiki_db, monkeypatch):
    kb_id, task_id, _ = _prepare(wiki_db)
    _two_shards(monkeypatch)
    monkeypatch.setattr(wiki, "_map_topics", lambda db, entries, *, shard_no, **kwargs: [{
        "title": "Alpha" if shard_no == 1 else "Beta", "summary": "依据原文",
        "chunk_ids": [entries[0][0]],
    }])
    monkeypatch.setattr(llm, "chat", lambda *args, **kwargs: _model_text())
    snapshots = []

    def fail_second_embedding(db, texts, **kwargs):
        snapshot = _snapshot(wiki_db, task_id)
        snapshots.append(snapshot)
        if len(snapshots) == 2:
            raise RuntimeError("章节索引失败")
        return []

    monkeypatch.setattr(llm, "embed", fail_second_embedding)
    assert worker.run_once("wiki-progress-failure")
    assert [item[1] for item in snapshots] == ["Wiki 页面处理 0/2", "Wiki 页面处理 1/2"]
    with wiki_db() as db:
        task = db.get(models.Task, task_id)
        assert (task.status, task.stage) == ("failed", "失败")
        assert "章节索引失败" in task.error
        assert task.progress == snapshots[-1][2] < 100
        assert db.get(models.KnowledgeBase, kb_id).wiki_ready_at is None


def test_progress_lock_does_not_commit_or_discard_locked_page(wiki_db, monkeypatch):
    _, task_id, page_id = _prepare(wiki_db, locked=True)
    _two_shards(monkeypatch)
    monkeypatch.setattr(config, "LOCK_RETRY_ATTEMPTS", 1)
    monkeypatch.setattr(wiki, "_map_topics", lambda db, entries, *, shard_no, **kwargs: [{
        "title": "Locked" if shard_no == 1 else "New", "summary": "依据原文",
        "chunk_ids": [entries[0][0]],
    }])
    state = {"raised": False}
    real_commit = Session.commit

    def locked_progress_commit(db):
        if isinstance(db, WorkerSession) and db._only_task_progress():
            task = next(obj for obj in db.dirty if isinstance(obj, models.Task))
            if task.stage == "Wiki 页面处理 1/2" and not state["raised"]:
                state["raised"] = True
                raise OperationalError("UPDATE tasks", None, Exception("database is locked"))
        return real_commit(db)

    def page_chat(db, messages, **kwargs):
        assert state["raised"]
        assert _snapshot(wiki_db, task_id)[1] == "Wiki 页面处理 0/2"
        with wiki_db() as other:
            assert other.get(models.WikiPage, page_id).status == "published"
        return _model_text()

    monkeypatch.setattr(Session, "commit", locked_progress_commit)
    monkeypatch.setattr(llm, "chat", page_chat)
    monkeypatch.setattr(llm, "embed", lambda *args, **kwargs: [])
    assert worker.run_once("wiki-progress-locked")
    with wiki_db() as db:
        task = db.get(models.Task, task_id)
        assert state["raised"]
        assert (task.status, task.progress) == ("success", 100), task.error
        assert db.get(models.WikiPage, page_id).status == "needs_review"


def test_invalid_evidence_skip_counts_as_processed(wiki_db, monkeypatch):
    kb_id, _, _ = _prepare(wiki_db)
    monkeypatch.setattr(wiki, "_map_topics", lambda *args, **kwargs: [{
        "title": "无证据", "summary": "", "chunk_ids": [],
    }])
    events = []
    with wiki_db() as db:
        result = wiki.build_wiki(db, db.get(models.KnowledgeBase, kb_id),
                                 on_progress=lambda *event: events.append(event))
        assert result.pages_skipped == 1
        assert any("未能定位到有效原文" in note for note in result.notes)
        assert events[-2:] == [("pages", 0, 1), ("pages", 1, 1)]
        with wiki_db() as other:
            assert other.get(models.KnowledgeBase, kb_id).wiki_ready_at is None
