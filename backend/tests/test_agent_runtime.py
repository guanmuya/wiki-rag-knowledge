"""个人 Agent 运行时：CLI 核心、核心切换、停止与权限、执行边界、历史 kb 会话。"""
from __future__ import annotations

import json
import os
import socket
import sys
import time
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from app import config, models, security
from app.services import llm
from app.services.agent import plugins, runtime


def _wait_run(client, headers, run_id: int, timeout: float = 15.0) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        res = client.get(f"/api/agent/runs/{run_id}/events", headers=headers)
        assert res.status_code == 200, res.text
        last = res.json()
        if last["status"] != "running":
            return last
        time.sleep(0.1)
    raise AssertionError(f"run {run_id} 未在 {timeout}s 内结束：{last}")


def _make_model(db, *, protocol: str = "openai", base_url: str = "https://example.test") -> int:
    provider = models.Provider(
        name=f"AgentRT-{protocol}-{uuid.uuid4().hex[:8]}",
        base_url=base_url,
        api_key_enc=security.encrypt_secret("sk-test"),
        protocol=protocol,
    )
    db.add(provider)
    db.flush()
    cfg = models.ModelConfig(
        provider_id=provider.id, purpose="llm",
        model=f"agent-model-{uuid.uuid4().hex[:6]}", label="Agent Model",
    )
    db.add(cfg)
    db.commit()
    return cfg.id


def _make_kb(db, name: str | None = None) -> int:
    kb = models.KnowledgeBase(
        name=name or f"AgentKB-{uuid.uuid4().hex[:8]}", mode="basic", status="ready")
    db.add(kb)
    db.commit()
    return kb.id


def _write_fake_agent(directory, *, body: str, plugin_id: str = "fake-cli",
                      protocols: list[str] | None = None,
                      env: dict | None = None, output: str = "text",
                      text_paths: list[str] | None = None,
                      answer_item_types: list[str] | None = None,
                      usage_event: str = "") -> str:
    script = directory / f"{plugin_id}.py"
    script.write_text(body, encoding="utf-8")
    manifest = {
        "schema": 1,
        "id": plugin_id,
        "name": "Fake CLI",
        "description": "测试用 CLI 核心",
        "kind": "cli",
        "protocols": protocols or ["openai"],
        "bins": ["python"],
        "version_args": ["-c", "print('fake-cli 1.0')"],
        "run_args": [str(script), "{prompt}"],
        "env": env if env is not None else {
            "OPENAI_BASE_URL": "{base_url}",
            "OPENAI_MODEL": "{model}",
        },
        "output": output,
    }
    if text_paths:
        manifest["text_paths"] = text_paths
    if answer_item_types:
        manifest["answer_item_types"] = answer_item_types
    if usage_event:
        manifest["usage_event"] = usage_event
    (directory / f"{plugin_id}.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return plugin_id


@pytest.fixture()
def cli_plugin(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AGENT_PLUGIN_DIR", tmp_path)
    # 让 PATH 里的 python 一定指向当前解释器，避免命中 WindowsApps 桩。
    monkeypatch.setenv(
        "PATH",
        str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""),
    )
    plugins.clear_detection_cache()
    yield tmp_path
    plugins.clear_detection_cache()


@pytest.fixture()
def offline_llm(monkeypatch):
    """让这些用例完全离线：一次出网调用都不发，也不解析真实域名。

    由来：曾经的知识库核心会拿会话上的默认模型做意图分析、路由与生成，而测试库里
    供应商地址是假域名（`_make_model()` 用 `https://example.test`）。本机实测解析
    该域名要 11 秒、请求 10.6 秒，一次出网就把 `_wait_run()` 的 15 秒预算吃光——
    同一条用例于是「本机必挂、CI 全绿」（CI 对 `.test` 直接 NXDOMAIN）。实测与判定见
    `docs/evidence/release-v1.0.18-build.txt` §3.1。

    知识库核心已经下线（个人 Agent 不再与知识库交互），但这条守卫仍然值钱：它拦的是
    「用例悄悄长出出网路径」这件事本身，而不是某一条链路。两个手段都不改业务代码，
    也不放宽 `_wait_run()` 的超时：
    1. 在 HTTP 客户端这一层就地失败（`llm._client` 是仓库约定的测试接缝）：
       模型调用立刻拿到「不可用」，链路按设计降级为规则判断 + 抽取式回答；
    2. 把非回环域名的解析拦成异常：万一以后又长出绕过 `llm._client` 的出网路径，
       用例会当场在断言上暴露，而不是慢慢等到 15 秒超时——那才是把网络抖动藏起来。
    """
    def no_http_client(*args, **kwargs):
        raise llm.ModelUnavailable("测试强制离线：不发起任何真实模型调用")

    monkeypatch.setattr(llm, "_client", no_http_client)

    real_getaddrinfo = socket.getaddrinfo

    def guarded_getaddrinfo(host, *args, **kwargs):
        if host not in (None, "127.0.0.1", "localhost", "::1"):
            raise AssertionError(f"用例不应解析真实域名：{host}")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)


