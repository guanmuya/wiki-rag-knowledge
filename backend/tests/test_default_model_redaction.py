"""旧问答接口：默认模型与显式模型的供应商异常不得泄露给员工。"""
from __future__ import annotations

import json
import logging
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update
from sqlalchemy.exc import OperationalError

from app import models, security
from app.db import DB_BUSY_DETAIL
from app.routers import chat as chat_router
from app.services import llm
from conftest import mark_user_ready


PASSWORD = "Redaction-test-password-2026!"
CREDENTIAL = "sk-regression-default-private-4cf82"
PRIVATE_HOST = "model-gateway-redaction.internal"
PRIVATE_TOKEN = "query-token-regression-9d71"
PRIVATE_URL = f"https://{PRIVATE_HOST}/v1/chat?token={PRIVATE_TOKEN}"
VENDOR_ERROR = f"供应商返回 429: credential={CREDENTIAL} url={PRIVATE_URL}"
PRIVATE_MARKERS = (CREDENTIAL, PRIVATE_HOST, PRIVATE_TOKEN)
QUESTION = "一线城市住宿上限是多少？"
ANSWER = "一线城市住宿上限为 600 元每晚[1]。"


@pytest.fixture()
def employee_conversation(app_client, admin_headers, db_session):
    """复用 API 创建员工和授权，并按检索测试的方式插入真实分块。"""
    tag = uuid.uuid4().hex[:8]
    kb_response = app_client.post("/api/kbs", headers=admin_headers, json={
        "name": f"默认模型脱敏-{tag}", "description": "", "mode": "basic",
    })
    assert kb_response.status_code == 201, kb_response.text
    kb_id = kb_response.json()["id"]
    kb = db_session.get(models.KnowledgeBase, kb_id)
    kb.status = "ready"
    doc = models.Document(kb_id=kb_id, filename="travel.md", suffix=".md", status="ready")
    db_session.add(doc)
    db_session.flush()
    version = models.DocumentVersion(
        doc_id=doc.id, version_no=1, storage_path="/tmp/redaction-test", status="success")
    db_session.add(version)
    db_session.flush()
    db_session.add(models.Chunk(
        kb_id=kb_id, doc_id=doc.id, doc_version_id=version.id, ordinal=0,
        text="一线城市住宿上限 600 元每晚，其他城市住宿上限 400 元每晚。",
        heading="住宿上限", keywords="一线城市 住宿 上限 标准", chunk_hash=f"redact-{tag}",
    ))
    db_session.commit()

    username = f"emp_redaction_{tag}"
    user_response = app_client.post("/api/users", headers=admin_headers, json={
        "username": username, "display_name": "普通员工", "password": PASSWORD,
        "role": "employee", "grants": [{"kb_id": kb_id, "level": "read"}],
    })
    assert user_response.status_code == 201, user_response.text
    mark_user_ready(username)       # P1-8：先完成初始改密，再测业务接口
    login = app_client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['token']}"}
    conv = app_client.post("/api/conversations", headers=headers,
                           json={"title": "默认模型脱敏", "kb_ids": [kb_id]})
    assert conv.status_code == 201, conv.text
    return headers, conv.json()["id"], kb_id


@pytest.fixture()
def default_model(db_session, monkeypatch):
    """让测试模型确实经正常默认模型选择解析，结束后恢复已有默认项。"""
    previous_ids = list(db_session.scalars(select(models.ModelConfig.id).where(
        models.ModelConfig.purpose == "llm", models.ModelConfig.is_default.is_(True))))
    db_session.execute(update(models.ModelConfig).where(
        models.ModelConfig.id.in_(previous_ids)).values(is_default=False))
    provider = models.Provider(
        name=f"默认模型供应商-{uuid.uuid4().hex[:8]}",
        base_url=f"https://{PRIVATE_HOST}/v1", api_key_enc=security.encrypt_secret(CREDENTIAL),
        enabled=True, protocol="openai")
    db_session.add(provider)
    db_session.flush()
    cfg = models.ModelConfig(
        provider_id=provider.id, purpose="llm", model="redaction-default-model",
        label="默认模型", params="{}", enabled=True, is_default=True)
    db_session.add(cfg)
    db_session.commit()
    cfg_id, provider_id = cfg.id, provider.id
    assert llm.resolve_model(db_session, "llm")[0].id == cfg_id

    # 向量模型不是本用例的被测对象；固定本地哈希避免依赖别的测试留下的配置。
    resolve = llm.resolve_model

    def resolve_without_embedding(db, purpose):
        return None if purpose == "embedding" else resolve(db, purpose)

    monkeypatch.setattr(llm, "resolve_model", resolve_without_embedding)
    yield cfg_id
    db_session.execute(delete(models.ModelConfig).where(models.ModelConfig.id == cfg_id))
    db_session.execute(delete(models.Provider).where(models.Provider.id == provider_id))
    db_session.execute(update(models.ModelConfig).where(
        models.ModelConfig.id.in_(previous_ids)).values(is_default=True))
    db_session.commit()


