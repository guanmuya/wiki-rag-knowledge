"""Agent 插件注册表：清单校验、发现、探测、模型协议过滤。"""
from __future__ import annotations

import json
import sys

import pytest

from app import config, models, security
from app.services.agent import plugins


def _write_manifest(directory, name: str, payload: dict) -> None:
    (directory / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _fake_manifest(**overrides) -> dict:
    payload = {
        "schema": 1,
        "id": "fake-agent",
        "name": "Fake Agent",
        "description": "测试用",
        "kind": "cli",
        "protocols": ["openai"],
        "bins": [sys.executable],
        "version_args": ["-c", "print('fake-agent 9.9')"],
        "run_args": ["-c", "print('ok')", "{prompt}"],
        "env": {"TEST_BASE": "{base_url}", "TEST_MODEL": "{model}"},
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
    ],
)
def test_parse_manifest_rejects_bad_fields(overrides, expected):
    with pytest.raises(plugins.PluginError) as exc:
        plugins.parse_manifest(_fake_manifest(**overrides))
    assert expected in str(exc.value)


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
    assert binding["model_label"] == "Agent Model · AgentTestProvider"


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
    assert env["TEST_BASE"] == "https://x/v1"
    assert env["TEST_MODEL"] == "m1"