def test_create_session_ignores_legacy_kb_ids(app_client, admin_headers,
                                              db_session, cli_plugin, offline_llm):
    """个人 Agent 不再和知识库交互：核心列表里没有 kb，请求里的 kb_ids 被忽略。

    旧前端（或旧脚本）仍可能带着 `kb_ids`，这里保证它不会 422，也不会被写进会话。
    """
    kb_id = _make_kb(db_session)
    model_id = _make_model(db_session)
    _write_fake_agent(cli_plugin, body="print('ok')\n")
    plugins.clear_detection_cache()

    cores = app_client.get("/api/agent/cores", headers=admin_headers)
    assert cores.status_code == 200, cores.text
    ids = [item["id"] for item in cores.json()["items"]]
    assert "kb" not in ids
    assert all(item["kind"] == "cli" for item in cores.json()["items"])

    created = app_client.post("/api/agent/sessions", headers=admin_headers, json={
        "core_id": "fake-cli", "model_config_id": model_id,
        "kb_ids": [kb_id], "title": "忽略 kb_ids",
    })
    assert created.status_code == 201, created.text
    body = created.json()
    assert "kb_ids" not in body
    session = db_session.get(models.AgentSession, body["id"])
    assert json.loads(session.kb_ids or "[]") == []


def test_create_session_with_kb_core_explains_where_to_ask(app_client, admin_headers):
    """旧的前端还在传 core_id=kb 时，要说清「去智能问答页」，而不是干巴巴 404。"""
    res = app_client.post("/api/agent/sessions", headers=admin_headers, json={
        "core_id": "kb", "title": "旧会话",
    })
    assert res.status_code == 400, res.text
    detail = res.json()["detail"]
    assert "智能问答" in detail
    assert "CLI 核心" in detail


def test_legacy_kb_session_stays_readable_but_cannot_run(app_client, admin_headers,
                                                        db_session, cli_plugin,
                                                        offline_llm):
    """历史 kb 会话：历史消息可读、改标题可用、换核心后可继续，但不能直接再执行。"""
    admin = db_session.execute(
        select(models.User).where(models.User.username == "admin")).scalar_one()
    legacy = models.AgentSession(
        user_id=admin.id, title="旧的知识库会话", core_id="kb",
        kb_ids=json.dumps([_make_kb(db_session)]), status="idle")
    db_session.add(legacy)
    db_session.flush()
    db_session.add(models.AgentMessage(
        session_id=legacy.id, role="user", content="差旅报销标准是什么？",
        core_id="kb", status="ok"))
    db_session.add(models.AgentMessage(
        session_id=legacy.id, role="assistant", content="住宿上限 600 元。",
        core_id="kb", status="ok"))
    db_session.commit()

    detail = app_client.get(f"/api/agent/sessions/{legacy.id}", headers=admin_headers)
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["core_id"] == "kb"
    assert body["core_name"] == "知识库助手（已下线）"
    assert body["core_available"] is False
    assert body["core_legacy"] is True
    assert body["core"] is None
    assert [m["content"] for m in body["messages"]][0] == "差旅报销标准是什么？"
    assert body["messages"][1]["core_name"] == "知识库助手（已下线）"

    blocked = app_client.post(
        f"/api/agent/sessions/{legacy.id}/messages",
        headers=admin_headers, json={"content": "再问一次"})
    assert blocked.status_code == 400, blocked.text
    assert "智能问答" in blocked.json()["detail"]

    renamed = app_client.patch(
        f"/api/agent/sessions/{legacy.id}", headers=admin_headers,
        json={"title": "重命名后的旧会话"})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["title"] == "重命名后的旧会话"

    model_id = _make_model(db_session)
    _write_fake_agent(cli_plugin, body="import sys\nprint('switched:' + sys.argv[1][-12:])\n")
    plugins.clear_detection_cache()
    switched = app_client.patch(
        f"/api/agent/sessions/{legacy.id}", headers=admin_headers,
        json={"core_id": "fake-cli", "model_config_id": model_id})
    assert switched.status_code == 200, switched.text
    assert switched.json()["core_id"] == "fake-cli"
    assert switched.json()["core_available"] is True
    assert switched.json()["core_legacy"] is False

    detail = app_client.get(f"/api/agent/sessions/{legacy.id}", headers=admin_headers).json()
    assert any(m["role"] == "system" and "知识库助手（已下线）" in m["content"]
               for m in detail["messages"])

    sent = app_client.post(
        f"/api/agent/sessions/{legacy.id}/messages", headers=admin_headers,
        json={"content": "换核心后继续"})
    assert sent.status_code == 200, sent.text
    result = _wait_run(app_client, admin_headers, sent.json()["run_id"])
    assert result["status"] == "ok", result
    assert "switched:" in result["message"]["content"]