def _request_body(field: str, model_id: int, question: str) -> dict:
    body = {"question": question}
    if field == "null":
        body["model_config_id"] = None
    elif field == "explicit":
        body["model_config_id"] = model_id
    return body


def _events(response) -> list[dict]:
    assert response.headers["content-type"].startswith("text/event-stream")
    return [json.loads(line[5:].strip()) for line in response.text.splitlines()
            if line.startswith("data:")]


def _history(client, headers, cid):
    response = client.get(f"/api/conversations/{cid}", headers=headers)
    assert response.status_code == 200, response.text
    assistants = [message for message in response.json()["messages"]
                  if message["role"] == "assistant"]
    assert assistants
    return response, assistants[-1]


def _assert_private(**surfaces):
    leaks = [(name, marker) for name, text in surfaces.items()
             for marker in PRIVATE_MARKERS if marker in text]
    assert not leaks, f"供应商异常泄露到可见内容：{leaks}"


class _BrokenStream(httpx.SyncByteStream):
    def __init__(self, unhandled):
        self.unhandled = unhandled

    def __iter__(self):
        chunk = {"choices": [{"delta": {"content": ANSWER}}]}
        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()
        if self.unhandled:
            raise RuntimeError(VENDOR_ERROR)
        raise httpx.ReadError(VENDOR_ERROR)


class _Vendor:
    """只模拟 HTTP 边界，保留模型解析、分析/生成和流式处理的真实代码。"""
    def __init__(self, failure: str, chitchat: bool = False):
        self.failure = failure
        self.chitchat = chitchat
        self.analysis_requests = 0
        self.generation_requests = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["model"] == "redaction-default-model"
        assert request.headers["Authorization"] == f"Bearer {CREDENTIAL}"
        if payload.get("response_format"):
            self.analysis_requests += 1
            if self.failure == "analysis":
                return self.error_response()
            system = payload["messages"][0]["content"]
            if "意图分析节点" in system:
                data = {"label": "闲聊" if self.chitchat else "规范查询", "reason": "测试意图"}
            elif "查询路由节点" in system:
                data = {"mode": "original", "queries": [QUESTION], "reason": "直接检索"}
            elif "覆盖度评估节点" in system:
                data = {"sufficient": True, "missing_queries": [], "reason": "有依据"}
            else:
                raise AssertionError(f"意外的分析节点：{system}")
            return httpx.Response(200, json={"choices": [{"message": {
                "content": json.dumps(data, ensure_ascii=False)}}]})

        self.generation_requests += 1
        if self.failure == "unhandled":
            raise RuntimeError(VENDOR_ERROR)
        if self.failure == "generation":
            return self.error_response()
        if payload.get("stream"):
            if self.failure in {"partial", "partial_unhandled"}:
                return httpx.Response(200, stream=_BrokenStream(self.failure == "partial_unhandled"))
            chunk = {"choices": [{"delta": {"content": ANSWER}}]}
            body = f"data: {json.dumps(chunk, ensure_ascii=False)}\n\ndata: [DONE]\n\n"
            return httpx.Response(200, text=body)
        return httpx.Response(200, json={"choices": [{"message": {"content": ANSWER}}]})

    @staticmethod
    def error_response():
        return httpx.Response(429, json={"error": {"message": VENDOR_ERROR}})


