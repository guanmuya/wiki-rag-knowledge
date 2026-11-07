"""模式切换触发的首次 Wiki：停止、删除保护和后续工作。"""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from app import models
from app.services import wiki, worker
from test_wiki_initial_delete_race import _source_state
from test_wiki_stop_workflow import (
    DOC,
    FakeModels,
    _assert_page_complete,
    _assert_ready_and_route,
    _running_worker,
    _stop,
    _task,
    _tree,
    env,  # noqa: F401 -- the shared fixture uses an isolated WAL database
)


def _basic_kb(client, headers):
    response = client.post("/api/kbs", headers=headers["admin"],
                           json={"name": "首次 Wiki 停止", "mode": "basic"})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _upload(client, headers, kb_id, filename):
    response = client.post(
        f"/api/kbs/{kb_id}/documents", headers=headers["admin"],
        files=[("files", (filename, DOC.encode("utf-8"), "text/markdown"))],
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["items"]) == 1, response.text
    return response.json()["items"][0]


def _switch_to_wiki(client, headers, sessions, kb_id):
    response = client.patch(f"/api/kbs/{kb_id}", headers=headers["admin"],
                            json={"mode": "wiki"})
    assert response.status_code == 200, response.text
    assert response.json()["mode"] == "wiki"
    with sessions() as db:
        tasks = db.execute(select(models.Task).where(
            models.Task.kb_id == kb_id,
            models.Task.dedupe_key == f"{models.WIKI_INITIAL_TASK_PREFIX}{kb_id}",
            models.Task.status == "queued",
        )).scalars().all()
        assert len(tasks) == 1
        assert tasks[0].kind == "rebuild_wiki"
        return tasks[0].id


def _chunk_snapshot(sessions, doc_id):
    with sessions() as db:
        return db.execute(select(models.Chunk.__table__).where(
            models.Chunk.doc_id == doc_id).order_by(models.Chunk.id)).all()


def _all_source_topics(monkeypatch):
    # 主题引用每个实际分块，能验证之后入库的来源确实进入了 Wiki。
    monkeypatch.setattr(wiki, "_map_topics", lambda _db, entries, **_kwargs: [
        {"title": title, "summary": title, "chunk_ids": [idx for idx, _ in entries]}
        for title in ("Travel rules", "Account security", "Incident response")
    ])


def test_queued_initial_stop_releases_document_delete_protection(env, monkeypatch):
    client, headers, sessions = env
    fake = FakeModels(monkeypatch)
    kb_id = _basic_kb(client, headers)
    source = _upload(client, headers, kb_id, "indexed.md")
    assert worker.run_once("index-basic") is True
    assert _task(client, headers, source["task_id"])["status"] == "success"
    before = _source_state(sessions, kb_id)
    assert before[2]
    with sessions() as db:
        path = Path(db.get(models.DocumentVersion, source["version_id"]).storage_path)
    original = path.read_bytes()

    initial_id = _switch_to_wiki(client, headers, sessions, kb_id)
    queued = _task(client, headers, initial_id)
    assert queued["status"] == "queued" and queued["can_stop_wiki"] is True
    assert queued["run_token"] == ""
    blocked = client.delete(f"/api/documents/{source['doc_id']}", headers=headers["admin"])
    assert blocked.status_code == 409, blocked.text
    assert _source_state(sessions, kb_id) == before and path.read_bytes() == original

    stopped = _stop(client, headers, initial_id, run_token=queued["run_token"])
    assert stopped.status_code == 200, stopped.text
    task = _task(client, headers, initial_id)
    assert task["status"] == "stopped" and not task["can_stop_wiki"] and task["finished_at"]
    with sessions() as db:
        row = db.get(models.Task, initial_id)
        assert row.wiki_stop_requested and not row.wiki_active and row.attempts == 0
    assert _source_state(sessions, kb_id) == before
    assert worker.run_once("stopped-queued-initial") is False
    assert fake.chat_calls == 0 and _tree(client, headers, kb_id) == []

    deleted = client.delete(f"/api/documents/{source['doc_id']}", headers=headers["admin"])
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["ok"] is True and deleted.json()["rebuild_task_id"] is not None
    assert not path.exists()
    with sessions() as db:
        assert db.get(models.Document, source["doc_id"]).deleted
        assert not db.execute(select(models.Chunk.id).where(
            models.Chunk.doc_id == source["doc_id"])).first()
        rebuild = db.get(models.Task, deleted.json()["rebuild_task_id"])
        assert rebuild.kind == "rebuild_wiki" and rebuild.dedupe_key == f"wiki:{kb_id}"
        assert rebuild.status == "queued"
        assert db.get(models.Task, initial_id).status == "stopped"


