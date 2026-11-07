"""Generation must revalidate every Map input, including unreported sources.

A model can carry information from an entire Map shard into a topic's summary,
although it reports only some chunk IDs.  These tests keep the real Map/merge/
build pipeline and delete a source through the HTTP route during page generation.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing, contextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app import config, models, security
from app.db import Base
from app.routers import kbs, wiki as wiki_router
from app.services import chunking, llm, wiki, worker


TITLE = "Generated policy"
SECRET = "map_source_secret_7359"
OLD_CONTENT = "## 已审核的旧页面\n\n这份旧内容只使用仍然有效的来源，必须保留。"


@pytest.fixture
def race_env(tmp_path, monkeypatch):
    """Use separate connections to a temporary WAL database, as in production."""
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'wiki-input-race.sqlite3').as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 2},
    )

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(conn, _):
        conn.execute("PRAGMA foreign_keys=ON")

    with engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA journal_mode=WAL").scalar().lower() == "wal"
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    request_connections = []
    app = FastAPI()
    app.include_router(kbs.router, prefix="/api")
    app.include_router(wiki_router.router, prefix="/api")

    def request_db():
        with sessions() as db:
            request_connections.append(db.connection().connection.driver_connection)
            yield db

    app.dependency_overrides[kbs.get_db] = request_db
    # Both routers and the authentication dependency import the same get_db.
    assert wiki_router.get_db is kbs.get_db
    monkeypatch.setattr(security, "_TOKEN_KEY", b"wiki-input-race-test-key")
    monkeypatch.setattr(config, "WIKI_SHARD_CHAR_BUDGET", 100_000)
    monkeypatch.setattr(llm, "embed", lambda *args, **kwargs: [])
    try:
        with TestClient(app) as client:
            yield sessions, client, request_connections
    finally:
        engine.dispose()


def _prepare(env, tmp_path, *, count, secret_no, old_source_no):
    sessions, client, _ = env
    with sessions() as db:
        admin = models.User(username="admin", password_hash="unused", role="admin", status="active")
        reader = models.User(username="reader", password_hash="unused", role="employee", status="active")
        kb = models.KnowledgeBase(name="input-race", mode="wiki", status="ready")
        db.add_all([admin, reader, kb])
        db.flush()
        db.add(models.KbGrant(kb_id=kb.id, user_id=reader.id, level="read"))
        sources = []
        for number in range(1, count + 1):
            text = f"source_{number:02d}_marker " + (SECRET if number == secret_no else "公开制度内容")
            path = tmp_path / f"source-{number}.md"
            path.write_text(text, encoding="utf-8")
            doc = models.Document(kb_id=kb.id, filename=path.name, status="ready")
            db.add(doc)
            db.flush()
            version = models.DocumentVersion(
                doc_id=doc.id, version_no=1, storage_path=str(path), status="success",
            )
            db.add(version)
            db.flush()
            doc.current_version_id = version.id
            chunk = models.Chunk(
                kb_id=kb.id, doc_id=doc.id, doc_version_id=version.id,
                ordinal=0, text=text, heading=f"Source {number}", locator=f"第 {number} 段",
                keywords=text, chunk_hash=f"source-{number}",
            )
            db.add(chunk)
            db.flush()
            sources.append({"doc": doc.id, "version": version.id, "chunk": chunk.id, "path": path})

        # The target page is unlocked and links only to a source that survives.
        # This catches both a leaked replacement and destructive overwrite on failure.
        old_source = sources[old_source_no - 1]
        page = models.WikiPage(
            kb_id=kb.id, slug=chunking.slugify(TITLE), title=TITLE,
            summary="旧摘要", status="published", human_locked=False,
        )
        db.add(page)
        db.flush()
        old_version = models.WikiPageVersion(
            page_id=page.id, version_no=1, content_md=OLD_CONTENT,
            summary="旧摘要", origin="generated",
        )
        db.add(old_version)
        db.flush()
        page.current_version_id = old_version.id
        db.add(models.WikiEvidence(
            kb_id=kb.id, page_version_id=old_version.id,
            chunk_id=old_source["chunk"], doc_id=old_source["doc"],
            doc_version_id=old_source["version"], quote="旧页面的有效证据", locator="旧位置",
        ))
        db.commit()
        case = {
            "kb": kb.id, "page": page.id, "old_version": old_version.id,
            "sources": sources, "slug": page.slug,
            "admin": {"Authorization": f"Bearer {security.issue_token(admin.id, admin.role)}"},
            "reader": {"Authorization": f"Bearer {security.issue_token(reader.id, reader.role)}"},
        }
    page_url = f"/api/kbs/{case['kb']}/wiki/pages/{case['slug']}"
    assert client.get(page_url, headers=case["reader"]).json()["version"]["id"] == case["old_version"]
    assert client.delete(f"/api/documents/{sources[0]['doc']}", headers=case["reader"]).status_code == 403
    return case


def _model(monkeypatch, *, shards, secret_no, on_page):
    """Return real Map JSON, then a page contaminated via its topic summary."""
    calls = []

    def chat(db, messages, **kwargs):
        prompt = messages[0]["content"]
        if kwargs.get("json_mode"):
            numbers, reported = shards[len(calls)]
            for number in numbers:
                assert f"[{number}] Source {number}" in prompt
                assert f"source_{number:02d}_marker" in prompt
            has_secret = secret_no in numbers
            assert (SECRET in prompt) is has_secret
            calls.append((numbers, reported))
            return json.dumps({"topics": [{
                "title": TITLE,
                "summary": f"模型归纳包含 {SECRET}" if has_secret else "",
                "chunk_ids": reported,
            }]})

        assert len(calls) == len(shards), "all Map calls must precede page generation"
        assert SECRET in prompt, "the Map summary must reach page generation"
        context = prompt.split("资料片段：", 1)[1]
        merged_ids = list(dict.fromkeys(number for _, reported in shards for number in reported))
        expected_context = merged_ids[:wiki.MAX_TOPIC_CHUNK_IDS][:wiki.CHUNKS_PER_TOPIC]
        for number in expected_context:
            assert f"[{number}] Source {number}" in context
        if secret_no not in expected_context:
            assert SECRET not in context, "the leak should be carried by the topic summary"
        calls.append("page")
        on_page(db)
        cited = shards[0][1][0]
        return f"## 根据主题整理\n\n{SECRET} " + "这是模型根据归纳结果整理出来的页面内容。" * 4 + f" [{cited}]"

    monkeypatch.setattr(llm, "chat", chat)
    return calls


def _delete_during_page(env, case, number, *, replace_chunk=False):
    sessions, client, request_connections = env
    source = case["sources"][number - 1]
    state = {}

    def delete(db):
        build_connection = db.connection().connection.driver_connection
        previous_requests = len(request_connections)
        response = client.delete(f"/api/documents/{source['doc']}", headers=case["admin"])
        assert response.status_code == 200, response.text
        assert response.json()["ok"] is True
        assert response.json()["rebuild_task_id"] is not None
        assert len(request_connections) == previous_requests + 1
        assert request_connections[-1] is not build_connection
        assert not source["path"].exists()
        # The route committed before the model returns; a third session sees it.
        with sessions() as observer:
            assert observer.get(models.Document, source["doc"]).deleted is True
            assert observer.get(models.Chunk, source["chunk"]) is None

        if replace_chunk:
            # SQLite can reuse a deleted highest ROWID.  A live chunk with the
            # same ID but a different document/version is not the same input.
            replacement_path = source["path"].with_name("replacement.md")
            replacement_path.write_text("replacement content", encoding="utf-8")
            with sessions() as other:
                doc = models.Document(kb_id=case["kb"], filename=replacement_path.name, status="ready")
                other.add(doc)
                other.flush()
                version = models.DocumentVersion(
                    doc_id=doc.id, version_no=1, storage_path=str(replacement_path), status="success",
                )
                other.add(version)
                other.flush()
                doc.current_version_id = version.id
                chunk = models.Chunk(
                    kb_id=case["kb"], doc_id=doc.id, doc_version_id=version.id,
                    text="replacement content", heading="Replacement", locator="新来源",
                )
                other.add(chunk)
                other.flush()
                assert chunk.id == source["chunk"], "the deleted maximum SQLite ROWID must be reused"
                assert (doc.id, version.id) != (source["doc"], source["version"])
                state["replacement"] = (chunk.id, doc.id, version.id)
                other.commit()
        state["deleted"] = True

    return delete, state


def _build(sessions, case):
    """Keep the production per-page commits; only roll back a failed build."""
    with sessions() as db:
        try:
            result = wiki.build_wiki(db, db.get(models.KnowledgeBase, case["kb"]))
            db.commit()
        except RuntimeError as exc:
            db.rollback()
            return None, exc
        return result, None


def _assert_rejected_and_old_page_preserved(env, case, error):
    sessions, client, _ = env
    url = f"/api/kbs/{case['kb']}/wiki/pages/{case['slug']}"
    response = client.get(url, headers=case["reader"])
    assert response.status_code == 200, response.text
    assert SECRET not in response.text, "deleted Map input leaked to a read-only user"
    page = response.json()
    assert page["version"]["id"] == case["old_version"]
    assert page["version"]["content_md"] == OLD_CONTENT
    tree = client.get(f"/api/kbs/{case['kb']}/wiki/tree", headers=case["reader"])
    assert tree.status_code == 200 and SECRET not in tree.text
    history = client.get(url + "/versions", headers=case["reader"])
    assert history.status_code == 200 and SECRET not in history.text
    assert [v["id"] for v in history.json()["items"]] == [case["old_version"]]
    with sessions() as observer:
        old_page = observer.get(models.WikiPage, case["page"])
        assert old_page.current_version_id == case["old_version"]
        versions = observer.execute(select(models.WikiPageVersion).where(
            models.WikiPageVersion.page_id == case["page"])).scalars().all()
        assert [v.id for v in versions] == [case["old_version"]]
    assert error is not None, "generation must detect the changed source"
    assert "来源" in str(error), str(error)


def test_unreported_source_deleted_during_page_generation(race_env, tmp_path, monkeypatch):
    case = _prepare(race_env, tmp_path, count=2, secret_no=1, old_source_no=2)
    delete, state = _delete_during_page(race_env, case, 1)
    calls = _model(monkeypatch, shards=[([1, 2], [2])], secret_no=1, on_page=delete)

    _, error = _build(race_env[0], case)

    assert state.get("deleted") and calls[-1] == "page"
    _assert_rejected_and_old_page_preserved(race_env, case, error)


def test_unreported_source_deleted_during_map(race_env, tmp_path, monkeypatch):
    case = _prepare(race_env, tmp_path, count=2, secret_no=1, old_source_no=2)
    delete, state = _delete_during_page(race_env, case, 1)
    calls = _model(monkeypatch, shards=[([1, 2], [2])], secret_no=1, on_page=lambda db: None)
    real_chat = llm.chat

    def delete_after_map_reads_input(db, messages, **kwargs):
        result = real_chat(db, messages, **kwargs)
        if kwargs.get("json_mode"):
            delete(db)
        return result

    monkeypatch.setattr(llm, "chat", delete_after_map_reads_input)
    _, error = _build(race_env[0], case)

    assert state.get("deleted") and calls[-1] == "page"
    _assert_rejected_and_old_page_preserved(race_env, case, error)


def test_thirteenth_reported_source_deleted_after_merge_truncation(race_env, tmp_path, monkeypatch):
    case = _prepare(race_env, tmp_path, count=13, secret_no=13, old_source_no=1)
    delete, state = _delete_during_page(race_env, case, 13)
    calls = _model(monkeypatch, shards=[(list(range(1, 14)), list(range(1, 14)))],
                   secret_no=13, on_page=delete)

    _, error = _build(race_env[0], case)

    assert state.get("deleted") and calls[-1] == "page"
    _assert_rejected_and_old_page_preserved(race_env, case, error)


def test_unreported_source_from_second_shard_survives_topic_merge(race_env, tmp_path, monkeypatch):
    monkeypatch.setattr(wiki, "TOPIC_SAMPLE_SIZE", 2)
    case = _prepare(race_env, tmp_path, count=4, secret_no=3, old_source_no=1)
    delete, state = _delete_during_page(race_env, case, 3)
    calls = _model(monkeypatch, shards=[([1, 2], [1]), ([3, 4], [4])],
                   secret_no=3, on_page=delete)

    _, error = _build(race_env[0], case)

    assert state.get("deleted") and calls[-1] == "page" and len(calls) == 3
    _assert_rejected_and_old_page_preserved(race_env, case, error)


def test_reused_chunk_id_does_not_hide_a_changed_source(race_env, tmp_path, monkeypatch):
    case = _prepare(race_env, tmp_path, count=2, secret_no=2, old_source_no=1)
    delete, state = _delete_during_page(race_env, case, 2, replace_chunk=True)
    calls = _model(monkeypatch, shards=[([1, 2], [2])], secret_no=2, on_page=delete)

    _, error = _build(race_env[0], case)

    assert state.get("deleted") and state.get("replacement") and calls[-1] == "page"
    _assert_rejected_and_old_page_preserved(race_env, case, error)


@pytest.mark.parametrize(("count", "secret_no", "reported", "expected"), [
    (2, 1, [2], [2]),
    (13, 13, list(range(1, 14)), list(range(1, 13))),
], ids=["unreported-source", "thirteenth-id-truncated"])
def test_unchanged_inputs_generate_with_existing_evidence_display(
    race_env, tmp_path, monkeypatch, count, secret_no, reported, expected,
):
    case = _prepare(race_env, tmp_path, count=count, secret_no=secret_no, old_source_no=1)
    calls = _model(monkeypatch, shards=[(list(range(1, count + 1)), reported)],
                   secret_no=secret_no, on_page=lambda db: None)

    result, error = _build(race_env[0], case)

    assert error is None and result.pages_updated == 1
    assert calls[-1] == "page"
    url = f"/api/kbs/{case['kb']}/wiki/pages/{case['slug']}"
    response = race_env[1].get(url, headers=case["reader"])
    assert response.status_code == 200, response.text
    page = response.json()
    assert page["version"]["id"] != case["old_version"]
    assert SECRET in page["version"]["content_md"]
    assert {e["chunk_id"] for e in page["evidence"]} == {
        case["sources"][number - 1]["chunk"] for number in expected
    }


def test_large_unchanged_map_inputs_fit_sqlite_variable_limit(race_env, monkeypatch):
    """A merged topic must revalidate 1001 inputs despite SQLite's 999-bind limit."""
    sessions, _, _ = race_env
    engine = sessions.kw["bind"]
    limit_category = getattr(sqlite3, "SQLITE_LIMIT_VARIABLE_NUMBER", None)
    if (limit_category is None or not hasattr(sqlite3.Connection, "setlimit")
            or not hasattr(sqlite3.Connection, "getlimit")):
        pytest.skip("sqlite3 connection setlimit/getlimit is unavailable")

    count = 1001
    with sessions() as db:
        kb = models.KnowledgeBase(name="large-input-race", mode="wiki", status="ready")
        db.add(kb)
        db.flush()
        docs = [models.Document(kb_id=kb.id, filename=f"source-{i}.md", status="ready")
                for i in range(count)]
        db.add_all(docs)
        db.flush()
        versions = [models.DocumentVersion(
            doc_id=doc.id, version_no=1, storage_path="unused-offline-source", status="success",
        ) for doc in docs]
        db.add_all(versions)
        db.flush()
        chunks = []
        for i, (doc, version) in enumerate(zip(docs, versions)):
            doc.current_version_id = version.id
            chunks.append(models.Chunk(
                kb_id=kb.id, doc_id=doc.id, doc_version_id=version.id,
                text=f"第 {i} 份文档的有效制度内容。", heading="规模主题",
                locator=f"第 {i} 份", chunk_hash=f"scale-{i}",
            ))
        db.add_all(chunks)
        db.flush()
        chunk_ids = {chunk.id for chunk in chunks}
        assert len(chunk_ids) == len({doc.id for doc in docs}) == count
        kb_id = kb.id
        db.commit()

    model_calls = []

    def offline_chat(db, messages, **kwargs):
        model_calls.append("map" if kwargs.get("json_mode") else "page")
        raise llm.ModelUnavailable("offline scale regression")

    monkeypatch.setattr(llm, "chat", offline_chat)
    validation_sizes = []
    validation_batches = []
    validated_ids = set()
    real_validate = wiki._validate_sources

    # Bind the Session to one checked-out connection so its commits cannot
    # silently switch to a different connection with a higher variable limit.
    with engine.connect() as connection:
        raw = connection.connection.driver_connection
        original_limit = raw.getlimit(limit_category)
        raw.setlimit(limit_category, 999)
        try:
            assert raw.getlimit(limit_category) == 999

            def observe_validation(db, snapshots):
                assert db.connection().connection.driver_connection is raw
                assert raw.getlimit(limit_category) == 999
                validation_sizes.append(len(snapshots))
                assert set(snapshots) == chunk_ids

                def record_query(conn, cursor, statement, parameters, context, executemany):
                    if statement.lstrip().lower().startswith("select"):
                        assert conn.connection.driver_connection is raw
                        assert not executemany
                        validation_batches.append(len(parameters))
                        validated_ids.update(value for value in parameters if value in chunk_ids)

                event.listen(connection, "before_cursor_execute", record_query)
                try:
                    return real_validate(db, snapshots)
                finally:
                    event.remove(connection, "before_cursor_execute", record_query)

            monkeypatch.setattr(wiki, "_validate_sources", observe_validation)
            with sessions(bind=connection) as db:
                loaded = wiki._chunks_for_kb(db, kb_id)
                assert len(loaded) == len({chunk.doc_id for chunk in loaded}) == count
                assert {chunk.id for chunk in loaded} == chunk_ids
                result = wiki.build_wiki(db, db.get(models.KnowledgeBase, kb_id))
                db.commit()
                assert result.pages_created == 1 and result.pages_skipped == 0
                page = db.execute(select(models.WikiPage).where(
                    models.WikiPage.kb_id == kb_id)).scalar_one()
                assert page.status == "published" and page.current_version_id is not None
        finally:
            raw.setlimit(limit_category, original_limit)

    assert model_calls.count("map") > 1 and model_calls.count("page") == 1
    assert validation_sizes == [count], "all merged Map inputs must reach the publication check"
    assert len(validation_batches) >= 2 and max(validation_batches) <= 999
    assert validated_ids == chunk_ids, "every Map input must be checked in the database"


