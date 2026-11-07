"""Deleted Wiki sources must stop contributing to ordinary views immediately.

The fixtures write a small, deterministic index directly so the tests exercise the
API, source links and retrieval without running a model or depending on a worker.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app import models, security, serializers
from app.services import answering, llm, retrieval, wiki


@pytest.fixture(autouse=True)
def offline_models(monkeypatch):
    """No network calls, including when a reviewed page is re-indexed."""
    def unavailable(*args, **kwargs):
        raise llm.ModelUnavailable("offline test")

    monkeypatch.setattr(llm, "embed", unavailable)
    monkeypatch.setattr(llm, "chat", unavailable)


@pytest.fixture
def wiki_case(app_client, admin_headers, db_session, tmp_path):
    db = db_session
    suffix = uuid.uuid4().hex[:10]
    kb = models.KnowledgeBase(name=f"deleted-source-{suffix}", mode="wiki", status="ready")
    db.add(kb)
    db.flush()

    docs = {}
    for key, heading, text in (
        ("removed", "删除来源的原文", "secretlocktoken 旧文档原文与证据。"),
        ("live", "有效来源的原文", "survivortoken 仍然有效的资料。"),
    ):
        doc = models.Document(kb_id=kb.id, filename=f"{key}-{suffix}.md", status="ready")
        db.add(doc)
        db.flush()
        version = models.DocumentVersion(
            doc_id=doc.id, version_no=1, storage_path=str(tmp_path / f"{key}-{suffix}.md"),
            status="success",
        )
        db.add(version)
        db.flush()
        doc.current_version_id = version.id
        chunk = models.Chunk(
            kb_id=kb.id, doc_id=doc.id, doc_version_id=version.id, ordinal=0,
            heading=heading, locator="第 1 段", text=text, keywords=text,
        )
        db.add(chunk)
        db.flush()
        docs[key] = {"doc": doc.id, "version": version.id, "chunk": chunk.id}

    pages = {}

    def add_page(key, title, sources, *, locked=False, status="published"):
        page = models.WikiPage(
            kb_id=kb.id, slug=key, title=title, status=status,
            human_locked=locked, summary=f"{title} 摘要",
        )
        db.add(page)
        db.flush()
        content = f"## {title}\n\n{key} secretlocktoken {title} 的正文内容，供员工检索。"
        version = models.WikiPageVersion(
            page_id=page.id, version_no=1, content_md=content,
            summary=title, origin="human" if locked else "generated",
        )
        db.add(version)
        db.flush()
        page.current_version_id = version.id
        db.add(models.WikiSection(
            kb_id=kb.id, page_id=page.id, page_version_id=version.id, ordinal=0,
            heading_path=title, text=content, keywords=content,
        ))
        for source in sources:
            ref = docs[source]
            db.add(models.WikiEvidence(
                kb_id=kb.id, page_version_id=version.id, chunk_id=ref["chunk"],
                doc_id=ref["doc"], doc_version_id=ref["version"],
                quote=f"{source} 证据摘录", locator="第 1 段",
            ))
        pages[key] = {"id": page.id, "version": version.id, "title": title, "content": content}

    add_page("locked-old", "绝密锁定流程", ["removed"], locked=True)
    add_page("unlocked-old", "旧版保密政策", ["removed"])
    add_page("mixed-old", "混合来源标准", ["removed", "live"], locked=True)
    add_page("unaffected", "正常福利政策", ["live"])
    add_page("review-unaffected", "正常待审页面", ["live"], status="needs_review")

    headers = {}
    for level in ("read", "manage"):
        user = models.User(
            username=f"{level}-{suffix}", display_name=level, password_hash="unused",
            role="employee", status="active",
        )
        db.add(user)
        db.flush()
        db.add(models.KbGrant(kb_id=kb.id, user_id=user.id, level=level))
        headers[level] = {"Authorization": f"Bearer {security.issue_token(user.id, user.role)}"}
    db.commit()
    return {"kb": kb.id, "docs": docs, "pages": pages, "headers": headers}


def _delete_source(client, admin_headers, case):
    result = client.delete(f"/api/documents/{case['docs']['removed']['doc']}", headers=admin_headers)
    assert result.status_code == 200, result.text
    assert result.json()["rebuild_task_id"] is not None


def _page_url(case, slug):
    return f"/api/kbs/{case['kb']}/wiki/pages/{slug}"


def _tree(client, headers, case):
    response = client.get(f"/api/kbs/{case['kb']}/wiki/tree", headers=headers)
    assert response.status_code == 200, response.text
    return {item["slug"]: item for item in response.json()["items"]}


@pytest.mark.parametrize("level", ["read", "manage"])
def test_deletion_immediately_hides_affected_pages_from_employees(
    app_client, admin_headers, db_session, wiki_case, level,
):
    headers = wiki_case["headers"][level]
    assert set(_tree(app_client, headers, wiki_case)) == set(wiki_case["pages"])
    _delete_source(app_client, admin_headers, wiki_case)

    # Check before running the queued rebuild: hiding cannot depend on its success.
    visible = _tree(app_client, headers, wiki_case)
    assert set(visible) == {"unaffected", "review-unaffected"}
    kb_url = f"/api/kbs/{wiki_case['kb']}"
    assert app_client.get(kb_url, headers=headers).json()["stats"]["wiki_page_count"] == 2
    assert app_client.get(kb_url, headers=admin_headers).json()["stats"]["wiki_page_count"] == 5
    for slug in ("locked-old", "unlocked-old", "mixed-old"):
        assert app_client.get(_page_url(wiki_case, slug), headers=headers).status_code == 404
        assert app_client.get(_page_url(wiki_case, slug) + "/versions", headers=headers).status_code == 404
    assert app_client.get(_page_url(wiki_case, "unaffected"), headers=headers).status_code == 200
    assert app_client.get(_page_url(wiki_case, "review-unaffected"), headers=headers).status_code == 200

    db_session.expire_all()
    source = wiki_case["docs"]["removed"]
    assert db_session.get(models.Document, source["doc"]).deleted is True
    assert db_session.get(models.DocumentVersion, source["version"]) is not None
    assert db_session.get(models.Chunk, source["chunk"]) is None
    for slug in ("locked-old", "unlocked-old", "mixed-old"):
        page = wiki_case["pages"][slug]
        assert db_session.get(models.WikiPageVersion, page["version"]).content_md == page["content"]
        evidence = db_session.execute(select(models.WikiEvidence).where(
            models.WikiEvidence.page_version_id == page["version"],
            models.WikiEvidence.doc_id == source["doc"],
        )).scalar_one()
        assert evidence.doc_version_id == source["version"]
        assert evidence.chunk_id is None


@pytest.mark.parametrize("level", ["read", "manage"])
@pytest.mark.parametrize("action", ["edit", "unlock", "publish"])
def test_employee_cannot_mutate_a_hidden_page(
    app_client, admin_headers, wiki_case, level, action,
):
    _delete_source(app_client, admin_headers, wiki_case)
    url = _page_url(wiki_case, "locked-old")
    headers = wiki_case["headers"][level]
    if action == "edit":
        response = app_client.put(url, headers=headers, json={"content_md": "未授权的人工修改内容"})
    else:
        response = app_client.post(url + f"/{action}", headers=headers)
    assert response.status_code in (403, 404), response.text


def test_admin_can_review_hidden_content_history_and_source_links(
    app_client, admin_headers, wiki_case,
):
    _delete_source(app_client, admin_headers, wiki_case)
    tree = _tree(app_client, admin_headers, wiki_case)
    for slug in ("locked-old", "unlocked-old", "mixed-old"):
        assert tree[slug]["status_label"] == "来源已删除，待复核"
        response = app_client.get(_page_url(wiki_case, slug), headers=admin_headers)
        assert response.status_code == 200, response.text
        page = response.json()
        assert page["status_label"] == "来源已删除，待复核"
        assert page["version"]["content_md"] == wiki_case["pages"][slug]["content"]
        assert wiki_case["docs"]["removed"]["doc"] in {e["doc_id"] for e in page["evidence"]}
        history = app_client.get(_page_url(wiki_case, slug) + "/versions", headers=admin_headers)
        assert history.status_code == 200, history.text
        assert wiki_case["pages"][slug]["version"] in {v["id"] for v in history.json()["items"]}


def test_hidden_pages_are_not_searchable_or_suggested(
    app_client, admin_headers, db_session, wiki_case,
):
    kb = db_session.get(models.KnowledgeBase, wiki_case["kb"])
    before, _ = retrieval.search_wiki(db_session, kb, "secretlocktoken", top_n=20, qvec=None)
    assert wiki_case["pages"]["locked-old"]["id"] in {hit.page_id for hit in before}
    _delete_source(app_client, admin_headers, wiki_case)
    db_session.expire_all()
    kb = db_session.get(models.KnowledgeBase, wiki_case["kb"])
    hidden_ids = {wiki_case["pages"][slug]["id"] for slug in ("locked-old", "unlocked-old", "mixed-old")}

    hits, _ = retrieval.search_wiki(db_session, kb, "secretlocktoken", top_n=20, qvec=None)
    assert hidden_ids.isdisjoint({hit.page_id for hit in hits})
    assert wiki_case["pages"]["unaffected"]["id"] in {hit.page_id for hit in hits}

    for headers in (wiki_case["headers"]["read"], wiki_case["headers"]["manage"], admin_headers):
        response = app_client.post("/api/search/preview", headers=headers, json={
            "kb_ids": [wiki_case["kb"]], "query": "secretlocktoken",
        })
        assert response.status_code == 200, response.text
        data = response.json()
        assert hidden_ids.isdisjoint({e["page_id"] for e in data["evidence"] if e["page_id"]})
        for slug in ("locked-old", "unlocked-old", "mixed-old"):
            assert wiki_case["pages"][slug]["title"] not in data["context_preview"]

    suggestions = answering._question_items(db_session, [kb], limit=20)
    assert any("正常福利政策" in item["title"] for item in suggestions)
    for slug in ("locked-old", "unlocked-old", "mixed-old"):
        assert wiki_case["pages"][slug]["title"] not in str(suggestions)


@pytest.mark.parametrize("level", ["read", "manage"])
def test_related_pages_do_not_reveal_hidden_titles(app_client, admin_headers, wiki_case, level):
    headers = wiki_case["headers"][level]
    url = _page_url(wiki_case, "unaffected")
    before = app_client.get(url, headers=headers).json()["related"]
    assert "mixed-old" in {item["slug"] for item in before}
    _delete_source(app_client, admin_headers, wiki_case)
    response = app_client.get(url, headers=headers)
    assert response.status_code == 200, response.text
    assert "mixed-old" not in {item["slug"] for item in response.json()["related"]}


def test_hidden_only_wiki_does_not_count_as_ready_or_route_to_wiki(
    app_client, admin_headers, db_session, wiki_case,
):
    # Keep only affected pages; no active chunks remain after deletion.
    live_doc = db_session.get(models.Document, wiki_case["docs"]["live"]["doc"])
    live_doc.deleted = True
    db_session.query(models.Chunk).filter(models.Chunk.doc_id == live_doc.id).delete()
    db_session.commit()
    _delete_source(app_client, admin_headers, wiki_case)
    headers = wiki_case["headers"]["read"]
    detail = app_client.get(f"/api/kbs/{wiki_case['kb']}", headers=headers).json()
    tree = app_client.get(f"/api/kbs/{wiki_case['kb']}/wiki/tree", headers=headers).json()
    assert detail["wiki_ready"] is False
    assert detail["index"]["wiki_ready"] is False
    assert tree["wiki_ready"] is False
    db_session.expire_all()
    kb = db_session.get(models.KnowledgeBase, wiki_case["kb"])
    assert serializers.wiki_published_count(db_session, kb.id) == 0
    assert retrieval.plan_route(db_session, kb)[0] == "none"


def test_historical_deleted_source_version_is_not_exposed_after_regeneration(
    app_client, admin_headers, db_session, wiki_case,
):
    _delete_source(app_client, admin_headers, wiki_case)
    db_session.expire_all()
    page = db_session.get(models.WikiPage, wiki_case["pages"]["locked-old"]["id"])
    live = wiki_case["docs"]["live"]
    generated = models.WikiPageVersion(
        page_id=page.id, version_no=2, content_md="## 新版\n\n只依据仍然有效的来源重新生成正文。",
        summary="新版", origin="generated",
    )
    db_session.add(generated)
    db_session.flush()
    page.current_version_id = generated.id
    page.human_locked = False
    page.status = "published"
    db_session.add(models.WikiEvidence(
        kb_id=wiki_case["kb"], page_version_id=generated.id, chunk_id=live["chunk"],
        doc_id=live["doc"], doc_version_id=live["version"], quote="有效来源", locator="第 1 段",
    ))
    db_session.commit()

    url = _page_url(wiki_case, "locked-old")
    for level in ("read", "manage"):
        headers = wiki_case["headers"][level]
        assert app_client.get(url, headers=headers).status_code == 200
        response = app_client.get(url + "/versions", headers=headers)
        assert response.status_code == 200, response.text
        ids = {v["id"] for v in response.json()["items"]}
        assert generated.id in ids
        assert wiki_case["pages"]["locked-old"]["version"] not in ids
    admin_history = app_client.get(url + "/versions", headers=admin_headers).json()["items"]
    assert {generated.id, wiki_case["pages"]["locked-old"]["version"]} <= {v["id"] for v in admin_history}


def test_admin_edit_and_unlock_do_not_unhide_until_valid_regeneration(
    app_client, admin_headers, db_session, wiki_case, monkeypatch,
):
    _delete_source(app_client, admin_headers, wiki_case)
    url = _page_url(wiki_case, "locked-old")
    reader = wiki_case["headers"]["read"]
    edit = app_client.put(url, headers=admin_headers, json={
        "content_md": "## 人工复核\n\n这是保留的人工正文，不能因为编辑而重新公开。",
    })
    assert edit.status_code == 200, edit.text
    assert app_client.get(url, headers=reader).status_code == 404
    unlock = app_client.post(url + "/unlock", headers=admin_headers)
    assert unlock.status_code == 200, unlock.text
    assert app_client.get(url, headers=reader).status_code == 404

    # Generate the same slug exclusively from the remaining live document.
    db_session.expire_all()
    kb = db_session.get(models.KnowledgeBase, wiki_case["kb"])
    # The fixture uses an explicit slug. Generate a title whose real slugify
    # result matches it so build_wiki updates the existing page.
    monkeypatch.setattr(wiki, "_map_topics", lambda db, entries, **kwargs: [{
        "title": "locked-old", "summary": "有效来源", "chunk_ids": [idx for idx, _ in entries],
    }])
    wiki.build_wiki(db_session, kb)
    db_session.commit()
    response = app_client.get(url, headers=reader)
    assert response.status_code == 200, response.text
    assert {e["doc_id"] for e in response.json()["evidence"]} == {wiki_case["docs"]["live"]["doc"]}


def test_rebuild_does_not_publish_a_version_from_a_source_deleted_during_generation(
    app_client, admin_headers, db_session, wiki_case, monkeypatch,
):
    """A model call can overlap deletion after build_wiki snapshots its chunks."""
    kb = db_session.get(models.KnowledgeBase, wiki_case["kb"])
    removed_id = wiki_case["docs"]["removed"]["doc"]
    old_version = wiki_case["pages"]["unlocked-old"]["version"]
    monkeypatch.setattr(wiki, "_map_topics", lambda db, entries, **kwargs: [{
        "title": "unlocked-old", "summary": "旧来源", "chunk_ids": [
            idx for idx, chunk in entries if chunk.doc_id == removed_id
        ],
    }])
    called = False

    def delete_while_generating(db, messages, **kwargs):
        nonlocal called
        called = True
        _delete_source(app_client, admin_headers, wiki_case)
        return "## 旧资料\n\n" + "被删除的来源正文不能在生成完成后公开。" * 5 + " [1]"

    monkeypatch.setattr(llm, "chat", delete_while_generating)
    with pytest.raises(RuntimeError):
        wiki.build_wiki(db_session, kb)
        db_session.commit()
    assert called, "the source must be deleted inside the page-generation call"
    db_session.rollback()
    db_session.expire_all()

    page = db_session.get(models.WikiPage, wiki_case["pages"]["unlocked-old"]["id"])
    assert page.current_version_id == old_version
    assert db_session.get(models.WikiPageVersion, old_version).content_md == wiki_case["pages"]["unlocked-old"]["content"]
    assert app_client.get(_page_url(wiki_case, "unlocked-old"),
                          headers=wiki_case["headers"]["read"]).status_code == 404
    reviewed = app_client.get(_page_url(wiki_case, "unlocked-old"), headers=admin_headers)
    assert reviewed.status_code == 200
    assert reviewed.json()["version"]["id"] == old_version


def test_rebuild_does_not_overwrite_a_page_locked_during_generation(
    app_client, admin_headers, db_session, wiki_case, monkeypatch,
):
    kb = db_session.get(models.KnowledgeBase, wiki_case["kb"])
    monkeypatch.setattr(wiki, "_map_topics", lambda db, entries, **kwargs: [{
        "title": "unlocked-old", "summary": "后台重建", "chunk_ids": [idx for idx, _ in entries],
    }])
    human_content = "## 人工校对\n\n生成进行中保存的人工正文必须得到保护。"
    called = False

    def edit_while_generating(db, messages, **kwargs):
        nonlocal called
        called = True
        response = app_client.put(_page_url(wiki_case, "unlocked-old"), headers=admin_headers,
                                  json={"content_md": human_content})
        assert response.status_code == 200, response.text
        return "## 自动生成\n\n" + "生成内容不应覆盖并发的人工编辑。" * 5 + " [1]"

    monkeypatch.setattr(llm, "chat", edit_while_generating)
    result = wiki.build_wiki(db_session, kb)
    db_session.commit()
    assert called and result.pages_skipped == 1
    response = app_client.get(_page_url(wiki_case, "unlocked-old"), headers=admin_headers)
    assert response.status_code == 200
    assert response.json()["human_locked"] is True
    assert response.json()["version"]["content_md"] == human_content


def test_rebuild_records_all_supplied_sources_even_if_model_cites_only_one(
    app_client, admin_headers, db_session, wiki_case, monkeypatch,
):
    """An uncited input can still influence the generated page."""
    kb = db_session.get(models.KnowledgeBase, wiki_case["kb"])
    monkeypatch.setattr(wiki, "_map_topics", lambda db, entries, **kwargs: [{
        "title": "unlocked-old", "summary": "两份来源共同提供上下文",
        "chunk_ids": [idx for idx, _ in entries],
    }])
    monkeypatch.setattr(llm, "chat", lambda db, messages, **kwargs:
                        "## 两份资料\n\n" + "模型使用两份原文生成这段足够长的正文。" * 5 + " [2]")
    wiki.build_wiki(db_session, kb)
    db_session.commit()

    url = _page_url(wiki_case, "unlocked-old")
    reader = wiki_case["headers"]["read"]
    before = app_client.get(url, headers=reader)
    assert before.status_code == 200, before.text
    assert {wiki_case["docs"][key]["doc"] for key in ("removed", "live")} <= {
        e["doc_id"] for e in before.json()["evidence"]
    }
    _delete_source(app_client, admin_headers, wiki_case)
    assert app_client.get(url, headers=reader).status_code == 404
    assert app_client.get(url, headers=admin_headers).status_code == 200
