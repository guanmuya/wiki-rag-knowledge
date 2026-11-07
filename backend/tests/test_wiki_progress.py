"""独立 Wiki 重建的阶段计数与进度事务回归。"""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app import config, db as db_module, models
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
        assert role in ("worker", "progress")
        sessions = worker_sessions if role == "worker" else observer
        with sessions() as db:
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise

    monkeypatch.setattr(worker, "session_scope", session_scope)
    yield observer
    engine.dispose()


@pytest.fixture
def pool_db(tmp_path, monkeypatch):
    """真实独立连接池：worker 默认容量 5+10、交互池容量 1、生产进度池。"""
    url = f"sqlite:///{(tmp_path / 'wiki-pools.sqlite3').as_posix()}"
    kwargs = {"connect_args": {"check_same_thread": False}}
    worker_engine = create_engine(url, pool_size=5, max_overflow=10,
                                  pool_timeout=0.2, **kwargs)
    interactive_engine = create_engine(url, pool_size=1, max_overflow=0,
                                       pool_timeout=2, **kwargs)
    observer_engine = create_engine(url, **kwargs)
    with observer_engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA journal_mode=WAL")
    Base.metadata.create_all(observer_engine)
    observer = sessionmaker(bind=observer_engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(config, "DATABASE_URL", url)
    monkeypatch.setattr(config, "BUSY_TIMEOUT_PROGRESS", 100)
    progress_engine = db_module._make_engine("progress")
    monkeypatch.setattr(db_module, "WorkerSessionLocal", sessionmaker(
        bind=worker_engine, class_=WorkerSession, autoflush=False, expire_on_commit=False))
    monkeypatch.setattr(db_module, "SessionLocal", sessionmaker(
        bind=interactive_engine, autoflush=False, expire_on_commit=False))
    monkeypatch.setattr(db_module, "ProgressSessionLocal", sessionmaker(
        bind=progress_engine, autoflush=False, expire_on_commit=False))
    yield observer, worker_engine, interactive_engine, progress_engine
    worker_engine.dispose()
    interactive_engine.dispose()
    progress_engine.dispose()
    observer_engine.dispose()


def _prepare(session_factory, *, locked: bool = False,
             name: str = "进度测试") -> tuple[int, int, int | None]:
    with session_factory() as db:
        kb = models.KnowledgeBase(name=name, mode="wiki", status="indexing")
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

    @event.listens_for(wiki_db.kw["bind"], "before_cursor_execute")
    def locked_progress_update(conn, cursor, statement, parameters, context, executemany):
        # 进度现在用条件 UPDATE 防止旧执行覆盖重试，直接在 SQL 层模拟锁冲突。
        if (statement.lstrip().upper().startswith("UPDATE TASKS")
                and "Wiki 页面处理 1/2" in parameters and not state["raised"]):
            state["raised"] = True
            raise OperationalError("UPDATE tasks", None, Exception("database is locked"))

    def page_chat(db, messages, **kwargs):
        assert state["raised"]
        assert _snapshot(wiki_db, task_id)[1] == "Wiki 页面处理 0/2"
        with wiki_db() as other:
            assert other.get(models.WikiPage, page_id).status == "published"
        return _model_text()

    monkeypatch.setattr(llm, "chat", page_chat)
    monkeypatch.setattr(llm, "embed", lambda *args, **kwargs: [])
    assert worker.run_once("wiki-progress-locked")
    with wiki_db() as db:
        task = db.get(models.Task, task_id)
        assert state["raised"]
        assert (task.status, task.progress) == ("success", 100), task.error
        assert db.get(models.WikiPage, page_id).status == "needs_review"


def test_progress_with_all_default_worker_connections_checked_out(pool_db):
    observer, worker_engine, _, _ = pool_db
    records = [_prepare(observer, locked=True, name=f"进度测试{i}") for i in range(15)]
    with observer() as db:
        for _, task_id, _ in records:
            db.get(models.Task, task_id).status = models.TaskStatus.running.value
        db.commit()

    ready = threading.Barrier(len(records) + 1)
    reported = threading.Barrier(len(records) + 1)
    release = threading.Event()
    errors = []

    def run_progress(task_id, page_id):
        with db_module.session_scope(role="worker") as db:
            page = db.get(models.WikiPage, page_id)
            page.summary = "尚未提交的页面修改"
            ready.wait(timeout=20)
            error = None
            try:
                worker._report_wiki_progress(task_id, "topics", 1, 2)
            except Exception as exc:
                error = exc
                errors.append(exc)
            reported.wait(timeout=20)
            assert release.wait(timeout=20)
            db.rollback()
            if error:
                raise error

    with ThreadPoolExecutor(max_workers=len(records)) as executor:
        futures = [executor.submit(run_progress, task_id, page_id)
                   for _, task_id, page_id in records]
        try:
            ready.wait(timeout=20)
            assert worker_engine.pool.checkedout() == 15
            reported.wait(timeout=20)
            assert worker_engine.pool.checkedout() == 15
            assert not errors, str(errors[0]) if errors else ""
            with observer() as db:
                for _, task_id, page_id in records:
                    task = db.get(models.Task, task_id)
                    assert (task.stage, task.progress) == ("Wiki 主题归纳 1/2", 22)
                    assert db.get(models.WikiPage, page_id).summary == "原摘要"
        finally:
            release.set()
        for future in futures:
            future.result(timeout=20)


def test_saturated_interactive_pool_does_not_delay_progress(pool_db):
    observer, _, interactive_engine, _ = pool_db
    _, task_id, _ = _prepare(observer)
    with observer() as db:
        db.get(models.Task, task_id).status = "running"
        db.commit()

    with interactive_engine.connect():
        started = time.monotonic()
        worker._report_wiki_progress(task_id, "topics", 1, 2)
        elapsed = time.monotonic() - started
    assert elapsed < 1.5
    assert _snapshot(observer, task_id) == ("running", "Wiki 主题归纳 1/2", 22)


def test_saturated_progress_pool_checkout_is_bounded_without_write_lock(pool_db, caplog):
    observer, _, _, progress_engine = pool_db
    _, task_id, _ = _prepare(observer)
    with observer() as db:
        task = db.get(models.Task, task_id)
        task.status = "running"
        task.stage = "原进度"
        task.progress = 5
        db.commit()

    with ExitStack() as stack:
        for _ in range(db_module.PROGRESS_POOL_SIZE):
            stack.enter_context(progress_engine.connect())
        assert progress_engine.pool.checkedout() == db_module.PROGRESS_POOL_SIZE
        # 独立连接能写入，证明测试等待的是连接池而非 SQLite 写锁。
        with observer() as db:
            db.add(models.AppSetting(key="pool-saturation", value="write succeeds"))
            db.commit()
        started = time.monotonic()
        worker._report_wiki_progress(task_id, "topics", 1, 2)
        elapsed = time.monotonic() - started
        assert db_module.PROGRESS_POOL_TIMEOUT_SECONDS * 0.8 <= elapsed < 1.5
        assert _snapshot(observer, task_id) == ("running", "原进度", 5)
    assert "Wiki 进度连接池等待超时" in caplog.text
    worker._report_wiki_progress(task_id, "topics", 1, 2)
    assert _snapshot(observer, task_id) == ("running", "Wiki 主题归纳 1/2", 22)


def test_progress_engine_accepts_sqlite_memory_url(monkeypatch):
    # SQLite 内存 URL 默认使用不接受 pool_timeout 的 SingletonThreadPool。
    monkeypatch.setattr(config, "DATABASE_URL", "sqlite://")
    engine = db_module._make_engine("progress")
    try:
        with engine.connect() as conn:
            assert conn.exec_driver_sql("SELECT 1").scalar() == 1
    finally:
        engine.dispose()


def test_exhausted_progress_pool_does_not_fail_wiki(pool_db, monkeypatch, caplog):
    observer, _, _, progress_engine = pool_db
    _, task_id, page_id = _prepare(observer, locked=True)
    _two_shards(monkeypatch)
    monkeypatch.setattr(wiki, "_map_topics", lambda db, entries, *, shard_no, **kwargs: [{
        "title": "Locked" if shard_no == 1 else "New", "summary": "依据原文",
        "chunk_ids": [entries[0][0]],
    }])

    def page_chat(db, messages, **kwargs):
        # 所有进度取连接都超时，但外层页面修改依然没有被提前提交。
        assert _snapshot(observer, task_id) == ("running", "生成 Wiki 页面", 5)
        with observer() as other:
            assert other.get(models.WikiPage, page_id).status == "published"
        return _model_text()

    monkeypatch.setattr(llm, "chat", page_chat)
    monkeypatch.setattr(llm, "embed", lambda *args, **kwargs: [])
    with ExitStack() as stack:
        for _ in range(db_module.PROGRESS_POOL_SIZE):
            stack.enter_context(progress_engine.connect())
        assert progress_engine.pool.checkedout() == db_module.PROGRESS_POOL_SIZE
        assert worker.run_once("wiki-progress-pool-timeout")
    assert "Wiki 进度连接池等待超时" in caplog.text
    with observer() as db:
        task = db.get(models.Task, task_id)
        assert (task.status, task.stage, task.progress) == ("success", "完成", 100), task.error
        assert db.get(models.WikiPage, page_id).status == "needs_review"


def test_progress_retries_real_sqlite_lock_then_recovers(pool_db, monkeypatch, caplog):
    observer, _, _, progress_engine = pool_db
    _, task_id, _ = _prepare(observer)
    with observer() as db:
        task = db.get(models.Task, task_id)
        task.status = "running"
        task.stage = "原进度"
        task.progress = 5
        db.commit()
    monkeypatch.setattr(config, "BUSY_TIMEOUT_PROGRESS", 20)
    monkeypatch.setattr(config, "LOCK_RETRY_ATTEMPTS", 2)
    updates = []

    @event.listens_for(progress_engine, "before_cursor_execute")
    def count_progress_updates(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("UPDATE TASKS"):
            updates.append(statement)

    with observer() as blocker:
        blocker.connection().exec_driver_sql("BEGIN IMMEDIATE")
        worker._report_wiki_progress(task_id, "topics", 1, 2)
        blocker.rollback()
    assert len(updates) == 2
    assert "Wiki 进度写入被锁挡住" in caplog.text
    assert _snapshot(observer, task_id) == ("running", "原进度", 5)
    worker._report_wiki_progress(task_id, "topics", 1, 2)
    assert len(updates) == 3
    assert _snapshot(observer, task_id) == ("running", "Wiki 主题归纳 1/2", 22)


def test_unrelated_progress_database_errors_are_not_hidden(pool_db):
    observer, _, _, progress_engine = pool_db
    _, task_id, _ = _prepare(observer)
    with observer() as db:
        db.get(models.Task, task_id).status = "running"
        db.commit()

    @event.listens_for(progress_engine, "before_cursor_execute")
    def fail_update(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("UPDATE TASKS"):
            raise OperationalError(statement, parameters, Exception("no such column: missing"))

    with pytest.raises(OperationalError, match="no such column"):
        worker._report_wiki_progress(task_id, "topics", 1, 2)
    assert _snapshot(observer, task_id)[0] == "running"


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