class _MapDependencyNotPersisted(AssertionError):
    """A published version lost an unreported source from its durable dependencies."""


@pytest.mark.xfail(
    strict=True,
    raises=_MapDependencyNotPersisted,
    reason="Map input dependencies are not persisted after publication; deleting an unreported source cannot hide the page",
)
def test_deleting_unreported_map_input_after_publication_should_hide_page(race_env, tmp_path, monkeypatch):
    case = _prepare(race_env, tmp_path, count=2, secret_no=1, old_source_no=2)
    calls = _model(monkeypatch, shards=[([1, 2], [2])], secret_no=1, on_page=lambda db: None)
    result, error = _build(race_env[0], case)
    assert error is None and result.pages_updated == 1 and calls[-1] == "page"

    client = race_env[1]
    url = f"/api/kbs/{case['kb']}/wiki/pages/{case['slug']}"
    before = client.get(url, headers=case["reader"])
    assert before.status_code == 200 and SECRET in before.text
    assert {e["doc_id"] for e in before.json()["evidence"]} == {case["sources"][1]["doc"]}
    deleted = client.delete(f"/api/documents/{case['sources'][0]['doc']}", headers=case["admin"])
    assert deleted.status_code == 200 and deleted.json()["rebuild_task_id"] is not None
    with race_env[0]() as observer:
        assert observer.get(models.Document, case["sources"][0]["doc"]).deleted is True
        assert observer.get(models.Chunk, case["sources"][0]["chunk"]) is None

    # No queued rebuild has run. A persisted Map dependency should hide this
    # version immediately, even though A was not reported as display evidence.
    after = client.get(url, headers=case["reader"])
    assert after.status_code in (200, 404), after.text
    if after.status_code == 200:
        assert SECRET in after.text
        raise _MapDependencyNotPersisted("expected 404; the deleted unreported Map source remains visible")
    assert after.status_code == 404