def test_cli_core_streams_and_injects_model(app_client, admin_headers, db_session, cli_plugin):
    model_id = _make_model(db_session)
    _write_fake_agent(cli_plugin, body=(
        "import os, sys\n"
        "prompt = sys.argv[1] if len(sys.argv) > 1 else ''\n"
        "print('BASE=' + os.environ.get('OPENAI_BASE_URL', ''))\n"
        "print('MODEL=' + os.environ.get('OPENAI_MODEL', ''))\n"
        "print('USER=' + ('yes' if '你好' in prompt else 'no'))\n"
    ))
    plugins.clear_detection_cache()

    created = app_client.post("/api/agent/sessions", headers=admin_headers, json={
        "core_id": "fake-cli", "model_config_id": model_id,
    })
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]

    sent = app_client.post(
        f"/api/agent/sessions/{session_id}/messages",
        headers=admin_headers, json={"content": "你好"},
    )
    assert sent.status_code == 200, sent.text
    result = _wait_run(app_client, admin_headers, sent.json()["run_id"])
    assert result["status"] == "ok", result
    content = result["message"]["content"]
    assert "BASE=https://example.test/v1" in content
    assert "MODEL=agent-model-" in content
    assert "USER=yes" in content
    assert result["message"]["meta"]["protocol"] == "openai"


def test_switch_core_mid_session(app_client, admin_headers, db_session, cli_plugin,
                                 offline_llm):
    model_id = _make_model(db_session)
    _write_fake_agent(cli_plugin, body="import sys\nprint('first:' + sys.argv[1][-20:])\n",
                      plugin_id="fake-cli")
    _write_fake_agent(cli_plugin, body="import sys\nprint('switched:' + sys.argv[1][-20:])\n",
                      plugin_id="fake-cli-2")
    plugins.clear_detection_cache()

    created = app_client.post("/api/agent/sessions", headers=admin_headers, json={
        "core_id": "fake-cli", "model_config_id": model_id,
    })
    session_id = created.json()["id"]
    first = app_client.post(
        f"/api/agent/sessions/{session_id}/messages", headers=admin_headers,
        json={"content": "第一轮"})
    assert _wait_run(app_client, admin_headers, first.json()["run_id"])["status"] == "ok"

    switched = app_client.patch(
        f"/api/agent/sessions/{session_id}", headers=admin_headers,
        json={"core_id": "fake-cli-2", "model_config_id": model_id},
    )
    assert switched.status_code == 200, switched.text
    assert switched.json()["core_id"] == "fake-cli-2"

    detail = app_client.get(f"/api/agent/sessions/{session_id}", headers=admin_headers).json()
    assert any(m["role"] == "system" and "已切换" in m["content"] for m in detail["messages"])

    second = app_client.post(
        f"/api/agent/sessions/{session_id}/messages", headers=admin_headers,
        json={"content": "第二轮"})
    result = _wait_run(app_client, admin_headers, second.json()["run_id"])
    assert result["status"] == "ok", result
    assert result["message"]["core_id"] == "fake-cli-2"
    assert "switched:" in result["message"]["content"]


def test_stop_running_cli(app_client, admin_headers, db_session, cli_plugin):
    model_id = _make_model(db_session)
    _write_fake_agent(cli_plugin, body=(
        "import sys, time\n"
        "print('started', flush=True)\n"
        "time.sleep(30)\n"
        "print('finished')\n"
    ))
    plugins.clear_detection_cache()

    created = app_client.post("/api/agent/sessions", headers=admin_headers, json={
        "core_id": "fake-cli", "model_config_id": model_id,
    })
    session_id = created.json()["id"]
    sent = app_client.post(
        f"/api/agent/sessions/{session_id}/messages", headers=admin_headers,
        json={"content": "跑久一点"})
    run_id = sent.json()["run_id"]

    deadline = time.time() + 10
    while time.time() < deadline:
        snap = app_client.get(f"/api/agent/runs/{run_id}/events", headers=admin_headers).json()
        if any("started" in str(e["data"].get("text", "")) for e in snap["events"]):
            break
        time.sleep(0.1)

    stopped = app_client.post(f"/api/agent/runs/{run_id}/stop", headers=admin_headers)
    assert stopped.status_code == 200, stopped.text
    result = _wait_run(app_client, admin_headers, run_id)
    assert result["status"] == "stopped", result
    assert result["message"]["status"] == "stopped"


def test_employee_cannot_use_agent(app_client, db_session):
    username = f"agentuser{uuid.uuid4().hex[:6]}"
    db_session.add(models.User(
        username=username, display_name="Agent 员工",
        password_hash=security.hash_password("AgentUser#2026"),
        role=models.Role.employee.value, status=models.UserStatus.active.value,
        must_change_password=False,
    ))
    db_session.commit()
    login = app_client.post("/api/auth/login", json={
        "username": username, "password": "AgentUser#2026"})
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['token']}"}
    assert app_client.get("/api/agent/cores", headers=headers).status_code == 403
    assert app_client.get("/api/agent/sessions", headers=headers).status_code == 403


