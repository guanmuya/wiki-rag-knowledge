"""Deleting sources during and immediately after the first Wiki generation."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event, local

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import event, select

from app import models, serializers
from app.routers import kbs, wiki as wiki_router
from app.services import retrieval, wiki, worker
from test_wiki_mode_switch import _active_init, _claim, _kb, _switch, _task, store  # noqa: F401


@pytest.fixture
def wal_store(store):
    """Use separate SQLite connections with the production WAL/FK settings."""
    engine = store.kw["bind"]

    @event.listens_for(engine, "connect")
    def foreign_keys(conn, _):
        conn.execute("PRAGMA foreign_keys=ON")

    engine.dispose()
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA journal_mode=WAL")
    return store


@pytest.fixture
def client(wal_store):
    app = FastAPI()
    app.include_router(kbs.router, prefix="/api")
    app.include_router(wiki_router.router, prefix="/api")

    def request_db():
        with wal_store() as db:
            yield db

    def admin_user():
        with wal_store() as db:
            return db.execute(select(models.User).where(models.User.role == models.Role.admin.value)).scalar_one()

    app.dependency_overrides[kbs.get_db] = request_db
    app.dependency_overrides[kbs.get_current_user] = admin_user
    with TestClient(app) as test_client:
        yield test_client


def _source(sessions, tmp_path, name, *, mode="basic"):
    kb_id = _kb(sessions, chunks=True, mode=mode)
    path = tmp_path / f"{name}.md"
    path.write_text(f"original {name}", encoding="utf-8")
    with sessions() as db:
        doc = db.execute(select(models.Document).where(models.Document.kb_id == kb_id)).scalar_one()
        db.get(models.DocumentVersion, doc.current_version_id).storage_path = str(path)
        doc_id = doc.id
        db.commit()
    return kb_id, doc_id, path


def _add_remaining_source(sessions, kb_id, tmp_path):
    path = tmp_path / "remaining.md"
    path.write_text("remaining source", encoding="utf-8")
    with sessions() as db:
        doc = models.Document(kb_id=kb_id, filename=path.name, status="ready")
        db.add(doc)
        db.flush()
        version = models.DocumentVersion(doc_id=doc.id, version_no=1,
                                         storage_path=str(path), status="success")
        db.add(version)
        db.flush()
        doc.current_version_id = version.id
        db.add(models.Chunk(kb_id=kb_id, doc_id=doc.id, doc_version_id=version.id,
                            text="Remaining content about travel policy. " * 10,
                            heading="Travel policy", locator="section 2",
                            keywords="travel policy", chunk_hash="remaining"))
        db.commit()
        return doc.id


def _source_state(sessions, kb_id):
    """Capture complete persisted document, version and chunk rows."""
    with sessions() as db:
        docs = db.execute(select(models.Document.__table__).where(
            models.Document.kb_id == kb_id).order_by(models.Document.id)).all()
        versions = db.execute(select(models.DocumentVersion.__table__).join(
            models.Document, models.Document.id == models.DocumentVersion.doc_id
        ).where(models.Document.kb_id == kb_id).order_by(models.DocumentVersion.id)).all()
        chunks = db.execute(select(models.Chunk.__table__).where(
            models.Chunk.kb_id == kb_id).order_by(models.Chunk.id)).all()
        return docs, versions, chunks


def _assert_deleted(sessions, kb_id, doc_id, path, response):
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    assert not path.exists()
    with sessions() as db:
        doc = db.get(models.Document, doc_id)
        assert doc.deleted is True
        assert doc.status == models.KbStatus.empty.value
        assert not db.execute(select(models.Chunk.id).where(models.Chunk.doc_id == doc_id)).first()
        if response.json()["rebuild_task_id"] is not None:
            task = db.get(models.Task, response.json()["rebuild_task_id"])
            assert task.kb_id == kb_id
            assert task.kind == models.TaskKind.rebuild_wiki.value
            assert task.status == models.TaskStatus.queued.value
            assert task.dedupe_key == f"wiki:{kb_id}"


@pytest.mark.parametrize("status", ["queued", "running"])
def test_active_initial_generation_blocks_delete_without_mutation(wal_store, client, tmp_path, status):
    kb_id, doc_id, path = _source(wal_store, tmp_path, "blocked")
    _switch(wal_store, kb_id, "wiki")
    initial = _active_init(wal_store, kb_id)
    assert len(initial) == 1
    task_id = initial[0].id
    if status == "running":
        assert _claim(wal_store) == task_id
    with wal_store() as db:
        assert db.get(models.Task, task_id).status == status
        task_ids = db.execute(select(models.Task.id).order_by(models.Task.id)).scalars().all()
    before = _source_state(wal_store, kb_id)
    original_file = path.read_bytes()

    response = client.delete(f"/api/documents/{doc_id}")

    assert response.status_code == 409, response.text
    assert _source_state(wal_store, kb_id) == before
    assert path.read_bytes() == original_file
    with wal_store() as db:
        assert db.get(models.Task, task_id).status == status
        assert db.execute(select(models.Task.id).order_by(models.Task.id)).scalars().all() == task_ids


def _generate_with_delete_during_mapping(sessions, tmp_path, monkeypatch):
    kb_id, doc_id, path = _source(sessions, tmp_path, "first")
    remaining_id = _add_remaining_source(sessions, kb_id, tmp_path)
    before = _source_state(sessions, kb_id)
    _switch(sessions, kb_id, "wiki")
    initial = _active_init(sessions, kb_id)
    assert len(initial) == 1
    task_id = initial[0].id
    original_map = wiki._map_topics
    attempts = []

    def delete_while_mapping(db, entries, **kwargs):
        with sessions() as db2:
            admin = db2.execute(select(models.User)).scalar_one()
            task_status = db2.get(models.Task, task_id).status
            try:
                kbs.delete_document(doc_id, db2, admin)
            except HTTPException as exc:
                attempts.append((task_status, exc.status_code))
            else:
                attempts.append((task_status, 200))
        return original_map(db, entries, **kwargs)

    monkeypatch.setattr(wiki, "_map_topics", delete_while_mapping)
    assert worker.run_once("delete-race")
    monkeypatch.setattr(wiki, "_map_topics", original_map)

    with sessions() as db:
        task = db.get(models.Task, task_id)
        assert task.status == models.TaskStatus.success.value, task.error
        assert serializers.wiki_ready(db, kb_id)
    assert attempts and all(attempt == ("running", 409) for attempt in attempts)
    assert _source_state(sessions, kb_id) == before
    assert path.read_text(encoding="utf-8") == "original first"
    return kb_id, doc_id, remaining_id, path


def test_initial_generation_rejects_delete_and_allows_retry_after_success(
    wal_store, client, tmp_path, monkeypatch,
):
    kb_id, doc_id, remaining_id, path = _generate_with_delete_during_mapping(
        wal_store, tmp_path, monkeypatch,
    )

    response = client.delete(f"/api/documents/{doc_id}")

    _assert_deleted(wal_store, kb_id, doc_id, path, response)
    assert response.json()["rebuild_task_id"] is not None
    with wal_store() as db:
        assert db.get(models.Document, remaining_id).deleted is False
        assert db.execute(select(models.Chunk.id).where(models.Chunk.doc_id == remaining_id)).first()


@pytest.mark.parametrize("status", ["queued", "running"])
def test_initial_task_in_another_kb_does_not_block_delete(wal_store, client, tmp_path, status):
    other_kb, other_doc, other_path = _source(wal_store, tmp_path, "protected")
    target_kb, doc_id, path = _source(wal_store, tmp_path, "independent", mode="wiki")
    _switch(wal_store, other_kb, "wiki")
    task_id = _active_init(wal_store, other_kb)[0].id
    if status == "running":
        assert _claim(wal_store) == task_id
    before_other = _source_state(wal_store, other_kb)

    response = client.delete(f"/api/documents/{doc_id}")

    _assert_deleted(wal_store, target_kb, doc_id, path, response)
    assert response.json()["rebuild_task_id"] is not None
    assert _source_state(wal_store, other_kb) == before_other
    assert other_path.read_text(encoding="utf-8") == "original protected"
    with wal_store() as db:
        assert db.get(models.Document, other_doc).deleted is False
        assert db.get(models.Task, task_id).status == status


@pytest.mark.parametrize("status", ["queued", "running"])
@pytest.mark.parametrize("kind,key_format", [
    ("rebuild_wiki", "wiki:{kb_id}"),
    ("reindex_kb", "reindex:{kb_id}"),
    ("ingest_document", "ingest:{kb_id}"),
    ("rebuild_wiki", "wiki-init:{kb_id}:different"),
])
def test_active_non_initial_tasks_do_not_block_delete(
    wal_store, client, tmp_path, status, kind, key_format,
):
    kb_id, doc_id, path = _source(wal_store, tmp_path, "ordinary", mode="wiki")
    task_id = _task(wal_store, kb_id, kind=kind, status=status,
                    dedupe_key=key_format.format(kb_id=kb_id))

    response = client.delete(f"/api/documents/{doc_id}")

    _assert_deleted(wal_store, kb_id, doc_id, path, response)
    assert response.json()["rebuild_task_id"] is not None
    with wal_store() as db:
        assert db.get(models.Task, task_id).status == status


@pytest.mark.parametrize("status", ["success", "failed", "partial"])
def test_terminal_initial_task_does_not_block_delete(wal_store, client, tmp_path, status):
    kb_id, doc_id, path = _source(wal_store, tmp_path, "finished", mode="wiki")
    task_id = _task(wal_store, kb_id, kind="rebuild_wiki", status=status,
                    dedupe_key=f"wiki-init:{kb_id}")

    response = client.delete(f"/api/documents/{doc_id}")

    _assert_deleted(wal_store, kb_id, doc_id, path, response)
    assert response.json()["rebuild_task_id"] is not None
    with wal_store() as db:
        assert db.get(models.Task, task_id).status == status


def _is_write(statement):
    sql = " ".join(statement.lower().split())
    return sql.startswith(("begin immediate", "update ", "delete ", "insert ", "replace "))


def _is_task_read(statement):
    sql = " " + " ".join(statement.lower().split()) + " "
    return sql.lstrip().startswith("select ") and (" from tasks " in sql or " join tasks " in sql)


@pytest.mark.parametrize("first", ["switch", "claim"])
def test_delete_serializes_with_inflight_switch_or_claim(wal_store, tmp_path, first):
    kb_id, doc_id, path = _source(wal_store, tmp_path, first)
    if first == "claim":
        _switch(wal_store, kb_id, "wiki")
        task_id = _active_init(wal_store, kb_id)[0].id
    before = _source_state(wal_store, kb_id)
    engine = wal_store.kw["bind"]
    thread = local()
    first_has_lock, release_first = Event(), Event()
    delete_reached, delete_done = Event(), Event()
    first_delete_operation = []

    @event.listens_for(engine, "after_cursor_execute")
    def hold_first_write(conn, cursor, statement, parameters, context, executemany):
        sql = " ".join(statement.lower().split())
        wanted = "update knowledge_bases " if first == "switch" else "update tasks "
        if getattr(thread, "role", None) == "first" and sql.startswith(wanted):
            first_has_lock.set()
            if not release_first.wait(10):
                raise AssertionError("timed out waiting to release the first SQLite writer")

    @event.listens_for(engine, "before_cursor_execute")
    def observe_delete(conn, cursor, statement, parameters, context, executemany):
        if getattr(thread, "role", None) != "delete" or delete_reached.is_set():
            return
        if _is_write(statement):
            first_delete_operation.append("write")
            delete_reached.set()
        elif _is_task_read(statement):
            first_delete_operation.append("task read")
            delete_reached.set()

    def first_operation():
        thread.role = "first"
        if first == "switch":
            return _switch(wal_store, kb_id, "wiki")["mode"]
        return _claim(wal_store)

    def delete():
        try:
            with wal_store() as db:
                admin = db.execute(select(models.User)).scalar_one()
                thread.role = "delete"
                try:
                    kbs.delete_document(doc_id, db, admin)
                except HTTPException as exc:
                    db.rollback()
                    return exc.status_code
                return 200
        finally:
            delete_done.set()
            delete_reached.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(first_operation)
        try:
            assert first_has_lock.wait(5), "switch/claim did not acquire its write lock"
            delete_future = executor.submit(delete)
            assert delete_reached.wait(5), "delete did not reach its task check or write lock"
            assert first_delete_operation == ["write"], first_delete_operation
            assert not delete_done.is_set(), "delete returned while the other writer held the lock"
        finally:
            release_first.set()
        first_result = first_future.result(timeout=10)
        assert delete_future.result(timeout=10) == 409

    assert first_result == ("wiki" if first == "switch" else task_id)
    active = _active_init(wal_store, kb_id)
    assert len(active) == 1
    assert active[0].status == ("queued" if first == "switch" else "running")
    assert _source_state(wal_store, kb_id) == before
    assert path.read_text(encoding="utf-8") == f"original {first}"


def test_delete_that_gets_write_lock_first_precedes_mode_switch(wal_store, tmp_path):
    kb_id, doc_id, path = _source(wal_store, tmp_path, "delete-first")
    engine = wal_store.kw["bind"]
    thread = local()
    delete_has_lock, release_delete, switch_reached_write = Event(), Event(), Event()

    @event.listens_for(engine, "after_cursor_execute")
    def hold_delete_write(conn, cursor, statement, parameters, context, executemany):
        if getattr(thread, "role", None) == "delete" and _is_write(statement) and not delete_has_lock.is_set():
            delete_has_lock.set()
            if not release_delete.wait(10):
                raise AssertionError("timed out waiting to release the delete write lock")

    @event.listens_for(engine, "before_cursor_execute")
    def observe_switch_write(conn, cursor, statement, parameters, context, executemany):
        if getattr(thread, "role", None) == "switch" and _is_write(statement):
            switch_reached_write.set()

    def delete():
        with wal_store() as db:
            admin = db.execute(select(models.User)).scalar_one()
            thread.role = "delete"
            return kbs.delete_document(doc_id, db, admin)

    def switch():
        thread.role = "switch"
        return _switch(wal_store, kb_id, "wiki")

    with ThreadPoolExecutor(max_workers=2) as executor:
        delete_future = executor.submit(delete)
        try:
            assert delete_has_lock.wait(5), "delete did not acquire its write lock"
            switch_future = executor.submit(switch)
            assert switch_reached_write.wait(5), "mode switch did not attempt its write"
            assert not switch_future.done(), "switch returned while delete held the write lock"
        finally:
            release_delete.set()
        assert delete_future.result(timeout=10)["ok"] is True
        assert switch_future.result(timeout=10)["mode"] == "wiki"

    assert not path.exists()
    with wal_store() as db:
        assert db.get(models.Document, doc_id).deleted is True
        assert not db.execute(select(models.Chunk.id).where(models.Chunk.doc_id == doc_id)).first()
    assert not _active_init(wal_store, kb_id)


def test_hidden_retained_page_still_counts_as_an_existing_wiki(wal_store, tmp_path):
    kb_id, deleted_doc_id, _ = _source(wal_store, tmp_path, "hidden-source")
    _add_remaining_source(wal_store, kb_id, tmp_path)
    with wal_store() as db:
        doc = db.get(models.Document, deleted_doc_id)
        doc.deleted = True
        page = models.WikiPage(kb_id=kb_id, slug="retained", title="Retained",
                               status="published", human_locked=True)
        db.add(page)
        db.flush()
        version = models.WikiPageVersion(page_id=page.id, version_no=1, content_md="Retained text")
        db.add(version)
        db.flush()
        page.current_version_id = version.id
        db.add(models.WikiEvidence(kb_id=kb_id, page_version_id=version.id, doc_id=deleted_doc_id))
        db.commit()

    result = _switch(wal_store, kb_id, "wiki")
    assert result["wiki_ready"] is False
    assert result["stats"]["wiki_page_count"] == 1  # admin retains the page for review
    assert not _active_init(wal_store, kb_id)


def test_retry_after_initial_generation_hides_deleted_source_pages_from_readers(
    wal_store, client, tmp_path, monkeypatch,
):
    kb_id, doc_id, remaining_id, path = _generate_with_delete_during_mapping(
        wal_store, tmp_path, monkeypatch,
    )
    with wal_store() as db:
        pages = db.execute(select(models.WikiPage).where(
            models.WikiPage.kb_id == kb_id,
            models.WikiPage.status == models.WikiPageStatus.published.value,
        )).scalars().all()
        source_only_ids = []
        for page in pages:
            evidence_docs = set(db.execute(select(models.WikiEvidence.doc_id).where(
                models.WikiEvidence.page_version_id == page.current_version_id,
            )).scalars())
            if evidence_docs == {doc_id}:
                assert page.human_locked is False
                source_only_ids.append(page.id)
        assert source_only_ids, "initial generation must produce a source-only page"
        assert any(db.execute(select(models.WikiEvidence.id).where(
            models.WikiEvidence.doc_id == remaining_id,
        )).scalars())

    with wal_store() as db:
        slugs = {db.get(models.WikiPage, page_id).slug for page_id in source_only_ids}
        kb = db.get(models.KnowledgeBase, kb_id)
        hits, _ = retrieval.search_wiki(db, kb, "住宿报销", top_n=20, qvec=None)
        assert set(source_only_ids).intersection({hit.page_id for hit in hits})
        reader_ids = {}
        for level in ("read", "manage"):
            reader = models.User(username=f"source-{level}", password_hash="unused", role="employee", status="active")
            db.add(reader)
            db.flush()
            db.add(models.KbGrant(kb_id=kb_id, user_id=reader.id, level=level))
            reader_ids[level] = reader.id
        db.commit()

    def reader_user(reader_id):
        with wal_store() as db:
            return db.get(models.User, reader_id)

    admin_user = client.app.dependency_overrides[kbs.get_current_user]

    def assert_source_visibility():
        # Administrators retain the original pages for review; readers cannot
        # load their current contents or history, even if storage stays published.
        client.app.dependency_overrides[kbs.get_current_user] = admin_user
        admin_tree = client.get(f"/api/kbs/{kb_id}/wiki/tree")
        assert admin_tree.status_code == 200, admin_tree.text
        reviewed = {page["slug"]: page for page in admin_tree.json()["items"]}
        assert slugs.issubset(reviewed)
        assert all(reviewed[slug]["status"] == "needs_review" and
                   reviewed[slug]["status_label"] == "来源已删除，待复核" for slug in slugs)
        for slug in slugs:
            url = f"/api/kbs/{kb_id}/wiki/pages/{slug}"
            page = client.get(url)
            assert page.status_code == 200, page.text
            assert doc_id in {item["doc_id"] for item in page.json()["evidence"]}
            assert client.get(url + "/versions").status_code == 200
        for reader_id in reader_ids.values():
            client.app.dependency_overrides[kbs.get_current_user] = lambda: reader_user(reader_id)
            tree = client.get(f"/api/kbs/{kb_id}/wiki/tree")
            assert tree.status_code == 200, tree.text
            assert slugs.isdisjoint({page["slug"] for page in tree.json()["items"]})
            assert tree.json()["items"], "pages from the remaining source should stay visible"
            for slug in slugs:
                url = f"/api/kbs/{kb_id}/wiki/pages/{slug}"
                assert client.get(url).status_code == 404
                assert client.get(url + "/versions").status_code == 404
        with wal_store() as db:
            kb = db.get(models.KnowledgeBase, kb_id)
            hits, _ = retrieval.search_wiki(db, kb, "住宿报销", top_n=20, qvec=None)
            assert set(source_only_ids).isdisjoint({hit.page_id for hit in hits})

    response = client.delete(f"/api/documents/{doc_id}")
    _assert_deleted(wal_store, kb_id, doc_id, path, response)
    rebuild_id = response.json()["rebuild_task_id"]
    assert rebuild_id is not None
    assert_source_visibility()
    assert worker.run_once("delete-rebuild")
    with wal_store() as db:
        task = db.get(models.Task, rebuild_id)
        assert task.status == models.TaskStatus.success.value, task.error
    assert_source_visibility()
