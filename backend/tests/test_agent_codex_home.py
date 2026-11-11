"""Codex 核心的隔离配置目录：只用应用自己的 CODEX_HOME，技能只读复用。

这些用例**不依赖真的装没装 Codex**：它们只考察「应用准备出来的目录和进程环境
对不对」，以及「用户 ~/.codex 里的东西有没有被读/被改」。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from app import config
from app.services.agent import codex_home, plugins

SENTINEL_PERSONA = "主，已收到你的身份设定与开发规范，以后每次回复加「御主」"


def _write_skill(root: Path, name: str, description: str = "测试技能") -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n",
        encoding="utf-8")
    return skill_dir


@pytest.fixture()
def codex_env(tmp_path, monkeypatch):
    """把「用户 Codex 目录」和「应用数据目录」都搬到 tmp 下。

    用户目录里刻意放上真实用户都会有的东西：AGENTS.md 人格、config.toml、hooks、
    memories、登录态、历史会话——应用都不许碰。
    """
    user_home = tmp_path / "user-codex"
    (user_home / "skills").mkdir(parents=True)
    (user_home / "AGENTS.md").write_text(SENTINEL_PERSONA, encoding="utf-8")
    (user_home / "config.toml").write_text(
        "model_provider = 'user-custom'\n", encoding="utf-8")
    (user_home / "hooks.json").write_text('{"hooks": []}', encoding="utf-8")
    (user_home / "memories").mkdir()
    (user_home / "memories" / "note.md").write_text("私事", encoding="utf-8")
    (user_home / "auth.json").write_text('{"token": "secret"}', encoding="utf-8")
    (user_home / "sessions").mkdir()
    (user_home / "sessions" / "old.jsonl").write_text("{}", encoding="utf-8")

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(codex_home, "user_codex_home", lambda: user_home)
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    return {"user_home": user_home, "data_dir": data_dir,
            "app_home": data_dir / codex_home.HOME_DIR_NAME}


def _tree_digest(root: Path) -> str:
    """整棵树的指纹：文件名 + 大小 + mtime，用来证明「一个字都没动」。"""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        stat = path.stat()
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(str(stat.st_size).encode())
        digest.update(str(int(stat.st_mtime)).encode())
    return digest.hexdigest()


def test_app_home_is_app_owned_and_never_reads_user_config(codex_env):
    """隔离目录里只有应用写的 config.toml；用户的人格/配置一个字节都不进来。"""
    _write_skill(codex_env["user_home"] / "skills", "wf-verify")
    status = codex_home.prepare()

    app_home = Path(status["home"])
    assert app_home == codex_env["app_home"]
    assert app_home.is_dir()

    config_text = (app_home / codex_home.CONFIG_NAME).read_text(encoding="utf-8")
    assert "CODEX_HOME" in config_text
    assert SENTINEL_PERSONA not in config_text
    assert "御主" not in config_text
    assert "user-custom" not in config_text            # 用户的 model_provider 没被带进来

    everything = "\n".join(
        p.read_text(encoding="utf-8", errors="replace")
        for p in app_home.rglob("*") if p.is_file() and p.name != "SKILL.md")
    assert SENTINEL_PERSONA not in everything
    assert "御主" not in everything
    assert "secret" not in everything                  # 登录态也没被复制


def test_user_codex_home_is_untouched(codex_env):
    """准备隔离目录前后，用户的 ~/.codex 必须完全不变（不改写）。"""
    _write_skill(codex_env["user_home"] / "skills", "wf-verify")
    before = _tree_digest(codex_env["user_home"])
    codex_home.prepare()
    codex_home.prepare()
    assert _tree_digest(codex_env["user_home"]) == before


def test_user_skills_are_exposed_read_only(codex_env):
    """用户技能以链接方式挂进隔离目录：指向原目录，内容不复制。"""
    skills = codex_env["user_home"] / "skills"
    source = _write_skill(skills, "wf-verify", "校验用技能")
    _write_skill(skills, "ppt-master", "做 PPT 的技能")

    status = codex_home.prepare()
    mirror = Path(status["home"]) / codex_home.SKILLS_DIR_NAME

    assert status["skills"]["linked"] == 2
    for name in ("wf-verify", "ppt-master"):
        entry = mirror / name
        assert entry.is_dir()
        # 读到的就是用户那份（链接），而且应用没有把内容复制进数据目录。
        assert (entry / "SKILL.md").is_file()
        target = codex_home._link_target(entry)
        assert target is not None
        assert Path(target).resolve() == (skills / name).resolve()
    assert codex_home._link_target(mirror / "wf-verify") == str(source)


def test_skills_sync_is_idempotent(codex_env):
    _write_skill(codex_env["user_home"] / "skills", "wf-verify")
    first = codex_home.prepare()["skills"]
    second = codex_home.prepare()["skills"]
    assert first["linked"] == 1
    assert second["linked"] == 0 and second["copied"] == 0
    assert second["removed"] == 0


def test_skills_removed_from_source_are_removed_from_mirror(codex_env):
    skills = codex_env["user_home"] / "skills"
    _write_skill(skills, "wf-verify")
    kept = _write_skill(skills, "ppt-master")
    codex_home.prepare()

    import shutil
    shutil.rmtree(skills / "wf-verify")
    status = codex_home.prepare()["skills"]

    mirror = codex_env["app_home"] / codex_home.SKILLS_DIR_NAME
    assert not os.path.lexists(mirror / "wf-verify")
    assert (mirror / "ppt-master").is_dir()
    assert status["removed"] == 1
    # 源目录里那份当然还在
    assert (kept / "SKILL.md").is_file()


def test_hidden_dirs_and_non_skills_are_skipped(codex_env):
    skills = codex_env["user_home"] / "skills"
    _write_skill(skills, "ok-skill")
    (skills / "no-skill-md").mkdir()
    (skills / ".system").mkdir()
    _write_skill(skills / ".system", "imagegen")

    status = codex_home.prepare()["skills"]
    mirror = codex_env["app_home"] / codex_home.SKILLS_DIR_NAME
    assert status["linked"] == 1
    assert (mirror / "ok-skill").is_dir()
    assert not os.path.lexists(mirror / "no-skill-md")
    assert not os.path.lexists(mirror / ".system")


def test_missing_user_skills_dir_is_not_an_error(codex_env):
    import shutil
    shutil.rmtree(codex_env["user_home"] / "skills")
    status = codex_home.prepare()
    assert status["skills"]["linked"] == 0
    assert Path(status["home"]).is_dir()


def test_falls_back_to_copy_when_linking_is_unavailable(codex_env, monkeypatch):
    """非 NTFS / 无权限时退回复制：仍然只读，且不会写到用户目录里。"""
    source = _write_skill(codex_env["user_home"] / "skills", "wf-verify")

    def boom(*args, **kwargs):
        raise OSError("该文件系统不支持链接")

    monkeypatch.setattr(codex_home, "_make_junction", boom)
    monkeypatch.setattr(codex_home.os, "symlink", boom)

    status = codex_home.prepare()["skills"]
    mirror = codex_env["app_home"] / codex_home.SKILLS_DIR_NAME / "wf-verify"
    assert status["copied"] == 1
    assert status["linked"] == 0
    assert (mirror / "SKILL.md").read_text(encoding="utf-8").startswith("---")
    assert codex_home._link_target(mirror) is None          # 真的是副本
    assert (source / "SKILL.md").is_file()                  # 源目录没被动

    # 第二遍：内容和源一致，不该重复复制
    again = codex_home.prepare()["skills"]
    assert again["copied"] == 0


def test_build_env_injects_codex_home_only_for_codex(codex_env):
    """Codex 核心的进程环境里必定有 CODEX_HOME，其它核心不能有。"""
    _write_skill(codex_env["user_home"] / "skills", "wf-verify")
    binding = {"model": "gpt-x", "base_url": "https://example.test/v1",
               "api_key": "sk-test", "protocol": "openai"}

    codex = plugins.BUILTIN_BY_ID["codex"]
    env = plugins.build_env(codex, binding, str(config.AGENT_WORKSPACE_DIR), 1)
    assert env["CODEX_HOME"] == str(codex_env["app_home"])
    assert env["OPENAI_API_KEY"] == "sk-test"
    # 模型注入能力不能因为隔离而丢：base_url 同时出现在命令行与 env 里。
    args = plugins.substitute_args(codex, binding, str(config.AGENT_WORKSPACE_DIR), 1, "你好")
    assert "openai_base_url=https://example.test/v1" in args
    assert "gpt-x" in args

    claude = plugins.BUILTIN_BY_ID["claude-code"]
    other = plugins.build_env(
        claude, {**binding, "protocol": "anthropic"}, str(config.AGENT_WORKSPACE_DIR), 1)
    assert "CODEX_HOME" not in other


def test_codex_home_env_wins_over_process_value(codex_env, monkeypatch):
    """调用进程里恰好有 CODEX_HOME 时，隔离目录也必须压过它。"""
    monkeypatch.setenv("CODEX_HOME", str(codex_env["user_home"]))
    binding = {"model": "m", "base_url": "https://x/v1", "api_key": "k", "protocol": "openai"}
    env = plugins.build_env(plugins.BUILTIN_BY_ID["codex"], binding,
                            str(config.AGENT_WORKSPACE_DIR), 1)
    assert env["CODEX_HOME"] == str(codex_env["app_home"])


def test_probe_env_points_at_app_home(codex_env):
    env = plugins._probe_env(plugins.BUILTIN_BY_ID["codex"])
    assert env["CODEX_HOME"] == str(codex_env["app_home"])


def test_unusable_home_raises_readable_error(codex_env, monkeypatch):
    """目录建不出来时直接报错，绝不悄悄退回用户 ~/.codex。"""
    blocked = codex_env["data_dir"] / codex_home.HOME_DIR_NAME
    blocked.write_text("我是文件不是目录", encoding="utf-8")
    with pytest.raises(codex_home.CodexHomeError) as exc:
        codex_home.prepare()
    assert "隔离配置目录" in str(exc.value)


def test_plugin_error_surfaces_from_build_env(codex_env, monkeypatch):
    blocked = codex_env["data_dir"] / codex_home.HOME_DIR_NAME
    blocked.write_text("我是文件不是目录", encoding="utf-8")
    binding = {"model": "m", "base_url": "https://x/v1", "api_key": "k", "protocol": "openai"}
    with pytest.raises(plugins.PluginError):
        plugins.build_env(plugins.BUILTIN_BY_ID["codex"], binding,
                          str(config.AGENT_WORKSPACE_DIR), 1)


def test_config_toml_is_rewritten_when_tampered(codex_env):
    """config.toml 被手工改过就恢复成应用那份（不承载任何用户配置）。"""
    codex_home.ensure_dir()
    path = codex_env["app_home"] / codex_home.CONFIG_NAME
    path.write_text("# 用户手改\nmodel = 'o3'\n", encoding="utf-8")
    codex_home.ensure_dir()
    assert path.read_text(encoding="utf-8") == codex_home.CONFIG_TEXT


def test_skills_index_is_readable_json(codex_env):
    _write_skill(codex_env["user_home"] / "skills", "wf-verify")
    codex_home.prepare()
    index = json.loads(
        (codex_env["app_home"] / codex_home.SKILLS_DIR_NAME /
         codex_home.SKILLS_INDEX_NAME).read_text(encoding="utf-8"))
    assert "wf-verify" in index["skills"]
    assert index["skills"]["wf-verify"]["kind"] in ("junction", "symlink", "copy")
