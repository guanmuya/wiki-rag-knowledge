"""多 API 协议 + 模型探测 + 批量导入。

测什么、为什么这么测：

1. **三种协议的 payload 构造**：这是整个改造的核心风险点。协议写错的表现不是
   抛异常，而是「供应商返回 400」或「取到空字符串」——线上很难和 Key 失效区分。
   所以这里直接拦在 `llm._post` 这一层，断言真实发出去的 url / payload，
   而不是断言「函数没报错」。

2. **不真的联网**：所有用例 monkeypatch 掉 `llm._post` / `llm._get`。
   这样测的是「我们构造了什么」，与供应商是否在线无关，也不会因为 CI 无网而红。

3. **discover 容错**：探测失败必须返回 {items, error} 而不是抛异常。
   这是产品要求（很多中转根本没有 /models），用 `_get` 抛 ModelUnavailable 来模拟。

4. **import 幂等**：同一批导入两次，第二次全部 skipped。
   去重键是 (provider, purpose, model)——gpt-4o 既是对话也是视觉模型，
   只按 model 去重会静默丢掉第二个用途。
"""
from __future__ import annotations

import json

import pytest

from app import models, security
from app.services import llm


# --------------------------------------------------------------- 夹具
@pytest.fixture()
def make_provider(db_session):
    """建一个供应商；protocol 可指定。用完即删，避免污染其他用例的默认模型。"""
    created: list[int] = []

    def _make(name: str, protocol: str = "openai", base_url: str = "http://127.0.0.1:9/v1",
              api_key: str = ""):
        p = models.Provider(
            name=name, base_url=base_url,
            api_key_enc=security.encrypt_secret(api_key) if api_key else "",
            enabled=True, protocol=protocol,
        )
        db_session.add(p)
        db_session.commit()
        created.append(p.id)
        return p

    yield _make

    for pid in created:
        row = db_session.get(models.Provider, pid)
        if row:
            db_session.delete(row)
    db_session.commit()


@pytest.fixture()
def captured(monkeypatch):
    """拦截 _post / _get，记录 (url, payload, protocol) 并回放预设响应。"""
    state: dict = {"calls": [], "response": {}, "get_response": {}, "post_error": None,
                   "get_error": None}

    def fake_post(url, payload, provider, protocol=None):
        state["calls"].append({"url": url, "payload": payload,
                               "protocol": protocol, "provider": provider.name})
        if state["post_error"]:
            raise state["post_error"]
        return state["response"]

    def fake_get(url, provider, protocol=None):
        state["calls"].append({"url": url, "payload": None,
                               "protocol": protocol, "provider": provider.name})
        if state["get_error"]:
            raise state["get_error"]
        return state["get_response"]

    monkeypatch.setattr(llm, "_post", fake_post)
    monkeypatch.setattr(llm, "_get", fake_get)
    return state


def _add_model(db, provider, purpose: str, model: str, *, is_default: bool = True,
               params: str = "{}") -> models.ModelConfig:
    cfg = models.ModelConfig(
        provider_id=provider.id, purpose=purpose, model=model, label=model,
        params=params, is_default=is_default, enabled=True,
    )
    db.add(cfg)
    db.commit()
    return cfg


@pytest.fixture()
def clean_models(db_session):
    """清掉默认模型标记，避免不同用例互相抢「默认」。

    只重置 is_default，不删数据——删数据会踩到「已有索引时禁止删 embedding」的约束。
    """
    yield
    for row in db_session.query(models.ModelConfig).all():
        row.is_default = False
        db_session.add(row)
    db_session.commit()