def test_cli_core_rejects_incompatible_model(app_client, admin_headers,
                                             db_session, cli_plugin):
    # 本用例验证「CLI 核心因模型协议不兼容而拒绝」，命中 _validate_model 里
    # 带「不支持」的那条分支。不能用内置 codex 核心触发：create_session 会先经
    # _core 检查可执行文件是否安装，没装 Codex CLI 的干净机器（CI runner）会先
    # 返回「Codex CLI 未安装」，根本走不到协议校验。改用一份探测必然成功的假 CLI
    # 核心（可执行文件就是当前 Python 解释器、只声明 openai 协议），把「CLI 是否
    # 已安装」这一环境依赖从用例里隔离掉，断言保持原样。
    _write_fake_agent(cli_plugin, body="print('unused')\n",
                      plugin_id="openai-only-cli", protocols=["openai"])
    plugins.clear_detection_cache()

    anthropic_model = _make_model(db_session, protocol="anthropic",
                                  base_url="https://api.anthropic.test")
    res = app_client.post("/api/agent/sessions", headers=admin_headers, json={
        "core_id": "openai-only-cli", "model_config_id": anthropic_model,
    })
    assert res.status_code == 400, res.text
    assert "不支持" in res.json()["detail"]


def test_plugin_toggle_blocks_new_session(app_client, admin_headers, db_session, cli_plugin):
    model_id = _make_model(db_session)
    _write_fake_agent(cli_plugin, body="print('ok')\n")
    plugins.clear_detection_cache()

    disabled = app_client.patch(
        "/api/agent/plugins/fake-cli", headers=admin_headers, json={"enabled": False})
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["enabled"] is False

    blocked = app_client.post("/api/agent/sessions", headers=admin_headers, json={
        "core_id": "fake-cli", "model_config_id": model_id,
    })
    assert blocked.status_code == 400
    assert "已停用" in blocked.json()["detail"]

    enabled = app_client.patch(
        "/api/agent/plugins/fake-cli", headers=admin_headers, json={"enabled": True})
    assert enabled.status_code == 200
    allowed = app_client.post("/api/agent/sessions", headers=admin_headers, json={
        "core_id": "fake-cli", "model_config_id": model_id,
    })
    assert allowed.status_code == 201, allowed.text


def test_legacy_kb_core_toggle_explains_where_to_ask(app_client, admin_headers):
    """kb 核心已下线：PATCH 它要说明去哪儿问，而不是 404 了事。"""
    res = app_client.patch(
        "/api/agent/plugins/kb", headers=admin_headers, json={"enabled": False})
    assert res.status_code == 400
    assert "智能问答" in res.json()["detail"]


def test_recover_pending_marks_failed(app_client, db_session):
    admin = db_session.execute(
        select(models.User).where(models.User.username == "admin")).scalar_one()
    session = models.AgentSession(user_id=admin.id, core_id="codex", status="running")
    db_session.add(session)
    db_session.flush()
    message = models.AgentMessage(
        session_id=session.id, role="assistant", status="pending", content="")
    db_session.add(message)
    db_session.commit()

    assert runtime.recover_pending() >= 1
    db_session.refresh(message)
    db_session.refresh(session)
    assert message.status == "failed"
    assert message.content
    assert session.status == "idle"


def test_events_for_unknown_run_reports_failed(app_client, admin_headers, db_session):
    admin = db_session.execute(
        select(models.User).where(models.User.username == "admin")).scalar_one()
    session = models.AgentSession(user_id=admin.id, core_id="codex", status="running")
    db_session.add(session)
    db_session.flush()
    message = models.AgentMessage(
        session_id=session.id, role="assistant", status="pending", content="")
    db_session.add(message)
    db_session.commit()

    res = app_client.get(f"/api/agent/runs/{message.id}/events", headers=admin_headers)
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "failed"
    assert body["error"]


def test_cannot_delete_running_session(app_client, admin_headers, db_session, cli_plugin):
    model_id = _make_model(db_session)
    _write_fake_agent(cli_plugin, body="import time\nprint('started', flush=True)\ntime.sleep(30)\n")
    plugins.clear_detection_cache()
    created = app_client.post("/api/agent/sessions", headers=admin_headers, json={
        "core_id": "fake-cli", "model_config_id": model_id,
    })
    session_id = created.json()["id"]
    sent = app_client.post(
        f"/api/agent/sessions/{session_id}/messages", headers=admin_headers,
        json={"content": "跑"})
    run_id = sent.json()["run_id"]
    time.sleep(0.4)
    res = app_client.delete(f"/api/agent/sessions/{session_id}", headers=admin_headers)
    assert res.status_code == 409
    app_client.post(f"/api/agent/runs/{run_id}/stop", headers=admin_headers)
    _wait_run(app_client, admin_headers, run_id)


