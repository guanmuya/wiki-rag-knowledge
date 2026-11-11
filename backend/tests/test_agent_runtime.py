"""个人 Agent 运行时：CLI 核心、核心切换、停止与权限、执行边界、历史 kb 会话。"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from app import config, models, security
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
                      env: dict | None = None) -> str:
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
        "output": "text",
    }
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


def test_create_session_ignores_legacy_kb_ids(app_client, admin_headers,
                                              db_session, cli_plugin):
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
                                                        db_session, cli_plugin):
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


def test_switch_core_mid_session(app_client, admin_headers, db_session, cli_plugin):
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
            {"role": "assistant", "content": "你想从哪个角度看？"},
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
