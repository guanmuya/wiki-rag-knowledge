"""个人 Agent 运行时：知识库核心、CLI 核心、核心切换、停止与权限、执行边界。"""
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


def test_kb_session_flow(app_client, admin_headers, db_session):
    kb_id = _make_kb(db_session)
    created = app_client.post("/api/agent/sessions", headers=admin_headers, json={
        "core_id": "kb", "kb_ids": [kb_id], "title": "知识库测试",
    })
    assert created.status_code == 201, created.text
    session = created.json()
    assert session["core_id"] == "kb"
    assert session["kb_ids"] == [kb_id]

    sent = app_client.post(
        f"/api/agent/sessions/{session['id']}/messages",
        headers=admin_headers, json={"content": "差旅报销标准是什么？"},
    )
    assert sent.status_code == 200, sent.text
    run_id = sent.json()["run_id"]

    result = _wait_run(app_client, admin_headers, run_id)
    assert result["status"] == "ok", result
    assert result["message"]["status"] == "ok"
    assert result["message"]["content"]
    assert result["message"]["core_id"] == "kb"

    detail = app_client.get(f"/api/agent/sessions/{session['id']}", headers=admin_headers)
    assert detail.status_code == 200
    body = detail.json()
    assert len(body["messages"]) == 2
    assert body["messages"][0]["role"] == "user"
    assert body["messages"][1]["role"] == "assistant"


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
    kb_id = _make_kb(db_session)
    model_id = _make_model(db_session)
    _write_fake_agent(cli_plugin, body="import sys\nprint('switched:' + sys.argv[1][-20:])\n")
    plugins.clear_detection_cache()

    created = app_client.post("/api/agent/sessions", headers=admin_headers, json={
        "core_id": "kb", "kb_ids": [kb_id],
    })
    session_id = created.json()["id"]
    first = app_client.post(
        f"/api/agent/sessions/{session_id}/messages", headers=admin_headers,
        json={"content": "第一轮"})
    assert _wait_run(app_client, admin_headers, first.json()["run_id"])["status"] == "ok"

    switched = app_client.patch(
        f"/api/agent/sessions/{session_id}", headers=admin_headers,
        json={"core_id": "fake-cli", "model_config_id": model_id},
    )
    assert switched.status_code == 200, switched.text
    assert switched.json()["core_id"] == "fake-cli"

    detail = app_client.get(f"/api/agent/sessions/{session_id}", headers=admin_headers).json()
    assert any(m["role"] == "system" and "已切换" in m["content"] for m in detail["messages"])

    second = app_client.post(
        f"/api/agent/sessions/{session_id}/messages", headers=admin_headers,
        json={"content": "第二轮"})
    result = _wait_run(app_client, admin_headers, second.json()["run_id"])
    assert result["status"] == "ok", result
    assert result["message"]["core_id"] == "fake-cli"
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


def test_cli_core_rejects_incompatible_model(app_client, admin_headers, db_session):
    anthropic_model = _make_model(db_session, protocol="anthropic",
                                  base_url="https://api.anthropic.test")
    res = app_client.post("/api/agent/sessions", headers=admin_headers, json={
        "core_id": "codex", "model_config_id": anthropic_model,
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


def test_kb_core_cannot_be_disabled(app_client, admin_headers):
    res = app_client.patch(
        "/api/agent/plugins/kb", headers=admin_headers, json={"enabled": False})
    assert res.status_code == 400
    assert "不能停用" in res.json()["detail"]


def test_recover_pending_marks_failed(app_client, db_session):
    admin = db_session.execute(
        select(models.User).where(models.User.username == "admin")).scalar_one()
    session = models.AgentSession(user_id=admin.id, core_id="kb", status="running")
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
    session = models.AgentSession(user_id=admin.id, core_id="kb", status="running")
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
                                                    db_session, tmp_path):
    """会话工作目录越界（含同前缀目录）必须返回 400。"""
    root = Path(config.AGENT_WORKSPACE_DIR).resolve()
    inside = root / "nested-project"
    inside.mkdir(exist_ok=True)
    created = app_client.post("/api/agent/sessions", headers=admin_headers, json={
        "core_id": "kb", "workspace": str(inside),
    })
    assert created.status_code == 201, created.text
    assert Path(created.json()["workspace"]) == inside.resolve()

    sibling = root.parent / (root.name + "_evil")
    sibling.mkdir(exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    for bad in [str(outside), str(sibling), str(root / ".." / "outside")]:
        res = app_client.post("/api/agent/sessions", headers=admin_headers, json={
            "core_id": "kb", "workspace": bad,
        })
        assert res.status_code == 400, (bad, res.text)
    assert "工作区" in app_client.post(
        "/api/agent/sessions", headers=admin_headers,
        json={"core_id": "kb", "workspace": str(sibling)},
    ).json()["detail"]

    patched = app_client.patch(
        f"/api/agent/sessions/{created.json()['id']}", headers=admin_headers,
        json={"workspace": str(outside)},
    )
    assert patched.status_code == 400, patched.text


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