# --------------------------------------------------------------- 协议：openai
def test_openai_chat_payload_unchanged(db_session, make_provider, captured, clean_models):
    """openai 协议必须与改造前逐字段一致——这是「不破坏存量行为」的底线。"""
    provider = make_provider("协议测试-openai", "openai")
    _add_model(db_session, provider, "llm", "gpt-4o-mini")
    captured["response"] = {
        "choices": [{"message": {"content": "OK"}}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 3},
    }

    out = llm.chat(db_session, [{"role": "user", "content": "只回复 OK"}], max_tokens=16)

    assert out == "OK"
    call = captured["calls"][0]
    assert call["url"] == "http://127.0.0.1:9/v1/chat/completions"
    payload = call["payload"]
    assert payload["model"] == "gpt-4o-mini"
    assert payload["messages"] == [{"role": "user", "content": "只回复 OK"}]
    assert payload["max_tokens"] == 16
    assert "input" not in payload, "openai 协议不得发 input 字段"


def test_openai_embed_payload(db_session, make_provider, captured, clean_models):
    provider = make_provider("协议测试-openai-emb", "openai")
    _add_model(db_session, provider, "embedding", "text-embedding-3-small")
    captured["response"] = {
        "data": [{"index": 0, "embedding": [3.0, 4.0]}],
        "usage": {"prompt_tokens": 2},
    }

    vecs = llm.embed(db_session, ["连接测试"])

    assert len(vecs) == 1
    assert captured["calls"][0]["url"] == "http://127.0.0.1:9/v1/embeddings"
    assert captured["calls"][0]["payload"]["input"] == ["连接测试"]


# --------------------------------------------------------------- 协议：responses
def test_responses_chat_uses_input_and_max_output_tokens(
        db_session, make_provider, captured, clean_models):
    """Responses 协议：`input` 取代 `messages`，`max_output_tokens` 取代 `max_tokens`，
    system 消息必须被剥到独立的 `instructions` 字段。"""
    provider = make_provider("协议测试-responses", "openai-responses")
    _add_model(db_session, provider, "llm", "gpt-5")
    captured["response"] = {
        "output": [{"content": [{"type": "output_text", "text": "OK"}]}],
        "usage": {"input_tokens": 11, "output_tokens": 2},
    }
    stats = llm.CallStats()

    out = llm.chat(db_session, [
        {"role": "system", "content": "你是严谨的助手。"},
        {"role": "user", "content": "只回复 OK"},
    ], max_tokens=16, stats=stats)

    assert out == "OK"
    call = captured["calls"][0]
    assert call["url"] == "http://127.0.0.1:9/v1/responses"
    payload = call["payload"]
    assert "messages" not in payload, "responses 协议不得发 messages"
    assert "max_tokens" not in payload, "responses 协议不得发 max_tokens"
    assert payload["max_output_tokens"] == 16
    assert payload["instructions"] == "你是严谨的助手。"
    assert payload["input"] == [{"role": "user", "content": "只回复 OK"}]
    # usage 字段名不同，成本统计必须仍然记到
    assert stats.prompt_tokens == 11 and stats.completion_tokens == 2


def test_responses_extracts_aggregated_output_text(
        db_session, make_provider, captured, clean_models):
    """部分实现只给聚合字段 output_text，也要能取到。"""
    provider = make_provider("协议测试-responses2", "openai-responses")
    _add_model(db_session, provider, "llm", "gpt-5-mini")
    captured["response"] = {"output_text": "聚合文本", "output": []}

    assert llm.chat(db_session, [{"role": "user", "content": "hi"}]) == "聚合文本"


def test_responses_empty_text_raises_chinese_error(
        db_session, make_provider, captured, clean_models):
    """取不到文本必须报中文错误，不能静默返回空串——空串会被上层当成「模型答了空」。"""
    provider = make_provider("协议测试-responses3", "openai-responses")
    _add_model(db_session, provider, "llm", "gpt-5-empty")
    captured["response"] = {"output": [{"content": []}]}

    with pytest.raises(llm.ModelUnavailable) as exc:
        llm.chat(db_session, [{"role": "user", "content": "hi"}])
    assert "响应结构异常" in str(exc.value)