def test_same_source_ids_with_changed_chunk_text_are_rejected(race_env, tmp_path, monkeypatch):
    case = _prepare(race_env, tmp_path, count=2, secret_no=1, old_source_no=2)
    sessions = race_env[0]
    source = case["sources"][0]
    state = {}

    def change_during_page(db):
        build_connection = db.connection().connection.driver_connection
        with sessions() as other:
            assert other.connection().connection.driver_connection is not build_connection
            chunk = other.get(models.Chunk, source["chunk"])
            assert (chunk.id, chunk.doc_id, chunk.doc_version_id) == (
                source["chunk"], source["doc"], source["version"],
            )
            assert SECRET in chunk.text
            chunk.text = "同一分块、同一版本的新正文，原来的内容已被替换。"
            other.commit()
        with sessions() as observer:
            updated = observer.get(models.Chunk, source["chunk"])
            assert (updated.id, updated.doc_id, updated.doc_version_id) == (
                source["chunk"], source["doc"], source["version"],
            )
            assert SECRET not in updated.text
        state["changed"] = True

    calls = _model(monkeypatch, shards=[([1, 2], [2])], secret_no=1, on_page=change_during_page)
    _, error = _build(sessions, case)

    assert state.get("changed") and calls[-1] == "page"
    _assert_rejected_and_old_page_preserved(race_env, case, error)