# ------------------------------------------------------------------ P1-7 执行边界
def test_workspace_must_stay_inside_agent_workspace(app_client, admin_headers,
                                                    db_session, cli_plugin, tmp_path):
    """会话工作目录越界（含同前缀目录）必须返回 400。"""
    model_id = _make_model(db_session)
    _write_fake_agent(cli_plugin, body="print('ok')\n")
    plugins.clear_detection_cache()
    root = Path(config.AGENT_WORKSPACE_DIR).resolve()
    inside = root / "nested-project"
    inside.mkdir(exist_ok=True)
    created = app_client.post("/api/agent/sessions", headers=admin_headers, json={
        "core_id": "fake-cli", "model_config_id": model_id, "workspace": str(inside),
    })
    assert created.status_code == 201, created.text
    assert Path(created.json()["workspace"]) == inside.resolve()

    sibling = root.parent / (root.name + "_evil")
    sibling.mkdir(exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    for bad in [str(outside), str(sibling), str(root / ".." / "outside")]:
        res = app_client.post("/api/agent/sessions", headers=admin_headers, json={
            "core_id": "fake-cli", "model_config_id": model_id, "workspace": bad,
        })
        assert res.status_code == 400, (bad, res.text)
    assert "工作区" in app_client.post(
        "/api/agent/sessions", headers=admin_headers,
        json={"core_id": "fake-cli", "model_config_id": model_id,
              "workspace": str(sibling)},
    ).json()["detail"]

    patched = app_client.patch(
        f"/api/agent/sessions/{created.json()['id']}", headers=admin_headers,
        json={"workspace": str(outside)},
    )
    assert patched.status_code == 400, patched.text


def test_cli_prompt_is_a_generic_direct_answering_assistant():
    """提示词不再有知识库语境，并明确「直接回答、别先让用户确认任务」。"""
    ctx = runtime.RunContext(
        message_id=1, session_id=2, user_id=3,
        plugin=plugins.BUILTIN_BY_ID["codex"],
        question="2026 年中国企业级 AI Agent 市场有多大？",
        history=[
            {"role": "user", "content": "先聊聊市场"},
            {"role": "assistant", "content": "你想从哪个角度看？\n另外要哪个地区？"},
            {"role": "user", "content": "2026 年中国企业级 AI Agent 市场有多大？"},
        ],
        workspace=str(config.AGENT_WORKSPACE_DIR),
    )
    prompt = runtime._build_cli_prompt(ctx)
    assert "企业知识库" not in prompt
    assert "文档整理" not in prompt
    assert "知识库" not in prompt
    assert "直接回答" in prompt
    assert "不要先让用户确认任务类型" in prompt
    assert "2026 年中国企业级 AI Agent 市场有多大？" in prompt
    assert "助手：你想从哪个角度看？" in prompt
    # 本轮提问只出现一次：历史里最后一条是本轮提问，应被剔掉后再单列。
    assert prompt.count("2026 年中国企业级 AI Agent 市场有多大？") == 1
    # 必须是单行：codex exec 只取位置参数第一行，多行会把后面的问题丢掉
    # （codex-cli 0.167.0 本机实测），那样模型只会看到一个开场白。
    assert "\n" not in prompt
    assert "\r" not in prompt
    assert prompt.endswith("请直接回答用户最新的问题：")
    assert len(prompt) <= 20000


def test_cli_gets_prompt_as_a_single_argument_line(app_client, admin_headers,
                                                   db_session, cli_plugin):
    """运行期真实传给 CLI 的提示词必须是没有换行的单行参数。

    codex exec 只取位置参数第一行（本机 0.167.0 实测），多行会把用户提问丢掉。
    这条用例守的是 `_run_cli` 实际拼出来的那个参数，而不只是提示词构造函数。
    """
    model_id = _make_model(db_session)
    _write_fake_agent(cli_plugin, body=(
        "import sys\n"
        "prompt = sys.argv[-1]\n"
        "print('NL=%d' % prompt.count(chr(10)))\n"
        "print('HASQ=%d' % (1 if '市场有多大' in prompt else 0))\n"
    ))
    plugins.clear_detection_cache()

    created = app_client.post("/api/agent/sessions", headers=admin_headers, json={
        "core_id": "fake-cli", "model_config_id": model_id,
    })
    sent = app_client.post(
        f"/api/agent/sessions/{created.json()['id']}/messages",
        headers=admin_headers, json={"content": "2026 年中国企业级 AI Agent 市场有多大？"})
    result = _wait_run(app_client, admin_headers, sent.json()["run_id"])
    assert result["status"] == "ok", result
    content = result["message"]["content"]
    assert "NL=0" in content, content
    assert "HASQ=1" in content, content


def test_api_key_never_appears_in_command_line_or_events(
        app_client, admin_headers, db_session, cli_plugin):
    """密钥只经 env 注入；命令行摘要、事件日志、正文统一脱敏。"""
    model_id = _make_model(db_session)          # 供应商密钥 = sk-test
    _write_fake_agent(
        cli_plugin,
        body="import os\nprint('KEY=' + os.environ.get('OPENAI_API_KEY', ''))\n",
        env={"OPENAI_API_KEY": "{api_key}"},
    )
    plugins.clear_detection_cache()

    created = app_client.post("/api/agent/sessions", headers=admin_headers, json={
        "core_id": "fake-cli", "model_config_id": model_id,
    })
    assert created.status_code == 201, created.text
    sent = app_client.post(
        f"/api/agent/sessions/{created.json()['id']}/messages",
        headers=admin_headers, json={"content": "检查密钥脱敏"},
    )
    result = _wait_run(app_client, admin_headers, sent.json()["run_id"])
    assert result["status"] == "ok", result

    # 完整快照里含所有事件 + 最终消息 + meta.command，逐字检查密钥不出现。
    blob = json.dumps(result, ensure_ascii=False)
    assert "sk-test" not in blob
    assert "<密钥>" in blob            # 证明确实注入了密钥、随后被脱敏
    assert "sk-test" not in result["message"]["meta"]["command"]


# ------------------------------------------------------------------ 用量（P1 用量入统计）
@pytest.mark.parametrize("obj, event, expected", [
    # Codex 0.167.0 的 turn.completed 结构（input/output_tokens）
    ({"type": "turn.completed",
      "usage": {"input_tokens": 3, "output_tokens": 4}}, "turn.completed", (3, 4)),
    # OpenAI 兼容 chat 的写法（prompt/completion_tokens）同样认
    ({"type": "turn.completed",
      "usage": {"prompt_tokens": 5, "completion_tokens": 6}}, "turn.completed", (5, 6)),
    # 只有一侧有值也照实记，另一侧按 0
    ({"type": "turn.completed", "usage": {"output_tokens": 7}}, "turn.completed", (0, 7)),
    # 负数、bool、空对象、非用量事件一律返回 None（当作「没有用量」）
    ({"type": "turn.completed", "usage": {"input_tokens": -1}}, "turn.completed", None),
    # 坏掉的一侧忽略、好的一侧照记——与 usage._valid_counters 同口径
    ({"type": "turn.completed",
      "usage": {"input_tokens": True, "output_tokens": 2}}, "turn.completed", (0, 2)),
    ({"type": "turn.completed",
      "usage": {"input_tokens": "1", "output_tokens": 1.5}}, "turn.completed", None),
    ({"type": "turn.completed", "usage": {}}, "turn.completed", None),
    ({"type": "item.completed", "usage": {"input_tokens": 1}}, "turn.completed", None),
    ({"type": "turn.completed"}, "turn.completed", None),
    ({"type": "turn.completed", "usage": {"input_tokens": 1}}, "", None),
    ("not an object", "turn.completed", None),
])
def test_jsonl_usage_parser(obj, event, expected):
    assert runtime._jsonl_usage(obj, event) == expected


def test_jsonl_usage_event_is_written_to_agent_meta(app_client, admin_headers,
                                                    db_session, cli_plugin):
    """JSONL 核心按清单声明的 usage_event 累计 token，写进 meta.usage。

    样例按本机 codex-cli 0.167.0 的事件结构（thread.started / item.completed /
    turn.completed）构造；本机没有可用的在线模型，所以这是**样例验证**，
    不是真实模型调用的实测数字。
    """
    model_id = _make_model(db_session)
    _write_fake_agent(cli_plugin, output="jsonl", text_paths=["item.text"],
                      usage_event="turn.completed", body="""
import json


def emit(payload):
    print(json.dumps(payload), flush=True)


emit({"type": "thread.started", "thread_id": "t-1"})
emit({"type": "item.completed", "item": {"type": "agent_message", "text": "第一段"}})
emit({"type": "turn.completed", "usage": {"input_tokens": 120,
                                          "cached_input_tokens": 40,
                                          "output_tokens": 30,
                                          "reasoning_output_tokens": 5,
                                          "total_tokens": 150}})
emit({"type": "item.completed", "item": {"type": "agent_message", "text": "第二段"}})
emit({"type": "turn.completed", "usage": {"input_tokens": 80, "output_tokens": 20,
                                          "total_tokens": 100}})
""")
    plugins.clear_detection_cache()

    created = app_client.post("/api/agent/sessions", headers=admin_headers, json={
        "core_id": "fake-cli", "model_config_id": model_id,
    })
    assert created.status_code == 201, created.text
    sent = app_client.post(
        f"/api/agent/sessions/{created.json()['id']}/messages",
        headers=admin_headers, json={"content": "跑一轮"},
    )
    assert sent.status_code == 200, sent.text
    result = _wait_run(app_client, admin_headers, sent.json()["run_id"])
    assert result["status"] == "ok", result
    assert "第一段" in result["message"]["content"]
    assert "第二段" in result["message"]["content"]

    meta = result["message"]["meta"]
    usage = meta["usage"]
    # 两次 turn.completed 累加；调用仍记 1 次（这是「一次执行」）。
    assert usage["prompt_tokens"] == 200
    assert usage["completion_tokens"] == 50
    assert usage["calls"] == 1 and usage["chat_calls"] == 1 and usage["live_calls"] == 1
    assert usage["embed_tokens"] == 0
    assert usage["latency_ms"] >= 0
    assert "usage_unmetered" not in meta


def test_text_cli_without_usage_is_marked_unmetered(app_client, admin_headers,
                                                    db_session, cli_plugin):
    """纯文本核心解析不到用量：token 记 0 并标注，绝不猜数字。"""
    model_id = _make_model(db_session)
    _write_fake_agent(cli_plugin, body="print('plain text output')\n")
    plugins.clear_detection_cache()

    created = app_client.post("/api/agent/sessions", headers=admin_headers, json={
        "core_id": "fake-cli", "model_config_id": model_id,
    })
    assert created.status_code == 201, created.text
    sent = app_client.post(
        f"/api/agent/sessions/{created.json()['id']}/messages",
        headers=admin_headers, json={"content": "跑一轮"},
    )
    result = _wait_run(app_client, admin_headers, sent.json()["run_id"])
    assert result["status"] == "ok", result

    meta = result["message"]["meta"]
    assert meta["usage_unmetered"] is True
    assert meta["usage"]["calls"] == 1
    assert meta["usage"]["prompt_tokens"] == 0
    assert meta["usage"]["completion_tokens"] == 0


# -------------------------------------------------- 思考过程与回答分区（2026-11-11）
# 用户截图反馈：个人 Agent 的一条回复气泡里，整段模型内心推理直接铺在回答位置，
# 最终答案根本没出现。根因是 JSONL 解析只看 `text_paths`、不看 `item.type`，
# 于是 Codex 的 `reasoning` 项和 `agent_message` 项挤进同一条正文通道。
# 下面的事件结构来自本机 codex-cli 0.167.0 的实测输出（docs/AGENT_PLUGINS.md §3.4）。
CODEX_REASONING = "用户问的是 2026 年市场规模，先查资料再回答。"
CODEX_ANSWER = "2026 年中国企业级 AI Agent 市场约 240 亿元。"


def _jsonl_body(*events: dict) -> str:
    """一段把给定事件逐行打到 stdout 的假 CLI 源码（`codex exec --json` 同构）。"""
    lines = [
        "import json",
        "",
        "",
        "def emit(payload):",
        "    print(json.dumps(payload, ensure_ascii=False), flush=True)",
        "",
    ]
    for event in events:
        lines.append(f"emit({json.dumps(event, ensure_ascii=False)})")
    return "\n".join(lines) + "\n"


def _ask_once(app_client, admin_headers, model_id: int, question: str = "市场有多大？",
              plugin_id: str = "fake-cli") -> dict:
    created = app_client.post("/api/agent/sessions", headers=admin_headers, json={
        "core_id": plugin_id, "model_config_id": model_id,
    })
    assert created.status_code == 201, created.text
    sent = app_client.post(
        f"/api/agent/sessions/{created.json()['id']}/messages",
        headers=admin_headers, json={"content": question},
    )
    assert sent.status_code == 200, sent.text
    return _wait_run(app_client, admin_headers, sent.json()["run_id"])


def test_jsonl_reasoning_is_separated_from_the_answer(app_client, admin_headers,
                                                      db_session, cli_plugin):
    """推理进思考区（meta.thinking + thinking 事件），回答正文只留 agent_message。

    这是用户那条截图的回归用例：修复前 reasoning 的 `item.text` 会被当成正文，
    气泡里看到的是整段内心独白，答案反而没有落进气泡。
    """
    model_id = _make_model(db_session)
    _write_fake_agent(
        cli_plugin, output="jsonl", text_paths=["item.text"],
        answer_item_types=["agent_message"],
        body=_jsonl_body(
            {"type": "thread.started", "thread_id": "t-1"},
            {"type": "turn.started"},
            {"type": "item.completed",
             "item": {"id": "item_2", "type": "reasoning", "text": CODEX_REASONING}},
            {"type": "item.completed",
             "item": {"id": "item_3", "type": "agent_message", "text": CODEX_ANSWER}},
            {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}},
        ),
    )
    plugins.clear_detection_cache()

    result = _ask_once(app_client, admin_headers, model_id)
    assert result["status"] == "ok", result

    content = result["message"]["content"]
    assert CODEX_ANSWER in content
    assert "用户问的是" not in content
    assert result["message"]["meta"]["thinking"].strip() == CODEX_REASONING

    text_events = "".join(
        e["data"]["text"] for e in result["events"] if e["type"] == "text")
    thinking_events = "".join(
        e["data"]["text"] for e in result["events"] if e["type"] == "thinking")
    assert CODEX_ANSWER in text_events
    assert "用户问的是" not in text_events
    assert CODEX_REASONING in thinking_events


