"""SQLite timestamp regressions for real ingestion and Wiki publication.

A newly flushed Chunk keeps its aware UTC default in a non-expiring Session,
whereas selecting the same SQLite column returns a naive UTC datetime.  Both
represent the same input and must not be mistaken for a source change.
"""
from __future__ import annotations

import datetime as dt
import json
from contextlib import contextmanager
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, select, update
from sqlalchemy.orm import sessionmaker

from app import models
from app.db import Base, WorkerSession
from app.services import ingest, llm, wiki, worker


SOURCE = (
    "# 住宿报销\n\n"
    "员工提交住宿发票后，由直属主管审批，财务复核合规票据并办理付款。"
    "差旅结束后应及时提交相关材料，费用以实际发生的合规金额为准。"
)
PAGE = (
    "## 住宿报销流程\n\n"
    "员工提交住宿发票和相关材料，由直属主管审批，财务复核合规票据并办理付款。"
    "费用以实际发生的合规金额为准。 [1]"
)


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    """A separate file-backed SQLite database with production session settings."""
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'wiki-timestamp.sqlite3').as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 2},
    )

    @event.listens_for(engine, "connect")
    def sqlite_pragmas(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA journal_mode=WAL").scalar().lower() == "wal"
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=WorkerSession, autoflush=False,
                           expire_on_commit=False)
    monkeypatch.setattr(llm, "active_embedding", lambda db: (None, None, "test-embedding", 0))
    monkeypatch.setattr(llm, "embed", lambda db, texts, **kwargs: [])
    try:
        yield factory
    finally:
        engine.dispose()


def _prepare_ingest(sessions, tmp_path):
    path = tmp_path / "source.md"
    path.write_text(SOURCE, encoding="utf-8")
    with sessions() as db:
        kb = models.KnowledgeBase(name="timestamp-regression", mode=models.KbMode.wiki.value)
        db.add(kb)
        db.flush()
        doc = models.Document(kb_id=kb.id, filename=path.name, suffix=".md", status="indexing")
        db.add(doc)
        db.flush()
        version = models.DocumentVersion(doc_id=doc.id, version_no=1,
                                         storage_path=str(path), status="queued")
        db.add(version)
        db.flush()
        task = models.Task(kind=models.TaskKind.ingest_document.value,
                           status=models.TaskStatus.queued.value, kb_id=kb.id,
                           doc_id=doc.id, doc_version_id=version.id,
                           dedupe_key=f"ingest:{version.id}")
        db.add(task)
        db.commit()
        return kb.id, doc.id, version.id, task.id


def _claim_for_direct_ingest(db, task_id):
    """Model a worker claim before directly invoking the ingest function."""
    task = db.get(models.Task, task_id)
    assert task.status == models.TaskStatus.queued.value
    task.status = models.TaskStatus.running.value
    task.run_token = uuid4().hex
    db.commit()
    return task


def _watch_new_chunks(db, state):
    """Observe actual flush and commit; retain the same ORM objects as ingest."""
    state["session"] = db
    state["chunks"] = []

    @event.listens_for(db, "after_flush")
    def after_flush(session, _):
        state["chunks"].extend(obj for obj in session.new if isinstance(obj, models.Chunk))

    @event.listens_for(db, "after_commit")
    def after_commit(session):
        if state["chunks"] and "committed_timestamps" not in state:
            state["committed_timestamps"] = {
                chunk.id: chunk.created_at for chunk in state["chunks"]
            }


def _stub_chat(monkeypatch, *, on_map=None, on_page=None):
    calls = []

    def chat(db, messages, **kwargs):
        prompt = messages[0]["content"]
        if kwargs.get("json_mode"):
            calls.append("map")
            assert "员工提交住宿发票" in prompt
            if on_map:
                on_map(db)
            return json.dumps({"topics": [{"title": "住宿报销", "summary": "住宿报销流程",
                                           "chunk_ids": [1]}]}, ensure_ascii=False)
        calls.append("page")
        assert "员工提交住宿发票" in prompt
        if on_page:
            on_page(db)
        return PAGE

    monkeypatch.setattr(llm, "chat", chat)
    return calls


def _assert_published(sessions, kb_id, doc_id, version_id):
    with sessions() as db:
        kb = db.get(models.KnowledgeBase, kb_id)
        doc = db.get(models.Document, doc_id)
        version = db.get(models.DocumentVersion, version_id)
        chunks = db.execute(select(models.Chunk).where(
            models.Chunk.doc_version_id == version_id).order_by(models.Chunk.ordinal)).scalars().all()
        assert chunks and version.chunk_count == len(chunks)
        assert version.status == models.TaskStatus.success.value
        assert doc.current_version_id == version_id and doc.status == models.KbStatus.ready.value
        assert kb.status == models.KbStatus.ready.value and kb.wiki_ready_at is not None
        page = db.execute(select(models.WikiPage).where(models.WikiPage.kb_id == kb_id)).scalar_one()
        assert page.status == models.WikiPageStatus.published.value
        page_version = db.get(models.WikiPageVersion, page.current_version_id)
        assert page_version is not None and page_version.content_md == PAGE
        evidence = db.execute(select(models.WikiEvidence).where(
            models.WikiEvidence.page_version_id == page_version.id)).scalars().all()
        assert [(link.chunk_id, link.doc_id, link.doc_version_id) for link in evidence] == [
            (chunks[0].id, doc_id, version_id)
        ]
        sections = db.execute(select(models.WikiSection).where(
            models.WikiSection.page_version_id == page_version.id)).scalars().all()
        assert sections and "住宿发票" in "".join(section.text for section in sections)
        assert {item.kind for item in db.execute(select(models.IndexVersion).where(
            models.IndexVersion.kb_id == kb_id)).scalars()} == {"chunk", "wiki"}