def test_reindex_worker_does_not_commit_page_after_source_changes(race_env, tmp_path, monkeypatch):
    """A caught Wiki failure must not survive the reindex worker's final commit."""
    sessions, client, _ = race_env
    case = _prepare(race_env, tmp_path, count=2, secret_no=1, old_source_no=2)
    with sessions() as db:
        page_before = db.execute(select(models.WikiPage.__table__).where(
            models.WikiPage.id == case["page"])).one()
        versions_before = db.execute(select(models.WikiPageVersion.__table__).where(
            models.WikiPageVersion.page_id == case["page"]).order_by(
                models.WikiPageVersion.id)).all()
        evidence_before = db.execute(select(models.WikiEvidence.__table__).where(
            models.WikiEvidence.page_version_id == case["old_version"]).order_by(
                models.WikiEvidence.id)).all()
        task = models.Task(
            kb_id=case["kb"], kind=models.TaskKind.reindex_kb.value,
            status=models.TaskStatus.queued.value, dedupe_key=f"reindex:{case['kb']}",
        )
        db.add(task)
        db.commit()
        task_id = task.id

    @contextmanager
    def local_session_scope(*, role):
        assert role in ("worker", "progress")
        with sessions() as db:
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise

    monkeypatch.setattr(worker, "session_scope", local_session_scope)
    monkeypatch.setattr(llm, "active_embedding", lambda db: (None, None, "race-test", 3))
    monkeypatch.setattr(llm, "embed", lambda db, texts, **kwargs: [[0.1, 0.2, 0.3] for _ in texts])
    delete, state = _delete_during_page(race_env, case, 1)

    def delete_while_page_is_generated(db):
        with sessions() as observer:
            assert observer.get(models.Task, task_id).status == models.TaskStatus.running.value
        delete(db)

    calls = _model(monkeypatch, shards=[([1, 2], [2])], secret_no=1,
                   on_page=delete_while_page_is_generated)
    assert worker.run_once("reindex-source-race")
    assert state.get("deleted") and calls[-1] == "page"

    with sessions() as observer:
        task = observer.get(models.Task, task_id)
        assert task.status == models.TaskStatus.success.value, task.error
        result = json.loads(task.result)
        assert "来源" in result.get("wiki_error", ""), result
        assert observer.get(models.KnowledgeBase, case["kb"]).status == models.KbStatus.partial.value
        page_after = observer.execute(select(models.WikiPage.__table__).where(
            models.WikiPage.id == case["page"])).one()
        versions_after = observer.execute(select(models.WikiPageVersion.__table__).where(
            models.WikiPageVersion.page_id == case["page"]).order_by(
                models.WikiPageVersion.id)).all()
        evidence_after = observer.execute(select(models.WikiEvidence.__table__).where(
            models.WikiEvidence.page_version_id == case["old_version"]).order_by(
                models.WikiEvidence.id)).all()

    url = f"/api/kbs/{case['kb']}/wiki/pages/{case['slug']}"
    reader_page = client.get(url, headers=case["reader"])
    assert reader_page.status_code == 200, reader_page.text
    assert SECRET not in reader_page.text, "reindex committed deleted source content for a reader"
    assert page_after == page_before
    assert versions_after == versions_before
    assert evidence_after == evidence_before
    _assert_rejected_and_old_page_preserved(race_env, case, RuntimeError(result["wiki_error"]))