def _install_vendor(monkeypatch, vendor):
    transport = httpx.MockTransport(vendor)
    monkeypatch.setattr(llm, "_client", lambda: httpx.Client(transport=transport, trust_env=False))


@pytest.mark.parametrize("field", ["omitted", "null", "explicit"])
@pytest.mark.parametrize("endpoint", ["ask", "ask/stream"])
@pytest.mark.parametrize("chitchat", [False, True], ids=["retrieval", "chitchat"])
@pytest.mark.parametrize("failure", ["analysis", "generation"])
def test_employee_provider_fallback_redacts_entire_response_and_history(
        app_client, employee_conversation, default_model, monkeypatch, caplog,
        field, endpoint, chitchat, failure):
    """分析降级与生成降级分别测试，确保另一阶段真实执行。"""
    headers, cid, _ = employee_conversation
    vendor = _Vendor(failure, chitchat)
    _install_vendor(monkeypatch, vendor)
    caplog.set_level(logging.WARNING)
    question = "你好" if chitchat else QUESTION
    response = app_client.post(f"/api/conversations/{cid}/{endpoint}", headers=headers,
                               json=_request_body(field, default_model, question))
    assert response.status_code == 200, response.text
    if endpoint == "ask/stream":
        events = _events(response)
        assert any(event["type"] == "answer" for event in events), events
        assert events[-1]["type"] == "done", events
        result = events[-1]
    else:
        result = response.json()
    history, assistant = _history(app_client, headers, cid)
    assert vendor.analysis_requests and vendor.generation_requests
    assert assistant["status"] == "ok"
    assert result["content"] == assistant["content"]
    if not chitchat:
        assert result["evidence"], "必须经过有证据的回答生成分支"
        assert "600" in result["content"]
    if failure == "generation":
        assert result["usage"]["mode"] == "offline"
        assert result["usage"]["offline_reason"]
    else:
        assert result["usage"]["mode"] == "live"
        assert any("降级" in note for note in result["usage"]["notes"])
    _assert_private(response=response.text, history=history.text, logs=caplog.text)


@pytest.mark.parametrize("field", ["omitted", "null", "explicit"])
@pytest.mark.parametrize("endpoint", ["ask", "ask/stream"])
def test_employee_unhandled_failure_redacts_response_and_history(
        app_client, employee_conversation, default_model, monkeypatch, caplog, field, endpoint):
    headers, cid, _ = employee_conversation
    vendor = _Vendor("unhandled")
    _install_vendor(monkeypatch, vendor)
    caplog.set_level(logging.WARNING)
    # 不让 TestClient 重抛服务端异常，才能检查真正发给用户的 500 响应。
    client = TestClient(app_client.app, raise_server_exceptions=False)
    try:
        response = client.post(f"/api/conversations/{cid}/{endpoint}", headers=headers,
                               json=_request_body(field, default_model, QUESTION))
    finally:
        client.close()
    assert vendor.analysis_requests and vendor.generation_requests
    if endpoint == "ask":
        assert response.status_code == 500
    else:
        assert response.status_code == 200
        events = _events(response)
        assert events[-1]["type"] == "error", events
        assert all(event["type"] != "done" for event in events)
    history, assistant = _history(app_client, headers, cid)
    assert assistant["status"] == "failed"
    assert assistant["content"]
    _assert_private(response=response.text, history=history.text, logs=caplog.text)