def test_jsonl_command_items_are_process_not_answer(app_client, admin_headers,
                                                    db_session, cli_plugin):
    """命令执行/工具调用算「过程」：绝不进回答气泡，但仍可在思考区看到。"""
    model_id = _make_model(db_session)
    command_text = "$ rg --files\n退出码 1：blocked by policy"
    _write_fake_agent(
        cli_plugin, output="jsonl", text_paths=["item.text"],
        answer_item_types=["agent_message"],
        body=_jsonl_body(
            {"type": "item.completed",
             "item": {"type": "reasoning", "text": "先看看工作目录里有没有资料。"}},
            {"type": "item.completed",
             "item": {"type": "command_execution", "command": "rg --files",
                      "text": command_text, "exit_code": 1, "status": "failed"}},
            {"type": "item.completed",
             "item": {"type": "agent_message", "text": CODEX_ANSWER}},
        ),
    )
    plugins.clear_detection_cache()

    result = _ask_once(app_client, admin_headers, model_id)
    assert result["status"] == "ok", result
    assert result["message"]["content"].strip() == CODEX_ANSWER
    thinking = result["message"]["meta"]["thinking"]
    assert "先看看工作目录" in thinking
    assert "rg --files" in thinking


def test_jsonl_without_answer_item_types_still_keeps_process_out(app_client, admin_headers,
                                                                 db_session, cli_plugin):
    """没声明 `answer_item_types` 的清单：过程类 item 仍然不进正文，其它照旧。

    兼容口径：外部插件不改清单也不会再被「推理冒充回答」坑到（reasoning 等已知
    过程类型默认归思考区）；但不认识的事件类型必须保持旧行为——不能悄悄吞掉
    别人的正文。
    """
    model_id = _make_model(db_session)
    _write_fake_agent(
        cli_plugin, output="jsonl", text_paths=["item.text"],
        body=_jsonl_body(
            {"type": "item.completed", "item": {"type": "reasoning", "text": CODEX_REASONING}},
            {"type": "item.completed", "item": {"type": "message", "text": CODEX_ANSWER}},
        ),
    )
    plugins.clear_detection_cache()

    result = _ask_once(app_client, admin_headers, model_id)
    assert result["status"] == "ok", result
    assert result["message"]["content"].strip() == CODEX_ANSWER
    assert result["message"]["meta"]["thinking"].strip() == CODEX_REASONING