# --------------------------------------------------------------- 协议：anthropic
def test_anthropic_chat_payload_and_headers(db_session, make_provider, captured, clean_models):
    """Anthropic：system 独立字段、max_tokens 必填、temperature 钳到 0..1。"""
    provider = make_provider("协议测试-anthropic", "anthropic")
    _add_model(db_session, provider, "llm", "claude-sonnet-4-0",
               params=json.dumps({"temperature": 1.7}))
    captured["response"] = {
        "content": [{"type": "text", "text": "OK"}],
        "usage": {"input_tokens": 9, "output_tokens": 4},
    }
    stats = llm.CallStats()

    out = llm.chat(db_session, [
        {"role": "system", "content": "你是严谨的助手。"},
        {"role": "user", "content": "只回复 OK"},
    ], max_tokens=16, stats=stats)

    assert out == "OK"
    call = captured["calls"][0]
    assert call["url"] == "http://127.0.0.1:9/v1/messages"
    assert call["protocol"] == "anthropic"
    payload = call["payload"]
    assert payload["system"] == "你是严谨的助手。"
    assert payload["max_tokens"] == 16
    assert payload["messages"] == [{"role": "user", "content": "只回复 OK"}]
    assert all(m["role"] != "system" for m in payload["messages"]), "system 不得留在 messages 里"
    assert payload["temperature"] <= 1.0, "Anthropic 的 temperature 上限是 1"
    assert stats.prompt_tokens == 9 and stats.completion_tokens == 4


def test_anthropic_max_tokens_always_present(db_session, make_provider, captured, clean_models):
    """max_tokens 是 Anthropic 的必填项：业务侧传 0 也必须补一个值，否则供应商直接 400。"""
    provider = make_provider("协议测试-anthropic2", "anthropic")
    _add_model(db_session, provider, "llm", "claude-3-5-haiku-latest")
    captured["response"] = {"content": [{"type": "text", "text": "OK"}]}

    llm.chat(db_session, [{"role": "user", "content": "hi"}], max_tokens=0)

    payload = captured["calls"][0]["payload"]
    assert "max_tokens" in payload and payload["max_tokens"] > 0


def test_anthropic_headers_use_x_api_key(db_session, make_provider, clean_models):
    """Anthropic 走 x-api-key + anthropic-version，不是 Bearer。"""
    provider = make_provider("协议测试-anthropic-hdr", "anthropic", api_key="sk-ant-test")
    headers = llm._headers(provider, "anthropic")
    assert "Authorization" not in headers
    assert headers["x-api-key"] == "sk-ant-test"
    assert headers["anthropic-version"] == llm.ANTHROPIC_VERSION


def test_openai_headers_use_bearer(db_session, make_provider, clean_models):
    provider = make_provider("协议测试-openai-hdr", "openai", api_key="sk-openai-test")
    headers = llm._headers(provider, "openai")
    assert headers["Authorization"] == "Bearer sk-openai-test"
    assert "x-api-key" not in headers


def test_anthropic_embedding_reports_explicitly(db_session, make_provider, captured, clean_models):
    """Anthropic 没有 Embedding 端点：必须明确报错，不能悄悄降级成哈希向量。"""
    provider = make_provider("协议测试-anthropic-emb", "anthropic")
    _add_model(db_session, provider, "embedding", "claude-no-embed")

    with pytest.raises(llm.ModelUnavailable) as exc:
        llm.embed(db_session, ["连接测试"])

    msg = str(exc.value)
    assert "Embedding" in msg and "不提供" in msg
    assert not captured["calls"], "不该真的发请求出去"


def test_responses_embedding_reports_explicitly(db_session, make_provider, captured, clean_models):
    provider = make_provider("协议测试-resp-emb", "openai-responses")
    _add_model(db_session, provider, "embedding", "gpt-5-embed-nope")

    with pytest.raises(llm.ModelUnavailable) as exc:
        llm.embed(db_session, ["x"])
    assert "不提供 Embedding" in str(exc.value)


