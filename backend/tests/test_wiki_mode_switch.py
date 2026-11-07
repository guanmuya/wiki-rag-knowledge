"""Regressions for initial Wiki generation when a basic KB changes mode.

Each case uses its own SQLite database, including worker claims and execution;
no shared test queue or configured external models are involved.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app import models, schemas, serializers
from app.db import Base
from app.routers import kbs
from app.services import llm, taskflow, wiki, worker


ACTIVE = (models.TaskStatus.queued.value, models.TaskStatus.running.value)


@pytest.fixture
def store(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'wiki-mode.sqlite3').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with sessions() as db:
        db.add(models.User(username="admin", password_hash="unused", role="admin"))
        db.commit()

    @contextmanager
    def session_scope(role="interactive"):
        with sessions() as db:
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise

    monkeypatch.setattr(worker, "session_scope", session_scope)
    try:
        yield sessions
    finally:
        engine.dispose()


def _kb(sessions, *, chunks=False, mode="basic"):
    with sessions() as db:
        kb = models.KnowledgeBase(name=f"mode-{uuid4().hex}", mode=mode, status="ready")
        db.add(kb)
        db.flush()
        if chunks:
            doc = models.Document(kb_id=kb.id, filename="source.md", status="ready")
            db.add(doc)
            db.flush()
            ver = models.DocumentVersion(doc_id=doc.id, version_no=1,
                                         storage_path="unused", status="success")
            db.add(ver)
            db.flush()
            doc.current_version_id = ver.id
            db.add(models.Chunk(
                kb_id=kb.id, doc_id=doc.id, doc_version_id=ver.id,
                text="住宿费由员工提交合规票据，主管审批后由财务复核并支付。" * 3,
                heading="住宿报销", locator="第一节", keywords="住宿 报销", chunk_hash="source",
            ))
        db.commit()
        return kb.id


def _switch(sessions, kb_id, mode):
    with sessions() as db:
        return kbs.update_kb(kb_id, schemas.KbUpdateIn(mode=mode), db,
                             db.execute(select(models.User)).scalar_one())


def _task(sessions, kb_id, *, kind="ingest_document", status="queued", dedupe_key=None):
    with sessions() as db:
        task = models.Task(kb_id=kb_id, kind=kind, status=status,
                           dedupe_key=dedupe_key or f"test:{uuid4().hex}")
        db.add(task)
        db.commit()
        return task.id


def _ingest_task(sessions, kb_id, source, *, missing=False):
    if not missing:
        source.write_text("# 住宿报销\n\n" + "住宿费由员工提交合规票据，主管审批后财务支付。" * 5,
                          encoding="utf-8")
    with sessions() as db:
        doc = models.Document(kb_id=kb_id, filename=source.name, suffix=".md", status="indexing")
        db.add(doc)
        db.flush()
        ver = models.DocumentVersion(doc_id=doc.id, version_no=1, storage_path=str(source),
                                     status="queued")
        db.add(ver)
        db.flush()
        task = models.Task(kind="ingest_document", status="queued", kb_id=kb_id,
                           doc_id=doc.id, doc_version_id=ver.id, dedupe_key=f"ingest:{ver.id}")
        db.add(task)
        db.commit()
        return task.id


def _active_init(sessions, kb_id):
    with sessions() as db:
        return list(db.execute(select(models.Task).where(
            models.Task.kb_id == kb_id,
            models.Task.dedupe_key == f"wiki-init:{kb_id}",
            models.Task.status.in_(ACTIVE),
        )).scalars())


def _page(sessions, kb_id, *, status="draft", locked=True):
    with sessions() as db:
        page = models.WikiPage(kb_id=kb_id, slug="existing", title="Existing",
                               status=status, human_locked=locked)
        db.add(page)
        db.flush()
        if status == "published":
            version = models.WikiPageVersion(page_id=page.id, version_no=1,
                                             content_md="Existing page content", origin="generated")
            db.add(version)
            db.flush()
            page.current_version_id = version.id
        db.commit()
        return page.id


def _claim(sessions):
    with sessions() as db:
        task = worker._claim_locked(db, "test")
        return task.id if task else None


def _finish(sessions, task_id):
    with sessions() as db:
        db.get(models.Task, task_id).status = models.TaskStatus.success.value
        db.commit()


def test_switch_with_indexed_chunks_queues_one_initial_build(store):
    kb_id = _kb(store, chunks=True)
    response = _switch(store, kb_id, "wiki")

    tasks = _active_init(store, kb_id)
    assert response["mode"] == "wiki"
    assert len(tasks) == 1
    assert tasks[0].kind == models.TaskKind.rebuild_wiki.value
    assert tasks[0].status == models.TaskStatus.queued.value


def test_switch_of_empty_kb_does_not_queue_a_build(store):
    kb_id = _kb(store)
    _switch(store, kb_id, "wiki")

    with store() as db:
        assert not db.execute(select(models.Task).where(models.Task.kb_id == kb_id)).first()


@pytest.mark.parametrize("page_status", ["draft", "published"])
@pytest.mark.parametrize("locked", [False, True])
def test_existing_page_retains_manual_rebuild_flow(store, page_status, locked):
    kb_id = _kb(store, chunks=True)
    page_id = _page(store, kb_id, status=page_status, locked=locked)
    with store() as db:
        original_version_id = db.get(models.WikiPage, page_id).current_version_id
    response = _switch(store, kb_id, "wiki")

    assert response["mode"] == "wiki"
    with store() as db:
        assert not db.execute(select(models.Task).where(models.Task.kb_id == kb_id)).first()
        page = db.get(models.WikiPage, page_id)
        assert page.human_locked == locked
        assert page.current_version_id == original_version_id
        if original_version_id is not None:
            assert db.get(models.WikiPageVersion, original_version_id).content_md == "Existing page content"


@pytest.mark.parametrize("status", ["queued", "running"])
@pytest.mark.parametrize("kind", ["ingest_document", "reindex_kb", "embedding_rebuild"])
@pytest.mark.parametrize("chunks", [False, True])
def test_active_work_has_a_durable_initial_build_after_it(store, status, kind, chunks):
    kb_id = _kb(store, chunks=chunks)
    prior_id = _task(store, kb_id, kind=kind, status=status)
    _switch(store, kb_id, "wiki")

    tasks = _active_init(store, kb_id)
    assert len(tasks) == 1
    assert tasks[0].id > prior_id
    assert tasks[0].kind == models.TaskKind.rebuild_wiki.value
    assert tasks[0].status == models.TaskStatus.queued.value


def test_repeated_mode_switch_does_not_duplicate_pending_init(store):
    kb_id = _kb(store, chunks=True)
    _switch(store, kb_id, "wiki")
    _switch(store, kb_id, "basic")
    _switch(store, kb_id, "wiki")

    assert len(_active_init(store, kb_id)) == 1


def test_concurrent_switches_enqueue_one_initial_build(store):
    kb_id = _kb(store, chunks=True)
    barrier = Barrier(2)

    def change_mode():
        with store() as db:
            # Both requests observe basic mode before either attempts the update.
            kb = db.get(models.KnowledgeBase, kb_id)
            admin = db.execute(select(models.User)).scalar_one()
            assert kb.mode == "basic"
            barrier.wait(timeout=5)
            return kbs.update_kb(kb_id, schemas.KbUpdateIn(mode="wiki"), db, admin)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: change_mode(), range(2)))
    assert all(result["mode"] == "wiki" for result in results)
    assert len(_active_init(store, kb_id)) == 1


@pytest.mark.parametrize("active_status", ["queued", "running"])
def test_retry_initial_build_conflicts_until_active_initial_finishes(store, active_status):
    kb_id = _kb(store, chunks=True, mode="wiki")
    key = f"wiki-init:{kb_id}"
    failed = _task(store, kb_id, kind="rebuild_wiki", status="failed", dedupe_key=key)
    active = _task(store, kb_id, kind="rebuild_wiki", status=active_status, dedupe_key=key)

    with store() as db:
        admin = db.execute(select(models.User)).scalar_one()
        with pytest.raises(HTTPException) as exc:
            kbs.retry_task(failed, db, admin)
        assert exc.value.status_code == 409
        db.rollback()
    with store() as db:
        assert db.get(models.Task, failed).status == "failed"
        assert db.get(models.Task, active).status == active_status

    _finish(store, active)
    with store() as db:
        admin = db.execute(select(models.User)).scalar_one()
        response = kbs.retry_task(failed, db, admin)
        assert response["task_id"] == failed and response["reused"] is True
    with store() as db:
        assert db.get(models.Task, failed).status == "queued"
        assert db.get(models.Task, active).status == "success"
    assert [task.id for task in _active_init(store, kb_id)] == [failed]


def test_concurrent_initial_retries_leave_only_one_active(store):
    kb_id = _kb(store, chunks=True, mode="wiki")
    key = f"wiki-init:{kb_id}"
    failed = [
        _task(store, kb_id, kind="rebuild_wiki", status="failed", dedupe_key=key),
        _task(store, kb_id, kind="rebuild_wiki", status="failed", dedupe_key=key),
    ]
    barrier = Barrier(2)

    def retry(task_id):
        with store() as db:
            # Both requests read their failed task before either starts retrying.
            task = db.get(models.Task, task_id)
            admin = db.execute(select(models.User)).scalar_one()
            assert task.status == "failed"
            barrier.wait(timeout=5)
            try:
                kbs.retry_task(task_id, db, admin)
            except HTTPException as exc:
                db.rollback()
                return exc.status_code
            return 200

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(retry, failed))
    assert sorted(results) == [200, 409]
    with store() as db:
        assert sorted(db.get(models.Task, task_id).status for task_id in failed) == ["failed", "queued"]
    assert len(_active_init(store, kb_id)) == 1


def test_retry_racing_mode_switch_leaves_only_one_initial_build(store):
    kb_id = _kb(store, chunks=True)
    failed = _task(store, kb_id, kind="rebuild_wiki", status="failed",
                   dedupe_key=f"wiki-init:{kb_id}")
    barrier = Barrier(2)

    def retry():
        with store() as db:
            task = db.get(models.Task, failed)
            admin = db.execute(select(models.User)).scalar_one()
            assert task.status == "failed"
            barrier.wait(timeout=5)
            try:
                kbs.retry_task(failed, db, admin)
            except HTTPException as exc:
                db.rollback()
                return exc.status_code
            return 200

    def change_mode():
        with store() as db:
            kb = db.get(models.KnowledgeBase, kb_id)
            admin = db.execute(select(models.User)).scalar_one()
            assert kb.mode == "basic"
            barrier.wait(timeout=5)
            return kbs.update_kb(kb_id, schemas.KbUpdateIn(mode="wiki"), db, admin)["mode"]

    with ThreadPoolExecutor(max_workers=2) as executor:
        retry_result = executor.submit(retry)
        switch_result = executor.submit(change_mode)
        assert retry_result.result() in (200, 409)
        assert switch_result.result() == "wiki"
    assert len(_active_init(store, kb_id)) == 1


@pytest.mark.parametrize("prior_status", ["queued", "running"])
def test_claim_orders_initialization_per_kb_but_allows_other_kbs(store, prior_status):
    kb_id = _kb(store, chunks=True)
    other_kb = _kb(store)
    other_kb_2 = _kb(store)
    prior = _task(store, kb_id, kind="reindex_kb", status=prior_status)
    init = _task(store, kb_id, kind="rebuild_wiki", dedupe_key=f"wiki-init:{kb_id}")
    other = _task(store, other_kb)

    if prior_status == "queued":
        assert _claim(store) == prior
    # A second worker may process a different KB while the earlier task runs.
    assert _claim(store) == other
    _finish(store, prior)
    assert _claim(store) == init

    later = _task(store, kb_id)
    other_2 = _task(store, other_kb_2)
    assert _claim(store) == other_2
    assert _claim(store) is None
    with store() as db:
        assert db.get(models.Task, later).status == "queued"


@pytest.mark.parametrize("kind", ["ingest_document", "reindex_kb", "embedding_rebuild"])
@pytest.mark.parametrize("pending_init", [False, True])
def test_prior_work_defers_inline_wiki_to_pending_initial_build(
    store, tmp_path, monkeypatch, kind, pending_init,
):
    kb_id = _kb(store, chunks=True, mode="wiki")
    if kind == "ingest_document":
        prior_id = _ingest_task(store, kb_id, tmp_path / "prior.md")
    else:
        prior_id = _task(store, kb_id, kind=kind)
    if pending_init:
        _task(store, kb_id, kind="rebuild_wiki", dedupe_key=f"wiki-init:{kb_id}")

    builds = []

    def build(db, kb, **kwargs):
        builds.append(kb.id)
        return wiki.WikiBuildResult()

    monkeypatch.setattr(wiki, "build_wiki", build)
    assert worker.run_once("test")
    with store() as db:
        prior = db.get(models.Task, prior_id)
        assert prior.status == models.TaskStatus.success.value, prior.error
    assert len(builds) == (0 if pending_init else 1)

    if pending_init:
        assert worker.run_once("test")
        assert builds == [kb_id]


@pytest.mark.parametrize("changed", ["basic", "pages"])
def test_worker_skips_initial_build_if_preconditions_changed(store, monkeypatch, changed):
    kb_id = _kb(store, chunks=True, mode="wiki")
    task_id = _task(store, kb_id, kind="rebuild_wiki", dedupe_key=f"wiki-init:{kb_id}")
    if changed == "basic":
        # Model the mode changing after enqueue, independently of router cancellation policy.
        with store() as db:
            db.get(models.KnowledgeBase, kb_id).mode = "basic"
            db.commit()
    else:
        _page(store, kb_id)

    calls = []
    monkeypatch.setattr(worker.wiki, "build_wiki", lambda *args, **kwargs: calls.append(True))
    assert worker.run_once("test")
    assert not calls
    with store() as db:
        task = db.get(models.Task, task_id)
        assert task.status == models.TaskStatus.success.value, task.error


def test_interrupted_initial_build_retries_after_creating_a_page(store, monkeypatch):
    kb_id = _kb(store, chunks=True, mode="wiki")
    task_id = _task(store, kb_id, kind="rebuild_wiki", dedupe_key=f"wiki-init:{kb_id}")
    attempts = []

    def build(db, kb, **kwargs):
        attempts.append(kb.id)
        if len(attempts) == 1:
            db.add(models.WikiPage(kb_id=kb.id, slug="interrupted", title="Partial page"))
            db.commit()  # build_wiki commits pages independently before a later failure
            raise RuntimeError("interrupted after first page")
        return wiki.WikiBuildResult()

    monkeypatch.setattr(wiki, "build_wiki", build)
    assert worker.run_once("test")
    with store() as db:
        task = db.get(models.Task, task_id)
        assert task.status == models.TaskStatus.failed.value
        assert json.loads(task.payload)["initial_wiki_started"] is True
        assert db.execute(select(models.WikiPage.id).where(
            models.WikiPage.kb_id == kb_id)).first()
        taskflow.requeue(db, task)
        db.commit()
        assert json.loads(task.payload)["initial_wiki_started"] is True

    assert worker.run_once("test")
    assert attempts == [kb_id, kb_id]
    with store() as db:
        task = db.get(models.Task, task_id)
        assert task.status == models.TaskStatus.success.value, task.error
        assert json.loads(task.payload)["initial_wiki_started"] is True


def test_switch_during_running_ingest_still_builds_first_pages(store, tmp_path, monkeypatch):
    kb_id = _kb(store)
    ingest_id = _ingest_task(store, kb_id, tmp_path / "source.md")

    original_embed = llm.embed
    switched = []

    def switch_while_embedding(db, texts, **kwargs):
        if not switched:
            switched.append(True)
            _switch(store, kb_id, "wiki")
            assert len(_active_init(store, kb_id)) == 1
        return original_embed(db, texts, **kwargs)

    monkeypatch.setattr(llm, "embed", switch_while_embedding)
    assert worker.run_once("test")
    with store() as db:
        task = db.get(models.Task, ingest_id)
        assert task.status == models.TaskStatus.success.value, task.error
    assert switched
    assert worker.run_once("test")
    with store() as db:
        assert serializers.wiki_ready(db, kb_id)
        init = db.execute(select(models.Task).where(
            models.Task.dedupe_key == f"wiki-init:{kb_id}")).scalar_one()
        assert init.status == models.TaskStatus.success.value, init.error


@pytest.mark.parametrize("failure_before_switch", [True, False], ids=[
    "failed-before-switch", "fails-while-init-waits",
])
def test_initial_build_preserves_ingest_failure(store, tmp_path, failure_before_switch):
    kb_id = _kb(store, chunks=True)
    ingest_id = _ingest_task(store, kb_id, tmp_path / "missing.md", missing=True)

    if not failure_before_switch:
        _switch(store, kb_id, "wiki")
        init = _active_init(store, kb_id)
        assert len(init) == 1
        init_id = init[0].id

    # The existing indexed document keeps the KB usable when this ingest fails.
    assert worker.run_once("test")
    with store() as db:
        ingest_task = db.get(models.Task, ingest_id)
        failed_doc_id = ingest_task.doc_id
        assert ingest_task.status == models.TaskStatus.failed.value
        assert "missing.md" in ingest_task.error
        assert db.get(models.Document, failed_doc_id).status == models.KbStatus.failed.value
        kb = db.get(models.KnowledgeBase, kb_id)
        assert kb.status == models.KbStatus.partial.value
        assert "missing.md" in kb.status_detail
        if not failure_before_switch:
            assert db.get(models.Task, init_id).status == models.TaskStatus.queued.value

    if failure_before_switch:
        _switch(store, kb_id, "wiki")
        init = _active_init(store, kb_id)
        assert len(init) == 1
        init_id = init[0].id

    assert worker.run_once("test")
    with store() as db:
        init_task = db.get(models.Task, init_id)
        assert init_task.status == models.TaskStatus.success.value, init_task.error
        assert serializers.wiki_ready(db, kb_id)
        kb = db.get(models.KnowledgeBase, kb_id)
        assert kb.status == models.KbStatus.partial.value
        assert "失败" in kb.status_detail or "fail" in kb.status_detail.lower()
        failed_doc = db.get(models.Document, failed_doc_id)
        assert failed_doc.status == models.KbStatus.failed.value
        assert "missing.md" in failed_doc.status_detail
        assert db.execute(select(func.count(models.Chunk.id)).where(
            models.Chunk.kb_id == kb_id)).scalar_one() > 0


@pytest.mark.parametrize("kind", ["reindex_kb", "embedding_rebuild"])
def test_initial_build_preserves_full_index_failure(store, monkeypatch, kind):
    kb_id = _kb(store, chunks=True)
    rebuild_id = _task(store, kb_id, kind=kind)

    def fail_rebuild(db, task, *, stats):
        raise RuntimeError("full index rebuild unavailable")

    monkeypatch.setattr(worker.ingest, "reindex_kb", fail_rebuild)
    assert worker.run_once("test")
    with store() as db:
        rebuild = db.get(models.Task, rebuild_id)
        assert rebuild.status == models.TaskStatus.failed.value
        assert rebuild.finished_at is not None
        assert "full index rebuild unavailable" in rebuild.error
        assert db.get(models.KnowledgeBase, kb_id).status == models.KbStatus.partial.value
        docs = db.execute(select(models.Document).where(models.Document.kb_id == kb_id)).scalars().all()
        assert docs and all(doc.status == models.KbStatus.ready.value for doc in docs)

    _switch(store, kb_id, "wiki")
    init = _active_init(store, kb_id)
    assert len(init) == 1
    assert worker.run_once("test")
    with store() as db:
        init_task = db.get(models.Task, init[0].id)
        assert init_task.status == models.TaskStatus.success.value, init_task.error
        assert serializers.wiki_ready(db, kb_id)
        kb = db.get(models.KnowledgeBase, kb_id)
        assert kb.status == models.KbStatus.partial.value
        assert "失败" in kb.status_detail or "fail" in kb.status_detail.lower()
        docs = db.execute(select(models.Document).where(models.Document.kb_id == kb_id)).scalars().all()
        assert docs and all(doc.status == models.KbStatus.ready.value for doc in docs)


@pytest.mark.parametrize(("failed_kind", "success_kind"), [
    ("reindex_kb", "embedding_rebuild"),
    ("embedding_rebuild", "reindex_kb"),
])
def test_initial_build_ignores_full_index_failure_superseded_by_success(
    store, failed_kind, success_kind,
):
    kb_id = _kb(store, chunks=True)
    # A retried older task can finish after a newer task, despite its lower ID.
    success_id = _task(store, kb_id, kind=success_kind, status="success")
    failed_id = _task(store, kb_id, kind=failed_kind, status="failed")
    assert success_id < failed_id
    finished_at = models.utcnow()
    with store() as db:
        failed = db.get(models.Task, failed_id)
        failed.finished_at = finished_at - timedelta(minutes=5)
        failed.error = "full index rebuild unavailable"
        success = db.get(models.Task, success_id)
        success.finished_at = finished_at
        db.commit()

    _switch(store, kb_id, "wiki")
    init = _active_init(store, kb_id)
    assert len(init) == 1
    assert worker.run_once("test")
    with store() as db:
        failed = db.get(models.Task, failed_id)
        success = db.get(models.Task, success_id)
        assert failed.finished_at < success.finished_at
        init_task = db.get(models.Task, init[0].id)
        assert init_task.status == models.TaskStatus.success.value, init_task.error
        assert serializers.wiki_ready(db, kb_id)
        kb = db.get(models.KnowledgeBase, kb_id)
        assert kb.status == models.KbStatus.ready.value
        assert "full index rebuild unavailable" not in kb.status_detail


def test_indexed_basic_kb_builds_wiki_offline_after_switch(store):
    kb_id = _kb(store, chunks=True)
    _switch(store, kb_id, "wiki")
    tasks = _active_init(store, kb_id)
    assert len(tasks) == 1
    task_id = tasks[0].id

    # This isolated DB has no configured models, so normal offline fallback is used.
    assert worker.run_once("test")
    with store() as db:
        task = db.get(models.Task, task_id)
        assert task.status == models.TaskStatus.success.value, task.error
        assert db.get(models.KnowledgeBase, kb_id).status == models.KbStatus.ready.value
        assert serializers.wiki_ready(db, kb_id)
        assert db.execute(select(func.count(models.WikiSection.id)).where(
            models.WikiSection.kb_id == kb_id)).scalar_one() > 0
        assert db.execute(select(func.count(models.WikiEvidence.id)).where(
            models.WikiEvidence.kb_id == kb_id)).scalar_one() > 0
