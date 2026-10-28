"""端到端：上传 → 后台任务 → 检索 → 问答 → 证据带版本；以及幂等与人工校对保护。"""
from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app import models
from app.services import worker

DOC_MD = """# 差旅与报销

## 住宿标准

一线城市住宿上限 600 元每晚，其他城市 400 元每晚，超标部分由个人承担。

## 交通标准

高铁二等座为标准配置，市内交通实报实销，单次超过 200 元需要说明事由。

## 审批流程

提交人提交单据后，依次经过直属主管、部门负责人、财务复核，最后出纳付款。
财务在收到完整单据后 5 个工作日内完成付款。

## 票据要求

所有报销必须提供合规发票，电子发票需提供 PDF 原件，超过 30 天未提交需附说明。
"""


@pytest.fixture(scope="module")
def basic_kb(app_client, admin_headers):
    r = app_client.post("/api/kbs", headers=admin_headers,
                        json={"name": "E2E 基础库", "description": "", "mode": "basic"})
    assert r.status_code == 201, r.text
    kb = r.json()
    up = app_client.post(f"/api/kbs/{kb['id']}/documents", headers=admin_headers,
                         files=[("files", ("差旅报销.md", DOC_MD.encode(), "text/markdown"))])
    assert up.status_code == 200, up.text
    assert up.json()["items"], up.text
    _drain()
    return kb


@pytest.fixture(scope="module")
def wiki_kb(app_client, admin_headers):
    r = app_client.post("/api/kbs", headers=admin_headers,
                        json={"name": "E2E Wiki 库", "description": "", "mode": "wiki"})
    assert r.status_code == 201, r.text
    kb = r.json()
    up = app_client.post(f"/api/kbs/{kb['id']}/documents", headers=admin_headers,
                         files=[("files", ("差旅报销.md", DOC_MD.encode(), "text/markdown"))])
    assert up.status_code == 200, up.text
    _drain()
    return kb


def _drain(max_steps: int = 40) -> int:
    """同步驱动 worker，直到没有排队任务。"""
    steps = 0
    while worker.run_once("test") and steps < max_steps:
        steps += 1
    return steps


def test_upload_creates_document_and_chunks(app_client, admin_headers, basic_kb, db_session):
    docs = app_client.get(f"/api/kbs/{basic_kb['id']}/documents", headers=admin_headers).json()
    assert docs["total"] == 1
    doc = docs["items"][0]
    assert doc["current_version"]["chunk_count"] > 0
    assert doc["status"] == "ready"

    chunks = app_client.get(f"/api/documents/{doc['id']}/chunks", headers=admin_headers).json()
    assert chunks["total"] > 0
    first = chunks["items"][0]
    assert first["locator"], "分块必须带位置信息"
    assert first["keywords"], "分块必须有可检索关键词"