def test_unknown_protocol_falls_back_to_openai(db_session, make_provider, captured, clean_models):
    """老库迁移后 protocol 可能是空串；必须回退 openai，不能报错也不能乱发。"""
    provider = make_provider("协议测试-legacy", "")
    provider.protocol = ""
    db_session.add(provider)
    db_session.commit()

    assert llm.provider_protocol(provider) == "openai"


def test_json_mode_maps_per_protocol(db_session, make_provider, captured, clean_models):
    """json_mode 三种协议各有写法：openai=response_format，
    responses=text.format，anthropic 没有对应字段只能加指令。"""
    p_openai = make_provider("json-openai", "openai")
    _add_model(db_session, p_openai, "llm", "gpt-4o-mini")
    captured["response"] = {"choices": [{"message": {"content": "{}"}}]}
    llm.chat(db_session, [{"role": "user", "content": "hi"}], json_mode=True)
    assert captured["calls"][-1]["payload"]["response_format"] == {"type": "json_object"}

    p_resp = make_provider("json-resp", "openai-responses")
    _add_model(db_session, p_resp, "llm", "gpt-5-json")
    captured["response"] = {"output": [{"content": [{"type": "output_text", "text": "{}"}]}]}
    llm.chat(db_session, [{"role": "user", "content": "hi"}], json_mode=True)
    assert captured["calls"][-1]["payload"]["text"] == {"format": {"type": "json_object"}}

    p_ant = make_provider("json-ant", "anthropic")
    _add_model(db_session, p_ant, "llm", "claude-json")
    captured["response"] = {"content": [{"type": "text", "text": "{}"}]}
    llm.chat(db_session, [{"role": "user", "content": "hi"}], json_mode=True)
    ant_payload = captured["calls"][-1]["payload"]
    assert "response_format" not in ant_payload
    assert "JSON" in ant_payload["system"]


# --------------------------------------------------------------- 连接测试
def test_test_model_targets_the_clicked_model(db_session, make_provider, captured, clean_models):
    """「点哪个测哪个」：默认模型是 A，点 B 测的必须是 B。

    改造前 chat() 按用途取默认模型，测非默认模型时实际测的是别人。
    """
    provider = make_provider("连接测试供应商", "openai")
    _add_model(db_session, provider, "llm", "model-default", is_default=True)
    target = _add_model(db_session, provider, "llm", "model-clicked", is_default=False)
    captured["response"] = {"choices": [{"message": {"content": "OK"}}]}

    result = llm.test_model(db_session, target)

    assert result["ok"] is True
    assert captured["calls"][-1]["payload"]["model"] == "model-clicked"
    # 不能顺手把默认模型改掉
    db_session.refresh(target)
    assert target.is_default is False


def test_test_model_reports_protocol(db_session, make_provider, captured, clean_models):
    provider = make_provider("连接测试供应商2", "anthropic")
    cfg = _add_model(db_session, provider, "llm", "claude-3-haiku-20240307")
    captured["response"] = {"content": [{"type": "text", "text": "OK"}]}

    result = llm.test_model(db_session, cfg)
    assert result["protocol"] == "anthropic"
    assert result["ok"] is True


# --------------------------------------------------------------- discover
def test_discover_openai_lists_models(db_session, make_provider, captured, clean_models):
    provider = make_provider("探测-openai", "openai")
    captured["get_response"] = {"data": [
        {"id": "gpt-4o-mini"}, {"id": "text-embedding-3-small"}, {"id": "gpt-4o-mini"},
    ]}

    result = llm.discover_models(db_session, provider)

    assert captured["calls"][0]["url"] == "http://127.0.0.1:9/v1/models"
    assert result["source"] == "api"
    assert result["error"] == ""
    ids = [i["model"] for i in result["items"]]
    assert ids == ["gpt-4o-mini", "text-embedding-3-small"], "重复项必须去重"
    hints = {i["model"]: i["purpose_hint"] for i in result["items"]}
    assert hints["text-embedding-3-small"] == "embedding"
    assert hints["gpt-4o-mini"] == "llm"


