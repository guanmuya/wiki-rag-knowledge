"""Agent 插件注册表：清单校验、发现、探测、模型协议过滤、执行边界收敛（P1-7）。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from app import config, models, security
from app.services.agent import plugins


def _write_manifest(directory, name: str, payload: dict) -> None:
    (directory / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _python_on_path(monkeypatch) -> None:
    """让 PATH 里的 python 一定指向当前解释器，避免命中 WindowsApps 桩。"""
    monkeypatch.setenv(
        "PATH",
        str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""),
    )


def _fake_manifest(**overrides) -> dict:
    payload = {
        "schema": 1,
        "id": "fake-agent",
        "name": "Fake Agent",
        "description": "测试用",
        "kind": "cli",
        "protocols": ["openai"],
        "bins": ["python"],
        "version_args": ["-c", "print('fake-agent 9.9')"],
        "run_args": ["-c", "print('ok')", "{prompt}"],
        "env": {"OPENAI_BASE_URL": "{base_url}", "OPENAI_MODEL": "{model}"},
        "output": "text",
    }
    payload.update(overrides)
    return payload


def test_parse_manifest_accepts_minimal_cli():
    plugin = plugins.parse_manifest(_fake_manifest())
    assert plugin.id == "fake-agent"
    assert plugin.kind == "cli"
    assert plugin.protocols == ("openai",)
    assert plugin.timeout_seconds == 900
    assert plugin.builtin is False


@pytest.mark.parametrize(
    "overrides, expected",
    [
        ({"id": "Bad ID"}, "id"),
        ({"protocols": ["gemini"]}, "协议"),
        ({"protocols": []}, "protocols"),
        ({"bins": []}, "bins"),
        ({"run_args": ["--model", "{model}"]}, "{prompt}"),
        ({"output": "xml"}, "output"),
        ({"timeout_seconds": 1}, "timeout_seconds"),
        ({"env": {"1BAD": "x"}}, "env"),
        ({"kind": "kb"}, "kb"),
        ({"id": "kb"}, "保留"),
        # 纯文本输出没有可解析的事件：写了 usage_event 也必须拒绝，不能静默忽略。
        ({"usage_event": "turn.completed"}, "usage_event"),
        ({"output": "jsonl", "usage_event": 123}, "usage_event"),
        ({"output": "jsonl", "usage_event": "x" * 65}, "usage_event"),
    ],
)
def test_parse_manifest_rejects_bad_fields(overrides, expected):
    with pytest.raises(plugins.PluginError) as exc:
        plugins.parse_manifest(_fake_manifest(**overrides))
    assert expected in str(exc.value)


def test_parse_manifest_accepts_usage_event_for_jsonl():
    plugin = plugins.parse_manifest(_fake_manifest(
        output="jsonl", text_paths=["item.text"], usage_event="turn.completed"))
    assert plugin.usage_event == "turn.completed"
    assert plugins._plugin_out(plugin, enabled=True)["usage_event"] == "turn.completed"


def test_builtin_cores_declare_usage_events_only_for_jsonl():
    codex = plugins.BUILTIN_BY_ID["codex"]
    assert codex.output == "jsonl"
    assert codex.usage_event == "turn.completed"
    # 纯文本核心拿不到逐次用量，不能假装有。
    for core_id, core in plugins.BUILTIN_BY_ID.items():
        if core.output != "jsonl":
            assert core.usage_event == "", core_id


def test_parse_manifest_rejects_bad_schema():
    with pytest.raises(plugins.PluginError):
        plugins.parse_manifest(_fake_manifest(schema=2))


def test_external_plugin_discovery_ignores_broken_files(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AGENT_PLUGIN_DIR", tmp_path)
    _write_manifest(tmp_path, "ok.json", _fake_manifest())
    (tmp_path / "broken.json").write_text("{ not json", encoding="utf-8")
    found, errors = plugins._load_external()
    assert [p.id for p in found] == ["fake-agent"]
    assert [e["file"] for e in errors] == ["broken.json"]


def test_external_plugin_cannot_shadow_builtin(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AGENT_PLUGIN_DIR", tmp_path)
    _write_manifest(tmp_path, "codex.json", _fake_manifest(id="codex"))
    found, errors = plugins._load_external()
    assert found == []
    assert errors and "重复" in errors[0]["error"]


def test_manifest_with_bom_is_readable(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AGENT_PLUGIN_DIR", tmp_path)
    raw = json.dumps(_fake_manifest(), ensure_ascii=False)
    (tmp_path / "bom.json").write_text("\ufeff" + raw, encoding="utf-8")
    found, errors = plugins._load_external()
    assert not errors
    assert [p.id for p in found] == ["fake-agent"]


def test_detection_reports_version(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AGENT_PLUGIN_DIR", tmp_path)
    _python_on_path(monkeypatch)
    _write_manifest(tmp_path, "fake.json", _fake_manifest())
    plugins.clear_detection_cache()
    found, _ = plugins._load_external()
    result = plugins.detect(found[0])
    assert result["installed"] is True
    assert result["version"] == "fake-agent 9.9"


def test_detection_reports_missing_binary(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AGENT_PLUGIN_DIR", tmp_path)
    _write_manifest(tmp_path, "missing.json",
                    _fake_manifest(id="missing-agent", bins=["definitely-not-a-real-binary-xyz"]))
    found, _ = plugins._load_external()
    result = plugins.detect(found[0])
    assert result["installed"] is False
    assert "未找到" in result["detect_error"]


def test_version_probe_does_not_create_threads(monkeypatch):
    """回归：探测版本不能靠新建线程。

    v1.0.14 桌面版「正在加载 Agent 核心…」的根因：`subprocess.run(capture_output=True)`
    要起读线程收输出，而当时预热线程正卡在 numpy 的 loader lock 上，`Thread.start()`
    永远不返回，`GET /api/agent/cores` 跟着挂死。这里把 `Thread.start` 换成
    「一调用就失败」，探测必须仍能正常返回。
    """
    started: list[object] = []

    def _forbidden(self):            # pragma: no cover - 触发即说明回归
        started.append(self)
        raise AssertionError("版本探测不应创建线程")

    monkeypatch.setattr(threading.Thread, "start", _forbidden)
    assert plugins._run_version(
        sys.executable, ("-c", "print('fake-agent 9.9')")) == "fake-agent 9.9"
    assert started == []


def test_version_probe_times_out_without_hanging(monkeypatch):
    """CLI 卡住时必须按超时返回空串，而不是把请求线程一起拖住。"""
    monkeypatch.setattr(plugins, "_VERSION_TIMEOUT_SECONDS", 0.3)
    t0 = time.monotonic()
    out = plugins._run_version(sys.executable, ("-c", "import time; time.sleep(30)"))
    assert out == ""
    assert time.monotonic() - t0 < 10


def test_normalize_base_url():
    assert plugins.normalize_base_url("openai", "https://api.deepseek.com") == \
        "https://api.deepseek.com/v1"
    assert plugins.normalize_base_url("openai", "https://api.deepseek.com/v1/") == \
        "https://api.deepseek.com/v1"
    assert plugins.normalize_base_url("openai", "https://x.test/v1beta/openai") == \
        "https://x.test/v1beta/openai"
    assert plugins.normalize_base_url("anthropic", "https://api.anthropic.com/v1") == \
        "https://api.anthropic.com"


def test_model_binding_injects_key_and_model(db_session):
    provider = models.Provider(
        name="AgentTestProvider", base_url="https://example.test",
        api_key_enc=security.encrypt_secret("sk-agent"), protocol="openai",
    )
    db_session.add(provider)
    db_session.flush()
    cfg = models.ModelConfig(
        provider_id=provider.id, purpose="llm", model="agent-model", label="Agent Model",
        is_default=True,
    )
    db_session.add(cfg)
    db_session.commit()

    binding = plugins.model_binding(cfg, provider)
    assert binding["base_url"] == "https://example.test/v1"
    assert binding["api_key"] == "sk-agent"
    assert binding["model"] == "agent-model"
    assert binding["model_label"] == "Agent Model · agent-model · AgentTestProvider"


def test_model_label_carries_model_identifier(db_session):
    """同名模型只靠显示名分不清，展示名必须带模型标识；标识与显示名相同时不重复。"""
    provider = models.Provider(name="Magpie", base_url="https://example.test", protocol="openai")
    db_session.add(provider)
    db_session.flush()
    labeled = models.ModelConfig(
        provider_id=provider.id, purpose="llm", model="openai/gpt-6.1-sol", label="GPT-6.1 Sol",
    )
    same_name = models.ModelConfig(
        provider_id=provider.id, purpose="llm", model="kiro/auto", label="kiro/auto",
    )
    unlabeled = models.ModelConfig(
        provider_id=provider.id, purpose="llm", model="codex/gpt-6-luna", label="",
    )
    db_session.add_all([labeled, same_name, unlabeled])
    db_session.commit()

    assert plugins.model_label(labeled, provider) == "GPT-6.1 Sol · openai/gpt-6.1-sol · Magpie"
    assert plugins.model_label(same_name, provider) == "kiro/auto · Magpie"
    assert plugins.model_label(unlabeled, provider) == "codex/gpt-6-luna · Magpie"


def test_model_list_filters_by_core_protocol(db_session):
    provider = models.Provider(
        name="AgentTestProtocols", base_url="https://example.test",
        api_key_enc=security.encrypt_secret("sk"), protocol="anthropic",
    )
    db_session.add(provider)
    db_session.flush()
    db_session.add(models.ModelConfig(
        provider_id=provider.id, purpose="llm", model="claude-test", label="Claude Test",
    ))
    db_session.commit()

    openai_core = plugins.find_plugin("codex")
    anthropic_core = plugins.find_plugin("claude-code")
    assert openai_core is not None and anthropic_core is not None

    openai_rows = plugins.list_models(db_session, openai_core)
    anthropic_rows = plugins.list_models(db_session, anthropic_core)
    claude_openai = next(m for m in openai_rows if m["model"] == "claude-test")
    claude_anthropic = next(m for m in anthropic_rows if m["model"] == "claude-test")
    assert claude_openai["compatible"] is False
    assert claude_anthropic["compatible"] is True


def test_plugin_enable_disable_roundtrip(db_session):
    plugins.set_enabled(db_session, "codex", False)
    db_session.commit()
    data = plugins.list_plugins(db_session)
    codex = next(item for item in data["items"] if item["id"] == "codex")
    assert codex["enabled"] is False
    assert codex["available"] is False
    assert codex["unavailable_reason"] == "已停用"

    plugins.set_enabled(db_session, "codex", True)
    db_session.commit()
    data = plugins.list_plugins(db_session)
    codex = next(item for item in data["items"] if item["id"] == "codex")
    assert codex["enabled"] is True


def test_builtin_kb_core_always_available(db_session):
    data = plugins.list_plugins(db_session)
    kb = next(item for item in data["items"] if item["id"] == "kb")
    assert kb["installed"] is True
    assert kb["available"] is True
    assert kb["kind"] == "kb"


def test_substitute_args_and_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AGENT_PLUGIN_DIR", tmp_path)
    _write_manifest(tmp_path, "fake.json", _fake_manifest())
    found, _ = plugins._load_external()
    plugin = found[0]
    binding = {"model": "m1", "base_url": "https://x/v1", "api_key": "k1"}
    args = plugins.substitute_args(plugin, binding, "C:/work", 7, "你好")
    assert args[0] == "-c"
    assert args[-1] == "你好"
    env = plugins.build_env(plugin, binding, "C:/work", 7)
    assert env["OPENAI_BASE_URL"] == "https://x/v1"
    assert env["OPENAI_MODEL"] == "m1"


# ------------------------------------------------------------------ P1-7 执行边界
# 下列用例逐条对应 docs/HEALTH_CHECK.md §4.2 P1-7 / §5.1 的恶意清单场景，
# 每条都是可复现的「一份恶意 JSON 清单」。
@pytest.mark.parametrize("bins", [
    [sys.executable],                       # 绝对路径直接解析成可执行程序
    ["C:/Windows/System32/cmd.exe"],         # Windows 绝对路径
    ["/usr/bin/env"],                        # POSIX 绝对路径
    ["../evil/agent"],                      # 相对路径 + ..
    [".\\evil\\agent.exe"],                  # Windows 相对路径
    ["cmd"],                                 # 命令解释器（会把 run_args 当命令串）
    ["powershell.exe"],
    ["my agent"],                            # 空格/元字符
    ["agent&calc.exe"],
    ["evil.cmd"],                            # 批处理脚本宿主
    ["payload.bat"],
])
def test_parse_manifest_rejects_unsafe_bins(bins):
    with pytest.raises(plugins.PluginError) as exc:
        plugins.parse_manifest(_fake_manifest(bins=bins))
    assert "bins" in str(exc.value)


def test_parse_manifest_accepts_plain_command_name():
    plugin = plugins.parse_manifest(_fake_manifest(bins=["my-agent", "my_agent"]))
    assert plugin.bins == ("my-agent", "my_agent")


@pytest.mark.parametrize("arg", ["&", "|", "^", "&&", "||", "%COMSPEC%", "%PATH%"])
def test_parse_manifest_rejects_cmd_metacharacter_args(arg):
    """`& | ^ %VAR%` 曾被 cmd.exe /c 二次解析，作为独立参数一律拒绝。"""
    with pytest.raises(plugins.PluginError) as exc:
        plugins.parse_manifest(_fake_manifest(run_args=["run", arg, "{prompt}"]))
    assert "cmd" in str(exc.value) or "元字符" in str(exc.value)


def test_parse_manifest_rejects_api_key_in_run_args():
    with pytest.raises(plugins.PluginError) as exc:
        plugins.parse_manifest(_fake_manifest(
            run_args=["--key", "{api_key}", "{prompt}"]))
    assert "api_key" in str(exc.value)


def test_parse_manifest_allows_api_key_in_env_template():
    plugin = plugins.parse_manifest(_fake_manifest(
        env={"OPENAI_API_KEY": "{api_key}"}))
    assert plugin.env["OPENAI_API_KEY"] == "{api_key}"


@pytest.mark.parametrize("key", [
    "PATH", "PYTHONPATH", "NODE_OPTIONS", "LD_PRELOAD", "WRK_SUPER_SECRET",
])
def test_parse_manifest_rejects_env_keys_outside_allowlist(key):
    with pytest.raises(plugins.PluginError) as exc:
        plugins.parse_manifest(_fake_manifest(env={key: "x"}))
    assert "env" in str(exc.value)


def test_parse_manifest_accepts_allowlisted_env_keys():
    plugin = plugins.parse_manifest(_fake_manifest(env={
        "OPENAI_API_KEY": "{api_key}",
        "ANTHROPIC_AUTH_TOKEN": "{api_key}",
        "PYTHONIOENCODING": "utf-8",
    }))
    assert plugin.env["OPENAI_API_KEY"] == "{api_key}"
    assert plugin.env["ANTHROPIC_AUTH_TOKEN"] == "{api_key}"


@pytest.mark.parametrize("payload", [
    _fake_manifest(bins=["C:/Windows/System32/cmd.exe"]),
    _fake_manifest(bins=["../evil/agent.exe"]),
    _fake_manifest(run_args=["--key", "{api_key}", "{prompt}"]),
    _fake_manifest(env={"PATH": "C:\\evil"}),
    _fake_manifest(env={"PYTHONPATH": "C:\\evil"}),
    _fake_manifest(env={"NODE_OPTIONS": "--require evil.js"}),
    _fake_manifest(bins=["cmd"], run_args=["/c", "whoami", "{prompt}"]),
    _fake_manifest(bins=["evil.cmd"], run_args=["&", "calc.exe", "{prompt}"]),
    _fake_manifest(run_args=["run", "%COMSPEC%", "{prompt}"]),
])
def test_malicious_manifest_file_is_rejected(tmp_path, monkeypatch, payload):
    """恶意清单落盘后必须被 _load_external 拒绝，而不是被当成可运行核心。"""
    monkeypatch.setattr(config, "AGENT_PLUGIN_DIR", tmp_path)
    _write_manifest(tmp_path, "evil.json", payload)
    found, errors = plugins._load_external()
    assert found == []
    assert errors and errors[0]["file"] == "evil.json"


def test_detect_rejects_batch_resolved_binary(monkeypatch, tmp_path):
    """PATH 查到 .cmd/.bat 时拒绝：Windows 会隐式交给 cmd.exe 二次解析参数。"""
    monkeypatch.setattr(config, "AGENT_PLUGIN_DIR", tmp_path)
    plugin = plugins.parse_manifest(_fake_manifest(bins=["fake-cli"]))
    for resolved in ["C:/fake/fake-cli.cmd", "C:/fake/fake-cli.bat"]:
        monkeypatch.setattr(plugins.shutil, "which", lambda name, _r=resolved: _r)
        plugins.clear_detection_cache()
        result = plugins.detect(plugin)
        assert result["installed"] is False, resolved


def test_detect_rejects_shell_resolved_binary(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "AGENT_PLUGIN_DIR", tmp_path)
    plugin = plugins.parse_manifest(_fake_manifest(bins=["fake-cli"]))
    monkeypatch.setattr(plugins.shutil, "which",
                        lambda name: "C:/Windows/System32/cmd.exe")
    plugins.clear_detection_cache()
    assert plugins.detect(plugin)["installed"] is False


def test_detect_accepts_regular_executable(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "AGENT_PLUGIN_DIR", tmp_path)
    _python_on_path(monkeypatch)
    plugin = plugins.parse_manifest(_fake_manifest(bins=["python"]))
    plugins.clear_detection_cache()
    result = plugins.detect(plugin)
    assert result["installed"] is True
    assert result["version"] == "fake-agent 9.9"


def test_version_probe_no_cmd_exe_wrapper(monkeypatch):
    """回归：探测命令不再拼 `cmd.exe /c`（参数会被二次解析而注入）。"""
    captured: dict = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        raise OSError("stop")

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    assert plugins._run_version(sys.executable, ("--version",)) == ""
    assert captured["cmd"][0] == sys.executable
    assert not any("cmd.exe" in str(part).lower() for part in captured["cmd"])


def test_version_probe_does_not_inherit_backend_env(monkeypatch):
    """版本探测子进程必须带干净 env，不能继承后端全量环境里的密钥。"""
    monkeypatch.setenv("WRK_AGENT_PROBE_SECRET", "super-secret-value")
    out = plugins._run_version(
        sys.executable,
        ("-c", "import os; print(os.environ.get('WRK_AGENT_PROBE_SECRET', ''))"),
    )
    assert "super-secret-value" not in out
    assert out == ""


def test_substitute_args_never_exposes_api_key(tmp_path, monkeypatch):
    """即使绕过 parse_manifest 手工构造插件，api_key 也不能进命令行。"""
    monkeypatch.setattr(config, "AGENT_PLUGIN_DIR", tmp_path)
    _write_manifest(tmp_path, "fake.json", _fake_manifest())
    found, _ = plugins._load_external()
    plugin = plugins.clone_with(found[0], run_args=("--key", "{api_key}", "{prompt}"))
    binding = {"model": "m1", "base_url": "https://x/v1", "api_key": "sk-boundary-test"}
    args = plugins.substitute_args(plugin, binding, "C:/work", 7, "你好")
    assert "sk-boundary-test" not in args
    assert "{api_key}" not in args
    assert args[-1] == "你好"


def test_build_env_cannot_override_system_path(tmp_path, monkeypatch):
    """env 白名单之后不能再被清单覆盖 PATH / PYTHONPATH / NODE_OPTIONS。"""
    monkeypatch.setattr(config, "AGENT_PLUGIN_DIR", tmp_path)
    _write_manifest(tmp_path, "fake.json", _fake_manifest())
    found, _ = plugins._load_external()
    plugin = plugins.clone_with(found[0], env={
        "PATH": "C:\\evil", "PYTHONPATH": "C:\\evil",
        "NODE_OPTIONS": "--require evil.js", "OPENAI_MODEL": "{model}",
    })
    binding = {"model": "m1", "base_url": "https://x/v1", "api_key": "sk-x"}
    env = plugins.build_env(plugin, binding, "C:/work", 7)
    assert env["PATH"] == os.environ["PATH"]
    assert "PYTHONPATH" not in env
    assert "NODE_OPTIONS" not in env
    assert env["OPENAI_MODEL"] == "m1"


def test_redact_text_scrubs_secrets():
    assert plugins.redact_text(
        "key=sk-boundary-test", secrets=("sk-boundary-test",)) == "key=<密钥>"


def test_secret_values_ignores_local_sentinel():
    assert plugins.secret_values({"api_key": "local"}) == ()
    assert plugins.secret_values({"api_key": "sk-real"}) == ("sk-real",)


def test_redacted_command_scrubs_key_and_prompt():
    command = ["agent", "--key", "sk-boundary-test", "--prompt", "很长很长的提示词"]
    out = plugins.redacted_command(command, "很长很长的提示词", ("sk-boundary-test",))
    assert "sk-boundary-test" not in out
    assert "<密钥>" in out
    assert "很长很长的提示词" not in out
    assert "<提示词>" in out


def test_resolve_workspace_stays_inside_root(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setattr(config, "AGENT_WORKSPACE_DIR", root)
    inside = root / "project"
    inside.mkdir()
    assert plugins.resolve_workspace(str(inside)) == str(inside.resolve())
    assert plugins.resolve_workspace(None) == str(root.resolve())
    assert plugins.resolve_workspace("   ") == str(root.resolve())


def test_resolve_workspace_rejects_escape(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setattr(config, "AGENT_WORKSPACE_DIR", root)
    sibling = tmp_path / "workspace_evil"       # 同前缀目录，专门测前缀绕过
    sibling.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    for bad in [str(outside), str(tmp_path), str(root / ".." / "outside")]:
        with pytest.raises(plugins.PluginError):
            plugins.resolve_workspace(bad)
    with pytest.raises(plugins.PluginError) as exc:
        plugins.resolve_workspace(str(sibling))
    assert "工作区" in str(exc.value)
