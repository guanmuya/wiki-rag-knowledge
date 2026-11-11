"""Agent 核心插件注册表。

设计目标（对齐 magpie 的「Agents 页」思路，但按本仓库的技术栈实现）：

1. **内置核心开箱即用**：知识库助手直接用现有 RAG 链路；Claude Code、Codex、
   Qwen Code、Aider、Goose 等 CLI Agent 只要本机装了对应命令，就能在界面上
   选到，并把「已配置的模型」通过各家自己的环境变量注入进去。
2. **插件只描述、不执行代码**：第三方核心是一个 JSON 清单（可执行文件名、
   参数模板、环境变量模板、输出格式）。管理员把它放进数据目录的
   `agent_plugins/`，应用负责发现、校验、探测安装状态。这样既能接入任意
   CLI Agent，又不需要在应用里加载第三方 Python/JS 代码。
3. **模型不写死**：核心声明自己说哪套协议（openai / openai-responses /
   anthropic），界面只列协议兼容的模型；具体 base_url、api_key、model 由
   运行时按供应商配置注入，不落盘到插件清单里。

清单格式与示例见 `docs/AGENT_PLUGINS.md`。

P1-7 执行边界：清单是敌意输入。`bins` 只接受 PATH 中可查找的命令名并校验解析
结果；`env` 只能覆盖白名单键；`{api_key}` 不得进入命令行；命令行摘要、运行日志、
正文统一脱敏；会话 `workspace` 必须落在 `AGENT_WORKSPACE_DIR` 内。
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import config, models, security

log = logging.getLogger("wrk.agent.plugins")

SCHEMA_VERSION = 1
SUPPORTED_PROTOCOLS = ("openai", "openai-responses", "anthropic")
SUPPORTED_OUTPUTS = ("text", "jsonl")
PROTOCOL_LABELS = {
    "openai": "OpenAI 兼容",
    "openai-responses": "OpenAI Responses",
    "anthropic": "Anthropic Messages",
}
DISABLED_SETTING_KEY = "agent_disabled_plugins"
_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_VERSION_SUFFIX_RE = re.compile(r"/v\d+(?:beta/openai)?$", re.I)
_MAX_MANIFEST_BYTES = 64 * 1024

# ------------------------------------------------------------------ P1-7 边界常量
# `bins` 只取 PATH 中的命令名：不接受绝对路径 / 相对路径 / `..` / 空格 / shell 元字符。
_BIN_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# 命令解释器（即便本身是 .exe）会把 run_args 当成命令串再次解析，必须拒绝。
_FORBIDDEN_BIN_NAMES = frozenset({
    "cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe",
    "wscript", "wscript.exe", "cscript", "cscript.exe",
    "mshta", "mshta.exe", "rundll32", "rundll32.exe", "regsvr32", "regsvr32.exe",
    "bash", "sh", "zsh", "fish", "wsl", "wsl.exe",
})
_FORBIDDEN_SHELL_STEMS = frozenset(
    name.rsplit(".", 1)[0] for name in _FORBIDDEN_BIN_NAMES
)
# Windows 会把这些脚本宿主隐式交给 cmd.exe / WSH，参数被二次解析（`& | ^ %VAR%`）。
_CMD_META_ONLY_RE = re.compile(r"^[&|^<>!()]+$")
_CMD_VAR_REF_RE = re.compile(r"^%[A-Za-z_][A-Za-z0-9_]*%$")
_REJECTED_BIN_SUFFIXES = frozenset({
    ".cmd", ".bat", ".ps1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".hta",
})
# `build_env` 从系统继承的最小变量集；清单不得覆盖其中的系统路径类变量。
_ENV_KEEP = (
    "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP",
    "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA",
    "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "LANG", "LC_ALL",
    "TERM", "NO_COLOR", "PYTHONIOENCODING",
)
# 清单 `env` 允许设置的键（默认拒绝）。刻意排除 PATH / PYTHONPATH / NODE_OPTIONS /
# LD_PRELOAD 等能改变「加载哪段代码」的变量；新增第三方 CLI 需要的键须在此登记。
ALLOWED_ENV_KEYS = frozenset({
    # OpenAI 兼容
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_BASE", "OPENAI_MODEL",
    "OPENAI_ORG_ID", "OPENAI_ORGANIZATION", "OPENAI_PROJECT_ID",
    # Anthropic
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL",
    # Goose
    "GOOSE_PROVIDER", "GOOSE_MODEL",
    # 通用显示 / 编码（不改变代码加载）
    "NO_COLOR", "FORCE_COLOR", "TERM", "LANG", "LC_ALL",
    "PYTHONIOENCODING", "PYTHONUTF8",
})


class PluginError(ValueError):
    """清单不合法。消息直接给管理员看，所以写清楚是哪一项。"""


@dataclass(frozen=True)
class AgentPlugin:
    id: str
    name: str
    description: str = ""
    kind: str = "cli"                       # kb（内置）| cli（外部命令）
    source: str = "plugin"                  # builtin | plugin
    homepage: str = ""
    protocols: tuple[str, ...] = ()
    bins: tuple[str, ...] = ()
    version_args: tuple[str, ...] = ("--version",)
    run_args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    output: str = "text"                    # text | jsonl
    text_paths: tuple[str, ...] = ()
    # 只有 output="jsonl" 的核心才可能有「用量事件」：在逐行 JSON 里，哪一类
    # 事件的 usage 字段是本次执行的 token 用量（例如 Codex 的 turn.completed）。
    # 纯文本输出拿不到逐次用量，这里留空，运行时按「未计量」记录。
    usage_event: str = ""
    timeout_seconds: int = 900
    requires_workspace: bool = True
    builtin: bool = False
    manifest_path: str = ""

    @property
    def is_kb(self) -> bool:
        return self.kind == "kb"


# ------------------------------------------------------------------ 内置核心
def _builtin(id: str, name: str, description: str, *, kind: str = "cli",
             protocols: tuple[str, ...] = (), bins: tuple[str, ...] = (),
             run_args: tuple[str, ...] = (), env: dict[str, str] | None = None,
             output: str = "text", text_paths: tuple[str, ...] = (),
             usage_event: str = "", homepage: str = "") -> AgentPlugin:
    return AgentPlugin(
        id=id, name=name, description=description, kind=kind, source="builtin",
        homepage=homepage, protocols=protocols, bins=bins, run_args=run_args,
        env=env or {}, output=output, text_paths=text_paths,
        usage_event=usage_event, builtin=True,
    )


BUILTIN_PLUGINS: tuple[AgentPlugin, ...] = (
    _builtin(
        "kb", "知识库助手",
        "内置核心：检索本应用的知识库并带引用回答；没有配置模型时也能用抽取式回答。",
        kind="kb", homepage="",
    ),
    _builtin(
        "claude-code", "Claude Code",
        "Anthropic 官方编码 Agent。使用已配置的 Anthropic Messages 协议模型。",
        protocols=("anthropic",),
        bins=("claude",),
        run_args=("-p", "{prompt}", "--output-format", "text", "--model", "{model}"),
        env={
            "ANTHROPIC_BASE_URL": "{base_url}",
            "ANTHROPIC_AUTH_TOKEN": "{api_key}",
            "ANTHROPIC_MODEL": "{model}",
        },
        homepage="https://docs.anthropic.com/en/docs/claude-code",
    ),
    _builtin(
        "codex", "Codex CLI",
        "OpenAI 官方编码 Agent。使用已配置的 OpenAI 兼容 / Responses 模型。",
        protocols=("openai", "openai-responses"),
        bins=("codex",),
        # Codex 的 provider 来自 config.toml；只用环境变量在用户自定义过
        # model_provider 时不生效。显式把内置 openai provider 指到本会话的
        # base_url，再用 OPENAI_API_KEY 提供密钥。
        run_args=("-c", "model_provider=openai", "-c",
                  "openai_base_url={base_url}", "exec", "--json",
                  "--skip-git-repo-check", "-m", "{model}", "{prompt}"),
        env={"OPENAI_BASE_URL": "{base_url}", "OPENAI_API_KEY": "{api_key}"},
        output="jsonl",
        text_paths=("item.text", "msg.message", "message", "text"),
        # `codex exec --json` 在每轮结束时打一条 turn.completed，里面带
        # usage（input_tokens / output_tokens 等）。字段名来自 codex 0.167.0
        # 实际输出，见 docs/AGENT_PLUGINS.md §3.4。
        usage_event="turn.completed",
        homepage="https://github.com/openai/codex",
    ),
    _builtin(
        "qwen-code", "Qwen Code",
        "通义千问开源 CLI（Gemini CLI 分支）。使用已配置的 OpenAI 兼容模型。",
        protocols=("openai",),
        bins=("qwen",),
        run_args=("-p", "{prompt}", "--model", "{model}"),
        env={
            "OPENAI_BASE_URL": "{base_url}",
            "OPENAI_API_KEY": "{api_key}",
            "OPENAI_MODEL": "{model}",
        },
        homepage="https://github.com/QwenLM/qwen-code",
    ),
    _builtin(
        "aider", "Aider",
        "开源结对编程 Agent。使用已配置的 OpenAI 兼容模型。",
        protocols=("openai",),
        bins=("aider",),
        run_args=("--message", "{prompt}", "--model", "openai/{model}",
                  "--no-git", "--yes", "--no-auto-commits"),
        env={"OPENAI_API_BASE": "{base_url}", "OPENAI_API_KEY": "{api_key}"},
        homepage="https://github.com/Aider-AI/aider",
    ),
    _builtin(
        "goose", "Goose",
        "Block 开源的本地 Agent。使用已配置的 OpenAI 兼容模型。",
        protocols=("openai",),
        bins=("goose",),
        run_args=("run", "-t", "{prompt}"),
        env={
            "GOOSE_PROVIDER": "openai",
            "GOOSE_MODEL": "{model}",
            "OPENAI_BASE_URL": "{base_url}",
            "OPENAI_API_KEY": "{api_key}",
        },
        homepage="https://github.com/block/goose",
    ),
)

BUILTIN_BY_ID = {p.id: p for p in BUILTIN_PLUGINS}


# ------------------------------------------------------------------ 清单解析
def _text(value: Any, field_name: str, *, max_len: int = 2000, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise PluginError(f"{field_name} 必须是字符串")
    value = value.strip()
    if required and not value:
        raise PluginError(f"缺少 {field_name}")
    if len(value) > max_len:
        raise PluginError(f"{field_name} 过长（上限 {max_len} 字符）")
    return value


def _str_tuple(value: Any, field_name: str, *, limit: int = 64) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise PluginError(f"{field_name} 必须是字符串数组")
    items = tuple(v.strip() for v in value if v.strip())
    if len(items) > limit:
        raise PluginError(f"{field_name} 最多 {limit} 项")
    return items


def parse_manifest(data: Any, *, source: str = "plugin", manifest_path: str = "") -> AgentPlugin:
    """校验并规范化一份插件清单。任何不确定的字段都拒绝，不做静默猜测。"""
    if not isinstance(data, dict):
        raise PluginError("清单必须是一个 JSON 对象")

    schema = data.get("schema", SCHEMA_VERSION)
    if schema != SCHEMA_VERSION:
        raise PluginError(f"不支持的 schema：{schema}（当前为 {SCHEMA_VERSION}）")

    plugin_id = _text(data.get("id"), "id", max_len=64, required=True)
    if not _ID_RE.match(plugin_id):
        raise PluginError("id 只能包含小写字母、数字、点、下划线或短横线，且以字母开头")

    name = _text(data.get("name"), "name", max_len=80, required=True)
    description = _text(data.get("description"), "description", max_len=1000)
    homepage = _text(data.get("homepage"), "homepage", max_len=500)

    kind = _text(data.get("kind"), "kind", max_len=16, required=True)
    if kind not in ("cli", "kb"):
        raise PluginError("kind 只能是 cli 或 kb")
    if kind == "kb" and source != "builtin":
        raise PluginError("外部插件不能声明 kb 核心（知识库助手是内置实现）")
    if plugin_id == "kb" and source != "builtin":
        raise PluginError("id `kb` 是内置核心保留标识")

    protocols = _str_tuple(data.get("protocols"), "protocols")
    if kind == "cli":
        if not protocols:
            raise PluginError("cli 核心必须声明 protocols（openai / openai-responses / anthropic）")
        unknown = [p for p in protocols if p not in SUPPORTED_PROTOCOLS]
        if unknown:
            raise PluginError(f"不支持的协议：{', '.join(unknown)}")
        protocols = tuple(dict.fromkeys(protocols))

    bins = _str_tuple(data.get("bins"), "bins")
    if kind == "cli" and not bins:
        raise PluginError("cli 核心必须声明 bins（可执行文件名，按顺序探测）")
    if kind == "cli":
        for candidate in bins:
            if not _BIN_NAME_RE.fullmatch(candidate) or ".." in candidate:
                raise PluginError(
                    f"bins 只能写 PATH 中可查找的命令名，不能是路径或含非法字符：{candidate!r}")
            if Path(candidate).suffix.lower() in _REJECTED_BIN_SUFFIXES:
                raise PluginError(f"bins 不能指向批处理/脚本宿主：{candidate!r}")
            if candidate.lower() in _FORBIDDEN_BIN_NAMES:
                raise PluginError(f"bins 不能指向命令解释器或系统外壳：{candidate!r}")

    run_args = _str_tuple(data.get("run_args"), "run_args")
    if kind == "cli" and not run_args:
        raise PluginError("cli 核心必须声明 run_args（非交互执行参数）")
    if "{prompt}" not in " ".join(run_args) and kind == "cli":
        raise PluginError("run_args 必须包含 {prompt} 占位符")
    if any("{api_key}" in arg for arg in run_args):
        raise PluginError(
            "run_args 不能包含 {api_key}：密钥只能放进 env 模板，"
            "避免出现在进程命令行与事件日志中")
    for arg in run_args:
        if _CMD_META_ONLY_RE.fullmatch(arg) or _CMD_VAR_REF_RE.fullmatch(arg):
            raise PluginError(
                f"run_args 不能包含 cmd 元字符/变量引用参数：{arg!r}"
                "（旧实现经 cmd.exe /c 二次解析时可借它注入命令）")

    version_args = _str_tuple(data.get("version_args"), "version_args") or ("--version",)

    raw_env = data.get("env") or {}
    if not isinstance(raw_env, dict):
        raise PluginError("env 必须是对象")
    if len(raw_env) > 32:
        raise PluginError("env 最多 32 项")
    env: dict[str, str] = {}
    for key, value in raw_env.items():
        if not isinstance(key, str) or not _ENV_KEY_RE.match(key):
            raise PluginError(f"env 变量名不合法：{key!r}")
        if key not in ALLOWED_ENV_KEYS:
            raise PluginError(
                f"env.{key} 不在允许的白名单键内；"
                "不允许覆盖 PATH / PYTHONPATH / NODE_OPTIONS 等系统或代码加载变量")
        if not isinstance(value, str):
            raise PluginError(f"env.{key} 必须是字符串")
        if len(value) > 1000:
            raise PluginError(f"env.{key} 过长")
        env[key] = value

    output = _text(data.get("output"), "output", max_len=16) or "text"
    if output not in SUPPORTED_OUTPUTS:
        raise PluginError("output 只能是 text 或 jsonl")
    text_paths = _str_tuple(data.get("text_paths"), "text_paths", limit=16)
    usage_event = _text(data.get("usage_event"), "usage_event", max_len=64)
    if usage_event and output != "jsonl":
        # 纯文本输出里没有可解析的事件，写了也不会生效——宁可拒绝，不做静默忽略。
        raise PluginError("usage_event 只在 output=jsonl 时可用（纯文本输出没有可解析的事件）")

    timeout = data.get("timeout_seconds", 900)
    if not isinstance(timeout, int) or not (10 <= timeout <= 3600):
        raise PluginError("timeout_seconds 必须是 10..3600 的整数")

    requires_workspace = data.get("requires_workspace", True)
    if not isinstance(requires_workspace, bool):
        raise PluginError("requires_workspace 必须是布尔值")

    return AgentPlugin(
        id=plugin_id, name=name, description=description, kind=kind,
        source=source, homepage=homepage, protocols=protocols, bins=bins,
        version_args=version_args, run_args=run_args, env=env, output=output,
        text_paths=text_paths, usage_event=usage_event, timeout_seconds=timeout,
        requires_workspace=requires_workspace, builtin=(source == "builtin"),
        manifest_path=manifest_path,
    )


def _load_external() -> tuple[list[AgentPlugin], list[dict]]:
    plugins: list[AgentPlugin] = []
    errors: list[dict] = []
    directory = Path(config.AGENT_PLUGIN_DIR)
    if not directory.is_dir():
        return plugins, errors
    seen: set[str] = set(BUILTIN_BY_ID)
    for path in sorted(directory.glob("*.json")):
        try:
            if path.stat().st_size > _MAX_MANIFEST_BYTES:
                raise PluginError(f"清单超过 {_MAX_MANIFEST_BYTES // 1024} KB")
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            plugin = parse_manifest(data, source="plugin", manifest_path=str(path))
            if plugin.id in seen:
                raise PluginError(f"核心 id 与内置核心或另一个插件重复：{plugin.id}")
            seen.add(plugin.id)
            plugins.append(plugin)
        except (PluginError, ValueError, OSError) as exc:
            errors.append({"file": path.name, "error": str(exc)})
            log.warning("忽略 Agent 插件 %s：%s", path.name, exc)
    return plugins, errors


# ------------------------------------------------------------------ 探测安装
_detect_lock = threading.Lock()
_detect_cache: dict[str, tuple[float, dict]] = {}
_DETECT_TTL_SECONDS = 30


def clear_detection_cache() -> None:
    with _detect_lock:
        _detect_cache.clear()


def _minimal_env() -> dict[str, str]:
    """子进程的最小环境变量集：不继承后端进程里的密钥类变量。

    `build_env` 与版本探测共用同一份基线，避免探测子进程偷偷继承全量环境
    （§5.1 记录过 `plugins.py` 版本探测未传 `env=` 的问题）。
    """
    env = {k: os.environ[k] for k in _ENV_KEEP if k in os.environ}
    # Python 写的 CLI（Aider 等）在 Windows 默认用 cp936 输出；统一成 UTF-8。
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def _command(path: str, args: tuple[str, ...] | list[str]) -> list[str]:
    """构造探测命令，绝不显式拼 `cmd.exe /c`。

    旧实现为 `.cmd/.bat` 包一层 `cmd.exe /c`：参数会被 cmd 二次解析，
    `& | ^ %VAR%` 可注入命令。外部清单的 `.cmd/.bat` 等脚本宿主现在由
    `_resolve_bin` 在解析阶段拒绝；这里直接把 `[path, *args]` 交给
    `Popen(shell=False)`，由 Python 自己做参数转义，不再有 shell 二次解析。
    """
    return [path, *args]


# 版本探测的硬超时（秒）。探测只是给界面显示一行版本号，不值得等太久。
_VERSION_TIMEOUT_SECONDS = 6


def _kill_tree(proc: subprocess.Popen) -> None:
    """结束探测进程及其子进程。

    Windows 上 `cmd.exe /c claude.cmd` 会再拉起 node，只 kill cmd.exe 会把
    node 留在后台；所以优先用 taskkill /T 杀整棵树。
    """
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass


def _run_version(path: str, args: tuple[str, ...]) -> str:
    """探测 CLI 的版本行；失败/超时返回空串。

    为什么不用 `subprocess.run(capture_output=True)`（v1.0.14 事故）：
    Windows 上它靠两个读线程收 stdout/stderr，而 `Thread.start()` 要等
    Windows 的 loader lock。打包版启动预热线程正在加载原生模块（numpy /
    OpenBLAS）时，loader lock 被占住，这个探测线程就会**永久**卡在
    `Thread.start()`，`GET /api/agent/cores` 永不返回，界面一直停在
    「正在加载 Agent 核心…」。这里把输出重定向到临时文件，只用
    `wait(timeout)` 等进程退出，全程不创建任何线程。
    """
    with tempfile.TemporaryFile() as sink:
        kwargs: dict[str, Any] = dict(
            stdin=subprocess.DEVNULL, stdout=sink, stderr=subprocess.STDOUT,
            cwd=str(config.AGENT_WORKSPACE_DIR), env=_minimal_env(),
        )
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = subprocess.Popen(_command(path, list(args)), **kwargs)
        except (OSError, subprocess.SubprocessError):
            return ""
        try:
            proc.wait(timeout=_VERSION_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            return ""
        except (OSError, subprocess.SubprocessError):
            return ""
        sink.seek(0)
        raw = sink.read().decode("utf-8", "replace")
    for line in raw.splitlines():
        line = line.strip()
        if line:
            return line[:160]
    return ""


def _resolve_bin(candidate: str, *, strict: bool) -> str:
    """PATH 查找 + 校验解析结果：不再信任清单里的路径，也不接受脚本宿主 / 命令解释器。

    `strict` 针对外部清单（敌意输入）：解析结果若是 `.cmd/.bat` 等脚本宿主，
    或落在命令解释器上，一律拒绝——Windows 会隐式把它们交给 cmd.exe 二次解析参数。
    内置核心的 bins 由代码写死、run_args 固定，仍按原方式探测，避免把 npm 安装的
    `.cmd` CLI 在本机桌面版里直接判定成「未安装」。
    """
    resolved = shutil.which(candidate) or ""
    if not resolved:
        return ""
    path = Path(resolved)
    if strict and path.suffix.lower() in _REJECTED_BIN_SUFFIXES:
        return ""
    if strict and path.stem.lower() in _FORBIDDEN_SHELL_STEMS:
        return ""
    try:
        real = str(path.resolve())
    except (OSError, RuntimeError):
        return ""
    if not path.is_file():
        return ""
    return real


def _detect_uncached(plugin: AgentPlugin) -> dict:
    if plugin.kind == "kb":
        return {"installed": True, "path": "", "version": "", "detect_error": ""}
    strict = plugin.source != "builtin"
    for candidate in plugin.bins:
        resolved = _resolve_bin(candidate, strict=strict)
        if not resolved:
            continue
        version = _run_version(resolved, plugin.version_args)
        return {"installed": True, "path": resolved, "version": version, "detect_error": ""}
    return {
        "installed": False, "path": "", "version": "",
        "detect_error": f"未找到可执行文件：{', '.join(plugin.bins)}",
    }


def detect(plugin: AgentPlugin) -> dict:
    """探测可执行文件与版本。缓存 30 秒，避免每次刷新页面都起子进程。"""
    key = f"{plugin.id}|{','.join(plugin.bins)}|{','.join(plugin.version_args)}"
    now = time.time()
    with _detect_lock:
        cached = _detect_cache.get(key)
        if cached and now - cached[0] < _DETECT_TTL_SECONDS:
            return dict(cached[1])
    result = _detect_uncached(plugin)
    with _detect_lock:
        _detect_cache[key] = (now, result)
    return dict(result)


# ------------------------------------------------------------------ 启停状态
def _disabled_ids(db: Session) -> set[str]:
    row = db.get(models.AppSetting, DISABLED_SETTING_KEY)
    if not row or not row.value:
        return set()
    try:
        value = json.loads(row.value)
    except ValueError:
        return set()
    return {str(v) for v in value} if isinstance(value, list) else set()


def _save_disabled_ids(db: Session, ids: set[str]) -> None:
    row = db.get(models.AppSetting, DISABLED_SETTING_KEY)
    payload = json.dumps(sorted(ids))
    if row:
        row.value = payload
    else:
        db.add(models.AppSetting(key=DISABLED_SETTING_KEY, value=payload))


def set_enabled(db: Session, plugin_id: str, enabled: bool) -> None:
    ids = _disabled_ids(db)
    if enabled:
        ids.discard(plugin_id)
    else:
        ids.add(plugin_id)
    _save_disabled_ids(db, ids)


# ------------------------------------------------------------------ 列表与模型
def all_plugins() -> tuple[list[AgentPlugin], list[dict]]:
    external, errors = _load_external()
    return [*BUILTIN_PLUGINS, *external], errors


def find_plugin(plugin_id: str) -> AgentPlugin | None:
    plugins, _ = all_plugins()
    for plugin in plugins:
        if plugin.id == plugin_id:
            return plugin
    return None


def _plugin_out(plugin: AgentPlugin, *, enabled: bool) -> dict:
    detection = detect(plugin)
    available = enabled and (plugin.is_kb or detection["installed"])
    reason = ""
    if not enabled:
        reason = "已停用"
    elif not plugin.is_kb and not detection["installed"]:
        reason = detection["detect_error"] or "未安装"
    return {
        "id": plugin.id,
        "name": plugin.name,
        "description": plugin.description,
        "kind": plugin.kind,
        "source": plugin.source,
        "homepage": plugin.homepage,
        "protocols": list(plugin.protocols),
        "protocol_labels": [PROTOCOL_LABELS.get(p, p) for p in plugin.protocols],
        "bins": list(plugin.bins),
        "installed": detection["installed"],
        "path": detection["path"],
        "version": detection["version"],
        "enabled": enabled,
        "available": available,
        "unavailable_reason": reason,
        "builtin": plugin.builtin,
        "requires_workspace": plugin.requires_workspace,
        "timeout_seconds": plugin.timeout_seconds,
        "output": plugin.output,
        "usage_event": plugin.usage_event,
        "manifest_path": plugin.manifest_path,
    }


def list_plugins(db: Session) -> dict:
    plugins, errors = all_plugins()
    disabled = _disabled_ids(db)
    items = [_plugin_out(p, enabled=p.id not in disabled) for p in plugins]
    items.sort(key=lambda item: (0 if item["kind"] == "kb" else 1,
                                 0 if item["builtin"] else 1,
                                 item["name"].lower()))
    return {
        "items": items,
        "errors": errors,
        "plugin_dir": str(config.AGENT_PLUGIN_DIR),
        "schema": SCHEMA_VERSION,
        "supported_protocols": [
            {"value": p, "label": PROTOCOL_LABELS[p]} for p in SUPPORTED_PROTOCOLS
        ],
    }


def plugin_out(db: Session, plugin: AgentPlugin) -> dict:
    return _plugin_out(plugin, enabled=plugin.id not in _disabled_ids(db))


def model_label(cfg: models.ModelConfig, provider: models.Provider) -> str:
    """界面展示用：显示名 · 模型标识 · 供应商。

    同名模型（例如不同渠道的同款模型）只靠显示名分不清，所以把模型标识一起带上；
    显示名留空时它本身就是模型标识，不重复显示。
    """
    name = (cfg.label or "").strip() or cfg.model
    parts = [name]
    if cfg.model and cfg.model != name:
        parts.append(cfg.model)
    if provider.name:
        parts.append(provider.name)
    return " · ".join(parts)


def normalize_base_url(protocol: str, base: str) -> str:
    """把供应商 base_url 转成 CLI 期望的形态。

    - Anthropic 客户端（Claude Code）自己会拼 `/v1/messages`，所以去掉尾部 `/v1`；
    - OpenAI 兼容客户端把 base_url 当作「到 /chat/completions 之前」的根，
      缺版本段时补 `/v1`（与 `services/llm._join` 的规则保持一致）。
    """
    base = (base or "").strip().rstrip("/")
    if protocol == "anthropic":
        return re.sub(r"/v1$", "", base, flags=re.I)
    if _VERSION_SUFFIX_RE.search(base):
        return base
    return base + "/v1"


def model_binding(cfg: models.ModelConfig, provider: models.Provider) -> dict:
    protocol = provider.protocol or "openai"
    try:
        api_key = security.decrypt_secret(provider.api_key_enc) if provider.api_key_enc else ""
    except Exception:  # noqa: BLE001 - 密钥坏了就按无密钥处理，别让会话直接 500
        log.warning("解密供应商密钥失败（provider=%s）", provider.id, exc_info=True)
        api_key = ""
    return {
        "protocol": protocol,
        "base_url": normalize_base_url(protocol, provider.base_url),
        "api_key": api_key or "local",
        "model": cfg.model,
        "model_label": model_label(cfg, provider),
    }


def list_models(db: Session, plugin: AgentPlugin | None = None) -> list[dict]:
    rows = db.execute(
        select(models.ModelConfig)
        .join(models.Provider, models.Provider.id == models.ModelConfig.provider_id)
        .where(
            models.ModelConfig.purpose == models.ModelPurpose.llm.value,
            models.ModelConfig.enabled.is_(True),
            models.Provider.enabled.is_(True),
        )
        .order_by(models.ModelConfig.is_default.desc(), models.ModelConfig.id.desc())
    ).scalars().all()
    out: list[dict] = []
    for cfg in rows:
        provider = cfg.provider
        protocol = provider.protocol or "openai"
        compatible = plugin is None or plugin.is_kb or protocol in plugin.protocols
        out.append({
            "id": cfg.id,
            "model": cfg.model,
            "label": cfg.label,
            "provider_id": provider.id,
            "provider": provider.name,
            "protocol": protocol,
            "protocol_label": PROTOCOL_LABELS.get(protocol, protocol),
            "is_default": bool(cfg.is_default),
            "compatible": compatible,
            "display": model_label(cfg, provider),
        })
    return out


def default_model_id(db: Session, plugin: AgentPlugin) -> int | None:
    """给核心挑一个默认模型：优先兼容的默认项，其次第一条兼容项。"""
    models_list = [m for m in list_models(db, plugin) if m["compatible"]]
    if not models_list:
        return None
    for item in models_list:
        if item["is_default"]:
            return int(item["id"])
    return int(models_list[0]["id"])


def build_env(plugin: AgentPlugin, binding: dict, workspace: str,
              session_id: int, prompt: str = "") -> dict[str, str]:
    """最小环境变量集 + 白名单内的清单模板。不继承调用进程里的密钥类变量。"""
    env = _minimal_env()
    values = {
        "prompt": prompt,
        "model": binding["model"],
        "base_url": binding["base_url"],
        "api_key": binding["api_key"],
        "workspace": workspace,
        "session_id": str(session_id),
        "app_name": "企业知识库",
    }
    for key, template in plugin.env.items():
        if key not in ALLOWED_ENV_KEYS:
            # parse_manifest 已经拒绝外部清单；这里兜住绕过校验手工构造的插件。
            log.warning("忽略 Agent 插件 %s 的非白名单环境变量 %s", plugin.id, key)
            continue
        env[key] = _substitute(template, values)
    # 系统白名单变量是权威值：即便清单绕过了 parse_manifest，也不允许把
    # PATH / COMSPEC / TEMP 等改到别处（P1-7：白名单之后不再被任意覆盖）。
    for key in _ENV_KEEP:
        if key in os.environ and key not in ALLOWED_ENV_KEYS:
            env[key] = os.environ[key]
    env.setdefault("NO_COLOR", "1")
    env.setdefault("TERM", "dumb")
    return env


_PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")


def _substitute(template: str, values: dict[str, str]) -> str:
    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        return str(values.get(key, ""))
    return _PLACEHOLDER_RE.sub(repl, template)


def substitute_args(plugin: AgentPlugin, binding: dict, workspace: str,
                    session_id: int, prompt: str) -> list[str]:
    """展开命令行参数。**不含 api_key**：密钥只应经 `env` 模板注入。"""
    values = {
        "prompt": prompt,
        "model": binding["model"],
        "base_url": binding["base_url"],
        "workspace": workspace,
        "session_id": str(session_id),
        "app_name": "企业知识库",
    }
    return [_substitute(arg, values) for arg in plugin.run_args]


_REDACTED_PROMPT = "<提示词>"
_REDACTED_SECRET = "<密钥>"


def secret_values(binding: dict) -> tuple[str, ...]:
    """需要从命令行摘要与事件日志里抹掉的密钥；无密钥时的哨兵 `local` 不算。"""
    key = str(binding.get("api_key") or "")
    if not key or key == "local":
        return ()
    return (key,)


def redact_text(text: str, *, secrets: tuple[str, ...] = ()) -> str:
    """统一脱敏：命令行摘要、运行日志、正文、错误信息共用同一套替换。"""
    for secret in secrets:
        if secret:
            text = text.replace(secret, _REDACTED_SECRET)
    return text


def redacted_command(command: list[str], prompt: str,
                     secrets: tuple[str, ...] = ()) -> str:
    """给界面看的命令摘要：不展开完整提示词，也不出现密钥。"""
    shown = []
    for part in command:
        if prompt and part == prompt:
            shown.append(_REDACTED_PROMPT)
        else:
            shown.append(redact_text(part, secrets=secrets))
    return redact_text(" ".join(shown), secrets=secrets)[:500]


def _is_within(path: Path, root: Path) -> bool:
    """共同路径前缀比较，避免 `workspace_evil` 这类字符串前缀绕过。"""
    try:
        return os.path.commonpath([
            os.path.normcase(str(path)), os.path.normcase(str(root)),
        ]) == os.path.normcase(str(root))
    except ValueError:      # Windows 不同盘符
        return False


def resolve_workspace(value: str | None) -> str:
    """把会话工作目录限制在 `AGENT_WORKSPACE_DIR` 内；越界抛 `PluginError`。

    `resolve()` 会归一化 `..` 与符号链接，再用 `commonpath` 判定包含关系；
    Windows 下用 `normcase` 消掉盘符大小写差异。路由器把 `PluginError`
    翻译成 400，运行器也再做一次兜底校验。
    """
    root = Path(config.AGENT_WORKSPACE_DIR).resolve()
    raw = (value or "").strip()
    if not raw:
        return str(root)
    try:
        path = Path(raw).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        raise PluginError(f"工作目录不合法：{raw}") from exc
    if not path.is_dir():
        raise PluginError(f"工作目录不存在或不是目录：{raw}")
    if not _is_within(path, root):
        raise PluginError(f"工作目录必须位于 Agent 工作区内：{root}")
    resolved = str(path)
    if len(resolved) > 500:
        raise PluginError("工作目录路径过长")
    return resolved


def clone_with(plugin: AgentPlugin, **changes: Any) -> AgentPlugin:
    return replace(plugin, **changes)