def test_discover_failure_returns_empty_not_raise(db_session, make_provider, captured, clean_models):
    """探测失败必须返回空列表 + error，而不是把异常抛给路由（会变 500）。"""
    provider = make_provider("探测-失败", "openai")
    captured["get_error"] = llm.ModelUnavailable("供应商返回 404: not found")

    result = llm.discover_models(db_session, provider)

    assert result["items"] == []
    assert result["source"] == "api"
    assert "404" in result["error"]


def test_discover_anthropic_falls_back_to_builtin(db_session, make_provider, captured, clean_models):
    """Anthropic 的 /models 经常不可用：回退内置清单并标 source=builtin。"""
    provider = make_provider("探测-anthropic", "anthropic")
    captured["get_error"] = llm.ModelUnavailable("供应商返回 404: not_found_error")

    result = llm.discover_models(db_session, provider)

    assert result["source"] == "builtin"
    assert result["error"], "回退时仍要说明为什么回退"
    assert result["items"], "内置清单不能为空"
    assert all(i["model"].startswith("claude") for i in result["items"])


def test_discover_marks_already_imported(db_session, make_provider, captured, clean_models):
    provider = make_provider("探测-已导入", "openai")
    _add_model(db_session, provider, "llm", "gpt-4o-mini")
    captured["get_response"] = {"data": [{"id": "gpt-4o-mini"}, {"id": "gpt-4o"}]}

    result = llm.discover_models(db_session, provider)
    flags = {i["model"]: i["already_imported"] for i in result["items"]}
    assert flags["gpt-4o-mini"] is True
    assert flags["gpt-4o"] is False


def test_discover_handles_odd_shapes(db_session, make_provider, captured, clean_models):
    """中转代理的 /models 形状五花八门，解析要容错而不是崩。"""
    provider = make_provider("探测-异形", "openai")
    captured["get_response"] = {"models": [{"name": "vendor/model-a", "display_name": "A"}]}

    result = llm.discover_models(db_session, provider)
    assert [i["model"] for i in result["items"]] == ["vendor/model-a"]
    assert result["items"][0]["label"] == "A"


# --------------------------------------------------------------- import 幂等
def test_import_is_idempotent(db_session, make_provider, clean_models):
    provider = make_provider("导入-幂等", "openai")
    items = [
        {"model": "gpt-4o", "purpose": "llm"},
        {"model": "gpt-4o", "purpose": "vision"},       # 同模型不同用途，必须都建
        {"model": "text-embedding-3-small", "purpose": "embedding"},
    ]

    created, skipped = llm.import_models(db_session, provider, items)
    assert len(created) == 3 and skipped == []

    created2, skipped2 = llm.import_models(db_session, provider, items)
    assert created2 == [], "第二次导入不应新建"
    assert len(skipped2) == 3
    assert all(s["reason"] == "已存在" for s in skipped2)


def test_import_rejects_invalid_purpose(db_session, make_provider, clean_models):
    provider = make_provider("导入-非法用途", "openai")
    created, skipped = llm.import_models(db_session, provider, [
        {"model": "x", "purpose": "chat"},     # 非法用途
        {"model": "", "purpose": "llm"},       # 空模型名
    ])
    assert created == []
    assert len(skipped) == 2
    assert all(s["reason"] == "参数不合法" for s in skipped)


def test_import_uses_model_as_default_label(db_session, make_provider, clean_models):
    provider = make_provider("导入-默认标签", "openai")
    created, _ = llm.import_models(db_session, provider, [{"model": "gpt-4o-mini", "purpose": "llm"}])
    assert created[0]["label"] == "gpt-4o-mini"