@pytest.mark.parametrize("prior_state", ["queued", "running"])
def test_stopped_initial_does_not_restart_from_prior_ingest_but_new_ingest_builds(
    env, monkeypatch, prior_state,
):
    client, headers, sessions = env
    fake = FakeModels(monkeypatch, block_embed_at=1 if prior_state == "running" else None)
    _all_source_topics(monkeypatch)
    kb_id = _basic_kb(client, headers)
    prior = _upload(client, headers, kb_id, "prior.md")

    def switch_and_stop():
        initial_id = _switch_to_wiki(client, headers, sessions, kb_id)
        assert prior["task_id"] < initial_id
        task = _task(client, headers, initial_id)
        assert task["status"] == "queued" and task["can_stop_wiki"] is True
        response = _stop(client, headers, initial_id, run_token=task["run_token"])
        assert response.status_code == 200, response.text
        assert _task(client, headers, initial_id)["status"] == "stopped"
        return initial_id

    if prior_state == "running":
        with _running_worker(fake) as (thread, results):
            assert fake.embed_entered.wait(15), "先前入库未进入分块向量化"
            assert _task(client, headers, prior["task_id"])["status"] == "running"
            initial_id = switch_and_stop()
            fake.embed_release.set()
            thread.join(20)
            assert not thread.is_alive() and results == [True]
    else:
        initial_id = switch_and_stop()
        assert worker.run_once("prior-ingest-after-stop") is True

    finished = _task(client, headers, prior["task_id"])
    assert finished["status"] == "success", finished
    assert "wiki" not in finished["result"] and "wiki_error" not in finished["result"]
    assert fake.chat_calls == 0 and _tree(client, headers, kb_id) == []
    prior_chunks = _chunk_snapshot(sessions, prior["doc_id"])
    assert prior_chunks
    with sessions() as db:
        doc = db.get(models.Document, prior["doc_id"])
        assert doc.current_version_id == prior["version_id"] and doc.status == "ready"
        assert db.get(models.DocumentVersion, prior["version_id"]).status == "success"
    assert worker.run_once("no-hidden-initial-build") is False
    assert _task(client, headers, initial_id)["status"] == "stopped"
    _assert_ready_and_route(client, headers, sessions, kb_id, ready=False)

    later = _upload(client, headers, kb_id, "later.md")
    assert later["task_id"] > initial_id
    assert worker.run_once("new-ingest-after-stop") is True
    later_task = _task(client, headers, later["task_id"])
    assert later_task["status"] == "success" and "wiki" in later_task["result"], later_task
    assert _task(client, headers, initial_id)["status"] == "stopped"
    assert _chunk_snapshot(sessions, prior["doc_id"]) == prior_chunks
    later_chunks = _chunk_snapshot(sessions, later["doc_id"])
    assert later_chunks and fake.chat_calls == 3
    pages = _tree(client, headers, kb_id)
    assert len(pages) == 3
    for page in pages:
        version_id = _assert_page_complete(client, headers, sessions, kb_id, page)
        with sessions() as db:
            sources = set(db.execute(select(models.WikiEvidence.doc_id).where(
                models.WikiEvidence.page_version_id == version_id)).scalars())
        assert sources == {prior["doc_id"], later["doc_id"]}
    _assert_ready_and_route(client, headers, sessions, kb_id, ready=True)
    assert worker.run_once("no-extra-initial-build") is False