def test_jsonl_reasoning_only_run_says_there_is_no_answer(app_client, admin_headers,
                                                          db_session, cli_plugin):
    """只出推理、没有最终回答：状态记 failed，正文说清「没有最终回答」并留证据。

    不把推理倒进正文（那是用户的抱怨），也不假装成功：思考过程留在 meta.thinking
    里，界面据此给出可展开的「思考过程」，能看出它卡在哪一步。
    """
    model_id = _make_model(db_session)
    _write_fake_agent(
        cli_plugin, output="jsonl", text_paths=["item.text"],
        answer_item_types=["agent_message"],
        body=_jsonl_body(
            {"type": "item.completed", "item": {"type": "reasoning", "text": CODEX_REASONING}},
            {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}},
        ),
    )
    plugins.clear_detection_cache()

    result = _ask_once(app_client, admin_headers, model_id)
    assert result["status"] == "failed", result
    content = result["message"]["content"]
    assert "只产出了思考过程" in content
    assert "用户问的是" not in content
    assert result["message"]["meta"]["thinking"].strip() == CODEX_REASONING
    assert result["message"]["status"] == "failed"


def test_thinking_is_not_replayed_into_the_next_prompt(app_client, admin_headers,
                                                       db_session, cli_plugin):
    """思考过程只给用户看，不回放进下一轮提示词：历史仍只带回答正文。"""
    model_id = _make_model(db_session)
    prompt_log = cli_plugin / "prompt.log"
    _write_fake_agent(
        cli_plugin, output="jsonl", text_paths=["item.text"],
        answer_item_types=["agent_message"],
        body=(
            "import json, sys\n"
            f"open({str(prompt_log)!r}, 'a', encoding='utf-8')"
            ".write(sys.argv[1] + '\\n')\n"
            "print(json.dumps({'type': 'item.completed',"
            " 'item': {'type': 'reasoning', 'text': "
            f"{CODEX_REASONING!r}" "}}, ensure_ascii=False), flush=True)\n"
            "print(json.dumps({'type': 'item.completed',"
            " 'item': {'type': 'agent_message', 'text': "
            f"{CODEX_ANSWER!r}" "}}, ensure_ascii=False), flush=True)\n"
        ),
    )
    plugins.clear_detection_cache()

    created = app_client.post("/api/agent/sessions", headers=admin_headers, json={
        "core_id": "fake-cli", "model_config_id": model_id,
    })
    session_id = created.json()["id"]
    for question in ("第一轮：市场有多大？", "第二轮：再说细一点"):
        sent = app_client.post(
            f"/api/agent/sessions/{session_id}/messages",
            headers=admin_headers, json={"content": question},
        )
        assert sent.status_code == 200, sent.text
        assert _wait_run(app_client, admin_headers, sent.json()["run_id"])["status"] == "ok"

    prompts = prompt_log.read_text(encoding="utf-8").splitlines()
    assert len(prompts) == 2, prompts
    # 第二轮提示词带上了上一轮的回答（历史回放），但绝不能带上思考过程。
    assert CODEX_ANSWER in prompts[1]
    assert "用户问的是" not in prompts[1]