def test_human_edit_wins_over_unreported_source_deletion(race_env, tmp_path, monkeypatch):
    """A concurrent human lock makes stale generated content safe to skip."""
    sessions, client, request_connections = race_env
    case = _prepare(race_env, tmp_path, count=2, secret_no=1, old_source_no=2)
    url = f"/api/kbs/{case['kb']}/wiki/pages/{case['slug']}"
    human_content = "## 人工校对\n\n这段人工正文只依据仍然有效的第二份来源，不使用已删除的资料。"
    with sessions() as db:
        task = models.Task(
            kb_id=case["kb"], kind=models.TaskKind.rebuild_wiki.value,
            status=models.TaskStatus.queued.value, dedupe_key=f"wiki:{case['kb']}",
        )
        db.add(task)
        db.commit()
        task_id = task.id

    @contextmanager
    def local_session_scope(*, role):
        assert role in ("worker", "progress")
        with sessions() as db:
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise

    monkeypatch.setattr(worker, "session_scope", local_session_scope)
    delete, state = _delete_during_page(race_env, case, 1)
    human = {}

    def edit_then_delete(db):
        with sessions() as observer:
            assert observer.get(models.Task, task_id).status == models.TaskStatus.running.value
        build_connection = db.connection().connection.driver_connection
        previous_requests = len(request_connections)
        edit = client.put(url, headers=case["admin"], json={"content_md": human_content})
        assert edit.status_code == 200, edit.text
        assert edit.json()["human_locked"] is True
        assert len(request_connections) == previous_requests + 1
        assert request_connections[-1] is not build_connection
        human["version"] = edit.json()["version_id"]
        with sessions() as observer:
            page = observer.get(models.WikiPage, case["page"])
            assert page.human_locked and page.current_version_id == human["version"]
            assert observer.get(models.WikiPageVersion, human["version"]).content_md == human_content
            assert set(observer.execute(select(models.WikiEvidence.doc_id).where(
                models.WikiEvidence.page_version_id == human["version"])).scalars()) == {
                    case["sources"][1]["doc"]
                }
        # The human edit is already committed before the separate delete request.
        delete(db)

    calls = _model(monkeypatch, shards=[([1, 2], [2])], secret_no=1, on_page=edit_then_delete)
    assert worker.run_once("edit-delete-source-race")
    assert human.get("version") and state.get("deleted") and calls[-1] == "page"

    with sessions() as observer:
        task = observer.get(models.Task, task_id)
        assert task.status == models.TaskStatus.success.value, task.error
        result = json.loads(task.result)
        assert (result["created"], result["updated"], result["skipped"]) == (0, 0, 1), result
        assert any("人工校对" in note for note in result["notes"]), result
        page = observer.get(models.WikiPage, case["page"])
        assert page.human_locked and page.status == models.WikiPageStatus.needs_review.value
        assert page.current_version_id == human["version"]
        version = observer.get(models.WikiPageVersion, human["version"])
        assert version.origin == "human" and version.content_md == human_content
        version_ids = set(observer.execute(select(models.WikiPageVersion.id).where(
            models.WikiPageVersion.page_id == case["page"])).scalars())
        assert version_ids == {case["old_version"], human["version"]}
        assert set(observer.execute(select(models.WikiEvidence.doc_id).where(
            models.WikiEvidence.page_version_id == human["version"])).scalars()) == {
                case["sources"][1]["doc"]
            }

    page = client.get(url, headers=case["reader"])
    assert page.status_code == 200, page.text
    assert SECRET not in page.text and page.json()["version"]["content_md"] == human_content
    history = client.get(url + "/versions", headers=case["reader"])
    assert history.status_code == 200 and SECRET not in history.text
    assert {version["id"] for version in history.json()["items"]} == version_ids


