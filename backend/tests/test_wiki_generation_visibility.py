"""Published Map inputs govern visibility independently of displayed evidence.

All generation cases use the real Map, topic merge and build pipeline. Only
model responses and embeddings are deterministic; each test gets its own
SQLite database and the delete/edit/stop/search operations use HTTP routes.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app import models, security
from app.routers import chat, kbs
from app.services import chunking, llm, retrieval, wiki
from test_wiki_input_source_race import (  # reuse its isolated WAL database and real builder
    SECRET, TITLE, _build, _model, _prepare, race_env,  # noqa: F401
)


@pytest.fixture
def visibility_env(race_env, monkeypatch):
    sessions, client, _ = race_env
    client.app.include_router(chat.router, prefix="/api")
    assert chat.get_db is kbs.get_db  # race_env overrides this dependency for all three routers

    def no_embedding(*args, **kwargs):
        raise llm.ModelUnavailable("offline visibility regression")

    monkeypatch.setattr(llm, "embed", no_embedding)
    return race_env


def _url(case, slug=None):
    return f"/api/kbs/{case['kb']}/wiki/pages/{slug or case['slug']}"


def _dependencies(sessions, case, version_id):
    with sessions() as db:
        rows = db.execute(select(models.WikiGenerationSource.kb_id,
                                 models.WikiGenerationSource.doc_id).where(
            models.WikiGenerationSource.page_version_id == version_id)).all()
    assert len(rows) == len(set(rows)), "a document is recorded only once per version"
    assert all(kb_id == case["kb"] for kb_id, _ in rows)
    return {doc_id for _, doc_id in rows}


def _tree(client, case, headers):
    response = client.get(f"/api/kbs/{case['kb']}/wiki/tree", headers=headers)
    assert response.status_code == 200, response.text
    return {page["slug"]: page for page in response.json()["items"]}


def _delete(client, case, source_no):
    source = case["sources"][source_no - 1]
    response = client.delete(f"/api/documents/{source['doc']}", headers=case["admin"])
    assert response.status_code == 200, response.text
    task_id = response.json()["rebuild_task_id"]
    assert response.json()["ok"] and task_id is not None
    assert not source["path"].exists()
    return task_id


def _page(client, case, headers, slug=None):
    response = client.get(_url(case, slug), headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _search(client, case, headers, query):
    response = client.post("/api/search/preview", headers=headers, json={
        "kb_ids": [case["kb"]], "query": query,
    })
    assert response.status_code == 200, response.text
    return response.json()


def _add_user(sessions, case, level):
    with sessions() as db:
        user = models.User(username=f"visibility-{level}", password_hash="unused",
                           role="employee", status="active", must_change_password=False)
        db.add(user)
        db.flush()
        db.add(models.KbGrant(kb_id=case["kb"], user_id=user.id, level=level))
        db.commit()
        return {"Authorization": f"Bearer {security.issue_token(user.id, user.role)}"}


def _add_legacy_linked_page(sessions, case):
    """An older page with only B evidence shares a real citation, but no Map input A."""
    source = case["sources"][1]
    slug = "legacy-linked"
    with sessions() as db:
        page = models.WikiPage(kb_id=case["kb"], slug=slug, title="Legacy linked page",
                               summary="Legacy source", status="published")
        db.add(page)
        db.flush()
        version = models.WikiPageVersion(page_id=page.id, version_no=1, origin="human",
                                         content_md="## 旧页面\n\n仅使用第二份资料的历史正文。")
        db.add(version)
        db.flush()
        page.current_version_id = version.id
        db.add(models.WikiEvidence(kb_id=case["kb"], page_version_id=version.id,
                                   chunk_id=source["chunk"], doc_id=source["doc"],
                                   doc_version_id=source["version"], quote="第二份资料"))
        db.commit()
    return slug


@pytest.mark.parametrize("scenario", ["truncated", "merged-shards"])
def test_published_dependencies_survive_truncation_and_cross_shard_merge(
    visibility_env, tmp_path, monkeypatch, scenario,
):
    sessions, client, _ = visibility_env
    if scenario == "truncated":
        case = _prepare(visibility_env, tmp_path, count=13, secret_no=13, old_source_no=1)
        shards = [(list(range(1, 14)), list(range(1, 14)))]
        secret_no = 13
        evidence_numbers = set(range(1, 13))
    else:
        monkeypatch.setattr(wiki, "TOPIC_SAMPLE_SIZE", 2)
        case = _prepare(visibility_env, tmp_path, count=4, secret_no=3, old_source_no=1)
        shards = [([1, 2], [1]), ([3, 4], [4])]
        secret_no = 3
        evidence_numbers = {1, 4}
    calls = _model(monkeypatch, shards=shards, secret_no=secret_no, on_page=lambda db: None)
    result, error = _build(sessions, case)
    assert error is None and result.pages_updated == 1 and calls[-1] == "page"

    before = _page(client, case, case["reader"])
    assert SECRET in before["version"]["content_md"]
    assert {e["doc_id"] for e in before["evidence"]} == {
        case["sources"][number - 1]["doc"] for number in evidence_numbers
    }
    version_id = before["version"]["id"]
    assert _dependencies(sessions, case, version_id) == {s["doc"] for s in case["sources"]}

    task_id = _delete(client, case, secret_no)
    with sessions() as db:
        assert db.get(models.Task, task_id).status == "queued"
        assert db.get(models.Document, case["sources"][secret_no - 1]["doc"]).deleted
        assert db.get(models.Chunk, case["sources"][secret_no - 1]["chunk"]) is None
    assert client.get(_url(case), headers=case["reader"]).status_code == 404
    assert client.get(_url(case) + "/versions", headers=case["reader"]).status_code == 404
    reviewed = _page(client, case, case["admin"])
    assert reviewed["version"]["id"] == version_id
    assert reviewed["status_label"] == "来源已删除，待复核"
    assert {e["doc_id"] for e in reviewed["evidence"]} == {
        case["sources"][number - 1]["doc"] for number in evidence_numbers
    }


def test_shard_visibility_search_related_and_stopped_rebuild(visibility_env, tmp_path, monkeypatch):
    sessions, client, _ = visibility_env
    monkeypatch.setattr(wiki, "TOPIC_SAMPLE_SIZE", 2)
    case = _prepare(visibility_env, tmp_path, count=4, secret_no=1, old_source_no=2)
    manager = _add_user(sessions, case, "manage")
    legacy_slug = _add_legacy_linked_page(sessions, case)
    sibling_title = "Same shard benefits"
    independent_title = "Independent remote policy"
    sibling_slug = chunking.slugify(sibling_title)
    independent_slug = chunking.slugify(independent_title)
    query = "topicindexmarker"
    map_calls = []

    def model(db, messages, **kwargs):
        prompt = messages[0]["content"]
        if kwargs.get("json_mode"):
            if "[1] Source 1" in prompt:
                assert "[2] Source 2" in prompt and SECRET in prompt
                assert "[3] Source 3" not in prompt
                map_calls.append((1, 2))
                topics = [
                    {"title": TITLE, "summary": f"summary from {SECRET}", "chunk_ids": [2]},
                    {"title": sibling_title, "summary": "unrelated topic", "chunk_ids": [2]},
                ]
            else:
                assert "[3] Source 3" in prompt and "[4] Source 4" in prompt
                assert SECRET not in prompt
                map_calls.append((3, 4))
                topics = [{"title": independent_title, "summary": "remote topic", "chunk_ids": [4]}]
            return json.dumps({"topics": topics})
        if f"主题：{TITLE}\n" in prompt:
            assert SECRET in prompt.split("资料片段：", 1)[0]
            assert SECRET not in prompt.split("资料片段：", 1)[1]
            return f"## 生成页面\n\n{query} {SECRET} " + "源于摘要的内容。" * 8 + " [2]"
        if f"主题：{sibling_title}\n" in prompt:
            return f"## 同批页面\n\n{query} " + "同批归纳出的其他内容。" * 8 + " [2]"
        assert f"主题：{independent_title}\n" in prompt and SECRET not in prompt
        return f"## 其他分片\n\n{query} " + "独立来源中的正常内容。" * 8 + " [4]"

    monkeypatch.setattr(llm, "chat", model)
    result, error = _build(sessions, case)
    assert error is None and (result.pages_created, result.pages_updated) == (2, 1)
    assert map_calls == [(1, 2), (3, 4)]
    target = _page(client, case, case["reader"])
    sibling = _page(client, case, case["reader"], sibling_slug)
    independent = _page(client, case, case["reader"], independent_slug)
    assert "deleted_generation_sources" not in target
    assert "deleted_generation_sources" not in _page(client, case, manager)
    assert _page(client, case, case["admin"]).get("deleted_generation_sources", []) == []
    docs = [source["doc"] for source in case["sources"]]
    assert _dependencies(sessions, case, target["version"]["id"]) == set(docs[:2])
    assert _dependencies(sessions, case, sibling["version"]["id"]) == set(docs[:2])
    assert _dependencies(sessions, case, independent["version"]["id"]) == set(docs[2:])
    assert {e["doc_id"] for e in target["evidence"]} == {docs[1]}
    assert {e["doc_id"] for e in sibling["evidence"]} == {docs[1]}
    assert {e["doc_id"] for e in independent["evidence"]} == {docs[3]}
    assert {sibling_slug, legacy_slug} <= {item["slug"] for item in target["related"]}
    assert independent_slug not in {item["slug"] for item in target["related"]}
    before_search = _search(client, case, case["reader"], query)
    before_hits = {hit["page_id"]: hit for hit in before_search["evidence"] if hit["source_type"] == "wiki"}
    assert {target["id"], sibling["id"], independent["id"]} <= set(before_hits)
    assert {e["doc_id"] for e in before_hits[target["id"]]["evidence"]} == {docs[1]}

    task_id = _delete(client, case, 1)
    with sessions() as db:
        task = db.get(models.Task, task_id)
        assert task.status == "queued"
        run_token = task.run_token
    for headers in (case["reader"], manager):
        tree = _tree(client, case, headers)
        assert case["slug"] not in tree and sibling_slug not in tree
        assert {independent_slug, legacy_slug} <= set(tree)
        for slug in (case["slug"], sibling_slug):
            assert client.get(_url(case, slug), headers=headers).status_code == 404
            assert client.get(_url(case, slug) + "/versions", headers=headers).status_code == 404
        assert _page(client, case, headers, independent_slug)["version"]["id"] == independent["version"]["id"]
        related = _page(client, case, headers, legacy_slug)["related"]
        assert {case["slug"], sibling_slug}.isdisjoint({item["slug"] for item in related})
        search = _search(client, case, headers, query)
        ids = {hit["page_id"] for hit in search["evidence"] if hit["source_type"] == "wiki"}
        assert independent["id"] in ids and {target["id"], sibling["id"]}.isdisjoint(ids)
        assert SECRET not in search["context_preview"]
    admin_tree = _tree(client, case, case["admin"])
    for slug in (case["slug"], sibling_slug):
        assert admin_tree[slug]["status_label"] == "来源已删除，待复核"
        assert _page(client, case, case["admin"], slug)["status_label"] == "来源已删除，待复核"
        assert client.get(_url(case, slug) + "/versions", headers=case["admin"]).status_code == 200
    reviewed = _page(client, case, case["admin"])
    assert {e["doc_id"] for e in reviewed["evidence"]} == {docs[1]}
    assert reviewed["deleted_generation_sources"] == [
        {"id": docs[0], "name": case["sources"][0]["path"].name}
    ]
    assert _page(client, case, case["admin"], independent_slug).get("deleted_generation_sources", []) == []
    assert "deleted_generation_sources" not in _page(client, case, manager, independent_slug)

    stopped = client.post(f"/api/tasks/{task_id}/stop-wiki", params={"run_token": run_token},
                          headers=case["admin"])
    assert stopped.status_code == 200, stopped.text
    with sessions() as db:
        assert db.get(models.Task, task_id).status == "stopped"
        kb = db.get(models.KnowledgeBase, case["kb"])
        hits, _ = retrieval.search_wiki(db, kb, query, top_n=20, qvec=None)
        assert independent["id"] in {hit.page_id for hit in hits}
        assert {target["id"], sibling["id"]}.isdisjoint({hit.page_id for hit in hits})
    assert client.get(_url(case), headers=case["reader"]).status_code == 404
    assert _page(client, case, case["reader"], independent_slug)["version"]["id"] == independent["version"]["id"]
    assert SECRET not in _search(client, case, case["reader"], query)["context_preview"]


def test_human_edit_inherits_current_dependencies_then_valid_rebuild_recovers(
    visibility_env, tmp_path, monkeypatch,
):
    sessions, client, _ = visibility_env
    case = _prepare(visibility_env, tmp_path, count=2, secret_no=1, old_source_no=2)
    calls = _model(monkeypatch, shards=[([1, 2], [2])], secret_no=1, on_page=lambda db: None)
    result, error = _build(sessions, case)
    assert error is None and result.pages_updated == 1 and calls[-1] == "page"
    generated = _page(client, case, case["reader"])
    generated_id = generated["version"]["id"]
    a, b = case["sources"]
    assert _dependencies(sessions, case, generated_id) == {a["doc"], b["doc"]}
    assert {e["doc_id"] for e in generated["evidence"]} == {b["doc"]}

    # Simulate a page restored to v2 while a newer v3 remains in history. v3
    # has deliberately different dependencies AND evidence from current v2.
    with sessions() as db:
        page = db.get(models.WikiPage, case["page"])
        assert page.current_version_id == generated_id
        later = models.WikiPageVersion(page_id=page.id, version_no=3, origin="generated",
                                       content_md="## 历史分支\n\n另一版本的来源不同。")
        db.add(later)
        db.flush()
        later_id = later.id
        db.add(models.WikiGenerationSource(kb_id=case["kb"], page_version_id=later_id,
                                           doc_id=b["doc"], doc_ref_id=b["doc"]))
        db.add(models.WikiEvidence(kb_id=case["kb"], page_version_id=later_id,
                                   chunk_id=a["chunk"], doc_id=a["doc"],
                                   doc_version_id=a["version"], quote="不同的历史证据"))
        db.commit()

    edit = client.put(_url(case), headers=case["admin"], json={
        "content_md": f"## 人工编辑\n\n{SECRET} 保留了先前生成内容，不能丢失来源依赖。",
    })
    assert edit.status_code == 200, edit.text
    human_id = edit.json()["version_id"]
    assert edit.json()["version_no"] == 4
    assert _dependencies(sessions, case, human_id) == {a["doc"], b["doc"]}
    assert {e["doc_id"] for e in _page(client, case, case["reader"])["evidence"]} == {b["doc"]}
    task_id = _delete(client, case, 1)
    assert client.get(_url(case), headers=case["reader"]).status_code == 404
    reviewed = _page(client, case, case["admin"])
    assert reviewed["version"]["id"] == human_id and reviewed["status_label"] == "来源已删除，待复核"

    # An administrator can edit hidden content, but that edit cannot publish it.
    hidden_edit = client.put(_url(case), headers=case["admin"], json={
        "content_md": "## 管理员复核\n\n这次编辑不能删除对已删除文档的依赖。",
    })
    assert hidden_edit.status_code == 200, hidden_edit.text
    hidden_id = hidden_edit.json()["version_id"]
    assert _dependencies(sessions, case, hidden_id) == {a["doc"], b["doc"]}
    assert {e["doc_id"] for e in _page(client, case, case["admin"])["evidence"]} == {b["doc"]}
    assert client.get(_url(case), headers=case["reader"]).status_code == 404
    unlock = client.post(_url(case) + "/unlock", headers=case["admin"])
    assert unlock.status_code == 200, unlock.text
    assert client.get(_url(case), headers=case["reader"]).status_code == 404

    clean_marker = "clean_rebuild_marker"
    rebuild_calls = []

    def clean_model(db, messages, **kwargs):
        prompt = messages[0]["content"]
        assert SECRET not in prompt and "source_01_marker" not in prompt
        assert "[1] Source 2" in prompt and "source_02_marker" in prompt
        if kwargs.get("json_mode"):
            rebuild_calls.append("map")
            return json.dumps({"topics": [{"title": TITLE, "summary": "仅剩余来源", "chunk_ids": [1]}]})
        rebuild_calls.append("page")
        return f"## 安全重建\n\n{clean_marker} " + "只依据有效来源重新生成。" * 6 + " [1]"

    monkeypatch.setattr(llm, "chat", clean_model)
    rebuilt, error = _build(sessions, case)
    assert error is None and rebuilt.pages_updated == 1 and rebuild_calls == ["map", "page"]
    with sessions() as db:
        assert db.get(models.Task, task_id).status == "queued", "no background rebuild was run"
    restored = _page(client, case, case["reader"])
    restored_id = restored["version"]["id"]
    assert restored_id not in {generated_id, later_id, human_id, hidden_id}
    assert clean_marker in restored["version"]["content_md"] and SECRET not in restored["version"]["content_md"]
    assert _dependencies(sessions, case, restored_id) == {b["doc"]}
    assert {e["doc_id"] for e in restored["evidence"]} == {b["doc"]}
    assert _page(client, case, case["admin"]).get("deleted_generation_sources", []) == []
    reader_history = client.get(_url(case) + "/versions", headers=case["reader"])
    assert reader_history.status_code == 200, reader_history.text
    reader_ids = {v["id"] for v in reader_history.json()["items"]}
    assert restored_id in reader_ids and {generated_id, later_id, human_id, hidden_id}.isdisjoint(reader_ids)
    assert SECRET not in reader_history.text
    admin_history = client.get(_url(case) + "/versions", headers=case["admin"])
    assert admin_history.status_code == 200, admin_history.text
    assert {generated_id, later_id, human_id, hidden_id, restored_id} <= {
        v["id"] for v in admin_history.json()["items"]
    }


def test_legacy_evidence_inherits_actual_current_version_and_stays_hidden(
    visibility_env, tmp_path,
):
    sessions, client, _ = visibility_env
    case = _prepare(visibility_env, tmp_path, count=2, secret_no=2, old_source_no=2)
    a, b = case["sources"]
    # The current legacy v1 has B evidence and no generation-dependency rows;
    # the greater v2 belongs to a different branch and has A evidence.
    with sessions() as db:
        page = db.get(models.WikiPage, case["page"])
        assert page.current_version_id == case["old_version"]
        later = models.WikiPageVersion(page_id=page.id, version_no=2, origin="generated",
                                       content_md="## 历史分支\n\n只使用另一份资料。")
        db.add(later)
        db.flush()
        db.add(models.WikiEvidence(kb_id=case["kb"], page_version_id=later.id,
                                   chunk_id=a["chunk"], doc_id=a["doc"],
                                   doc_version_id=a["version"], quote="另一份资料"))
        db.commit()
    assert _dependencies(sessions, case, case["old_version"]) == set()
    edit = client.put(_url(case), headers=case["admin"], json={
        "content_md": "## 旧版人工编辑\n\n编辑保留当前版本的旧证据。",
    })
    assert edit.status_code == 200, edit.text
    assert {e["doc_id"] for e in _page(client, case, case["reader"])["evidence"]} == {b["doc"]}
    _delete(client, case, 2)
    assert client.get(_url(case), headers=case["reader"]).status_code == 404
    edit_hidden = client.put(_url(case), headers=case["admin"], json={
        "content_md": "## 再次复核\n\n编辑历史页面不能绕过已删除的旧证据。",
    })
    assert edit_hidden.status_code == 200, edit_hidden.text
    assert client.get(_url(case), headers=case["reader"]).status_code == 404
    reviewed = _page(client, case, case["admin"])
    assert reviewed["status_label"] == "来源已删除，待复核"
    assert {e["doc_id"] for e in reviewed["evidence"]} == {b["doc"]}
    assert all(e["chunk_id"] is None for e in reviewed["evidence"])


def test_multiple_chunks_deduplicate_and_reused_document_id_stays_hidden(
    visibility_env, tmp_path, monkeypatch,
):
    sessions, client, _ = visibility_env
    # A is the highest Document ID, so SQLite can reuse it after physical deletion.
    case = _prepare(visibility_env, tmp_path, count=2, secret_no=2, old_source_no=1)
    b, a = case["sources"]
    assert a["doc"] > b["doc"]
    with sessions() as db:
        db.add(models.Chunk(kb_id=case["kb"], doc_id=a["doc"], doc_version_id=a["version"],
                            ordinal=1, heading="Source 2 extra", locator="第二段",
                            text="same_document_extra_input", keywords="extra input"))
        db.commit()
    calls = []

    def model(db, messages, **kwargs):
        prompt = messages[0]["content"]
        if kwargs.get("json_mode"):
            assert "[1] Source 1" in prompt and "[2] Source 2" in prompt
            assert "[3] Source 2 extra" in prompt and "same_document_extra_input" in prompt
            assert SECRET in prompt
            calls.append("map")
            return json.dumps({"topics": [{"title": TITLE, "summary": f"归纳 {SECRET}", "chunk_ids": [1]}]})
        assert SECRET in prompt.split("资料片段：", 1)[0]
        assert SECRET not in prompt.split("资料片段：", 1)[1]
        calls.append("page")
        return f"## 归纳\n\n{SECRET} " + "多个分块同属一份输入文档。" * 6 + " [1]"

    monkeypatch.setattr(llm, "chat", model)
    result, error = _build(sessions, case)
    assert error is None and result.pages_updated == 1 and calls == ["map", "page"]
    current = _page(client, case, case["reader"])
    version_id = current["version"]["id"]
    assert _dependencies(sessions, case, version_id) == {a["doc"], b["doc"]}
    assert {e["doc_id"] for e in current["evidence"]} == {b["doc"]}
    assert _page(client, case, case["admin"]).get("deleted_generation_sources", []) == []
    with sessions() as db:
        refs = dict(db.execute(select(models.WikiGenerationSource.doc_id,
                                      models.WikiGenerationSource.doc_ref_id).where(
            models.WikiGenerationSource.page_version_id == version_id)).all())
        assert refs == {a["doc"]: a["doc"], b["doc"]: b["doc"]}
        hits, _ = retrieval.search_wiki(db, db.get(models.KnowledgeBase, case["kb"]),
                                         SECRET, top_n=20, qvec=None)
        assert current["id"] in {hit.page_id for hit in hits}

    _delete(client, case, 2)
    assert client.get(_url(case), headers=case["reader"]).status_code == 404
    reviewed = _page(client, case, case["admin"])
    assert reviewed["deleted_generation_sources"] == [{"id": a["doc"], "name": a["path"].name}]
    assert {e["doc_id"] for e in reviewed["evidence"]} == {b["doc"]}

    # doc_id preserves the source identity; doc_ref_id is cleared by the FK.
    # A missing row or a later row reusing its ID must not make it live again.
    with sessions() as db:
        assert db.get(models.Document, a["doc"]).deleted
        db.execute(models.Document.__table__.delete().where(models.Document.id == a["doc"]))
        db.commit()
    with sessions() as db:
        assert db.get(models.Document, a["doc"]) is None
        ref = db.execute(select(models.WikiGenerationSource.doc_ref_id).where(
            models.WikiGenerationSource.page_version_id == version_id,
            models.WikiGenerationSource.doc_id == a["doc"])).scalar_one()
        assert ref is None
    assert _dependencies(sessions, case, version_id) == {a["doc"], b["doc"]}
    assert client.get(_url(case), headers=case["reader"]).status_code == 404
    assert case["slug"] not in _tree(client, case, case["reader"])
    assert client.get(_url(case) + "/versions", headers=case["reader"]).status_code == 404
    reviewed = _page(client, case, case["admin"])
    assert reviewed["version"]["id"] == version_id
    assert reviewed["status_label"] == "来源已删除，待复核"
    missing = reviewed["deleted_generation_sources"]
    assert len(missing) == 1 and missing[0]["id"] == a["doc"]
    assert isinstance(missing[0]["name"], str) and missing[0]["name"].strip()
    assert {e["doc_id"] for e in reviewed["evidence"]} == {b["doc"]}

    replacement_path = tmp_path / "unrelated-reused-id.md"
    replacement_path.write_text("Unrelated replacement content", encoding="utf-8")
    with sessions() as db:
        replacement = models.Document(kb_id=case["kb"], filename=replacement_path.name, status="ready")
        db.add(replacement)
        db.flush()
        assert replacement.id == a["doc"], "SQLite must reuse the deleted highest document ID"
        version = models.DocumentVersion(doc_id=replacement.id, version_no=1,
                                         storage_path=str(replacement_path), status="success")
        db.add(version)
        db.flush()
        replacement.current_version_id = version.id
        db.add(models.Chunk(kb_id=case["kb"], doc_id=replacement.id, doc_version_id=version.id,
                            ordinal=0, text="Unrelated replacement content", heading="Replacement",
                            locator="Replacement", keywords="unrelated replacement"))
        db.commit()

    with sessions() as db:
        assert not db.get(models.Document, a["doc"]).deleted
        assert db.get(models.Document, a["doc"]).filename == replacement_path.name
        ref = db.execute(select(models.WikiGenerationSource.doc_ref_id).where(
            models.WikiGenerationSource.page_version_id == version_id,
            models.WikiGenerationSource.doc_id == a["doc"])).scalar_one()
        assert ref is None, "the replacement must not repair the old dependency"
        hits, _ = retrieval.search_wiki(db, db.get(models.KnowledgeBase, case["kb"]),
                                         SECRET, top_n=20, qvec=None)
        assert current["id"] not in {hit.page_id for hit in hits}
    assert _dependencies(sessions, case, version_id) == {a["doc"], b["doc"]}
    assert client.get(_url(case), headers=case["reader"]).status_code == 404
    assert client.get(_url(case) + "/versions", headers=case["reader"]).status_code == 404
    assert case["slug"] not in _tree(client, case, case["reader"])
    assert SECRET not in _search(client, case, case["reader"], SECRET)["context_preview"]
    reviewed_again = _page(client, case, case["admin"])
    assert reviewed_again["version"]["id"] == version_id
    assert reviewed_again["status_label"] == "来源已删除，待复核"
    assert reviewed_again["deleted_generation_sources"] == missing
    assert reviewed_again["deleted_generation_sources"][0]["name"] != replacement_path.name
    assert {e["doc_id"] for e in reviewed_again["evidence"]} == {b["doc"]}


def test_legacy_evidence_only_page_stays_hidden_after_document_id_reuse(
    visibility_env, tmp_path,
):
    """The old evidence path cannot mistake a replacement row for its source."""
    sessions, client, _ = visibility_env
    case = _prepare(visibility_env, tmp_path, count=2, secret_no=2, old_source_no=2)
    source = case["sources"][1]
    assert source["doc"] > case["sources"][0]["doc"]
    manager = _add_user(sessions, case, "manage")

    # A genuinely legacy page: no WikiGenerationSource rows exist. The page
    # and its indexed section are retained while deletion removes its chunk.
    with sessions() as db:
        version = db.get(models.WikiPageVersion, case["old_version"])
        version.content_md = f"## 旧文档正文\n\n{SECRET} 这是删除前的历史资料内容。"
        db.add(models.WikiSection(kb_id=case["kb"], page_id=case["page"],
                                  page_version_id=version.id, ordinal=0,
                                  heading_path="旧文档正文", text=version.content_md,
                                  keywords=SECRET))
        db.commit()
    assert _dependencies(sessions, case, case["old_version"]) == set()
    before = _page(client, case, manager)
    assert before["version"]["id"] == case["old_version"] and SECRET in before["version"]["content_md"]
    assert {e["doc_id"] for e in before["evidence"]} == {source["doc"]}

    task_id = _delete(client, case, 2)
    with sessions() as db:
        assert db.get(models.Task, task_id).status == "queued"
        assert db.get(models.Document, source["doc"]).deleted
        assert db.get(models.Chunk, source["chunk"]) is None
        evidence = db.execute(select(models.WikiEvidence).where(
            models.WikiEvidence.page_version_id == case["old_version"])).scalar_one()
        assert evidence.doc_id == source["doc"] and evidence.chunk_id is None
    for headers in (case["reader"], manager):
        assert client.get(_url(case), headers=headers).status_code == 404
    admin_before = _page(client, case, case["admin"])
    assert admin_before["version"]["id"] == case["old_version"]
    assert admin_before["status_label"] == "来源已删除，待复核"

    # Delete the highest SQLite ROWID, then let SQLite allocate the new row;
    # explicitly verify reuse rather than assuming the fixture made it happen.
    with sessions() as db:
        db.execute(models.Document.__table__.delete().where(models.Document.id == source["doc"]))
        db.commit()
    with sessions() as db:
        assert db.get(models.Document, source["doc"]) is None
        assert db.get(models.WikiPageVersion, case["old_version"]).content_md.find(SECRET) >= 0
        replacement = models.Document(kb_id=case["kb"], filename="legacy-replacement.md", status="ready")
        db.add(replacement)
        db.flush()
        assert replacement.id == source["doc"], "SQLite must reuse the deleted maximum document ID"
        assert not replacement.deleted
        db.commit()
    assert _dependencies(sessions, case, case["old_version"]) == set()
    with sessions() as db:
        assert not db.get(models.Document, source["doc"]).deleted
        assert db.get(models.WikiPage, case["page"]).current_version_id == case["old_version"]
        assert db.get(models.WikiPageVersion, case["old_version"]).content_md.find(SECRET) >= 0

    for headers in (case["reader"], manager):
        page = client.get(_url(case), headers=headers)
        assert page.status_code == 404, f"legacy page reappeared after ID reuse: {page.text}"
        assert case["slug"] not in _tree(client, case, headers)
        assert client.get(_url(case) + "/versions", headers=headers).status_code == 404
        assert SECRET not in _search(client, case, headers, SECRET)["context_preview"]
    reviewed = _page(client, case, case["admin"])
    assert reviewed["version"]["id"] == case["old_version"]
    assert reviewed["status_label"] == "来源已删除，待复核"
    assert SECRET in reviewed["version"]["content_md"]


def test_manager_cannot_republish_cached_pre_delete_text_after_clean_rebuild(
    visibility_env, tmp_path, monkeypatch,
):
    """An edit based on cached old text must not inherit only the newer safe sources."""
    sessions, client, _ = visibility_env
    case = _prepare(visibility_env, tmp_path, count=2, secret_no=1, old_source_no=2)
    a, b = case["sources"]
    manager = _add_user(sessions, case, "manage")

    # Start with no Wiki versions so the real build publishes actual v1.
    # This synthetic fixture reset occurs before either user reads the page.
    with sessions() as db:
        db.execute(models.WikiEvidence.__table__.delete().where(
            models.WikiEvidence.page_version_id == case["old_version"]))
        page = db.get(models.WikiPage, case["page"])
        page.current_version_id = None
        db.execute(models.WikiPageVersion.__table__.delete().where(
            models.WikiPageVersion.id == case["old_version"]))
        db.commit()
    calls = _model(monkeypatch, shards=[([1, 2], [2])], secret_no=1, on_page=lambda db: None)
    result, error = _build(sessions, case)
    assert error is None and result.pages_updated == 1 and calls[-1] == "page"
    original = _page(client, case, manager)
    cached = original["version"]["content_md"]
    original_id = original["version"]["id"]
    assert original["version"]["version_no"] == 1 and SECRET in cached
    assert {e["doc_id"] for e in original["evidence"]} == {b["doc"]}
    assert _dependencies(sessions, case, original_id) == {a["doc"], b["doc"]}

    task_id = _delete(client, case, 1)
    for headers in (case["reader"], manager):
        assert client.get(_url(case), headers=headers).status_code == 404
    assert _page(client, case, case["admin"])["version"]["id"] == original_id

    clean_marker = "only_live_b_generation"
    rebuild_calls = []

    def clean_model(db, messages, **kwargs):
        prompt = messages[0]["content"]
        assert SECRET not in prompt and "source_01_marker" not in prompt
        assert "[1] Source 2" in prompt and "source_02_marker" in prompt
        if kwargs.get("json_mode"):
            rebuild_calls.append("map")
            return json.dumps({"topics": [{"title": TITLE, "summary": "仅有效来源", "chunk_ids": [1]}]})
        rebuild_calls.append("page")
        return f"## 新正文\n\n{clean_marker} " + "仅根据有效的第二份资料生成。" * 6 + " [1]"

    monkeypatch.setattr(llm, "chat", clean_model)
    rebuilt, error = _build(sessions, case)
    assert error is None and rebuilt.pages_updated == 1 and rebuild_calls == ["map", "page"]
    clean = _page(client, case, case["reader"])
    assert clean["version"]["version_no"] == 2 and clean["version"]["id"] != original_id
    assert clean_marker in clean["version"]["content_md"] and SECRET not in clean["version"]["content_md"]
    assert _dependencies(sessions, case, clean["version"]["id"]) == {b["doc"]}
    assert {e["doc_id"] for e in clean["evidence"]} == {b["doc"]}
    with sessions() as db:
        assert db.get(models.Task, task_id).status == "queued", "the explicit build, not a worker, restored v2"

    # A legacy client submits its cached v1 text with the existing request body.
    # Either reject it or retain enough provenance to hide it; don't require a
    # particular new request field or prescribe how the implementation works.
    response = client.put(_url(case), headers=manager, json={"content_md": cached})
    assert response.status_code in (200, 400, 403, 404, 409, 412, 422, 428), response.text
    for headers in (case["reader"], manager):
        page = client.get(_url(case), headers=headers)
        assert page.status_code in (200, 404), page.text
        assert SECRET not in page.text, "cached deleted-source text was republished"
        tree = client.get(f"/api/kbs/{case['kb']}/wiki/tree", headers=headers)
        assert tree.status_code == 200 and SECRET not in tree.text
        history = client.get(_url(case) + "/versions", headers=headers)
        assert history.status_code in (200, 404) and SECRET not in history.text
        assert SECRET not in _search(client, case, headers, SECRET)["context_preview"]
    admin_history = client.get(_url(case) + "/versions", headers=case["admin"])
    assert admin_history.status_code == 200 and SECRET in admin_history.text


def test_cross_shard_citation_stays_hidden_when_document_id_reused_in_another_kb(
    visibility_env, tmp_path, monkeypatch,
):
    """A cited non-Map source remains a dependency, even after its ID is reused."""
    sessions, client, _ = visibility_env
    monkeypatch.setattr(wiki, "TOPIC_SAMPLE_SIZE", 1)
    case = _prepare(visibility_env, tmp_path, count=2, secret_no=2, old_source_no=1)
    a, b = case["sources"]
    assert a["doc"] < b["doc"], "B must be SQLite's highest document ID"
    other_kb_name = "other-private-kb"
    replacement_name = "other-kb-private-document-name.md"
    with sessions() as db:
        other = models.KnowledgeBase(name=other_kb_name, mode="wiki", status="ready")
        db.add(other)
        db.commit()
        other_kb_id = other.id
    calls = []
    beta_title = "Unrelated Beta source"

    def model(db, messages, **kwargs):
        prompt = messages[0]["content"]
        if kwargs.get("json_mode"):
            if "[1] Source 1" in prompt:
                assert "source_01_marker" in prompt
                assert "source_02_marker" not in prompt and SECRET not in prompt
                assert "[2] Source 2" not in prompt
                calls.append("map A")
                return json.dumps({"topics": [{"title": TITLE, "summary": "仅归纳 A", "chunk_ids": [1]}]})
            assert "[2] Source 2" in prompt and "source_02_marker" in prompt and SECRET in prompt
            assert "[1] Source 1" not in prompt
            calls.append("map B")
            return json.dumps({"topics": [{"title": beta_title, "summary": "仅归纳 B", "chunk_ids": [2]}]})
        if f"主题：{TITLE}\n" in prompt:
            context = prompt.split("资料片段：", 1)[1]
            assert "[1] Source 1" in context and "[2] Source 2" not in context
            assert SECRET not in prompt
            calls.append("page Alpha")
            # Model fabricates a valid global citation outside this topic's Map shard.
            return "## Alpha 主题\n\n" + "这里测试跨分片引用对来源可见性的影响。" * 5 + " [2]"
        assert f"主题：{beta_title}\n" in prompt and "[2] Source 2" in prompt
        calls.append("page Beta")
        return "## Beta 主题\n\n" + "独立分片的正常内容仅用于完成真实生成。" * 5 + " [2]"

    monkeypatch.setattr(llm, "chat", model)
    result, error = _build(sessions, case)
    assert error is None and result.pages_updated == 1 and result.pages_created == 1
    assert calls == ["map A", "map B", "page Alpha", "page Beta"]
    before = _page(client, case, case["reader"])
    version_id = before["version"]["id"]
    assert "[2]" in before["version"]["content_md"]
    assert {e["doc_id"] for e in before["evidence"]} == {a["doc"], b["doc"]}
    assert _dependencies(sessions, case, version_id) == {a["doc"], b["doc"]}
    with sessions() as db:
        refs = dict(db.execute(select(models.WikiGenerationSource.doc_id,
                                      models.WikiGenerationSource.doc_ref_id).where(
            models.WikiGenerationSource.page_version_id == version_id)).all())
        assert refs == {a["doc"]: a["doc"], b["doc"]: b["doc"]}

    task_id = _delete(client, case, 2)
    with sessions() as db:
        assert db.get(models.Task, task_id).status == "queued"
        assert db.get(models.Document, b["doc"]).deleted
        assert db.get(models.Chunk, b["chunk"]) is None
    hidden = client.get(_url(case), headers=case["reader"])
    assert hidden.status_code == 404 and replacement_name not in hidden.text
    assert _page(client, case, case["admin"])["status_label"] == "来源已删除，待复核"

    with sessions() as db:
        db.execute(models.Document.__table__.delete().where(models.Document.id == b["doc"]))
        db.commit()
    with sessions() as db:
        assert db.get(models.Document, b["doc"]) is None
        ref = db.execute(select(models.WikiGenerationSource.doc_ref_id).where(
            models.WikiGenerationSource.page_version_id == version_id,
            models.WikiGenerationSource.doc_id == b["doc"])).scalar_one()
        assert ref is None
        replacement = models.Document(kb_id=other_kb_id, filename=replacement_name, status="ready")
        db.add(replacement)
        db.flush()
        assert replacement.id == b["doc"], "the other KB document must reuse B's ID"
        db.commit()
    with sessions() as db:
        replacement = db.get(models.Document, b["doc"])
        assert replacement.kb_id == other_kb_id and not replacement.deleted
        ref = db.execute(select(models.WikiGenerationSource.doc_ref_id).where(
            models.WikiGenerationSource.page_version_id == version_id,
            models.WikiGenerationSource.doc_id == b["doc"])).scalar_one()
        assert ref is None
    assert _dependencies(sessions, case, version_id) == {a["doc"], b["doc"]}

    hidden = client.get(_url(case), headers=case["reader"])
    assert hidden.status_code == 404, hidden.text
    assert replacement_name not in hidden.text
    history = client.get(_url(case) + "/versions", headers=case["reader"])
    assert history.status_code == 404 and replacement_name not in history.text
    tree = client.get(f"/api/kbs/{case['kb']}/wiki/tree", headers=case["reader"])
    assert tree.status_code == 200 and case["slug"] not in {p["slug"] for p in tree.json()["items"]}
    assert replacement_name not in tree.text
    search = _search(client, case, case["reader"], "Alpha")
    assert replacement_name not in json.dumps(search, ensure_ascii=False)
    assert before["id"] not in {item["page_id"] for item in search["evidence"] if item["page_id"]}
    reviewed = client.get(_url(case), headers=case["admin"])
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["version"]["id"] == version_id
    assert reviewed.json()["status_label"] == "来源已删除，待复核"
    assert replacement_name not in reviewed.text, "the reused ID must not reveal the other KB filename"


def test_expected_version_id_rejects_a_stale_edit(visibility_env, tmp_path):
    """界面随草稿提交的 expected_version_id 必须由编辑接口真正校验。"""
    sessions, client, _ = visibility_env
    case = _prepare(visibility_env, tmp_path, count=2, secret_no=1, old_source_no=2)
    manager = _add_user(sessions, case, "manage")
    version_id = _page(client, case, manager)["version"]["id"]
    body = {"content_md": "## 人工复核\n\n这是一份长度足够的全新人工正文。"}

    stale = client.put(_url(case), headers=manager,
                       json={**body, "expected_version_id": version_id + 1000})
    assert stale.status_code == 409, stale.text
    assert _page(client, case, manager)["version"]["id"] == version_id

    accepted = client.put(_url(case), headers=manager,
                          json={**body, "expected_version_id": version_id})
    assert accepted.status_code == 200, accepted.text
    assert _page(client, case, manager)["version"]["id"] != version_id
