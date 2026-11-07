"""智能问答页 LLM 模型选择器：下拉数据与「按轮指定模型」。

覆盖三件事：
1. `/api/chat/models` 只列启用中、供应商也启用中的对话模型，不泄露密钥；
   普通员工也能读，`default_id` 与问答实际使用的默认模型一致；
2. 不可用的模型 id 在落库占位之前就被拒绝（400），两个问答入口都一样；
3. 指定模型时以 `cfg_override` 传进问答链路，未指定时仍走全局默认。
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete

from app import models
from app.services import answering, llm, qa_pipeline

PASSWORD = "ChatModel#2026"


def _result() -> dict:
    """够 `_persist_result` 写回的最小链路结果。"""
    return {
        "content": "测试回答", "evidence": [], "route": {},
        "usage": {}, "insufficient": False,
    }


@pytest.fixture()
def model_set(db_session):
    """两条供应商 + 五条模型配置，用完即删，避免污染其它用例的默认模型。"""
    suffix = uuid.uuid4().hex[:8]
    live = models.Provider(
        name=f"选择器-可用-{suffix}", base_url="http://127.0.0.1:9/v1",
        api_key_enc="", enabled=True, protocol="openai")
    off = models.Provider(
        name=f"选择器-停用-{suffix}", base_url="http://127.0.0.1:9/v1",
        api_key_enc="", enabled=False, protocol="openai")
    db_session.add_all([live, off])
    db_session.flush()

    def cfg(provider, name, *, purpose="llm", enabled=True, default=False):
        return models.ModelConfig(
            provider_id=provider.id, purpose=purpose, model=f"{name}-{suffix}",
            label=name, params="{}", is_default=default, enabled=enabled, embed_dim=0)

    default = cfg(live, "默认对话模型", default=True)
    alternate = cfg(live, "备用对话模型")
    disabled = cfg(live, "停用对话模型", enabled=False)
    off_provider = cfg(off, "停用供应商模型")
    embedding = cfg(live, "向量模型", purpose="embedding")
    db_session.add_all([default, alternate, disabled, off_provider, embedding])
    db_session.commit()

    yield {
        "default": default, "alternate": alternate, "disabled": disabled,
        "off_provider": off_provider, "embedding": embedding,
    }

    ids = [m.id for m in (default, alternate, disabled, off_provider, embedding)]
    db_session.execute(delete(models.ModelConfig).where(models.ModelConfig.id.in_(ids)))
    db_session.execute(delete(models.Provider).where(models.Provider.id.in_([live.id, off.id])))
    db_session.commit()


@pytest.fixture()
def ask_target(app_client, admin_headers):
    kb = app_client.post("/api/kbs", headers=admin_headers, json={
        "name": f"模型选择测试库-{uuid.uuid4().hex[:8]}", "description": "", "mode": "basic"})
    assert kb.status_code == 201, kb.text
    conv = app_client.post("/api/conversations", headers=admin_headers, json={
        "title": "模型选择会话", "kb_ids": [kb.json()["id"]]})
    assert conv.status_code == 201, conv.text
    return conv.json()["id"]


def test_chat_models_lists_only_usable_llm_models(app_client, admin_headers, db_session, model_set):
    r = app_client.get("/api/chat/models", headers=admin_headers)
    assert r.status_code == 200, r.text
    data = r.json()
    ids = {item["id"] for item in data["items"]}
    assert model_set["default"].id in ids
    assert model_set["alternate"].id in ids
    assert model_set["disabled"].id not in ids, "停用模型不应出现在下拉里"
    assert model_set["off_provider"].id not in ids, "供应商停用的模型不应出现在下拉里"
    assert model_set["embedding"].id not in ids, "向量模型不应混进对话模型下拉"
    assert "api_key" not in r.text and "127.0.0.1" not in r.text, "下拉不得泄露供应商配置"

    resolved = llm.resolve_model(db_session, "llm")
    assert data["default_id"] == (resolved[0].id if resolved else None), \
        "默认项必须与问答实际解析出的默认模型一致"
    default_item = next(i for i in data["items"] if i["id"] == model_set["default"].id)
    assert default_item["is_default"] is True
    assert default_item["model"].startswith("默认对话模型-")
    assert "默认对话模型" in default_item["display"]
    assert default_item["display"].count(" · ") == 2, "展示名应为「显示名 · 模型标识 · 供应商」"


def test_chat_models_open_to_employee(app_client, admin_headers, model_set):
    username = "emp_chatmodel_" + uuid.uuid4().hex[:8]
    created = app_client.post("/api/users", headers=admin_headers, json={
        "username": username, "display_name": "模型选择员工",
        "password": PASSWORD, "role": "employee", "grants": [],
    })
    assert created.status_code == 201, created.text
    login = app_client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['token']}"}
    r = app_client.get("/api/chat/models", headers=headers)
    assert r.status_code == 200, r.text
    assert any(i["id"] == model_set["default"].id for i in r.json()["items"])


@pytest.mark.parametrize("which", ["unknown", "disabled", "off_provider", "embedding"])
def test_ask_rejects_unusable_model_before_writing(app_client, admin_headers, model_set, which):
    conv = app_client.post("/api/conversations", headers=admin_headers,
                           json={"title": "模型校验", "kb_ids": []})
    assert conv.status_code == 201, conv.text
    cid = conv.json()["id"]
    model_id = 999999999 if which == "unknown" else model_set[which].id
    body = {"question": "你好", "model_config_id": model_id}

    plain = app_client.post(f"/api/conversations/{cid}/ask", headers=admin_headers, json=body)
    assert plain.status_code == 400, plain.text
    stream = app_client.post(f"/api/conversations/{cid}/ask/stream", headers=admin_headers, json=body)
    assert stream.status_code == 400, stream.text

    detail = app_client.get(f"/api/conversations/{cid}", headers=admin_headers).json()
    assert detail["messages"] == [], "无效模型必须在落库占位之前被拒绝"


def test_ask_forwards_selected_model(app_client, admin_headers, model_set, ask_target, monkeypatch):
    seen: list[int | None] = []

    def fake_answer(db, question, kbs, *, top_k=None, cfg_override=None):
        seen.append(cfg_override.id if cfg_override is not None else None)
        return _result()

    monkeypatch.setattr(answering, "answer", fake_answer)
    r = app_client.post(f"/api/conversations/{ask_target}/ask", headers=admin_headers,
                        json={"question": "你好", "model_config_id": model_set["alternate"].id})
    assert r.status_code == 200, r.text
    assert seen == [model_set["alternate"].id]


def test_ask_stream_forwards_selected_model(app_client, admin_headers, model_set, ask_target, monkeypatch):
    seen: list[int | None] = []

    def fake_iter_run(db, question, kbs, *, top_k=None, cfg_override=None, stream=True):
        seen.append(cfg_override.id if cfg_override is not None else None)
        if False:  # pragma: no cover - 让它成为生成器，返回值走 StopIteration
            yield None
        return _result()

    monkeypatch.setattr(qa_pipeline, "iter_run", fake_iter_run)
    r = app_client.post(f"/api/conversations/{ask_target}/ask/stream", headers=admin_headers,
                        json={"question": "你好", "model_config_id": model_set["alternate"].id})
    assert r.status_code == 200, r.text
    assert '"type":"done"' in r.text.replace(" ", ""), r.text
    assert seen == [model_set["alternate"].id]


def test_ask_without_model_keeps_global_default(app_client, admin_headers, ask_target, monkeypatch):
    seen: list[int | None] = []

    def fake_answer(db, question, kbs, *, top_k=None, cfg_override=None):
        seen.append(cfg_override.id if cfg_override is not None else None)
        return _result()

    monkeypatch.setattr(answering, "answer", fake_answer)
    r = app_client.post(f"/api/conversations/{ask_target}/ask", headers=admin_headers,
                        json={"question": "你好"})
    assert r.status_code == 200, r.text
    assert seen == [None]