@pytest.mark.parametrize("field", ["omitted", "null", "explicit"])
@pytest.mark.parametrize("chitchat", [False, True], ids=["retrieval", "chitchat"])
@pytest.mark.parametrize("failure", ["partial", "partial_unhandled"])
def test_employee_stream_failure_after_partial_answer_is_redacted(
        app_client, employee_conversation, default_model, monkeypatch, caplog,
        field, chitchat, failure):
    headers, cid, _ = employee_conversation
    vendor = _Vendor(failure, chitchat)
    _install_vendor(monkeypatch, vendor)
    caplog.set_level(logging.WARNING)
    response = app_client.post(f"/api/conversations/{cid}/ask/stream", headers=headers,
                               json=_request_body(field, default_model,
                                                  "你好" if chitchat else QUESTION))
    assert response.status_code == 200, response.text
    events = _events(response)
    assert vendor.analysis_requests and vendor.generation_requests
    assert any(event["type"] == "answer" and ANSWER in event.get("delta", "")
               for event in events), events
    if failure == "partial":
        assert events[-1]["type"] == "done", events
    else:
        assert events[-1]["type"] == "error", events
        assert all(event["type"] != "done" for event in events)
    history, assistant = _history(app_client, headers, cid)
    assert assistant["status"] == ("ok" if failure == "partial" else "failed")
    if failure == "partial":
        assert ANSWER in assistant["content"]
    _assert_private(response=response.text, history=history.text, logs=caplog.text)


@pytest.mark.parametrize("field", ["omitted", "null"])
@pytest.mark.parametrize("endpoint", ["ask", "ask/stream"])
@pytest.mark.parametrize("chitchat", [False, True], ids=["retrieval", "chitchat"])
def test_no_model_keeps_offline_answer_semantics(
        app_client, employee_conversation, monkeypatch, field, endpoint, chitchat):
    """未配置模型仍能提供离线回答，且不会发起供应商请求。"""
    headers, cid, _ = employee_conversation
    monkeypatch.setattr(llm, "resolve_model", lambda db, purpose: None)

    def no_http():
        raise AssertionError("无模型时不应请求供应商")

    monkeypatch.setattr(llm, "_client", no_http)
    response = app_client.post(f"/api/conversations/{cid}/{endpoint}", headers=headers,
                               json=_request_body(field, 0, "你好" if chitchat else QUESTION))
    assert response.status_code == 200, response.text
    if endpoint == "ask/stream":
        events = _events(response)
        assert events[-1]["type"] == "done", events
        result = events[-1]
    else:
        result = response.json()
    history, assistant = _history(app_client, headers, cid)
    assert assistant["status"] == "ok"
    assert result["usage"]["mode"] == "offline"
    if chitchat:
        assert result["usage"]["offline_reason"] == ""
        assert "知识库问答助手" in result["content"]
    else:
        assert result["evidence"] and "600" in result["content"]
    _assert_private(response=response.text, history=history.text)


@pytest.fixture()
def malformed_default_embedding(db_session):
    """模拟默认向量模型的参数转换异常，其中携带凭据和私有 URL。"""
    previous_ids = list(db_session.scalars(select(models.ModelConfig.id).where(
        models.ModelConfig.purpose == "embedding", models.ModelConfig.is_default.is_(True))))
    db_session.execute(update(models.ModelConfig).where(
        models.ModelConfig.id.in_(previous_ids)).values(is_default=False))
    provider = models.Provider(
        name=f"向量脱敏供应商-{uuid.uuid4().hex[:8]}",
        base_url=f"https://{PRIVATE_HOST}/v1", enabled=True, protocol="openai")
    db_session.add(provider)
    db_session.flush()
    cfg = models.ModelConfig(
        provider_id=provider.id, purpose="embedding", model="redaction-embedding",
        params=json.dumps({"dim": f"{CREDENTIAL} {PRIVATE_URL}"}),
        is_default=True, enabled=True, embed_dim=0)
    db_session.add(cfg)
    db_session.commit()
    cfg_id, provider_id = cfg.id, provider.id
    assert llm.resolve_model(db_session, "embedding")[0].id == cfg_id
    yield cfg_id
    db_session.execute(delete(models.ModelConfig).where(models.ModelConfig.id == cfg_id))
    db_session.execute(delete(models.Provider).where(models.Provider.id == provider_id))
    db_session.execute(update(models.ModelConfig).where(
        models.ModelConfig.id.in_(previous_ids)).values(is_default=True))
    db_session.commit()


