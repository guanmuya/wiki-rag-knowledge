"""Wiki 停止：真实 HTTP、SQLite 和 worker 的并发工作流回归。"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app import config, db as db_module, models, security
from app.services import llm, retrieval, taskflow, wiki, worker


DOC = """# 员工手册

## 差旅

住宿和交通报销需要提供发票，按流程由主管审核并由财务复核。

## 安全

账户必须启用多因素认证，访问内部系统应遵守最小权限原则。
"""


@pytest.fixture
def env(app_client, tmp_path, monkeypatch):
    """每例独立的 WAL 数据库；HTTP、worker 和停止检查各用独立会话。"""
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'workflow.sqlite3').as_posix()}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _pragmas(connection, _record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    db_module.Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    worker_sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False,
                                   class_=db_module.WorkerSession)
    # get_db 和 session_scope 在调用时读取模块级 sessionmaker；停止检查则持有导入别名。
    monkeypatch.setattr(db_module, "SessionLocal", sessions)
    monkeypatch.setattr(db_module, "WorkerSessionLocal", worker_sessions)
    monkeypatch.setattr(db_module, "ProgressSessionLocal", sessions)
    monkeypatch.setattr(taskflow, "SessionLocal", sessions)
    monkeypatch.setattr(config, "FILES_DIR", tmp_path / "files")
    with sessions() as db:
        db.add_all([
            # 模拟「已改过初始口令的既有账号」：P1-8 之后 must_change_password=True
            # 的账号会被后端拒绝业务接口（这条边界本身由 test_must_change_password 覆盖）。
            models.User(id=1, username="stop-admin", password_hash="unused", role="admin",
                        must_change_password=False),
            models.User(id=2, username="stop-reader", password_hash="unused", role="employee",
                        must_change_password=False),
            models.User(id=3, username="stop-manager", password_hash="unused", role="employee",
                        must_change_password=False),
        ])
        db.commit()
    headers = {
        name: {"Authorization": f"Bearer {security.issue_token(uid, role)}"}
        for name, uid, role in (("admin", 1, "admin"), ("read", 2, "employee"),
                                ("manage", 3, "employee"))
    }
    yield app_client, headers, sessions
    engine.dispose()


class FakeModels:
    """模型调用可在指定页面/向量批次暂停，计费数据仍走真实 stats。"""

    def __init__(self, monkeypatch, *, block_chat_at=None, block_embed_at=None):
        self.chat_entered = threading.Event()
        self.chat_release = threading.Event()
        self.embed_entered = threading.Event()
        self.embed_release = threading.Event()
        self.block_chat_at = block_chat_at
        self.block_embed_at = block_embed_at
        self.chat_calls = 0
        self.embed_calls = 0
        monkeypatch.setattr(wiki, "_map_topics", lambda *args, **kwargs: [
            {"title": "Travel rules", "summary": "travel", "chunk_ids": [1]},
            {"title": "Account security", "summary": "security", "chunk_ids": [1]},
            {"title": "Incident response", "summary": "incident", "chunk_ids": [1]},
        ])
        monkeypatch.setattr(llm, "active_embedding", lambda _db: (None, None, "test:model:2", 2))
        monkeypatch.setattr(llm, "chat", self.chat)
        monkeypatch.setattr(llm, "embed", self.embed)

    def chat(self, _db, _messages, *, stats, **_kwargs):
        self.chat_calls += 1
        stats.record_chat(prompt_tokens=7, completion_tokens=11)
        if self.chat_calls == self.block_chat_at:
            self.chat_entered.set()
            if not self.chat_release.wait(20):
                raise AssertionError("测试未释放 Wiki 页面模型")
        return "## 完整页面\n\n" + "可检索的完整内容和来源说明。" * 5 + " [1]"

    def embed(self, _db, texts, *, stats=None):
        self.embed_calls += 1
        if stats is not None:
            stats.record_embed(texts=len(texts), tokens=13)
        if self.embed_calls == self.block_embed_at:
            self.embed_entered.set()
            if not self.embed_release.wait(20):
                raise AssertionError("测试未释放向量模型")
        return [[0.25, 0.5] for _ in texts]

    def release(self):
        self.chat_release.set()
        self.embed_release.set()


@contextmanager
def _running_worker(fake):
    errors = []
    results = []

    def run():
        try:
            results.append(worker.run_once("wiki-stop-test"))
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run, name="wiki-stop-test")
    thread.start()
    try:
        yield thread, results
    finally:
        fake.release()
        thread.join(timeout=25)
        assert not thread.is_alive(), "worker 未能退出"
        assert not errors, errors


def _recover_in_new_process(tmp_path):
    """以另一 Python 进程连接同一 WAL 库，模拟应用重启时的回收。"""
    child_dir = tmp_path / "child"
    child_env = os.environ.copy()
    child_env.pop("WRK_DATABASE_URL", None)
    child_env.update({
        "WRK_DATA_DIR": str(child_dir),
        "WRK_DB_PATH": str(tmp_path / "workflow.sqlite3"),
        "WRK_SECRET_DIR": str(child_dir / "secrets"),
        "WRK_AGENT_PLUGIN_DIR": str(child_dir / "plugins"),
        "WRK_AGENT_WORKSPACE": str(child_dir / "agent"),
        "WRK_WORKER_ENABLED": "0",
    })
    subprocess.run(
        [sys.executable, "-c",
         "from app.services.worker import recover_interrupted; assert recover_interrupted() == 1"],
        cwd=Path(__file__).resolve().parent.parent, env=child_env, timeout=20, check=True,
    )


def _new_kb(client, headers):
    response = client.post("/api/kbs", headers=headers["admin"],
                           json={"name": "并发停止测试", "mode": "wiki"})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _upload(client, headers, kb_id):
    response = client.post(f"/api/kbs/{kb_id}/documents", headers=headers["admin"],
                           files=[("files", ("source.md", DOC.encode(), "text/markdown"))])
    assert response.status_code == 200, response.text
    assert len(response.json()["items"]) == 1, response.text
    return response.json()["items"][0]


def _seed_index(sessions, kb_id):
    """创建现有索引供重建使用；模型和 Wiki 流程仍由真实 worker 执行。"""
    with sessions() as db:
        kb = db.get(models.KnowledgeBase, kb_id)
        kb.embed_signature, kb.embed_dim = "old:model:2", 2
        doc = models.Document(kb_id=kb_id, filename="existing.md", status="ready")
        db.add(doc)
        db.flush()
        version = models.DocumentVersion(doc_id=doc.id, version_no=1,
                                         storage_path="unused", status="success", chunk_count=1)
        db.add(version)
        db.flush()
        doc.current_version_id = version.id
        db.add(models.Chunk(kb_id=kb_id, doc_id=doc.id, doc_version_id=version.id,
                            ordinal=0, text=DOC, locator="原文第 1 段"))
        db.commit()


def _task(client, headers, task_id):
    response = client.get(f"/api/tasks/{task_id}", headers=headers["admin"])
    assert response.status_code == 200, response.text
    return response.json()


def _stop(client, headers, task_id, *, role="admin", run_token=None):
    """先读取任务代次；首次排队的空字符串也必须显式传给 API。"""
    if run_token is None:
        run_token = _task(client, headers, task_id)["run_token"]
    actor_headers = headers[role] if role is not None else None
    return client.post(f"/api/tasks/{task_id}/stop-wiki", headers=actor_headers,
                       params={"run_token": run_token})


def _tree(client, headers, kb_id):
    response = client.get(f"/api/kbs/{kb_id}/wiki/tree", headers=headers["admin"])
    assert response.status_code == 200, response.text
    return response.json()["items"]


def _assert_ready_and_route(client, headers, sessions, kb_id, *, ready):
    detail_response = client.get(f"/api/kbs/{kb_id}", headers=headers["admin"])
    tree_response = client.get(f"/api/kbs/{kb_id}/wiki/tree", headers=headers["admin"])
    assert detail_response.status_code == tree_response.status_code == 200
    detail, tree = detail_response.json(), tree_response.json()
    assert detail["wiki_ready"] is ready
    assert detail["index"]["wiki_ready"] is ready
    assert tree["wiki_ready"] is ready
    with sessions() as db:
        route, _reason, fallback = retrieval.plan_route(db, db.get(models.KnowledgeBase, kb_id))
    assert (route, fallback) == (("wiki", False) if ready else ("chunk", True))


def _assert_page_complete(client, headers, sessions, kb_id, page):
    response = client.get(f"/api/kbs/{kb_id}/wiki/pages/{page['slug']}", headers=headers["admin"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "published"
    assert body["version"]["content_md"] and body["evidence"]
    assert all(e["chunk_id"] and e["doc_version_id"] for e in body["evidence"])
    with sessions() as db:
        sections = db.execute(select(models.WikiSection).where(
            models.WikiSection.page_version_id == body["version"]["id"])).scalars().all()
        assert sections and all(s.page_id == page["id"] and s.vector_blob for s in sections)
    return body["version"]["id"]


def _evidence_snapshot(sessions, kb_id):
    with sessions() as db:
        rows = db.execute(select(
            models.WikiEvidence.id, models.WikiEvidence.page_version_id,
            models.WikiEvidence.doc_id, models.WikiEvidence.doc_version_id,
            models.WikiEvidence.chunk_id, models.WikiEvidence.quote, models.WikiEvidence.locator,
        ).where(models.WikiEvidence.kb_id == kb_id).order_by(models.WikiEvidence.id)).all()
    return [tuple(row) for row in rows]


def _assert_rebuild_wiki_conflict(response):
    assert response.status_code == 409, response.text
    detail = str(response.json().get("detail", ""))
    assert "wiki" in detail.lower() and "重建" in detail, detail


def test_stop_requires_manage_for_ordinary_and_deletion_rebuilds(env):
    client, headers, sessions = env
    kb_id = _new_kb(client, headers)
    with sessions() as db:
        db.add_all([models.KbGrant(kb_id=kb_id, user_id=2, level="read"),
                    models.KbGrant(kb_id=kb_id, user_id=3, level="manage")])
        db.commit()
    response = client.post(f"/api/kbs/{kb_id}/rebuild", headers=headers["admin"],
                           json={"kind": "wiki"})
    assert response.status_code == 200, response.text
    task_id = response.json()["task_id"]
    initial = _task(client, headers, task_id)
    assert initial["run_token"] == "" and initial["can_stop_wiki"] is True
    assert _stop(client, headers, task_id, role=None, run_token=initial["run_token"]).status_code == 401
    assert _stop(client, headers, task_id, role="read", run_token=initial["run_token"]).status_code == 403
    response = _stop(client, headers, task_id, role="manage", run_token=initial["run_token"])
    assert response.status_code == 200, response.text
    assert _task(client, headers, task_id)["status"] == "stopped"
    assert worker.run_once("queued-stop") is False, "已停止的排队任务不能再被领取"

    doc = _upload(client, headers, kb_id)
    # 删除建立的任务通过真实 HTTP 入口取得；先结束未执行的入库任务以隔离后续断言。
    with sessions() as db:
        task = db.get(models.Task, doc["task_id"])
        task.status = "failed"
        db.commit()
    deleted = client.delete(f"/api/documents/{doc['doc_id']}", headers=headers["admin"])
    assert deleted.status_code == 200, deleted.text
    delete_id = deleted.json()["rebuild_task_id"]
    with sessions() as db:
        assert db.get(models.Task, delete_id).doc_id == doc["doc_id"]
    assert _task(client, headers, delete_id)["can_stop_wiki"] is True
    assert _stop(client, headers, delete_id, role="read").status_code == 403
    stopped = _stop(client, headers, delete_id, role="manage")
    assert stopped.status_code == 200, stopped.text
    assert _task(client, headers, delete_id)["status"] == "stopped"
    assert worker.run_once("queued-deletion-stop") is False
    with sessions() as db:
        task = db.get(models.Task, delete_id)
        assert task.wiki_stop_requested and not task.wiki_active

    # 同样的删除触发任务在 Wiki 阶段运行时应接受停止请求。
    second_doc = _upload(client, headers, kb_id)
    with sessions() as db:
        db.get(models.Task, second_doc["task_id"]).status = "failed"
        db.commit()
    deleted = client.delete(f"/api/documents/{second_doc['doc_id']}", headers=headers["admin"])
    assert deleted.status_code == 200, deleted.text
    running_id = deleted.json()["rebuild_task_id"]
    with sessions() as db:
        task = db.get(models.Task, running_id)
        assert task.doc_id == second_doc["doc_id"]
        task.status = "running"
        task.wiki_active = True
        db.commit()
    assert _task(client, headers, running_id)["can_stop_wiki"] is True
    stopped = _stop(client, headers, running_id, role="manage")
    assert stopped.status_code == 200, stopped.text
    pending = _task(client, headers, running_id)
    assert pending["status"] == "running" and pending["wiki_stop_requested"] is True


def test_stopped_deletion_rebuild_keeps_source_filter_and_live_chunks(env, monkeypatch, tmp_path):
    """仅能隐藏有来源记录的页面；没有历史来源记录无法推断其影响。"""
    client, headers, sessions = env
    FakeModels(monkeypatch)
    kb_id = _new_kb(client, headers)
    docs, pages = {}, {}
    with sessions() as db:
        db.add_all([models.KbGrant(kb_id=kb_id, user_id=2, level="read"),
                    models.KbGrant(kb_id=kb_id, user_id=3, level="manage")])
        for name, token in (("removed", "deletedsecretmarker"),
                            ("live", "survivororangemarker")):
            doc = models.Document(kb_id=kb_id, filename=f"{name}.md", status="ready")
            db.add(doc)
            db.flush()
            version = models.DocumentVersion(doc_id=doc.id, version_no=1,
                                             storage_path=str(tmp_path / f"{name}.md"),
                                             status="success", chunk_count=1)
            db.add(version)
            db.flush()
            doc.current_version_id = version.id
            chunk = models.Chunk(kb_id=kb_id, doc_id=doc.id, doc_version_id=version.id,
                                 ordinal=0, text=f"{token} 原文分块完整且可检索。",
                                 keywords=token, locator="原文第 1 段")
            db.add(chunk)
            db.flush()
            docs[name] = {"doc": doc.id, "version": version.id, "chunk": chunk.id}
        for slug, token, sources in (
            ("affected", "deletedsecretmarker", ("removed",)),
            ("mixed", "deletedsecretmarker", ("removed", "live")),
            ("unaffected", "survivororangemarker", ("live",)),
            ("unknown-history", "oldhistorymarker", ()),
        ):
            page = models.WikiPage(kb_id=kb_id, slug=slug, title=slug, status="published",
                                   human_locked=(slug == "affected"))
            db.add(page)
            db.flush()
            content = f"## {slug}\n\n{token} 页面的完整内容。"
            version = models.WikiPageVersion(page_id=page.id, version_no=1,
                                             content_md=content, origin="generated")
            db.add(version)
            db.flush()
            page.current_version_id = version.id
            db.add(models.WikiSection(kb_id=kb_id, page_id=page.id,
                                      page_version_id=version.id, ordinal=0,
                                      heading_path=slug, text=content, keywords=token))
            for source in sources:
                ref = docs[source]
                db.add(models.WikiEvidence(kb_id=kb_id, page_version_id=version.id,
                                           doc_id=ref["doc"], doc_version_id=ref["version"],
                                           chunk_id=ref["chunk"], quote="来源摘录", locator="第 1 段"))
            pages[slug] = {"id": page.id, "version": version.id}
        db.commit()

    tree_url = f"/api/kbs/{kb_id}/wiki/tree"
    chunk_url = f"/api/documents/{docs['live']['doc']}/chunks"
    original_tree = client.get(tree_url, headers=headers["read"])
    assert original_tree.status_code == 200
    assert {p["slug"] for p in original_tree.json()["items"]} == set(pages)
    original_chunks = client.get(chunk_url, headers=headers["read"])
    assert original_chunks.status_code == 200 and original_chunks.json()["total"] == 1
    with sessions() as db:
        kb = db.get(models.KnowledgeBase, kb_id)
        hits, _ = retrieval.search_wiki(db, kb, "deletedsecretmarker", top_n=20, qvec=None)
        assert {pages["affected"]["id"], pages["mixed"]["id"]} <= {hit.page_id for hit in hits}
        assert db.execute(select(models.WikiEvidence.id).where(
            models.WikiEvidence.page_version_id == pages["unknown-history"]["version"])).first() is None

    deleted = client.delete(f"/api/documents/{docs['removed']['doc']}", headers=headers["admin"])
    assert deleted.status_code == 200, deleted.text
    task_id = deleted.json()["rebuild_task_id"]
    assert _task(client, headers, task_id)["status"] == "queued"

    def assert_recorded_sources_hidden():
        for role in ("read", "manage"):
            response = client.get(tree_url, headers=headers[role])
            assert response.status_code == 200, response.text
            visible = {p["slug"] for p in response.json()["items"]}
            assert visible == {"unaffected", "unknown-history"}
            for slug in ("affected", "mixed"):
                url = f"/api/kbs/{kb_id}/wiki/pages/{slug}"
                assert client.get(url, headers=headers[role]).status_code == 404
                assert client.get(url + "/versions", headers=headers[role]).status_code == 404
        # 没有保存来源的历史页面不会凭空与已删文档关联，这不是可追溯性的保证。
        assert client.get(f"/api/kbs/{kb_id}/wiki/pages/unknown-history",
                          headers=headers["read"]).status_code == 200

    assert_recorded_sources_hidden()
    assert _task(client, headers, task_id)["can_stop_wiki"] is True
    assert _stop(client, headers, task_id, role="read").status_code == 403
    stopped = _stop(client, headers, task_id, role="manage")
    assert stopped.status_code == 200, stopped.text
    assert _task(client, headers, task_id)["status"] == "stopped"
    assert worker.run_once("stopped-deletion") is False
    assert_recorded_sources_hidden()
    reviewed = client.get(f"/api/kbs/{kb_id}/wiki/pages/affected", headers=headers["admin"])
    assert reviewed.status_code == 200 and reviewed.json()["status_label"] == "来源已删除，待复核"
    assert docs["removed"]["doc"] in {e["doc_id"] for e in reviewed.json()["evidence"]}
    with sessions() as db:
        assert db.get(models.Document, docs["removed"]["doc"]).deleted
        assert db.get(models.Chunk, docs["removed"]["chunk"]) is None
        evidence = db.execute(select(models.WikiEvidence).where(
            models.WikiEvidence.page_version_id == pages["affected"]["version"])).scalar_one()
        assert evidence.doc_id == docs["removed"]["doc"] and evidence.chunk_id is None
        kb = db.get(models.KnowledgeBase, kb_id)
        assert retrieval.plan_route(db, kb)[0] == "chunk"
        hits, _ = retrieval.search_wiki(db, kb, "deletedsecretmarker", top_n=20, qvec=None)
        assert {pages["affected"]["id"], pages["mixed"]["id"]}.isdisjoint(
            {hit.page_id for hit in hits})
    assert client.get(chunk_url, headers=headers["read"]).json() == original_chunks.json()
    live_search = client.post("/api/search/preview", headers=headers["read"],
                              json={"kb_ids": [kb_id], "query": "survivororangemarker"})
    assert live_search.status_code == 200, live_search.text
    assert live_search.json()["routes"][0]["used"] == "chunk"
    assert any(e["chunk_id"] == docs["live"]["chunk"] and
               e["doc_version_id"] == docs["live"]["version"]
               for e in live_search.json()["evidence"])
    deleted_search = client.post("/api/search/preview", headers=headers["read"],
                                 json={"kb_ids": [kb_id], "query": "deletedsecretmarker"})
    assert deleted_search.status_code == 200, deleted_search.text
    assert all(e["doc_id"] != docs["removed"]["doc"] and
               e["page_id"] not in {pages["affected"]["id"], pages["mixed"]["id"]}
               for e in deleted_search.json()["evidence"])
    assert "deletedsecretmarker" not in deleted_search.json()["context_preview"]

    # 普通管理者重试：隐藏的人工锁页跳过，其他页面成功重建后恢复 Wiki 检索。
    assert reviewed.json()["human_locked"] is True
    assert reviewed.json()["version"]["id"] == pages["affected"]["version"]
    monkeypatch.setattr(wiki, "_map_topics", lambda _db, entries, **_kwargs: [
        {"title": title, "summary": "有效来源", "chunk_ids": [idx for idx, _ in entries]}
        for title in ("affected", "survivor-route")
    ])
    retry = client.post(f"/api/tasks/{task_id}/retry", headers=headers["manage"])
    assert retry.status_code == 200, retry.text
    assert retry.json()["task_id"] == task_id and retry.json()["reused"] is True
    assert worker.run_once("manager-rebuild-after-delete") is True
    done = _task(client, headers, task_id)
    assert done["status"] == "success" and done["result"]["skipped"] == 1, done
    assert done["result"]["created"] + done["result"]["updated"] >= 1
    _assert_ready_and_route(client, headers, sessions, kb_id, ready=True)
    for role in ("read", "manage"):
        response = client.get(tree_url, headers=headers[role])
        assert response.status_code == 200, response.text
        visible = {p["slug"] for p in response.json()["items"]}
        assert "survivor-route" in visible and {"affected", "mixed"}.isdisjoint(visible)
        for slug in ("affected", "mixed"):
            assert client.get(f"/api/kbs/{kb_id}/wiki/pages/{slug}",
                              headers=headers[role]).status_code == 404
    reviewed_after = client.get(f"/api/kbs/{kb_id}/wiki/pages/affected", headers=headers["admin"])
    assert reviewed_after.status_code == 200, reviewed_after.text
    assert reviewed_after.json()["human_locked"] is True
    assert reviewed_after.json()["version"]["id"] == pages["affected"]["version"]
    assert docs["removed"]["doc"] in {e["doc_id"] for e in reviewed_after.json()["evidence"]}
    with sessions() as db:
        kb = db.get(models.KnowledgeBase, kb_id)
        hits, _ = retrieval.search_wiki(db, kb, "deletedsecretmarker", top_n=20, qvec=None)
        assert {pages["affected"]["id"], pages["mixed"]["id"]}.isdisjoint(
            {hit.page_id for hit in hits})


def test_stopping_older_queued_deletion_rebuild_preserves_newer_wiki(env, monkeypatch, tmp_path):
    client, headers, sessions = env
    FakeModels(monkeypatch)
    kb_id = _new_kb(client, headers)
    _seed_index(sessions, kb_id)
    with sessions() as db:
        old_doc = db.execute(select(models.Document).where(models.Document.kb_id == kb_id)).scalar_one()
        old_doc_id = old_doc.id
        version = db.get(models.DocumentVersion, old_doc.current_version_id)
        version.storage_path = str(tmp_path / "deleted-source.md")
        db.commit()

    # B 先排队但尚未运行；删除操作随后创建 A，保证 worker 在删除后先执行 B。
    uploaded = _upload(client, headers, kb_id)
    deleted = client.delete(f"/api/documents/{old_doc_id}", headers=headers["admin"])
    assert deleted.status_code == 200, deleted.text
    old_rebuild_id = deleted.json()["rebuild_task_id"]
    assert uploaded["task_id"] < old_rebuild_id
    assert _task(client, headers, old_rebuild_id)["status"] == "queued"
    assert worker.run_once("newer-wiki-after-delete") is True
    newer = _task(client, headers, uploaded["task_id"])
    assert newer["status"] == "success" and "wiki_error" not in newer["result"], newer
    assert _task(client, headers, old_rebuild_id)["status"] == "queued"
    _assert_ready_and_route(client, headers, sessions, kb_id, ready=True)
    pages = _tree(client, headers, kb_id)
    assert len(pages) == 3
    versions = {p["id"]: _assert_page_complete(client, headers, sessions, kb_id, p)
                for p in pages}
    with sessions() as db:
        kb = db.get(models.KnowledgeBase, kb_id)
        old_rebuild = db.get(models.Task, old_rebuild_id)
        newer_task = db.get(models.Task, uploaded["task_id"])
        assert old_rebuild.doc_id == old_doc_id
        assert newer_task.started_at >= old_rebuild.created_at
        assert kb.wiki_ready_at is not None and kb.wiki_ready_at >= old_rebuild.created_at
        assert not kb.wiki_incomplete
        epoch_before = kb.wiki_stop_epoch
        for version_id in versions.values():
            sources = set(db.execute(select(models.WikiEvidence.doc_id).where(
                models.WikiEvidence.page_version_id == version_id)).scalars())
            assert sources == {uploaded["doc_id"]}

    # A 没有执行；B 已经覆盖删除后的资料，停止 A 不应令 B 回退。
    current = _task(client, headers, old_rebuild_id)
    assert current["can_stop_wiki"] is True
    stopped = _stop(client, headers, old_rebuild_id, run_token=current["run_token"])
    assert stopped.status_code == 200, stopped.text
    assert _task(client, headers, old_rebuild_id)["status"] == "stopped"
    _assert_ready_and_route(client, headers, sessions, kb_id, ready=True)
    assert {p["id"]: _assert_page_complete(client, headers, sessions, kb_id, p)
            for p in _tree(client, headers, kb_id)} == versions
    with sessions() as db:
        kb = db.get(models.KnowledgeBase, kb_id)
        assert not kb.wiki_incomplete and kb.wiki_stop_epoch == epoch_before
    assert worker.run_once("old-deletion-was-stopped") is False


def test_stopped_ingest_rejects_retry_and_wiki_rebuild_preserves_data(env, monkeypatch):
    client, headers, sessions = env
    fake = FakeModels(monkeypatch, block_chat_at=2)
    kb_id = _new_kb(client, headers)
    item = _upload(client, headers, kb_id)
    task_id, doc_id, version_id = item["task_id"], item["doc_id"], item["version_id"]
    assert _stop(client, headers, task_id).status_code == 409
    assert _task(client, headers, task_id)["wiki_stop_requested"] is False
    with _running_worker(fake) as (thread, results):
        assert fake.chat_entered.wait(15), "worker 未开始第二个 Wiki 页面"
        active = _task(client, headers, task_id)
        assert active["status"] == "running" and active["can_stop_wiki"] is True
        first = _tree(client, headers, kb_id)
        assert len(first) == 1
        first_version = _assert_page_complete(client, headers, sessions, kb_id, first[0])
        chunks_before = client.get(f"/api/documents/{doc_id}/chunks", headers=headers["admin"]).json()
        assert chunks_before["total"] > 0
        response = _stop(client, headers, task_id)
        assert response.status_code == 200, response.text
        pending = _task(client, headers, task_id)
        assert pending["status"] == "running" and pending["wiki_stop_requested"] is True
        assert pending["can_stop_wiki"] is False and thread.is_alive()
        assert len(_tree(client, headers, kb_id)) == 1, "未完成的页面不能提前发布"
        _assert_ready_and_route(client, headers, sessions, kb_id, ready=False)
        fake.chat_release.set()
        thread.join(20)
        assert not thread.is_alive() and results == [True]
    done = _task(client, headers, task_id)
    assert done["status"] == "stopped" and done["result"]["wiki_stopped"] is True, done
    pages = _tree(client, headers, kb_id)
    assert len(pages) == 1, "已发布的第一页保留，尚未发布的第二页应丢弃"
    assert _assert_page_complete(client, headers, sessions, kb_id, pages[0]) == first_version
    assert fake.chat_calls == 2 and fake.embed_calls == 2, "停止后不能开始第二页的向量化"
    _assert_ready_and_route(client, headers, sessions, kb_id, ready=False)
    document = client.get(f"/api/documents/{doc_id}", headers=headers["admin"]).json()
    assert document["status"] == "ready" and document["current_version"]["id"] == version_id
    assert document["current_version"]["status"] == "success"
    assert client.get(f"/api/documents/{doc_id}/chunks", headers=headers["admin"]).json() == chunks_before
    cost = client.get(f"/api/kbs/{kb_id}/cost", headers=headers["admin"]).json()
    assert cost["by_group"]["ingest"]["embed_calls"] == 1
    assert cost["by_group"]["wiki"]["chat_calls"] == 2
    assert cost["by_group"]["wiki"]["embed_calls"] == 1
    assert cost["totals"]["prompt_tokens"] == 14 and cost["totals"]["completion_tokens"] == 22
    assert cost["totals"]["embed_tokens"] == 26

    # 停止的入库任务不能整份重跑，否则会重新生成分块并破坏已发布页面的来源链接。
    worker.recover_interrupted()
    stopped_task = _task(client, headers, task_id)
    assert stopped_task["status"] == "stopped"
    evidence_before = _evidence_snapshot(sessions, kb_id)
    assert evidence_before and all(row[4] for row in evidence_before)
    rejected = client.post(f"/api/tasks/{task_id}/retry", headers=headers["admin"])
    _assert_rebuild_wiki_conflict(rejected)
    rejected = client.post(f"/api/documents/{doc_id}/reindex", headers=headers["admin"])
    _assert_rebuild_wiki_conflict(rejected)
    assert _task(client, headers, task_id) == stopped_task
    assert client.get(f"/api/documents/{doc_id}", headers=headers["admin"]).json() == document
    assert client.get(f"/api/documents/{doc_id}/chunks", headers=headers["admin"]).json() == chunks_before
    assert _evidence_snapshot(sessions, kb_id) == evidence_before
    assert _assert_page_complete(client, headers, sessions, kb_id, pages[0]) == first_version
    assert worker.run_once("rejected-ingest-retry") is False

    # 重新生成 Wiki 不改文档版本和分块，并保留停止前已经产生的费用。
    rebuild = client.post(f"/api/kbs/{kb_id}/rebuild", headers=headers["admin"],
                          json={"kind": "wiki"})
    assert rebuild.status_code == 200, rebuild.text
    rebuild_id = rebuild.json()["task_id"]
    assert rebuild_id != task_id
    assert worker.run_once("rebuild-after-ingest-stop") is True
    assert _task(client, headers, rebuild_id)["status"] == "success"
    assert _task(client, headers, task_id)["status"] == "stopped"
    assert len(_tree(client, headers, kb_id)) == 3
    assert client.get(f"/api/documents/{doc_id}/chunks", headers=headers["admin"]).json() == chunks_before
    assert _evidence_snapshot(sessions, kb_id)[:len(evidence_before)] == evidence_before
    _assert_ready_and_route(client, headers, sessions, kb_id, ready=True)
    after_rebuild = client.get(f"/api/kbs/{kb_id}/cost", headers=headers["admin"]).json()
    assert after_rebuild["totals"]["chat_calls"] == 5
    assert after_rebuild["totals"]["embed_calls"] == 5


def test_stopped_old_ingest_cannot_replace_new_version_or_wiki_evidence(env, monkeypatch):
    client, headers, sessions = env
    fake = FakeModels(monkeypatch, block_chat_at=2)
    kb_id = _new_kb(client, headers)
    old = _upload(client, headers, kb_id)
    old_task_id, doc_id, old_version_id = old["task_id"], old["doc_id"], old["version_id"]
    with _running_worker(fake) as (thread, results):
        assert fake.chat_entered.wait(15), "旧版本未进入第二个 Wiki 页面"
        published = _tree(client, headers, kb_id)
        assert len(published) == 1
        _assert_page_complete(client, headers, sessions, kb_id, published[0])
        stopped = _stop(client, headers, old_task_id)
        assert stopped.status_code == 200, stopped.text
        fake.chat_release.set()
        thread.join(20)
        assert not thread.is_alive() and results == [True]
    assert _task(client, headers, old_task_id)["status"] == "stopped"

    # 使用真实版本上传与 worker 生成新索引和 Wiki，不手工修改版本状态。
    version_text = "# 新版制度\n\n## 新规定\n\nnewversionmarker 新版索引内容和审批流程。" * 3
    upload = client.post(f"/api/documents/{doc_id}/versions", headers=headers["admin"],
                         files={"file": ("source.md", version_text.encode(), "text/markdown")})
    assert upload.status_code == 201, upload.text
    new_version_id, new_task_id = upload.json()["version_id"], upload.json()["task_id"]
    assert new_version_id != old_version_id and new_task_id != old_task_id
    assert worker.run_once("ingest-new-version") is True
    new_task = _task(client, headers, new_task_id)
    assert new_task["status"] == "success" and "wiki_error" not in new_task["result"], new_task
    document = client.get(f"/api/documents/{doc_id}", headers=headers["admin"]).json()
    assert document["current_version"]["id"] == new_version_id and document["status"] == "ready"
    chunks = client.get(f"/api/documents/{doc_id}/chunks", headers=headers["admin"]).json()
    assert chunks["total"] > 0
    assert any("newversionmarker" in chunk["text"] for chunk in chunks["items"])
    chunk_ids = {chunk["id"] for chunk in chunks["items"]}
    pages = _tree(client, headers, kb_id)
    assert len(pages) == 3
    current_versions = {page["id"]: _assert_page_complete(client, headers, sessions, kb_id, page)
                        for page in pages}
    with sessions() as db:
        assert db.get(models.DocumentVersion, old_version_id).status == "success"
        assert db.get(models.DocumentVersion, new_version_id).status == "success"
        live_chunks = db.execute(select(models.Chunk).where(models.Chunk.doc_id == doc_id)).scalars().all()
        assert live_chunks and {chunk.doc_version_id for chunk in live_chunks} == {new_version_id}
        for page_version_id in current_versions.values():
            links = db.execute(select(models.WikiEvidence).where(
                models.WikiEvidence.page_version_id == page_version_id)).scalars().all()
            assert links and all(link.doc_version_id == new_version_id and link.chunk_id in chunk_ids
                                 for link in links)
    evidence_before = _evidence_snapshot(sessions, kb_id)
    assert evidence_before
    old_task = _task(client, headers, old_task_id)
    _assert_ready_and_route(client, headers, sessions, kb_id, ready=True)

    rejected = client.post(f"/api/tasks/{old_task_id}/retry", headers=headers["admin"])
    _assert_rebuild_wiki_conflict(rejected)
    assert _task(client, headers, old_task_id) == old_task
    assert client.get(f"/api/documents/{doc_id}", headers=headers["admin"]).json() == document
    assert client.get(f"/api/documents/{doc_id}/chunks", headers=headers["admin"]).json() == chunks
    assert _evidence_snapshot(sessions, kb_id) == evidence_before
    assert {page["id"]: _assert_page_complete(client, headers, sessions, kb_id, page)
            for page in _tree(client, headers, kb_id)} == current_versions
    _assert_ready_and_route(client, headers, sessions, kb_id, ready=True)
    assert worker.run_once("old-version-retry-rejected") is False


def test_restart_recovers_a_persisted_stop_request_without_requeue(env):
    client, headers, sessions = env
    kb_id = _new_kb(client, headers)
    task_id = client.post(f"/api/kbs/{kb_id}/rebuild", headers=headers["admin"],
                          json={"kind": "wiki"}).json()["task_id"]
    # 模拟进程在 Wiki 阶段崩溃：停止请求由真实 API 写入，之后不再运行 worker。
    with sessions() as db:
        task = db.get(models.Task, task_id)
        task.status = "running"
        task.wiki_active = True
        task.attempts = 1
        db.commit()
    response = _stop(client, headers, task_id)
    assert response.status_code == 200, response.text
    assert _task(client, headers, task_id)["wiki_stop_requested"] is True
    assert worker.recover_interrupted() == 1
    done = _task(client, headers, task_id)
    assert done["status"] == "stopped" and done["finished_at"]
    assert worker.run_once("restart-stop") is False
    with sessions() as db:
        task = db.get(models.Task, task_id)
        assert task.wiki_stop_requested and not task.wiki_active


def test_recovery_snapshot_write_lock_rejects_old_stop_token(env, monkeypatch):
    client, headers, sessions = env
    kb_id = _new_kb(client, headers)
    queued = client.post(f"/api/kbs/{kb_id}/rebuild", headers=headers["admin"],
                         json={"kind": "wiki"})
    assert queued.status_code == 200, queued.text
    task_id = queued.json()["task_id"]
    with sessions() as db:
        task = db.get(models.Task, task_id)
        task.status = "running"
        task.wiki_active = True
        task.attempts = 1
        task.run_token = "recovery-old-token"
        db.commit()
    snapshot_token = _task(client, headers, task_id)["run_token"]
    assert snapshot_token == "recovery-old-token"

    snapshot_ready, release_recovery = threading.Event(), threading.Event()
    stop_writing, stop_done = threading.Event(), threading.Event()
    snapshots, recover_results, stop_responses, errors = [], [], [], []
    original_scope = worker.session_scope

    class CapturedRows:
        def __init__(self, rows):
            self.rows = rows

        def all(self):
            return self.rows

    class CapturedResult:
        def __init__(self, rows):
            self.rows = rows

        def scalars(self):
            return CapturedRows(self.rows)

    @contextmanager
    def pause_recovery_scope(role="interactive"):
        with original_scope(role=role) as db:
            original_execute = db.execute

            def execute(statement, *args, **kwargs):
                result = original_execute(statement, *args, **kwargs)
                descriptions = getattr(statement, "column_descriptions", ())
                if (not snapshot_ready.is_set() and getattr(statement, "is_select", False)
                        and any(item.get("entity") is models.Task for item in descriptions)):
                    rows = result.scalars().all()
                    snapshots.extend((row.id, row.status, row.wiki_active, row.wiki_stop_requested)
                                     for row in rows)
                    snapshot_ready.set()
                    if not release_recovery.wait(20):
                        raise AssertionError("测试未释放恢复事务")
                    return CapturedResult(rows)
                return result

            monkeypatch.setattr(db, "execute", execute)
            yield db

    monkeypatch.setattr(worker, "session_scope", pause_recovery_scope)
    engine = sessions.kw["bind"]

    def observe_sql(_conn, _cursor, statement, _parameters, _context, _executemany):
        # 恢复暂停期间，只有停止请求会准备执行 tasks 更新。
        if statement.lstrip().upper().startswith("UPDATE TASKS"):
            stop_writing.set()

    event.listen(engine, "before_cursor_execute", observe_sql)

    def recover():
        try:
            recover_results.append(worker.recover_interrupted())
        except BaseException as exc:
            errors.append(exc)

    def stop():
        try:
            stop_responses.append(_stop(client, headers, task_id, run_token=snapshot_token))
        except BaseException as exc:
            errors.append(exc)
        finally:
            stop_done.set()

    recovery_thread = threading.Thread(target=recover, name="recovery-snapshot")
    stop_thread = threading.Thread(target=stop, name="recovery-stop-request")
    recovery_thread.start()
    try:
        assert snapshot_ready.wait(15), "恢复未读出 running 快照"
        assert snapshots == [(task_id, "running", True, False)]
        # 单独连接立即尝试拿写锁，确认恢复在读取快照时已持锁。
        probe = sqlite3.connect(engine.url.database, timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                probe.execute("BEGIN IMMEDIATE")
        finally:
            probe.close()
        stop_thread.start()
        assert stop_writing.wait(15), "停止请求未尝试写任务"
        assert not stop_done.is_set(), "恢复持有写锁时停止请求不应完成"
    finally:
        release_recovery.set()
        recovery_thread.join(25)
        if stop_thread.ident is not None:
            stop_thread.join(25)
        event.remove(engine, "before_cursor_execute", observe_sql)
        assert not recovery_thread.is_alive() and not stop_thread.is_alive()
        assert not errors, errors
    assert recover_results == [1]
    assert len(stop_responses) == 1 and stop_responses[0].status_code == 409
    recovered = _task(client, headers, task_id)
    assert recovered["status"] == "queued" and recovered["can_stop_wiki"] is True
    assert recovered["run_token"] and recovered["run_token"] != snapshot_token
    assert recovered["wiki_stop_requested"] is False
    with sessions() as db:
        task = db.get(models.Task, task_id)
        assert not task.wiki_stop_requested and not task.wiki_active
    # 当前队列代次可停止；随后启动恢复不能再次重排该任务。
    stopped = _stop(client, headers, task_id, run_token=recovered["run_token"])
    assert stopped.status_code == 200, stopped.text
    done = _task(client, headers, task_id)
    assert done["status"] == "stopped" and done["finished_at"]
    with sessions() as db:
        task = db.get(models.Task, task_id)
        assert task.wiki_stop_requested and not task.wiki_active
    assert worker.recover_interrupted() == 0
    assert _task(client, headers, task_id)["status"] == "stopped"
    assert worker.run_once("recovery-stop-race") is False


def test_lock_requeue_rotates_token_and_rejects_delayed_stop(env, monkeypatch):
    client, headers, sessions = env
    fake = FakeModels(monkeypatch)
    kb_id = _new_kb(client, headers)
    _seed_index(sessions, kb_id)
    queued = client.post(f"/api/kbs/{kb_id}/rebuild", headers=headers["admin"],
                         json={"kind": "wiki"})
    assert queued.status_code == 200, queued.text
    task_id = queued.json()["task_id"]
    in_wiki, release_lock_error = threading.Event(), threading.Event()

    def locked_wiki(_db, _kb, **_kwargs):
        in_wiki.set()
        if not release_lock_error.wait(20):
            raise AssertionError("测试未释放模拟数据库锁错误")
        raise OperationalError("Wiki publication", None, sqlite3.OperationalError("database is locked"))

    monkeypatch.setattr(wiki, "build_wiki", locked_wiki)
    with _running_worker(fake) as (thread, results):
        try:
            assert in_wiki.wait(15), "worker 未进入 Wiki 阶段"
            running = _task(client, headers, task_id)
            assert running["status"] == "running" and running["can_stop_wiki"] is True
            old_token = running["run_token"]
            assert old_token
            release_lock_error.set()
            thread.join(20)
            assert not thread.is_alive() and results == [True]
        finally:
            release_lock_error.set()
    requeued = _task(client, headers, task_id)
    assert requeued["status"] == "queued" and requeued["can_stop_wiki"] is True
    assert requeued["run_token"] and requeued["run_token"] != old_token
    assert "数据库" in requeued["error"]
    late_stop = _stop(client, headers, task_id, run_token=old_token)
    assert late_stop.status_code == 409, late_stop.text
    after_late_stop = _task(client, headers, task_id)
    assert after_late_stop["status"] == "queued"
    assert after_late_stop["run_token"] == requeued["run_token"]
    assert after_late_stop["wiki_stop_requested"] is False
    with sessions() as db:
        task = db.get(models.Task, task_id)
        assert not task.wiki_active and not task.wiki_stop_requested
    stopped = _stop(client, headers, task_id, run_token=requeued["run_token"])
    assert stopped.status_code == 200, stopped.text
    assert _task(client, headers, task_id)["status"] == "stopped"
    assert worker.recover_interrupted() == 0
    assert worker.run_once("lock-requeue-stop") is False


def test_recovery_after_worker_completes_stop_does_not_requeue(env, monkeypatch):
    client, headers, sessions = env
    fake = FakeModels(monkeypatch, block_chat_at=1)
    kb_id = _new_kb(client, headers)
    _seed_index(sessions, kb_id)
    queued = client.post(f"/api/kbs/{kb_id}/rebuild", headers=headers["admin"],
                         json={"kind": "wiki"})
    assert queued.status_code == 200, queued.text
    task_id = queued.json()["task_id"]
    with _running_worker(fake) as (thread, results):
        assert fake.chat_entered.wait(15), "worker 未开始 Wiki 页面生成"
        response = _stop(client, headers, task_id)
        assert response.status_code == 200, response.text
        assert _task(client, headers, task_id)["wiki_stop_requested"] is True
        fake.chat_release.set()
        thread.join(20)
        assert not thread.is_alive() and results == [True]
    before = _task(client, headers, task_id)
    assert before["status"] == "stopped" and before["finished_at"]
    assert worker.recover_interrupted() == 0
    after = _task(client, headers, task_id)
    assert after["status"] == "stopped" and after["finished_at"] == before["finished_at"]
    assert after["attempts"] == before["attempts"]
    assert worker.run_once("completed-stop-recovery") is False
    retry = client.post(f"/api/tasks/{task_id}/retry", headers=headers["admin"])
    assert retry.status_code == 200 and retry.json()["task_id"] == task_id
    retried = _task(client, headers, task_id)
    assert retried["status"] == "queued" and not retried["wiki_stop_requested"]
    assert worker.recover_interrupted() == 0
    after_retry_recovery = _task(client, headers, task_id)
    assert after_retry_recovery["status"] == "queued"
    assert after_retry_recovery["retry_count"] == retried["retry_count"]
    assert after_retry_recovery["stage"] == retried["stage"]
    with sessions() as db:
        task = db.get(models.Task, task_id)
        assert not task.wiki_stop_requested and not task.wiki_active and task.attempts == 0


@pytest.mark.parametrize("kind", ["reindex_kb", "embedding_rebuild"])
def test_reindex_only_accepts_stop_in_wiki_phase(env, monkeypatch, kind):
    client, headers, sessions = env
    fake = FakeModels(monkeypatch, block_embed_at=1, block_chat_at=1)
    kb_id = _new_kb(client, headers)
    _seed_index(sessions, kb_id)
    response = client.post(f"/api/kbs/{kb_id}/rebuild", headers=headers["admin"],
                           json={"kind": "chunk"})
    assert response.status_code == 200, response.text
    task_id = response.json()["task_id"]
    if kind == "embedding_rebuild":
        with sessions() as db:
            task = db.get(models.Task, task_id)
            task.kind = kind
            db.commit()
    assert _stop(client, headers, task_id).status_code == 409
    with _running_worker(fake) as (thread, results):
        assert fake.embed_entered.wait(15), "worker 未开始重建分块向量"
        before = _task(client, headers, task_id)
        assert before["status"] == "running" and before["can_stop_wiki"] is False
        assert _stop(client, headers, task_id).status_code == 409
        assert _task(client, headers, task_id)["wiki_stop_requested"] is False
        fake.embed_release.set()
        assert fake.chat_entered.wait(15), "worker 未进入 Wiki 页面生成"
        active = _task(client, headers, task_id)
        assert active["status"] == "running" and active["can_stop_wiki"] is True
        response = _stop(client, headers, task_id)
        assert response.status_code == 200, response.text
        assert _task(client, headers, task_id)["wiki_stop_requested"] is True
        fake.chat_release.set()
        thread.join(20)
        assert not thread.is_alive() and results == [True]
    done = _task(client, headers, task_id)
    assert done["status"] == "stopped", done
    pages = _tree(client, headers, kb_id)
    assert pages == [] and fake.chat_calls == 1 and fake.embed_calls == 1
    with sessions() as db:
        chunks = db.execute(select(models.Chunk).where(models.Chunk.kb_id == kb_id)).scalars().all()
        assert len(chunks) == 1 and chunks[0].embed_signature == "test:model:2"
        assert chunks[0].vector_blob
    cost = client.get(f"/api/kbs/{kb_id}/cost", headers=headers["admin"]).json()
    assert cost["by_group"]["ingest"]["embed_calls"] == 1
    assert cost["by_group"]["wiki"]["chat_calls"] == 1
    assert cost["by_group"]["wiki"]["embed_calls"] == 0
    with sessions() as db:
        before_chunks = [(c.id, c.vector_blob, c.embed_signature) for c in db.execute(
            select(models.Chunk).where(models.Chunk.kb_id == kb_id)).scalars()]
    before_evidence = _evidence_snapshot(sessions, kb_id)
    retry = client.post(f"/api/tasks/{task_id}/retry", headers=headers["admin"])
    _assert_rebuild_wiki_conflict(retry)
    assert _task(client, headers, task_id) == done
    with sessions() as db:
        assert [(c.id, c.vector_blob, c.embed_signature) for c in db.execute(
            select(models.Chunk).where(models.Chunk.kb_id == kb_id)).scalars()] == before_chunks
    assert _evidence_snapshot(sessions, kb_id) == before_evidence
    assert worker.run_once("reindex-retry-rejected") is False


def test_running_wiki_rebuild_stops_before_publishing_current_page(env, monkeypatch):
    client, headers, sessions = env
    fake = FakeModels(monkeypatch, block_chat_at=1)
    kb_id = _new_kb(client, headers)
    _seed_index(sessions, kb_id)
    response = client.post(f"/api/kbs/{kb_id}/rebuild", headers=headers["admin"],
                           json={"kind": "wiki"})
    assert response.status_code == 200, response.text
    task_id = response.json()["task_id"]
    with _running_worker(fake) as (thread, results):
        assert fake.chat_entered.wait(15), "worker 未开始生成页面"
        assert _task(client, headers, task_id)["can_stop_wiki"] is True
        assert _tree(client, headers, kb_id) == []
        response = _stop(client, headers, task_id)
        assert response.status_code == 200, response.text
        assert _task(client, headers, task_id)["wiki_stop_requested"] is True
        fake.chat_release.set()
        thread.join(20)
        assert not thread.is_alive() and results == [True]
    done = _task(client, headers, task_id)
    assert done["status"] == "stopped" and done["result"]["wiki_stopped"] is True
    pages = _tree(client, headers, kb_id)
    assert pages == [] and fake.chat_calls == 1 and fake.embed_calls == 0
    cost = client.get(f"/api/kbs/{kb_id}/cost", headers=headers["admin"]).json()
    assert cost["by_group"]["wiki"]["chat_calls"] == 1
    assert cost["by_group"]["wiki"]["embed_calls"] == 0


def test_stopping_recovered_queued_build_with_published_page_falls_back(env, monkeypatch):
    client, headers, sessions = env
    fake = FakeModels(monkeypatch, block_chat_at=2)
    kb_id = _new_kb(client, headers)
    _seed_index(sessions, kb_id)
    queued = client.post(f"/api/kbs/{kb_id}/rebuild", headers=headers["admin"],
                         json={"kind": "wiki"})
    assert queued.status_code == 200, queued.text
    task_id = queued.json()["task_id"]
    with _running_worker(fake) as (thread, results):
        assert fake.chat_entered.wait(15), "worker 未进入第二个 Wiki 页面"
        pages = _tree(client, headers, kb_id)
        assert len(pages) == 1
        page_version = _assert_page_complete(client, headers, sessions, kb_id, pages[0])
        old = _task(client, headers, task_id)
        assert old["status"] == "running" and old["run_token"] and old["attempts"] > 0
        assert worker.recover_interrupted() == 1
        recovered = _task(client, headers, task_id)
        assert recovered["status"] == "queued" and recovered["attempts"] > 0
        assert recovered["run_token"] and recovered["run_token"] != old["run_token"]
        assert thread.is_alive()
        response = _stop(client, headers, task_id, run_token=recovered["run_token"])
        assert response.status_code == 200, response.text
        assert _task(client, headers, task_id)["status"] == "stopped"
        _assert_ready_and_route(client, headers, sessions, kb_id, ready=False)
        assert _assert_page_complete(client, headers, sessions, kb_id, pages[0]) == page_version
        fake.chat_release.set()
        thread.join(20)
        assert not thread.is_alive() and results == [True]
    assert _task(client, headers, task_id)["status"] == "stopped"
    assert _assert_page_complete(client, headers, sessions, kb_id, pages[0]) == page_version
    _assert_ready_and_route(client, headers, sessions, kb_id, ready=False)
    with sessions() as db:
        assert db.get(models.KnowledgeBase, kb_id).wiki_incomplete
    assert worker.run_once("recovered-partial-stopped") is False


def test_stopping_retried_queue_after_partial_failure_falls_back(env, monkeypatch):
    client, headers, sessions = env
    FakeModels(monkeypatch)
    kb_id = _new_kb(client, headers)
    _seed_index(sessions, kb_id)
    initial = client.post(f"/api/kbs/{kb_id}/rebuild", headers=headers["admin"],
                          json={"kind": "wiki"})
    assert initial.status_code == 200, initial.text
    assert worker.run_once("baseline-wiki") is True
    assert _task(client, headers, initial.json()["task_id"])["status"] == "success"
    _assert_ready_and_route(client, headers, sessions, kb_id, ready=True)
    old_pages = _tree(client, headers, kb_id)
    assert len(old_pages) == 3
    old_versions = {p["id"]: _assert_page_complete(client, headers, sessions, kb_id, p)
                    for p in old_pages}

    fake = FakeModels(monkeypatch)
    original_chat = fake.chat
    calls = []

    def fail_second_page(db, messages, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("模拟第二页生成失败")
        return original_chat(db, messages, **kwargs)

    monkeypatch.setattr(llm, "chat", fail_second_page)
    queued = client.post(f"/api/kbs/{kb_id}/rebuild", headers=headers["admin"],
                         json={"kind": "wiki"})
    assert queued.status_code == 200, queued.text
    task_id = queued.json()["task_id"]
    assert worker.run_once("partial-failed-wiki") is True
    failed = _task(client, headers, task_id)
    assert failed["status"] == "failed" and failed["attempts"] > 0, failed
    assert len(calls) == 2
    partial_pages = _tree(client, headers, kb_id)
    partial_versions = {p["id"]: _assert_page_complete(client, headers, sessions, kb_id, p)
                        for p in partial_pages}
    assert set(partial_versions) == set(old_versions)
    assert sum(partial_versions[page_id] != version_id
               for page_id, version_id in old_versions.items()) == 1
    retry = client.post(f"/api/tasks/{task_id}/retry", headers=headers["admin"])
    assert retry.status_code == 200 and retry.json()["task_id"] == task_id
    retried = _task(client, headers, task_id)
    assert retried["status"] == "queued" and retried["attempts"] == 0
    with sessions() as db:
        task = db.get(models.Task, task_id)
        assert taskflow.retry_history(task)["previous_attempts"] > 0
        kb = db.get(models.KnowledgeBase, kb_id)
        assert not kb.wiki_incomplete
        epoch_before = kb.wiki_stop_epoch
    stopped = _stop(client, headers, task_id, run_token=retried["run_token"])
    assert stopped.status_code == 200, stopped.text
    assert _task(client, headers, task_id)["status"] == "stopped"
    with sessions() as db:
        kb = db.get(models.KnowledgeBase, kb_id)
        assert kb.wiki_incomplete and kb.wiki_stop_epoch > epoch_before
    assert {p["id"]: _assert_page_complete(client, headers, sessions, kb_id, p)
            for p in _tree(client, headers, kb_id)} == partial_versions
    _assert_ready_and_route(client, headers, sessions, kb_id, ready=False)
    assert worker.run_once("stopped-retry-queue") is False


@pytest.mark.parametrize("publish_again", [False, True])
def test_recovered_queue_stop_compares_last_publication_with_newer_build(
    env, monkeypatch, publish_again,
):
    client, headers, sessions = env
    kb_id = _new_kb(client, headers)
    _seed_index(sessions, kb_id)
    # B 的任务 ID 必须早于 A，才能在 A 恢复排队后由真实 FIFO worker 先领取。
    # 首次失败保留 B 的文件，之后用真实 retry 路由重新排队。
    uploaded = _upload(client, headers, kb_id)

    def fail_first_ingest(_db, _task, *, stats):
        raise RuntimeError("模拟文档任务第一次失败")

    with monkeypatch.context() as failure:
        failure.setattr(worker.ingest, "ingest_version", fail_first_ingest)
        assert worker.run_once("first-ingest-fails") is True
    assert _task(client, headers, uploaded["task_id"])["status"] == "failed"

    fake = FakeModels(monkeypatch, block_chat_at=2)
    monkeypatch.setattr(wiki, "_map_topics", lambda _db, entries, **_kwargs: [
        {"title": title, "summary": title, "chunk_ids": [idx for idx, _ in entries]}
        for title in ("Travel rules", "Account security", "Incident response")
    ])
    older = client.post(f"/api/kbs/{kb_id}/rebuild", headers=headers["admin"],
                        json={"kind": "wiki"})
    assert older.status_code == 200, older.text
    older_id = older.json()["task_id"]
    assert uploaded["task_id"] < older_id
    with _running_worker(fake) as (old_thread, old_results):
        assert fake.chat_entered.wait(15), "A 未发布第一页并进入第二页"
        first_pages = _tree(client, headers, kb_id)
        assert len(first_pages) == 1
        _assert_page_complete(client, headers, sessions, kb_id, first_pages[0])
        with sessions() as db:
            first_changed_at = db.get(models.Task, older_id).wiki_last_changed_at
            assert first_changed_at is not None
        retry_b = client.post(f"/api/tasks/{uploaded['task_id']}/retry", headers=headers["admin"])
        assert retry_b.status_code == 200 and retry_b.json()["task_id"] == uploaded["task_id"]
        assert worker.recover_interrupted() == 1
        recovered_a = _task(client, headers, older_id)
        assert recovered_a["status"] == "queued" and recovered_a["attempts"] > 0
        assert worker.run_once("newer-wiki-after-a") is True
        newer = _task(client, headers, uploaded["task_id"])
        assert newer["status"] == "success" and "wiki_error" not in newer["result"], newer
        assert _task(client, headers, older_id)["status"] == "queued"
        _assert_ready_and_route(client, headers, sessions, kb_id, ready=True)
        b_pages = _tree(client, headers, kb_id)
        assert len(b_pages) == 3
        b_versions = {p["id"]: _assert_page_complete(client, headers, sessions, kb_id, p)
                      for p in b_pages}
        with sessions() as db:
            kb = db.get(models.KnowledgeBase, kb_id)
            a_task = db.get(models.Task, older_id)
            assert kb.wiki_ready_at is not None and kb.wiki_ready_at >= first_changed_at
            assert a_task.wiki_last_changed_at == first_changed_at
            for version_id in b_versions.values():
                sources = set(db.execute(select(models.WikiEvidence.doc_id).where(
                    models.WikiEvidence.page_version_id == version_id)).scalars())
                assert uploaded["doc_id"] in sources

        if not publish_again:
            stopped = _stop(client, headers, older_id, run_token=recovered_a["run_token"])
            assert stopped.status_code == 200, stopped.text
            assert _task(client, headers, older_id)["status"] == "stopped"
            _assert_ready_and_route(client, headers, sessions, kb_id, ready=True)
            fake.chat_release.set()
            old_thread.join(20)
            assert not old_thread.is_alive() and old_results == [True]
            assert {p["id"]: _assert_page_complete(client, headers, sessions, kb_id, p)
                    for p in _tree(client, headers, kb_id)} == b_versions
            _assert_ready_and_route(client, headers, sessions, kb_id, ready=True)
        else:
            # A 的旧执行先因恢复代次失效退出，新的 A 再发布一页并再次被回收。
            fake.chat_release.set()
            old_thread.join(20)
            assert not old_thread.is_alive() and old_results == [True]
            assert _task(client, headers, older_id)["status"] == "queued"
            fake.chat_entered.clear()
            fake.chat_release.clear()
            fake.block_chat_at = fake.chat_calls + 2
            with _running_worker(fake) as (resumed_thread, resumed_results):
                assert fake.chat_entered.wait(15), "A 在 B 后未再次发布第一页"
                later_pages = _tree(client, headers, kb_id)
                later_versions = {p["id"]: _assert_page_complete(client, headers, sessions, kb_id, p)
                                  for p in later_pages}
                assert sum(later_versions[page_id] != version_id
                           for page_id, version_id in b_versions.items()) == 1
                with sessions() as db:
                    kb = db.get(models.KnowledgeBase, kb_id)
                    a_task = db.get(models.Task, older_id)
                    assert a_task.wiki_last_changed_at is not None
                    assert a_task.wiki_last_changed_at > kb.wiki_ready_at
                assert worker.recover_interrupted() == 1
                recovered_again = _task(client, headers, older_id)
                assert recovered_again["status"] == "queued" and recovered_again["attempts"] > 0
                stopped = _stop(client, headers, older_id, run_token=recovered_again["run_token"])
                assert stopped.status_code == 200, stopped.text
                assert _task(client, headers, older_id)["status"] == "stopped"
                _assert_ready_and_route(client, headers, sessions, kb_id, ready=False)
                fake.chat_release.set()
                resumed_thread.join(20)
                assert not resumed_thread.is_alive() and resumed_results == [True]
            assert {p["id"]: _assert_page_complete(client, headers, sessions, kb_id, p)
                    for p in _tree(client, headers, kb_id)} == later_versions
            _assert_ready_and_route(client, headers, sessions, kb_id, ready=False)
    assert _task(client, headers, older_id)["status"] == "stopped"
    assert worker.run_once("older-partial-queue-stopped") is False


def test_queued_stop_falls_back_when_newer_build_started_before_last_publication(env, monkeypatch):
    client, headers, sessions = env
    FakeModels(monkeypatch)
    kb_id = _new_kb(client, headers)
    _seed_index(sessions, kb_id)
    baseline = client.post(f"/api/kbs/{kb_id}/rebuild", headers=headers["admin"],
                           json={"kind": "wiki"})
    assert baseline.status_code == 200, baseline.text
    assert worker.run_once("baseline-wiki") is True
    assert _task(client, headers, baseline.json()["task_id"])["status"] == "success"
    _assert_ready_and_route(client, headers, sessions, kb_id, ready=True)

    # A 先失败以保留较小任务 ID；B 开始后再通过真实 retry 入口启动 A。
    older = client.post(f"/api/kbs/{kb_id}/rebuild", headers=headers["admin"],
                        json={"kind": "wiki"})
    assert older.status_code == 200, older.text
    older_id = older.json()["task_id"]

    def fail_initial(_db, _kb, **_kwargs):
        raise RuntimeError("模拟 A 首次生成失败")

    with monkeypatch.context() as failure:
        failure.setattr(wiki, "build_wiki", fail_initial)
        assert worker.run_once("first-a-fails") is True
    assert _task(client, headers, older_id)["status"] == "failed"
    newer = client.post(f"/api/kbs/{kb_id}/rebuild", headers=headers["admin"],
                        json={"kind": "wiki"})
    assert newer.status_code == 200, newer.text
    newer_id = newer.json()["task_id"]

    fake = FakeModels(monkeypatch)
    original_chat, original_lock = fake.chat, wiki._lock_wiki_write
    entered = {name: threading.Event() for name in ("interleave-A", "interleave-B")}
    release = {name: threading.Event() for name in entered}
    calls = {name: 0 for name in entered}
    a_write_attempts = []

    def gated_chat(db, messages, **kwargs):
        name = threading.current_thread().name
        if name in calls:
            calls[name] += 1
            if calls[name] == 2:
                entered[name].set()
                if not release[name].wait(30):
                    raise AssertionError(f"测试未释放 {name} 的第二页")
        return original_chat(db, messages, **kwargs)

    def lock_with_a_retry(db, locked_kb_id):
        if threading.current_thread().name == "interleave-A":
            a_write_attempts.append(1)
            if len(a_write_attempts) == 2:
                raise OperationalError("Wiki publication", None,
                                       sqlite3.OperationalError("database is locked"))
        return original_lock(db, locked_kb_id)

    monkeypatch.setattr(llm, "chat", gated_chat)
    monkeypatch.setattr(wiki, "_lock_wiki_write", lock_with_a_retry)
    results, errors = {}, []

    def run(name):
        try:
            results[name] = worker.run_once(name)
        except BaseException as exc:
            errors.append(exc)

    b_thread = threading.Thread(target=run, args=("interleave-B",), name="interleave-B")
    a_thread = threading.Thread(target=run, args=("interleave-A",), name="interleave-A")
    b_thread.start()
    try:
        # B 已发布 X 并等待第二页，A 的重试才能进入真实队列。
        assert entered["interleave-B"].wait(15), "B 未发布第一页"
        b_first = next(p for p in _tree(client, headers, kb_id) if p["slug"] == "travel-rules")
        b_first_version = _assert_page_complete(client, headers, sessions, kb_id, b_first)
        retry = client.post(f"/api/tasks/{older_id}/retry", headers=headers["admin"])
        assert retry.status_code == 200 and retry.json()["task_id"] == older_id
        a_thread.start()
        assert entered["interleave-A"].wait(15), "A 未覆盖第一页"
        a_first = next(p for p in _tree(client, headers, kb_id) if p["slug"] == "travel-rules")
        a_first_version = _assert_page_complete(client, headers, sessions, kb_id, a_first)
        assert a_first_version != b_first_version
        with sessions() as db:
            a_task = db.get(models.Task, older_id)
            b_task = db.get(models.Task, newer_id)
            assert b_task.wiki_started_at is not None and a_task.wiki_last_changed_at is not None
            b_started_at, a_published_at = b_task.wiki_started_at, a_task.wiki_last_changed_at
            assert b_started_at < a_published_at

        # A 的第二次页面写入模拟锁错误，真实 worker 原子重排并更换代次。
        release["interleave-A"].set()
        a_thread.join(20)
        assert not a_thread.is_alive() and results.get("interleave-A") is True
        a_queued = _task(client, headers, older_id)
        assert a_queued["status"] == "queued" and a_queued["attempts"] > 0
        assert len(a_write_attempts) == 2 and b_thread.is_alive()

        # B 继续发布 Y、Z 后完成；它不会回头覆盖 A 在 X 上较晚发布的版本。
        release["interleave-B"].set()
        b_thread.join(20)
        assert not b_thread.is_alive() and results.get("interleave-B") is True
        assert _task(client, headers, newer_id)["status"] == "success"
        final_first = next(p for p in _tree(client, headers, kb_id) if p["slug"] == "travel-rules")
        assert _assert_page_complete(client, headers, sessions, kb_id, final_first) == a_first_version
        with sessions() as db:
            kb = db.get(models.KnowledgeBase, kb_id)
            a_task = db.get(models.Task, older_id)
            assert kb.wiki_ready_started_at == b_started_at
            assert a_task.wiki_last_changed_at == a_published_at
            assert kb.wiki_ready_started_at < a_task.wiki_last_changed_at < kb.wiki_ready_at
        _assert_ready_and_route(client, headers, sessions, kb_id, ready=True)

        stopped = _stop(client, headers, older_id, run_token=a_queued["run_token"])
        assert stopped.status_code == 200, stopped.text
        assert _task(client, headers, older_id)["status"] == "stopped"
        assert _assert_page_complete(client, headers, sessions, kb_id, final_first) == a_first_version
        _assert_ready_and_route(client, headers, sessions, kb_id, ready=False)
        with sessions() as db:
            assert db.get(models.KnowledgeBase, kb_id).wiki_incomplete
    finally:
        release["interleave-A"].set()
        release["interleave-B"].set()
        if a_thread.ident is not None:
            a_thread.join(30)
        b_thread.join(30)
        assert not a_thread.is_alive() and not b_thread.is_alive()
        assert not errors, errors
    assert worker.run_once("interleaved-a-stopped") is False


@pytest.mark.parametrize("initial_incomplete", [False, True])
def test_stopping_queued_rebuild_preserves_existing_wiki_state(env, monkeypatch,
                                                                initial_incomplete):
    client, headers, sessions = env
    fake = FakeModels(monkeypatch)
    kb_id = _new_kb(client, headers)
    _seed_index(sessions, kb_id)
    first = client.post(f"/api/kbs/{kb_id}/rebuild", headers=headers["admin"],
                        json={"kind": "wiki"})
    assert first.status_code == 200, first.text
    assert worker.run_once("initial-wiki") is True
    assert _task(client, headers, first.json()["task_id"])["status"] == "success"
    old_pages = _tree(client, headers, kb_id)
    assert len(old_pages) == 3
    _assert_ready_and_route(client, headers, sessions, kb_id, ready=True)
    with sessions() as db:
        kb = db.get(models.KnowledgeBase, kb_id)
        if initial_incomplete:
            # 模拟此前运行中的 Wiki 已被停止；新的排队任务不应抹去该状态。
            kb.wiki_incomplete = True
            db.commit()
        epoch_before = kb.wiki_stop_epoch
    _assert_ready_and_route(client, headers, sessions, kb_id, ready=not initial_incomplete)

    queued = client.post(f"/api/kbs/{kb_id}/rebuild", headers=headers["admin"],
                         json={"kind": "wiki"})
    assert queued.status_code == 200, queued.text
    task_id = queued.json()["task_id"]
    calls_before = fake.chat_calls
    stopped = _stop(client, headers, task_id)
    assert stopped.status_code == 200, stopped.text
    assert _task(client, headers, task_id)["status"] == "stopped"
    assert worker.run_once("queued-wiki-stop") is False and fake.chat_calls == calls_before
    assert _tree(client, headers, kb_id) == old_pages
    with sessions() as db:
        kb = db.get(models.KnowledgeBase, kb_id)
        assert kb.wiki_incomplete is initial_incomplete and kb.wiki_stop_epoch == epoch_before
    _assert_ready_and_route(client, headers, sessions, kb_id, ready=not initial_incomplete)

    retry = client.post(f"/api/tasks/{task_id}/retry", headers=headers["admin"])
    assert retry.status_code == 200 and retry.json()["task_id"] == task_id
    assert worker.run_once("retry-wiki") is True
    assert _task(client, headers, task_id)["status"] == "success"
    assert {p["id"] for p in _tree(client, headers, kb_id)} == {p["id"] for p in old_pages}
    _assert_ready_and_route(client, headers, sessions, kb_id, ready=True)


@pytest.mark.parametrize("pause", ["embed", "before_publish_lock"])
@pytest.mark.parametrize("retry_before_release", [False, True])
def test_recovered_stop_prevents_old_worker_from_publishing(env, monkeypatch, tmp_path, pause,
                                                            retry_before_release):
    client, headers, sessions = env
    FakeModels(monkeypatch)
    kb_id = _new_kb(client, headers)
    _seed_index(sessions, kb_id)
    first = client.post(f"/api/kbs/{kb_id}/rebuild", headers=headers["admin"],
                        json={"kind": "wiki"})
    assert first.status_code == 200, first.text
    assert worker.run_once("initial-wiki") is True
    assert _task(client, headers, first.json()["task_id"])["status"] == "success"
    before = _tree(client, headers, kb_id)
    assert len(before) == 3
    versions = {p["id"]: _assert_page_complete(client, headers, sessions, kb_id, p)
                for p in before}
    _assert_ready_and_route(client, headers, sessions, kb_id, ready=True)

    fake = FakeModels(monkeypatch, block_embed_at=1 if pause == "embed" else None)
    lock_entered, lock_release = threading.Event(), threading.Event()
    if pause == "before_publish_lock":
        original_lock = wiki._lock_wiki_write

        def pause_before_lock(db, locked_kb_id):
            lock_entered.set()
            if not lock_release.wait(20):
                raise AssertionError("测试未释放页面发布锁")
            original_lock(db, locked_kb_id)

        monkeypatch.setattr(wiki, "_lock_wiki_write", pause_before_lock)
    queued = client.post(f"/api/kbs/{kb_id}/rebuild", headers=headers["admin"],
                         json={"kind": "wiki"})
    assert queued.status_code == 200, queued.text
    task_id = queued.json()["task_id"]
    with _running_worker(fake) as (thread, results):
        try:
            # 已通过正文后的停止检查，分别停在向量化和取得发布锁之前。
            entered = fake.embed_entered if pause == "embed" else lock_entered
            assert entered.wait(15), "旧 worker 未到达指定页面发布阶段"
            assert fake.chat_calls == 1
            response = _stop(client, headers, task_id)
            assert response.status_code == 200, response.text
            assert _task(client, headers, task_id)["wiki_stop_requested"] is True
            _assert_ready_and_route(client, headers, sessions, kb_id, ready=False)
            _recover_in_new_process(tmp_path)
            assert _task(client, headers, task_id)["status"] == "stopped"
            assert thread.is_alive()
            if retry_before_release:
                retry = client.post(f"/api/tasks/{task_id}/retry", headers=headers["admin"])
                assert retry.status_code == 200 and retry.json()["task_id"] == task_id
                assert _task(client, headers, task_id)["status"] == "queued"
            fake.embed_release.set()
            lock_release.set()
            thread.join(20)
            assert not thread.is_alive() and results == [True]
        finally:
            lock_release.set()
    assert _task(client, headers, task_id)["status"] == ("queued" if retry_before_release else "stopped")
    after = _tree(client, headers, kb_id)
    assert {p["id"] for p in after} == set(versions)
    assert {p["id"]: _assert_page_complete(client, headers, sessions, kb_id, p)
            for p in after} == versions, "恢复标停后，旧 worker 不能发布新版本"
    assert fake.chat_calls == 1 and fake.embed_calls == 1
    _assert_ready_and_route(client, headers, sessions, kb_id, ready=False)
    if retry_before_release:
        assert worker.run_once("retried-after-recovery") is True
        assert _task(client, headers, task_id)["status"] == "success"
        _assert_ready_and_route(client, headers, sessions, kb_id, ready=True)


def test_retried_worker_is_not_overwritten_by_old_worker(env, monkeypatch, tmp_path):
    client, headers, sessions = env
    FakeModels(monkeypatch)
    kb_id = _new_kb(client, headers)
    _seed_index(sessions, kb_id)
    initial = client.post(f"/api/kbs/{kb_id}/rebuild", headers=headers["admin"],
                          json={"kind": "wiki"})
    assert initial.status_code == 200, initial.text
    assert worker.run_once("initial-wiki") is True
    assert _task(client, headers, initial.json()["task_id"])["status"] == "success"
    pages = _tree(client, headers, kb_id)
    versions = {p["id"]: _assert_page_complete(client, headers, sessions, kb_id, p)
                for p in pages}
    assert len(versions) == 3
    before_cost = client.get(f"/api/kbs/{kb_id}/cost", headers=headers["admin"]).json()["totals"]

    fake = FakeModels(monkeypatch)
    original_chat, original_embed = fake.chat, fake.embed
    old_entered, old_release = threading.Event(), threading.Event()
    new_entered, new_release = threading.Event(), threading.Event()
    model_calls = []

    def chat(db, messages, **kwargs):
        model_calls.append((threading.current_thread().name, "chat"))
        return original_chat(db, messages, **kwargs)

    def embed(db, texts, **kwargs):
        name = threading.current_thread().name
        model_calls.append((name, "embed"))
        entered, release = ((old_entered, old_release) if name == "wiki-stop-test"
                            else (new_entered, new_release))
        entered.set()
        if not release.wait(20):
            raise AssertionError(f"测试未释放 {name} 的向量模型")
        return original_embed(db, texts, **kwargs)

    monkeypatch.setattr(llm, "chat", chat)
    monkeypatch.setattr(llm, "embed", embed)
    queued = client.post(f"/api/kbs/{kb_id}/rebuild", headers=headers["admin"],
                         json={"kind": "wiki"})
    assert queued.status_code == 200, queued.text
    task_id = queued.json()["task_id"]
    new_errors, new_results = [], []

    def new_worker():
        try:
            new_results.append(worker.run_once("new-wiki-run"))
        except BaseException as exc:
            new_errors.append(exc)

    new_thread = threading.Thread(target=new_worker, name="new-wiki-run")
    with _running_worker(fake) as (old_thread, old_results):
        try:
            assert old_entered.wait(15)
            old_token = _task(client, headers, task_id)["run_token"]
            assert old_token
            stopped = _stop(client, headers, task_id, run_token=old_token)
            assert stopped.status_code == 200, stopped.text
            _recover_in_new_process(tmp_path)
            assert _task(client, headers, task_id)["status"] == "stopped"
            retry = client.post(f"/api/tasks/{task_id}/retry", headers=headers["admin"])
            assert retry.status_code == 200 and retry.json()["task_id"] == task_id
            new_thread.start()
            assert new_entered.wait(15), "重试后的 worker 未进入模型调用"
            new_task = _task(client, headers, task_id)
            assert new_task["status"] == "running" and not new_task["wiki_stop_requested"]
            new_run_token = new_task["run_token"]
            assert new_run_token and new_run_token != old_token
            # 模拟旧页面发出的停止请求在同 ID 重试启动之后才抵达。
            late_stop = _stop(client, headers, task_id, run_token=old_token)
            assert late_stop.status_code == 409, late_stop.text
            after_late_stop = _task(client, headers, task_id)
            assert after_late_stop["status"] == "running" and new_thread.is_alive()
            assert after_late_stop["run_token"] == new_run_token
            assert after_late_stop["wiki_stop_requested"] is False
            assert after_late_stop["can_stop_wiki"] is True
            old_release.set()
            old_thread.join(20)
            assert not old_thread.is_alive() and old_results == [True]
            # 旧执行退出时，新执行仍在运行，且不能被旧执行改写或启动额外模型调用。
            task_after_old_exit = _task(client, headers, task_id)
            assert task_after_old_exit["status"] == "running" and new_thread.is_alive()
            assert not task_after_old_exit["wiki_stop_requested"]
            assert task_after_old_exit["result"] == {} and not task_after_old_exit["error"]
            with sessions() as db:
                assert db.get(models.Task, task_id).run_token == new_run_token
            assert [kind for name, kind in model_calls if name == "wiki-stop-test"] == ["chat", "embed"]
            old_cost = client.get(f"/api/kbs/{kb_id}/cost", headers=headers["admin"]).json()["totals"]
            assert old_cost["chat_calls"] >= before_cost["chat_calls"] + 1
            assert old_cost["embed_calls"] >= before_cost["embed_calls"] + 1
            assert {p["id"]: _assert_page_complete(client, headers, sessions, kb_id, p)
                    for p in _tree(client, headers, kb_id)} == versions
        finally:
            old_release.set()
            new_release.set()
            if new_thread.ident is not None:
                new_thread.join(25)
                assert not new_thread.is_alive() and not new_errors, new_errors
    assert new_results == [True]
    assert _task(client, headers, task_id)["status"] == "success"
    _assert_ready_and_route(client, headers, sessions, kb_id, ready=True)


@pytest.mark.parametrize("older_kind", ["rebuild_wiki", "reindex_kb", "embedding_rebuild"])
def test_completed_newer_build_remains_ready_when_stopped_older_run_finishes(
    env, monkeypatch, older_kind,
):
    client, headers, sessions = env
    FakeModels(monkeypatch)
    kb_id = _new_kb(client, headers)
    _seed_index(sessions, kb_id)
    initial = client.post(f"/api/kbs/{kb_id}/rebuild", headers=headers["admin"],
                          json={"kind": "wiki"})
    assert initial.status_code == 200, initial.text
    assert worker.run_once("initial-wiki") is True
    assert _task(client, headers, initial.json()["task_id"])["status"] == "success"
    _assert_ready_and_route(client, headers, sessions, kb_id, ready=True)

    fake = FakeModels(monkeypatch, block_chat_at=1)
    # 两个主题来源都传入生成，让 B 的完整生成真正包含新旧文档。
    monkeypatch.setattr(wiki, "_map_topics", lambda _db, entries, **_kwargs: [
        {"title": title, "summary": title, "chunk_ids": [idx for idx, _ in entries]}
        for title in ("Travel rules", "Account security", "Incident response")
    ])
    older = client.post(f"/api/kbs/{kb_id}/rebuild", headers=headers["admin"],
                        json={"kind": "wiki" if older_kind == "rebuild_wiki" else "chunk"})
    assert older.status_code == 200, older.text
    older_id = older.json()["task_id"]
    if older_kind == "embedding_rebuild":
        with sessions() as db:
            db.get(models.Task, older_id).kind = older_kind
            db.commit()
    with _running_worker(fake) as (older_thread, older_results):
        assert fake.chat_entered.wait(15), "A 未进入页面生成"
        older_token = _task(client, headers, older_id)["run_token"]
        assert older_token
        stopped = _stop(client, headers, older_id, run_token=older_token)
        assert stopped.status_code == 200, stopped.text
        assert _task(client, headers, older_id)["wiki_stop_requested"] is True
        _assert_ready_and_route(client, headers, sessions, kb_id, ready=False)
        with sessions() as db:
            stop_epoch = db.get(models.KnowledgeBase, kb_id).wiki_stop_epoch

        # A 仍被模型阻塞；上传新文档让真实 worker 执行 B 的完整 Wiki 构建。
        uploaded = _upload(client, headers, kb_id)
        assert worker.run_once("newer-complete-wiki") is True
        newer = _task(client, headers, uploaded["task_id"])
        assert newer["status"] == "success" and "wiki_error" not in newer["result"], newer
        assert older_thread.is_alive()
        assert _task(client, headers, older_id)["wiki_stop_requested"] is True
        _assert_ready_and_route(client, headers, sessions, kb_id, ready=True)
        new_pages = _tree(client, headers, kb_id)
        assert len(new_pages) == 3
        versions = {p["id"]: _assert_page_complete(client, headers, sessions, kb_id, p)
                    for p in new_pages}
        with sessions() as db:
            kb = db.get(models.KnowledgeBase, kb_id)
            assert not kb.wiki_incomplete and kb.wiki_stop_epoch == stop_epoch
            for version_id in versions.values():
                sources = set(db.execute(select(models.WikiEvidence.doc_id).where(
                    models.WikiEvidence.page_version_id == version_id)).scalars())
                assert uploaded["doc_id"] in sources
        fake.chat_release.set()
        older_thread.join(20)
        assert not older_thread.is_alive() and older_results == [True]
    assert _task(client, headers, older_id)["status"] == "stopped"
    assert _task(client, headers, uploaded["task_id"])["status"] == "success"
    _assert_ready_and_route(client, headers, sessions, kb_id, ready=True)
    assert {p["id"]: _assert_page_complete(client, headers, sessions, kb_id, p)
            for p in _tree(client, headers, kb_id)} == versions
    with sessions() as db:
        kb = db.get(models.KnowledgeBase, kb_id)
        assert kb.status == "ready" and not kb.wiki_incomplete and kb.wiki_stop_epoch == stop_epoch
    if older_kind != "rebuild_wiki":
        evidence_before = _evidence_snapshot(sessions, kb_id)
        rejected = client.post(f"/api/tasks/{older_id}/retry", headers=headers["admin"])
        _assert_rebuild_wiki_conflict(rejected)
        assert _evidence_snapshot(sessions, kb_id) == evidence_before
        _assert_ready_and_route(client, headers, sessions, kb_id, ready=True)
        with sessions() as db:
            assert db.get(models.KnowledgeBase, kb_id).status == "ready"


def test_stopping_running_older_build_after_newer_completion_keeps_wiki_ready(env, monkeypatch):
    client, headers, sessions = env
    fake = FakeModels(monkeypatch, block_chat_at=2)
    kb_id = _new_kb(client, headers)
    _seed_index(sessions, kb_id)
    monkeypatch.setattr(wiki, "_map_topics", lambda _db, entries, **_kwargs: [
        {"title": title, "summary": title, "chunk_ids": [idx for idx, _ in entries]}
        for title in ("Travel rules", "Account security", "Incident response")
    ])
    older = client.post(f"/api/kbs/{kb_id}/rebuild", headers=headers["admin"],
                        json={"kind": "wiki"})
    assert older.status_code == 200, older.text
    older_id = older.json()["task_id"]
    with _running_worker(fake) as (thread, results):
        # A 已发布第一页，阻塞在第二页模型调用，后续没有任何写入。
        assert fake.chat_entered.wait(15), "A 未完成第一页并进入第二页"
        first_pages = _tree(client, headers, kb_id)
        assert len(first_pages) == 1
        first_version = _assert_page_complete(client, headers, sessions, kb_id, first_pages[0])
        older_task = _task(client, headers, older_id)
        assert older_task["status"] == "running" and older_task["run_token"]
        with sessions() as db:
            task = db.get(models.Task, older_id)
            assert task.wiki_started_at is not None and task.wiki_last_changed_at is not None
            older_started_at, older_changed_at = task.wiki_started_at, task.wiki_last_changed_at
            assert older_started_at <= older_changed_at

        # B 在 A 的最后一次发布后启动，真实执行入库及完整 Wiki 生成。
        uploaded = _upload(client, headers, kb_id)
        assert worker.run_once("newer-build-before-stop") is True
        newer = _task(client, headers, uploaded["task_id"])
        assert newer["status"] == "success" and "wiki_error" not in newer["result"], newer
        assert thread.is_alive() and _task(client, headers, older_id)["status"] == "running"
        pages = _tree(client, headers, kb_id)
        assert len(pages) == 3
        versions = {p["id"]: _assert_page_complete(client, headers, sessions, kb_id, p)
                    for p in pages}
        assert versions[first_pages[0]["id"]] != first_version
        with sessions() as db:
            kb = db.get(models.KnowledgeBase, kb_id)
            task = db.get(models.Task, older_id)
            newer_task = db.get(models.Task, uploaded["task_id"])
            assert task.wiki_last_changed_at == older_changed_at
            assert kb.wiki_ready_started_at == newer_task.wiki_started_at
            assert kb.wiki_ready_started_at > max(older_started_at, older_changed_at)
            assert not kb.wiki_incomplete
            epoch_before_stop = kb.wiki_stop_epoch
            for version_id in versions.values():
                sources = set(db.execute(select(models.WikiEvidence.doc_id).where(
                    models.WikiEvidence.page_version_id == version_id)).scalars())
                assert uploaded["doc_id"] in sources
        _assert_ready_and_route(client, headers, sessions, kb_id, ready=True)

        # 用户此时才停止 A；B 的有效结果不能被较晚的停止请求置为不完整。
        stopped = _stop(client, headers, older_id, run_token=older_task["run_token"])
        assert stopped.status_code == 200, stopped.text
        pending = _task(client, headers, older_id)
        assert pending["status"] == "running" and pending["wiki_stop_requested"] is True
        _assert_ready_and_route(client, headers, sessions, kb_id, ready=True)
        with sessions() as db:
            kb = db.get(models.KnowledgeBase, kb_id)
            assert not kb.wiki_incomplete and kb.wiki_stop_epoch == epoch_before_stop
        fake.chat_release.set()
        thread.join(20)
        assert not thread.is_alive() and results == [True]
    assert _task(client, headers, older_id)["status"] == "stopped"
    assert _task(client, headers, uploaded["task_id"])["status"] == "success"
    assert {p["id"]: _assert_page_complete(client, headers, sessions, kb_id, p)
            for p in _tree(client, headers, kb_id)} == versions
    _assert_ready_and_route(client, headers, sessions, kb_id, ready=True)
    with sessions() as db:
        kb = db.get(models.KnowledgeBase, kb_id)
        task = db.get(models.Task, older_id)
        assert kb.status == "ready" and not kb.wiki_incomplete
        assert kb.wiki_stop_epoch == epoch_before_stop and task.wiki_last_changed_at == older_changed_at


def test_older_build_cannot_clear_newer_stop_epoch(env, monkeypatch):
    client, headers, sessions = env
    FakeModels(monkeypatch)
    kb_id = _new_kb(client, headers)
    _seed_index(sessions, kb_id)
    initial = client.post(f"/api/kbs/{kb_id}/rebuild", headers=headers["admin"],
                          json={"kind": "wiki"})
    assert initial.status_code == 200, initial.text
    assert worker.run_once("initial-wiki") is True
    assert _task(client, headers, initial.json()["task_id"])["status"] == "success"
    _assert_ready_and_route(client, headers, sessions, kb_id, ready=True)
    with sessions() as db:
        epoch_before = db.get(models.KnowledgeBase, kb_id).wiki_stop_epoch

    fake = FakeModels(monkeypatch)
    original_chat = fake.chat
    old_entered, old_release = threading.Event(), threading.Event()
    new_entered, new_release = threading.Event(), threading.Event()

    def chat(db, messages, **kwargs):
        if threading.current_thread().name == "wiki-stop-test":
            entered, release = old_entered, old_release
        else:
            entered, release = new_entered, new_release
        if not entered.is_set():
            entered.set()
            if not release.wait(20):
                raise AssertionError("测试未释放并发 Wiki 生成")
        return original_chat(db, messages, **kwargs)

    monkeypatch.setattr(llm, "chat", chat)
    older = client.post(f"/api/kbs/{kb_id}/rebuild", headers=headers["admin"],
                        json={"kind": "wiki"})
    assert older.status_code == 200, older.text
    older_id = older.json()["task_id"]
    new_errors, new_results = [], []

    def ingest_worker():
        try:
            new_results.append(worker.run_once("new-ingest"))
        except BaseException as exc:
            new_errors.append(exc)

    new_thread = threading.Thread(target=ingest_worker, name="new-ingest")
    with _running_worker(fake) as (old_thread, old_results):
        try:
            assert old_entered.wait(15), "较早的 Wiki 构建未开始"
            # 上传允许在旧 Wiki 运行时排队，第二个 worker 执行真实文档入库。
            uploaded = _upload(client, headers, kb_id)
            new_thread.start()
            assert new_entered.wait(15), "新文档未进入 Wiki 生成"
            new_task_id = uploaded["task_id"]
            stopped = _stop(client, headers, new_task_id)
            assert stopped.status_code == 200, stopped.text
            new_release.set()
            new_thread.join(20)
            assert not new_thread.is_alive() and new_results == [True] and not new_errors
            assert _task(client, headers, new_task_id)["status"] == "stopped"
            with sessions() as db:
                kb = db.get(models.KnowledgeBase, kb_id)
                assert kb.wiki_stop_epoch > epoch_before and kb.wiki_incomplete
            _assert_ready_and_route(client, headers, sessions, kb_id, ready=False)
            old_release.set()
            old_thread.join(20)
            assert not old_thread.is_alive() and old_results == [True]
        finally:
            old_release.set()
            new_release.set()
            if new_thread.ident is not None:
                new_thread.join(25)
                assert not new_thread.is_alive() and not new_errors, new_errors
    assert _task(client, headers, older_id)["status"] == "success"
    with sessions() as db:
        kb = db.get(models.KnowledgeBase, kb_id)
        assert kb.wiki_stop_epoch > epoch_before and kb.wiki_incomplete
    _assert_ready_and_route(client, headers, sessions, kb_id, ready=False)


@pytest.mark.parametrize("reason", ["human_lock", "page_limit"])
def test_successful_rebuild_restores_wiki_when_pages_are_skipped(env, monkeypatch, reason):
    client, headers, sessions = env
    fake = FakeModels(monkeypatch)
    kb_id = _new_kb(client, headers)
    _seed_index(sessions, kb_id)
    initial = client.post(f"/api/kbs/{kb_id}/rebuild", headers=headers["admin"],
                          json={"kind": "wiki"})
    assert initial.status_code == 200, initial.text
    assert worker.run_once("initial-wiki") is True
    assert _task(client, headers, initial.json()["task_id"])["status"] == "success"
    pages = _tree(client, headers, kb_id)
    assert len(pages) == 3
    _assert_ready_and_route(client, headers, sessions, kb_id, ready=True)
    queued = client.post(f"/api/kbs/{kb_id}/rebuild", headers=headers["admin"],
                         json={"kind": "wiki"})
    assert queued.status_code == 200, queued.text
    task_id = queued.json()["task_id"]
    # 排队中停止不会改变已有 Wiki；必须在实际生成中停止以形成 incomplete。
    fake.block_chat_at = fake.chat_calls + 1
    with _running_worker(fake) as (thread, results):
        assert fake.chat_entered.wait(15), "worker 未进入 Wiki 页面生成"
        stopped = _stop(client, headers, task_id)
        assert stopped.status_code == 200, stopped.text
        assert _task(client, headers, task_id)["wiki_stop_requested"] is True
        _assert_ready_and_route(client, headers, sessions, kb_id, ready=False)
        fake.chat_release.set()
        thread.join(20)
        assert not thread.is_alive() and results == [True]
    assert _task(client, headers, task_id)["status"] == "stopped"
    _assert_ready_and_route(client, headers, sessions, kb_id, ready=False)
    if reason == "human_lock":
        edited = client.put(f"/api/kbs/{kb_id}/wiki/pages/{pages[0]['slug']}",
                            headers=headers["admin"],
                            json={"content_md": "## 人工校对\n\n此页面已经人工校对，不应被重建覆盖。"})
        assert edited.status_code == 200, edited.text
        locked_version = edited.json()["version_id"]
    else:
        monkeypatch.setattr(config, "WIKI_MAX_PAGES", 1)
    chats_before = fake.chat_calls
    retry = client.post(f"/api/tasks/{task_id}/retry", headers=headers["admin"])
    assert retry.status_code == 200 and retry.json()["task_id"] == task_id
    assert worker.run_once("partial-wiki-retry") is True
    done = _task(client, headers, task_id)
    assert done["status"] == "success", done
    if reason == "human_lock":
        assert done["result"]["skipped"] == 1 and fake.chat_calls - chats_before == 2
        page = client.get(f"/api/kbs/{kb_id}/wiki/pages/{pages[0]['slug']}",
                          headers=headers["admin"])
        assert page.status_code == 200, page.text
        assert page.json()["human_locked"] is True and page.json()["version"]["id"] == locked_version
        assert "此页面已经人工校对" in page.json()["version"]["content_md"]
    else:
        assert fake.chat_calls - chats_before == 1 and done["result"]["notes"]
    assert len(_tree(client, headers, kb_id)) == 3
    _assert_ready_and_route(client, headers, sessions, kb_id, ready=True)


def test_task_list_includes_old_running_without_changing_normal_limit(env):
    client, headers, sessions = env
    kb_id = _new_kb(client, headers)
    with sessions() as db:
        old = models.Task(kind="rebuild_wiki", status="running", kb_id=kb_id, wiki_active=True)
        db.add(old)
        db.flush()
        old_id = old.id
        queued = [models.Task(kind="rebuild_wiki", status="queued", kb_id=kb_id)
                  for _ in range(55)]
        db.add_all(queued)
        db.flush()
        newest = models.Task(kind="rebuild_wiki", status="running", kb_id=kb_id, wiki_active=True)
        db.add(newest)
        db.flush()
        newer_ids = [task.id for task in queued] + [newest.id]
        db.commit()
    expected = sorted(newer_ids, reverse=True)[:50]
    normal = client.get("/api/tasks", params={"kb_id": kb_id}, headers=headers["admin"])
    assert normal.status_code == 200, normal.text
    assert [task["id"] for task in normal.json()["items"]] == expected
    assert normal.json()["total"] == 50 and old_id not in expected
    extended = client.get("/api/tasks", params={"kb_id": kb_id, "include_running": "true"},
                          headers=headers["admin"])
    assert extended.status_code == 200, extended.text
    ids = [task["id"] for task in extended.json()["items"]]
    assert ids == sorted([*expected, old_id], reverse=True)
    assert len(ids) == len(set(ids)) == extended.json()["total"] == 51
    only_queued = client.get("/api/tasks", params={"kb_id": kb_id, "status": "queued",
                                                    "include_running": "true"},
                             headers=headers["admin"])
    assert only_queued.status_code == 200
    assert len(only_queued.json()["items"]) == 50
    assert all(task["status"] == "queued" for task in only_queued.json()["items"])


def test_employee_task_limit_is_applied_after_access_filter(env):
    client, headers, sessions = env
    visible_id = _new_kb(client, headers)
    response = client.post("/api/kbs", headers=headers["admin"],
                           json={"name": "员工不可见的任务库", "mode": "wiki"})
    assert response.status_code == 201, response.text
    hidden_id = response.json()["id"]
    with sessions() as db:
        db.add(models.KbGrant(kb_id=visible_id, user_id=2, level="read"))
        old = models.Task(kind="rebuild_wiki", status="running", kb_id=visible_id, wiki_active=True)
        db.add(old)
        db.flush()
        old_id = old.id
        visible = [models.Task(kind="rebuild_wiki", status="queued", kb_id=visible_id)
                   for _ in range(55)]
        db.add_all(visible)
        db.flush()
        visible_ids = [task.id for task in visible]
        hidden = [models.Task(kind="rebuild_wiki", status="queued", kb_id=hidden_id)
                  for _ in range(55)]
        hidden.append(models.Task(kind="rebuild_wiki", status="running", kb_id=hidden_id,
                                  wiki_active=True))
        db.add_all(hidden)
        db.commit()
    expected = sorted(visible_ids, reverse=True)[:50]
    ordinary = client.get("/api/tasks", headers=headers["read"])
    assert ordinary.status_code == 200, ordinary.text
    assert [task["id"] for task in ordinary.json()["items"]] == expected
    assert ordinary.json()["total"] == 50
    extended = client.get("/api/tasks", params={"include_running": "true"}, headers=headers["read"])
    assert extended.status_code == 200, extended.text
    items = extended.json()["items"]
    assert [task["id"] for task in items] == sorted([*expected, old_id], reverse=True)
    assert extended.json()["total"] == 51
    assert all(task["kb_id"] == visible_id for task in items)
    hidden_only = client.get("/api/tasks", params={"kb_id": hidden_id, "include_running": "true"},
                             headers=headers["read"])
    assert hidden_only.status_code == 200 and hidden_only.json()["items"] == []


def test_queued_stop_and_worker_claim_race_are_atomic(env, monkeypatch):
    client, headers, sessions = env
    fake = FakeModels(monkeypatch, block_chat_at=1)
    kb_id = _new_kb(client, headers)
    _seed_index(sessions, kb_id)
    response = client.post(f"/api/kbs/{kb_id}/rebuild", headers=headers["admin"],
                           json={"kind": "wiki"})
    assert response.status_code == 200, response.text
    task_id = response.json()["task_id"]
    queued_token = _task(client, headers, task_id)["run_token"]
    assert queued_token == ""
    gate = threading.Barrier(3)
    stop_responses, worker_results, errors = [], [], []

    def stop():
        try:
            gate.wait(5)
            stop_responses.append(_stop(client, headers, task_id, run_token=queued_token))
        except BaseException as exc:
            errors.append(exc)

    def claim():
        try:
            gate.wait(5)
            worker_results.append(worker.run_once("claim-race"))
        except BaseException as exc:
            errors.append(exc)

    stop_thread = threading.Thread(target=stop, name="race-stop")
    worker_thread = threading.Thread(target=claim, name="race-claim")
    stop_thread.start()
    worker_thread.start()
    try:
        gate.wait(5)
        stop_thread.join(15)
        assert not stop_thread.is_alive() and not errors, errors
        response = stop_responses[0]
        if response.status_code == 409:
            # worker 抢先领取后空代次失效；阶段开始后读取新代次再请求停止。
            assert fake.chat_entered.wait(15), "已领取任务未到达 Wiki 阶段"
            running_token = _task(client, headers, task_id)["run_token"]
            assert running_token and running_token != queued_token
            response = _stop(client, headers, task_id, run_token=running_token)
        assert response.status_code == 200, response.text
    finally:
        fake.release()
        stop_thread.join(25)
        worker_thread.join(25)
        assert not stop_thread.is_alive() and not worker_thread.is_alive()
        assert not errors, errors
    assert _task(client, headers, task_id)["status"] == "stopped"
    assert worker_results in ([False], [True])
    if worker_results == [False]:
        assert fake.chat_calls == 0 and _tree(client, headers, kb_id) == []
    else:
        assert fake.chat_calls <= 1 and _tree(client, headers, kb_id) == []


def test_late_queued_stop_does_not_cancel_retried_queue(env):
    client, headers, _sessions = env
    kb_id = _new_kb(client, headers)
    queued = client.post(f"/api/kbs/{kb_id}/rebuild", headers=headers["admin"],
                         json={"kind": "wiki"})
    assert queued.status_code == 200, queued.text
    task_id = queued.json()["task_id"]
    old_token = _task(client, headers, task_id)["run_token"]
    assert _stop(client, headers, task_id, run_token=old_token).status_code == 200
    assert client.post(f"/api/tasks/{task_id}/retry", headers=headers["admin"]).status_code == 200
    retried = _task(client, headers, task_id)
    assert retried["status"] == "queued" and retried["run_token"] != old_token
    assert _stop(client, headers, task_id, run_token=old_token).status_code == 409
    assert _task(client, headers, task_id)["status"] == "queued"
    assert _stop(client, headers, task_id, run_token=retried["run_token"]).status_code == 200