def test_source_validation_error_preserves_pending_kb_status(race_env, tmp_path, monkeypatch):
    """A caller may catch a source error and commit other pending changes."""
    sessions = race_env[0]
    case = _prepare(race_env, tmp_path, count=2, secret_no=1, old_source_no=2)
    delete, state = _delete_during_page(race_env, case, 1)

    def change_status_then_delete(db):
        assert db.autoflush is False
        kb = db.get(models.KnowledgeBase, case["kb"])
        assert kb.status == models.KbStatus.ready.value
        kb.status = models.KbStatus.partial.value
        # The status is pending in the generator Session, not yet in SQLite.
        with sessions() as observer:
            assert observer.get(models.KnowledgeBase, case["kb"]).status == models.KbStatus.ready.value
        delete(db)
        with sessions() as observer:
            assert observer.get(models.KnowledgeBase, case["kb"]).status == models.KbStatus.ready.value

    calls = _model(monkeypatch, shards=[([1, 2], [2])], secret_no=1,
                   on_page=change_status_then_delete)
    with sessions() as db:
        try:
            wiki.build_wiki(db, db.get(models.KnowledgeBase, case["kb"]))
        except RuntimeError as exc:
            error = exc
        else:
            pytest.fail("generation must detect the deleted Map input")
        # Like ingest.reindex_kb, the caller catches the error and commits;
        # no test rollback is allowed to mask the state of the Session.
        db.commit()

    assert state.get("deleted") and calls[-1] == "page"
    with sessions() as observer:
        assert observer.get(models.KnowledgeBase, case["kb"]).status == models.KbStatus.partial.value
    _assert_rejected_and_old_page_preserved(race_env, case, error)