def test_invalid_unicode_stream_ends_with_safe_error(
        app_client, employee_conversation, monkeypatch, caplog):
    headers, cid, _ = employee_conversation

    def invalid_stream(*args, **kwargs):
        yield {"type": "answer", "delta": CREDENTIAL + "\ud800"}

    monkeypatch.setattr(chat_router.qa_pipeline, "iter_run", invalid_stream)
    caplog.set_level(logging.WARNING)
    response = app_client.post(f"/api/conversations/{cid}/ask/stream", headers=headers,
                               json={"question": QUESTION})
    assert response.status_code == 200
    assert _events(response)[-1]["type"] == "error"
    history, assistant = _history(app_client, headers, cid)
    assert assistant["status"] == "failed"
    _assert_private(response=response.text, history=history.text, logs=caplog.text)


@pytest.mark.parametrize("endpoint", ["ask", "ask/stream"])
def test_persist_failure_redacts_exception_and_closes_pending_answer(
        app_client, employee_conversation, monkeypatch, caplog, endpoint):
    headers, cid, _ = employee_conversation

    def fake_stream(*args, **kwargs):
        yield {"type": "answer", "delta": "公开回答"}
        return {}

    def fail_persist(*args, **kwargs):
        raise ValueError(VENDOR_ERROR)

    monkeypatch.setattr(chat_router.answering, "answer", lambda *args, **kwargs: {})
    monkeypatch.setattr(chat_router.qa_pipeline, "iter_run", fake_stream)
    monkeypatch.setattr(chat_router, "_persist_result", fail_persist)
    caplog.set_level(logging.WARNING)
    response = app_client.post(f"/api/conversations/{cid}/{endpoint}", headers=headers,
                               json={"question": QUESTION})
    if endpoint == "ask":
        assert response.status_code == 500
    else:
        assert response.status_code == 200
        assert _events(response)[-1]["type"] == "error"
    history, assistant = _history(app_client, headers, cid)
    assert assistant["status"] == "failed"
    _assert_private(response=response.text, history=history.text, logs=caplog.text)


@pytest.mark.parametrize("endpoint", ["ask", "ask/stream", "preview"])
def test_database_lock_remains_retryable_without_exposing_details(
        app_client, employee_conversation, monkeypatch, caplog, endpoint):
    headers, cid, kb_id = employee_conversation

    def locked(*args, **kwargs):
        raise OperationalError("SELECT " + CREDENTIAL, None,
                               Exception("database is locked " + PRIVATE_URL))

    if endpoint == "ask":
        monkeypatch.setattr(chat_router.answering, "answer", locked)
    elif endpoint == "ask/stream":
        monkeypatch.setattr(chat_router.qa_pipeline, "iter_run", locked)
    else:
        monkeypatch.setattr(chat_router.retrieval, "retrieve", locked)
    caplog.set_level(logging.WARNING)
    if endpoint == "preview":
        response = app_client.post("/api/search/preview", headers=headers,
                                   json={"query": QUESTION, "kb_ids": [kb_id]})
    else:
        response = app_client.post(f"/api/conversations/{cid}/{endpoint}", headers=headers,
                                   json={"question": QUESTION})
    if endpoint == "ask/stream":
        assert response.status_code == 200
        assert _events(response)[-1]["message"] == DB_BUSY_DETAIL
    else:
        assert response.status_code == 503
        assert response.json()["detail"] == DB_BUSY_DETAIL
    _assert_private(response=response.text, logs=caplog.text)


def test_employee_search_preview_redacts_unhandled_default_embedding_error(
        app_client, employee_conversation, malformed_default_embedding, monkeypatch, caplog):
    headers, _, kb_id = employee_conversation
    active_embedding = llm.active_embedding
    calls = []

    def track_embedding(db):
        calls.append(True)
        return active_embedding(db)

    monkeypatch.setattr(llm, "active_embedding", track_embedding)
    caplog.set_level(logging.WARNING)
    client = TestClient(app_client.app, raise_server_exceptions=False)
    try:
        response = client.post("/api/search/preview", headers=headers,
                               json={"query": QUESTION, "kb_ids": [kb_id]})
    finally:
        client.close()
    assert calls, "必须确实解析默认向量模型"
    assert response.status_code == 500, response.text
    assert response.json().get("detail"), response.text
    _assert_private(response=response.text, logs=caplog.text)