@pytest.mark.parametrize("execution", ["ingest", "worker"])
def test_ingest_publishes_wiki_for_equivalent_aware_and_naive_timestamps(
    sessions, tmp_path, monkeypatch, execution,
):
    kb_id, doc_id, version_id, task_id = _prepare_ingest(sessions, tmp_path)
    state = {}

    def observe_map(db):
        # The flushed chunks were committed in the Session now building the Wiki.
        assert db is state["session"]
        assert db.autoflush is False and db.expire_on_commit is False
        assert state["chunks"] and state.get("committed_timestamps")
        stored = dict(db.execute(select(models.Chunk.id, models.Chunk.created_at).where(
            models.Chunk.doc_version_id == version_id)).all())
        assert set(stored) == set(state["committed_timestamps"])
        for chunk in state["chunks"]:
            assert db.get(models.Chunk, chunk.id) is chunk
            aware = state["committed_timestamps"][chunk.id]
            naive = stored[chunk.id]
            assert aware.utcoffset() == dt.timedelta(0)
            assert naive.tzinfo is None
            assert aware.replace(tzinfo=None) == naive

    calls = _stub_chat(monkeypatch, on_map=observe_map)
    if execution == "ingest":
        with sessions() as db:
            _watch_new_chunks(db, state)
            task = _claim_for_direct_ingest(db, task_id)
            result = ingest.ingest_version(db, task, stats=llm.CallStats())
            db.commit()
    else:
        @contextmanager
        def local_session_scope(role="interactive"):
            assert role == "worker"
            with sessions() as db:
                _watch_new_chunks(db, state)
                try:
                    yield db
                    db.commit()
                except Exception:
                    db.rollback()
                    raise

        monkeypatch.setattr(worker, "session_scope", local_session_scope)
        assert worker.run_once("timestamp-regression")
        with sessions() as db:
            task = db.get(models.Task, task_id)
            assert task.status == models.TaskStatus.success.value, task.error
            result = json.loads(task.result)

    assert calls == ["map", "page"]
    assert "wiki_error" not in result, result
    assert result["wiki"]["created"] == 1 and result["wiki"]["skipped"] == 0
    assert result["wiki"]["sections"] > 0 and result["wiki"]["evidence_links"] == 1
    _assert_published(sessions, kb_id, doc_id, version_id)


def test_build_wiki_with_naive_utc_timestamp_loaded_from_sqlite(sessions, tmp_path, monkeypatch):
    kb_id, doc_id, version_id, task_id = _prepare_ingest(sessions, tmp_path)
    # Generate the real chunks first without Wiki, then load them in a fresh Session.
    with sessions() as db:
        kb = db.get(models.KnowledgeBase, kb_id)
        kb.mode = models.KbMode.basic.value
        db.commit()
        result = ingest.ingest_version(db, db.get(models.Task, task_id), stats=llm.CallStats())
        assert result["chunks"] > 0 and "wiki" not in result
        db.commit()

    calls = _stub_chat(monkeypatch)
    with sessions() as db:
        chunks = db.execute(select(models.Chunk).where(
            models.Chunk.doc_version_id == version_id)).scalars().all()
        assert chunks and all(chunk.created_at.tzinfo is None for chunk in chunks)
        kb = db.get(models.KnowledgeBase, kb_id)
        kb.mode = models.KbMode.wiki.value
        result = wiki.build_wiki(db, kb, stats=llm.CallStats())
        db.commit()
        assert result.pages_created == 1 and result.pages_skipped == 0
        assert result.sections > 0 and result.evidence_links == 1

    assert calls == ["map", "page"]
    _assert_published(sessions, kb_id, doc_id, version_id)


def test_ingest_rejects_a_real_timestamp_change_during_generation(sessions, tmp_path, monkeypatch):
    kb_id, doc_id, version_id, task_id = _prepare_ingest(sessions, tmp_path)
    state = {}

    def change_timestamp(build_db):
        chunk_id = state["chunks"][0].id
        with sessions() as other:
            assert other.connection().connection.driver_connection is not (
                build_db.connection().connection.driver_connection
            )
            before = other.execute(select(models.Chunk.created_at).where(
                models.Chunk.id == chunk_id)).scalar_one()
            assert before.tzinfo is None
            after = before + dt.timedelta(microseconds=1)
            other.execute(update(models.Chunk).where(models.Chunk.id == chunk_id)
                          .values(created_at=after))
            other.commit()
        state["changed"] = (chunk_id, before, after)

    calls = _stub_chat(monkeypatch, on_page=change_timestamp)
    with sessions() as db:
        _watch_new_chunks(db, state)
        task = _claim_for_direct_ingest(db, task_id)
        result = ingest.ingest_version(db, task, stats=llm.CallStats())
        db.commit()

    assert calls == ["map", "page"] and state.get("changed")
    assert "来源" in result.get("wiki_error", ""), result
    assert "wiki" not in result
    chunk_id, before, after = state["changed"]
    assert after != before
    with sessions() as db:
        assert db.get(models.Chunk, chunk_id).created_at == after
        assert db.get(models.KnowledgeBase, kb_id).status == models.KbStatus.partial.value
        assert db.get(models.Document, doc_id).current_version_id == version_id
        assert not db.execute(select(models.WikiPage.id).where(models.WikiPage.kb_id == kb_id)).first()
        assert not db.execute(select(models.WikiEvidence.id).where(models.WikiEvidence.kb_id == kb_id)).first()