def _page_snapshot(sessions, page_id):
    """Capture the page and all its versions, evidence and indexed sections."""
    with sessions() as db:
        versions = select(models.WikiPageVersion.id).where(models.WikiPageVersion.page_id == page_id)
        return (
            db.execute(select(models.WikiPage.__table__).where(
                models.WikiPage.id == page_id)).one(),
            db.execute(select(models.WikiPageVersion.__table__).where(
                models.WikiPageVersion.page_id == page_id).order_by(models.WikiPageVersion.id)).all(),
            db.execute(select(models.WikiEvidence.__table__).where(
                models.WikiEvidence.page_version_id.in_(versions)).order_by(models.WikiEvidence.id)).all(),
            db.execute(select(models.WikiSection.__table__).where(
                models.WikiSection.page_id == page_id).order_by(models.WikiSection.id)).all(),
        )


def _add_old_section(db, case):
    db.add(models.WikiSection(
        kb_id=case["kb"], page_id=case["page"], page_version_id=case["old_version"],
        ordinal=0, heading_path="旧章节", text=OLD_CONTENT, keywords="旧页面",
    ))


def test_unreported_source_deleted_during_section_embedding(race_env, tmp_path, monkeypatch):
    """An input deleted during section preparation is checked under the publish lock."""
    sessions = race_env[0]
    case = _prepare(race_env, tmp_path, count=2, secret_no=1, old_source_no=2)
    with sessions() as db:
        _add_old_section(db, case)
        db.commit()
    before = _page_snapshot(sessions, case["page"])
    delete, state = _delete_during_page(race_env, case, 1)
    calls = _model(monkeypatch, shards=[([1, 2], [2])], secret_no=1, on_page=lambda db: None)

    def delete_during_embed(db, texts, **kwargs):
        assert calls[-1] == "page" and SECRET in " ".join(texts)
        assert not state.get("deleted")
        delete(db)
        return []

    monkeypatch.setattr(llm, "embed", delete_during_embed)
    real_validate = wiki._validate_sources
    validation = []

    def validate_with_lock(db, snapshots):
        assert state.get("deleted")
        assert set(snapshots) == {source["chunk"] for source in case["sources"]}
        assert db.connection().connection.driver_connection.in_transaction
        # An independent connection cannot write while this validation runs.
        # This tests the real SQLite lock, not just the order of helper calls.
        database = sessions.kw["bind"].url.database
        with closing(sqlite3.connect(database, timeout=0)) as other:
            with pytest.raises(sqlite3.OperationalError, match="database is locked"):
                other.execute("UPDATE knowledge_bases SET status = status WHERE id = ?", (case["kb"],))
        validation.append(True)
        return real_validate(db, snapshots)

    monkeypatch.setattr(wiki, "_validate_sources", validate_with_lock)
    _, error = _build(sessions, case)

    assert state.get("deleted") and calls[-1] == "page" and validation == [True]
    assert _page_snapshot(sessions, case["page"]) == before
    _assert_rejected_and_old_page_preserved(race_env, case, error)