# --------------------------------------------------------------- HTTP 层
def test_discover_endpoint_never_500(app_client, admin_headers):
    """探测接口对不可达供应商必须返回 200 + error，前端才能正常渲染空态。"""
    r = app_client.post("/api/settings/providers", headers=admin_headers, json={
        "name": "探测接口-不可达", "base_url": "http://127.0.0.1:9/v1",
        "api_key": "sk-x", "enabled": True, "protocol": "openai",
    })
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    assert r.json()["protocol"] == "openai"

    res = app_client.post(f"/api/settings/providers/{pid}/discover", headers=admin_headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["items"] == []
    assert body["error"], "失败必须带可读原因"

    app_client.delete(f"/api/settings/providers/{pid}", headers=admin_headers)


def test_import_endpoint_round_trip(app_client, admin_headers):
    """导入接口：第一次 created，第二次全 skipped，且模型能在列表里查到。"""
    r = app_client.post("/api/settings/providers", headers=admin_headers, json={
        "name": "导入接口-供应商", "base_url": "http://127.0.0.1:9/v1",
        "api_key": "sk-x", "enabled": True, "protocol": "anthropic",
    })
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    assert r.json()["protocol"] == "anthropic"

    payload = {"items": [
        {"model": "claude-sonnet-4-0", "purpose": "llm"},
        {"model": "claude-3-5-haiku-latest", "purpose": "llm"},
    ]}
    first = app_client.post(f"/api/settings/providers/{pid}/models/import",
                            headers=admin_headers, json=payload)
    assert first.status_code == 200, first.text
    assert first.json()["created_count"] == 2

    second = app_client.post(f"/api/settings/providers/{pid}/models/import",
                             headers=admin_headers, json=payload)
    assert second.status_code == 200
    assert second.json()["created_count"] == 0
    assert second.json()["skipped_count"] == 2

    models_list = app_client.get("/api/settings/models", headers=admin_headers).json()["items"]
    assert sum(1 for m in models_list if m["provider_id"] == pid) == 2

    app_client.delete(f"/api/settings/providers/{pid}", headers=admin_headers)


def test_provider_protocol_update_persists(app_client, admin_headers):
    """协议可改，且列表接口回填 protocol + 中文标签。"""
    r = app_client.post("/api/settings/providers", headers=admin_headers, json={
        "name": "协议更新-供应商", "base_url": "http://127.0.0.1:9/v1",
        "api_key": "", "enabled": True,
    })
    pid = r.json()["id"]
    assert r.json()["protocol"] == "openai", "不传 protocol 时必须默认 openai"

    patched = app_client.patch(f"/api/settings/providers/{pid}", headers=admin_headers,
                               json={"protocol": "openai-responses"})
    assert patched.status_code == 200, patched.text
    assert patched.json()["protocol"] == "openai-responses"
    assert "Responses" in patched.json()["protocol_label"]

    listed = app_client.get("/api/settings/providers", headers=admin_headers).json()["items"]
    row = next(p for p in listed if p["id"] == pid)
    assert row["protocol"] == "openai-responses"

    app_client.delete(f"/api/settings/providers/{pid}", headers=admin_headers)


def test_old_database_gets_protocol_column(tmp_path):
    """老库升级：providers 缺 protocol 列时必须被自动补齐，且旧行为 openai。"""
    from sqlalchemy import create_engine, text

    from app.db import ensure_sqlite_columns

    db_path = tmp_path / "old_providers.sqlite3"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)
    models.Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(text('ALTER TABLE "providers" DROP COLUMN "protocol"'))
        conn.execute(text(
            "INSERT INTO providers (name, base_url, api_key_enc, enabled, created_at) "
            "VALUES ('旧供应商', 'http://127.0.0.1:9/v1', '', 1, '2026-01-01 00:00:00')"))

    added = ensure_sqlite_columns(engine, models.Base.metadata)
    assert "providers.protocol" in added

    with engine.begin() as conn:
        row = conn.execute(text("SELECT name, protocol FROM providers")).one()
    assert row[0] == "旧供应商"
    assert row[1] == "openai", "老库新列必须回填默认协议"
    engine.dispose()