def test_search_preview_returns_versioned_evidence(app_client, admin_headers, basic_kb):
    r = app_client.post("/api/search/preview", headers=admin_headers,
                        json={"kb_ids": [basic_kb["id"]], "query": "一线城市住宿上限是多少"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["evidence"], "应检索到证据"
    top = data["evidence"][0]
    assert top["doc_version_id"] and top["version_no"] == 1
    assert top["locator"], "证据必须能定位回原文"
    assert data["routes"][0]["used"] == "chunk"
    assert "600" in "".join(e["text"] for e in data["evidence"])


def test_ask_returns_answer_with_citations_and_route(app_client, admin_headers, basic_kb):
    conv = app_client.post("/api/conversations", headers=admin_headers,
                           json={"title": "E2E 会话", "kb_ids": [basic_kb["id"]]})
    assert conv.status_code == 201
    cid = conv.json()["id"]
    r = app_client.post(f"/api/conversations/{cid}/ask", headers=admin_headers,
                        json={"question": "一线城市住宿上限是多少？"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["message"]["content"]
    assert data["evidence"], "回答必须带证据"
    assert data["route"]["kb_routes"][0]["used"] == "chunk"
    assert data["usage"]["mode"] in ("live", "offline")

    detail = app_client.get(f"/api/conversations/{cid}", headers=admin_headers).json()
    assert len(detail["messages"]) == 2
    assistant = detail["messages"][1]
    assert assistant["evidence"], "消息必须持久化证据快照"
    assert assistant["evidence"][0]["doc_version_id"], "证据快照必须保留版本号"


def test_wiki_kb_generates_pages_with_evidence(app_client, admin_headers, wiki_kb, db_session):
    tree = app_client.get(f"/api/kbs/{wiki_kb['id']}/wiki/tree", headers=admin_headers).json()
    assert tree["total"] > 0, tree
    slug = tree["items"][0]["slug"]

    page = app_client.get(f"/api/kbs/{wiki_kb['id']}/wiki/pages/{slug}", headers=admin_headers).json()
    assert page["version"]["content_md"]
    assert page["evidence"], "Wiki 页面必须挂回原文证据"
    assert page["evidence"][0]["doc_name"]
    assert page["evidence"][0]["locator"]


def test_wiki_kb_routes_to_wiki_not_chunk(app_client, admin_headers, wiki_kb):
    r = app_client.post("/api/search/preview", headers=admin_headers,
                        json={"kb_ids": [wiki_kb["id"]], "query": "住宿标准"})
    data = r.json()
    assert data["routes"][0]["used"] == "wiki"
    assert data["routes"][0]["fallback"] is False
    assert data["evidence"][0]["source_type"] == "wiki"
    assert data["evidence"][0]["page_title"]
    assert data["evidence"][0]["evidence"], "Wiki 证据要带原文依据链"


def test_mixed_kb_query_uses_both_retrievers(app_client, admin_headers, basic_kb, wiki_kb):
    r = app_client.post("/api/search/preview", headers=admin_headers,
                        json={"kb_ids": [basic_kb["id"], wiki_kb["id"]], "query": "报销审批流程"})
    data = r.json()
    used = {r["kb_id"]: r["used"] for r in data["routes"]}
    assert used[basic_kb["id"]] == "chunk"
    assert used[wiki_kb["id"]] == "wiki"
    types = {e["source_type"] for e in data["evidence"]}
    assert "chunk" in types and "wiki" in types, "同一会话应同时使用两类库"


def test_reingest_is_idempotent(app_client, admin_headers, basic_kb, db_session):
    """同一文件重复上传：不产生重复分块（旧版本分块被替换）。"""
    before = db_session.execute(
        select(func.count(models.Chunk.id)).where(models.Chunk.kb_id == basic_kb["id"])
    ).scalar()
    up = app_client.post(f"/api/kbs/{basic_kb['id']}/documents", headers=admin_headers,
                         files=[("files", ("差旅报销.md", DOC_MD.encode(), "text/markdown"))])
    assert up.status_code == 200
    _drain()
    db_session.expire_all()
    after = db_session.execute(
        select(func.count(models.Chunk.id)).where(models.Chunk.kb_id == basic_kb["id"])
    ).scalar()
    docs = app_client.get(f"/api/kbs/{basic_kb['id']}/documents", headers=admin_headers).json()
    assert docs["total"] == 1, "同名文件应作为新版本而不是新文档"
    versions = app_client.get(f"/api/documents/{docs['items'][0]['id']}", headers=admin_headers).json()
    assert len(versions["versions"]) == 2
    assert after == before, "重跑不应累积重复分块"


def test_wiki_rebuild_is_idempotent(app_client, admin_headers, wiki_kb, db_session):
    tree1 = app_client.get(f"/api/kbs/{wiki_kb['id']}/wiki/tree", headers=admin_headers).json()
    r = app_client.post(f"/api/kbs/{wiki_kb['id']}/rebuild", headers=admin_headers,
                        json={"kind": "wiki"})
    assert r.status_code == 200, r.text
    _drain()
    tree2 = app_client.get(f"/api/kbs/{wiki_kb['id']}/wiki/tree", headers=admin_headers).json()
    assert tree2["total"] == tree1["total"], "重复生成不应产生重复页面"
    slugs1 = {p["slug"] for p in tree1["items"]}
    slugs2 = {p["slug"] for p in tree2["items"]}
    assert slugs1 == slugs2

    # 重建排队时写的是「重建任务已排队」，任务成功后必须刷新成真实状态，
    # 否则界面上会永远停在「已排队」（实测缺陷）。
    kb_after = app_client.get(f"/api/kbs/{wiki_kb['id']}", headers=admin_headers).json()
    assert "排队" not in kb_after["status_detail"], kb_after["status_detail"]
    assert "索引单元" in kb_after["status_detail"], kb_after["status_detail"]


def test_human_edit_protects_page_from_silent_overwrite(app_client, admin_headers, wiki_kb):
    tree = app_client.get(f"/api/kbs/{wiki_kb['id']}/wiki/tree", headers=admin_headers).json()
    slug = tree["items"][0]["slug"]
    page = app_client.get(f"/api/kbs/{wiki_kb['id']}/wiki/pages/{slug}", headers=admin_headers).json()
    edited = page["version"]["content_md"] + "\n\n## 人工补充\n\n本条为人工校对内容，重建时不得被覆盖。\n"

    r = app_client.put(f"/api/kbs/{wiki_kb['id']}/wiki/pages/{slug}", headers=admin_headers,
                       json={"content_md": edited, "note": "人工补充"})
    assert r.status_code == 200, r.text
    assert r.json()["human_locked"] is True

    # 触发重建
    rb = app_client.post(f"/api/kbs/{wiki_kb['id']}/rebuild", headers=admin_headers,
                         json={"kind": "wiki"})
    assert rb.status_code == 200
    _drain()

    after = app_client.get(f"/api/kbs/{wiki_kb['id']}/wiki/pages/{slug}", headers=admin_headers).json()
    assert "本条为人工校对内容" in after["version"]["content_md"], "人工内容不得被静默覆盖"
    assert after["human_locked"] is True

    # 版本历史里应能区分人工版本
    versions = app_client.get(
        f"/api/kbs/{wiki_kb['id']}/wiki/pages/{slug}/versions", headers=admin_headers).json()
    assert any(v["origin"] == "human" for v in versions["items"])


def test_wiki_page_search_uses_sections(app_client, admin_headers, wiki_kb):
    r = app_client.post("/api/search/preview", headers=admin_headers,
                        json={"kb_ids": [wiki_kb["id"]], "query": "票据要求 电子发票"})
    data = r.json()
    assert data["evidence"]
    top = data["evidence"][0]
    assert top["heading"], "Wiki 证据应保留章节层级"
    assert top["source_type"] == "wiki"


def test_task_list_and_failure_visibility(app_client, admin_headers, basic_kb):
    tasks = app_client.get(f"/api/tasks?kb_id={basic_kb['id']}", headers=admin_headers).json()
    assert tasks["total"] >= 1
    t = tasks["items"][0]
    assert t["status"] in ("success", "partial", "failed", "queued", "running")
    assert "stage" in t and "progress" in t


def test_delete_document_removes_chunks(app_client, admin_headers, db_session):
    r = app_client.post("/api/kbs", headers=admin_headers,
                        json={"name": "E2E 删除库", "description": "", "mode": "basic"})
    kb = r.json()
    up = app_client.post(f"/api/kbs/{kb['id']}/documents", headers=admin_headers,
                         files=[("files", ("todelete.md", b"# t\n\ndelete me content here please", "text/markdown"))])
    _drain()
    doc_id = up.json()["items"][0]["doc_id"]
    before = db_session.execute(
        select(func.count(models.Chunk.id)).where(models.Chunk.doc_id == doc_id)
    ).scalar()
    assert before > 0
    d = app_client.delete(f"/api/documents/{doc_id}", headers=admin_headers)
    assert d.status_code == 200
    db_session.expire_all()
    after = db_session.execute(
        select(func.count(models.Chunk.id)).where(models.Chunk.doc_id == doc_id)
    ).scalar()
    assert after == 0, "删除文档必须清除其分块，避免残留可检索内容"