def test_running_initial_stop_keeps_complete_page_and_retry_only_builds_wiki(env, monkeypatch):
    client, headers, sessions = env
    fake = FakeModels(monkeypatch, block_chat_at=2)
    kb_id = _basic_kb(client, headers)
    source = _upload(client, headers, kb_id, "source.md")
    spare = _upload(client, headers, kb_id, "spare.md")
    assert worker.run_once("index-source") is True
    assert worker.run_once("index-spare") is True
    for item in (source, spare):
        assert _task(client, headers, item["task_id"])["status"] == "success"
    before = _source_state(sessions, kb_id)
    source_chunks = _chunk_snapshot(sessions, source["doc_id"])
    assert source_chunks and _chunk_snapshot(sessions, spare["doc_id"])
    with sessions() as db:
        spare_path = Path(db.get(models.DocumentVersion, spare["version_id"]).storage_path)
    spare_bytes = spare_path.read_bytes()

    initial_id = _switch_to_wiki(client, headers, sessions, kb_id)
    assert client.delete(f"/api/documents/{spare['doc_id']}", headers=headers["admin"]).status_code == 409
    with _running_worker(fake) as (thread, results):
        assert fake.chat_entered.wait(15), "首次 Wiki 未完成第一页并进入第二页"
        active = _task(client, headers, initial_id)
        assert active["status"] == "running" and active["can_stop_wiki"] and active["run_token"]
        pages = _tree(client, headers, kb_id)
        assert len(pages) == 1
        first_page = pages[0]
        first_version = _assert_page_complete(client, headers, sessions, kb_id, first_page)
        blocked = client.delete(f"/api/documents/{spare['doc_id']}", headers=headers["admin"])
        assert blocked.status_code == 409, blocked.text
        assert _source_state(sessions, kb_id) == before and spare_path.read_bytes() == spare_bytes

        response = _stop(client, headers, initial_id, run_token=active["run_token"])
        assert response.status_code == 200, response.text
        pending = _task(client, headers, initial_id)
        assert pending["status"] == "running" and pending["wiki_stop_requested"] is True
        assert pending["can_stop_wiki"] is False and thread.is_alive()
        blocked = client.delete(f"/api/documents/{spare['doc_id']}", headers=headers["admin"])
        assert blocked.status_code == 409, blocked.text
        assert _source_state(sessions, kb_id) == before and spare_path.read_bytes() == spare_bytes
        assert _assert_page_complete(client, headers, sessions, kb_id, first_page) == first_version
        _assert_ready_and_route(client, headers, sessions, kb_id, ready=False)
        fake.chat_release.set()
        thread.join(20)
        assert not thread.is_alive() and results == [True]

    done = _task(client, headers, initial_id)
    assert done["status"] == "stopped" and done["result"]["wiki_stopped"] is True, done
    assert done["finished_at"] and fake.chat_calls == 2
    assert _source_state(sessions, kb_id) == before
    assert len(_tree(client, headers, kb_id)) == 1
    assert _assert_page_complete(client, headers, sessions, kb_id, first_page) == first_version
    _assert_ready_and_route(client, headers, sessions, kb_id, ready=False)
    with sessions() as db:
        original_links = db.execute(select(models.WikiEvidence.__table__).where(
            models.WikiEvidence.page_version_id == first_version).order_by(models.WikiEvidence.id)).all()
        assert original_links and all(link.chunk_id for link in original_links)

    # 停止完成后解锁删除；先停止删除触发的独立重建，以单独验证首次任务重试。
    deleted = client.delete(f"/api/documents/{spare['doc_id']}", headers=headers["admin"])
    assert deleted.status_code == 200 and deleted.json()["ok"], deleted.text
    assert not spare_path.exists()
    deletion_rebuild = deleted.json()["rebuild_task_id"]
    assert deletion_rebuild is not None and deletion_rebuild != initial_id
    assert _stop(client, headers, deletion_rebuild).status_code == 200
    assert _chunk_snapshot(sessions, source["doc_id"]) == source_chunks

    retry = client.post(f"/api/tasks/{initial_id}/retry", headers=headers["admin"])
    assert retry.status_code == 200, retry.text
    assert retry.json()["task_id"] == initial_id and retry.json()["reused"] is True
    queued = _task(client, headers, initial_id)
    assert queued["status"] == "queued" and queued["can_stop_wiki"] is True
    assert queued["run_token"] != active["run_token"]
    blocked = client.delete(f"/api/documents/{source['doc_id']}", headers=headers["admin"])
    assert blocked.status_code == 409, blocked.text
    assert _chunk_snapshot(sessions, source["doc_id"]) == source_chunks
    assert worker.run_once("retry-initial-wiki-only") is True
    retried = _task(client, headers, initial_id)
    assert retried["status"] == "success", retried
    assert retried["result"]["created"] + retried["result"]["updated"] == 3
    assert fake.chat_calls == 5
    assert _chunk_snapshot(sessions, source["doc_id"]) == source_chunks
    with sessions() as db:
        doc = db.get(models.Document, source["doc_id"])
        assert doc.current_version_id == source["version_id"] and doc.status == "ready"
        assert db.get(models.DocumentVersion, source["version_id"]).status == "success"
        old_version = db.get(models.WikiPageVersion, first_version)
        assert old_version and old_version.content_md
        links = db.execute(select(models.WikiEvidence.__table__).where(
            models.WikiEvidence.page_version_id == first_version).order_by(models.WikiEvidence.id)).all()
        assert links == original_links
    assert len(_tree(client, headers, kb_id)) == 3
    _assert_ready_and_route(client, headers, sessions, kb_id, ready=True)
    assert worker.run_once("no-other-work-after-retry") is False