def test_pending_review_not_committed_after_source_deleted_during_section_embedding(
    race_env, tmp_path, monkeypatch,
):
    """A caller committing a caught error must not persist deferred review changes."""
    sessions = race_env[0]
    case = _prepare(race_env, tmp_path, count=2, secret_no=1, old_source_no=2)
    locked_title = "人工审核的库存指南"
    locked_content = "## 人工审核\n\n这些内容依据仍然存在的第二份资料，必须保持原状。"
    source = case["sources"][1]
    with sessions() as db:
        _add_old_section(db, case)
        page = models.WikiPage(
            kb_id=case["kb"], slug=chunking.slugify(locked_title), title=locked_title,
            summary="人工摘要", status=models.WikiPageStatus.published.value, human_locked=True,
        )
        db.add(page)
        db.flush()
        version = models.WikiPageVersion(
            page_id=page.id, version_no=1, content_md=locked_content,
            summary="人工摘要", origin="human",
        )
        db.add(version)
        db.flush()
        page.current_version_id = version.id
        db.add_all([
            models.WikiEvidence(
                kb_id=case["kb"], page_version_id=version.id, chunk_id=source["chunk"],
                doc_id=source["doc"], doc_version_id=source["version"], quote="人工证据", locator="旧位置",
            ),
            models.WikiSection(
                kb_id=case["kb"], page_id=page.id, page_version_id=version.id,
                ordinal=0, heading_path="人工章节", text=locked_content, keywords="人工审核",
            ),
        ])
        db.commit()
        locked_id, locked_slug = page.id, page.slug

    locked_before = _page_snapshot(sessions, locked_id)
    generated_before = _page_snapshot(sessions, case["page"])
    assert locked_before[0].status == models.WikiPageStatus.published.value
    assert locked_before[0].human_locked is True
    delete, state = _delete_during_page(race_env, case, 1)
    calls = _model(monkeypatch, shards=[([1, 2], [2])], secret_no=1, on_page=lambda db: None)
    real_chat = llm.chat

    def map_locked_page_first(db, messages, **kwargs):
        result = real_chat(db, messages, **kwargs)
        if kwargs.get("json_mode"):
            topics = json.loads(result)
            topics["topics"].insert(0, {
                "title": locked_title, "summary": "人工整理的资料", "chunk_ids": [2],
            })
            return json.dumps(topics)
        return result

    monkeypatch.setattr(llm, "chat", map_locked_page_first)
    progress = []

    def on_progress(stage, done, total):
        progress.append((stage, done, total))
        if (stage, done, total) == ("pages", 1, 2):
            assert _page_snapshot(sessions, locked_id) == locked_before

    def delete_during_embed(db, texts, **kwargs):
        assert ("pages", 1, 2) in progress, "the locked topic must be processed first"
        assert calls[-1] == "page" and SECRET in " ".join(texts)
        assert _page_snapshot(sessions, locked_id) == locked_before
        delete(db)
        return []

    monkeypatch.setattr(llm, "embed", delete_during_embed)
    with sessions() as db:
        try:
            wiki.build_wiki(db, db.get(models.KnowledgeBase, case["kb"]), on_progress=on_progress)
        except RuntimeError as exc:
            error = exc
        else:
            pytest.fail("generation must detect the deleted Map input")
        # Model a reindex caller which catches the error and commits anyway.
        # A rollback here would hide a premature pending_review update.
        db.commit()

    assert state.get("deleted") and calls[-1] == "page"
    assert ("pages", 1, 2) in progress and ("pages", 2, 2) not in progress
    assert _page_snapshot(sessions, locked_id) == locked_before
    assert _page_snapshot(sessions, case["page"]) == generated_before
    _assert_rejected_and_old_page_preserved(race_env, case, error)
    url = f"/api/kbs/{case['kb']}/wiki/pages/{locked_slug}"
    response = race_env[1].get(url, headers=case["reader"])
    assert response.status_code == 200, response.text
    assert response.json()["version"]["content_md"] == locked_content
    assert SECRET not in response.text
