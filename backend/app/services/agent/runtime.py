"""Agent 运行器：把一次用户提问跑成一个可轮询的事件流。

为什么是「后台线程 + 内存事件队列」而不是 SSE：
- 桌面版的请求经过 Electron sidecar，长连接断开后很难判断是用户离开还是网络抖动；
- 本仓库已有「消息先落 pending、后台生成、前端轮询」的成熟模式（chat.py），
  这里沿用同一套语义，刷新页面还能看到「正在执行」。
- 事件只保存在内存里，最终结果落 `agent_messages` 一行；进程重启时 pending
  会被收尾成 failed（与 `chat.recover_pending_messages` 一致），不会永远转圈。

核心（core）是插件注册表里的一个清单：
- `kb`：调用现有 `answering.answer`，带证据与引用；
- `cli`：按清单启动本机 CLI，把配置好的模型通过协议原生环境变量注入，
  逐行读取 stdout/stderr 并转成 text/log 事件。
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import queue
import re
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from ... import config, models, serializers
from ...db import SessionLocal, is_lock_error
from .. import answering
from . import plugins

log = logging.getLogger("wrk.agent.runtime")

RECOVERED_TEXT = "上次执行没有完成（应用已重启），请重新发送。"
STOPPED_TEXT = "已停止。"
EMPTY_OUTPUT_TEXT = "Agent 没有返回内容。"
MAX_EVENTS = 2500
MAX_TEXT_CHARS = 200_000
MAX_FINISHED_RUNS = 40


class AgentRunError(RuntimeError):
    """可预期的执行失败：消息直接给用户看。"""


class RunStopped(RuntimeError):
    """用户点了停止。"""


# ------------------------------------------------------------------ 运行状态
class RunState:
    def __init__(self, message_id: int, session_id: int):
        self.message_id = message_id
        self.session_id = session_id
        self.events: list[dict] = []
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.status = "running"          # running | ok | failed | stopped
        self.error = ""
        self.text = ""
        self.truncated = False
        self.started_at = time.time()
        self.finished_at: float | None = None
        self.terminate: Callable[[], None] | None = None

    def emit(self, type_: str, **data: Any) -> None:
        with self.lock:
            if len(self.events) >= MAX_EVENTS and type_ not in ("done", "error", "stopped"):
                return
            self.events.append({
                "seq": len(self.events) + 1,
                "type": type_,
                "data": data,
            })

    def emit_text(self, delta: str) -> None:
        if not delta:
            return
        remaining = MAX_TEXT_CHARS - len(self.text)
        if remaining <= 0:
            if not self.truncated:
                self.truncated = True
                self.emit("log", text=f"输出超过 {MAX_TEXT_CHARS} 字符，后续内容已截断")
            return
        if len(delta) > remaining:
            delta = delta[:remaining]
            self.truncated = True
            self.emit("log", text=f"输出超过 {MAX_TEXT_CHARS} 字符，后续内容已截断")
        self.text += delta
        self.emit("text", text=delta)

    def snapshot(self, after: int = 0) -> dict:
        with self.lock:
            events = [e for e in self.events if e["seq"] > after]
            return {
                "events": events,
                "next": self.events[-1]["seq"] if self.events else after,
                "status": self.status,
                "error": self.error,
            }


_runs: dict[int, RunState] = {}
_runs_lock = threading.Lock()


def get_run(message_id: int) -> RunState | None:
    with _runs_lock:
        return _runs.get(message_id)


def _prune_runs() -> None:
    finished = [r for r in _runs.values() if r.finished_at is not None]
    if len(finished) <= MAX_FINISHED_RUNS:
        return
    finished.sort(key=lambda r: r.finished_at or 0)
    for state in finished[: len(finished) - MAX_FINISHED_RUNS]:
        _runs.pop(state.message_id, None)


def start_run(message_id: int, session_id: int) -> RunState:
    state = RunState(message_id, session_id)
    with _runs_lock:
        _runs[message_id] = state
        _prune_runs()
    thread = threading.Thread(
        target=_execute, args=(message_id, state), daemon=True,
        name=f"wrk-agent-{message_id}",
    )
    thread.start()
    return state


def stop_run(message_id: int) -> bool:
    state = get_run(message_id)
    if not state or state.status != "running":
        return False
    state.stop_event.set()
    if state.terminate is not None:
        try:
            state.terminate()
        except Exception:  # noqa: BLE001 - 停止是 best-effort
            log.debug("终止 Agent 子进程失败", exc_info=True)
    return True


def recover_pending() -> int:
    """启动时收尾上一个进程留下的 pending 消息与会话状态。"""
    with SessionLocal() as db:
        messages = db.execute(
            select(models.AgentMessage).where(
                models.AgentMessage.role == "assistant",
                models.AgentMessage.status == "pending",
            )
        ).scalars().all()
        for message in messages:
            message.status = "failed"
            if not (message.content or "").strip():
                message.content = RECOVERED_TEXT
            db.add(message)
        sessions = db.execute(
            select(models.AgentSession).where(models.AgentSession.status == "running")
        ).scalars().all()
        for session in sessions:
            session.status = "idle"
            db.add(session)
        if messages or sessions:
            db.commit()
        return len(messages)


# ------------------------------------------------------------------ 上下文
@dataclass
class RunContext:
    message_id: int
    session_id: int
    user_id: int
    plugin: plugins.AgentPlugin
    question: str
    history: list[dict] = field(default_factory=list)
    kb_ids: list[int] = field(default_factory=list)
    workspace: str = ""
    model_config_id: int | None = None
    model_binding: dict | None = None
    model_label: str = ""


def _load_context(message_id: int) -> RunContext:
    with SessionLocal() as db:
        message = db.get(models.AgentMessage, message_id)
        if not message or message.role != "assistant":
            raise AgentRunError("运行记录不存在")
        session = db.get(models.AgentSession, message.session_id)
        if session is None:
            raise AgentRunError("会话不存在或已删除")
        plugin = plugins.find_plugin(session.core_id)
        if plugin is None:
            raise AgentRunError(f"Agent 核心 `{session.core_id}` 已不存在，请重新选择核心")

        rows = db.execute(
            select(models.AgentMessage)
            .where(
                models.AgentMessage.session_id == session.id,
                models.AgentMessage.id < message.id,
            )
            .order_by(models.AgentMessage.id.desc())
            .limit(max(1, config.AGENT_HISTORY_MESSAGES))
        ).scalars().all()
        history = [
            {"role": m.role, "content": m.content or ""}
            for m in reversed(rows)
            if m.role in ("user", "assistant", "system")
        ]
        question = ""
        for item in reversed(history):
            if item["role"] == "user":
                question = item["content"]
                break
        if not question:
            raise AgentRunError("没有找到本轮提问")

        binding = None
        label = ""
        cfg = None
        if session.model_config_id:
            cfg = db.get(models.ModelConfig, session.model_config_id)
        if cfg is not None:
            provider = db.get(models.Provider, cfg.provider_id)
            if provider is not None:
                binding = plugins.model_binding(cfg, provider)
                label = binding["model_label"]

        try:
            kb_ids = [int(v) for v in json.loads(session.kb_ids or "[]")]
        except (ValueError, TypeError):
            kb_ids = []

        workspace = session.workspace or str(config.AGENT_WORKSPACE_DIR)
        return RunContext(
            message_id=message.id,
            session_id=session.id,
            user_id=session.user_id,
            plugin=plugin,
            question=question,
            history=history,
            kb_ids=kb_ids,
            workspace=workspace,
            model_config_id=session.model_config_id,
            model_binding=binding,
            model_label=label,
        )


# ------------------------------------------------------------------ 执行入口
def _execute(message_id: int, state: RunState) -> None:
    ctx: RunContext | None = None
    try:
        ctx = _load_context(message_id)
        if ctx.plugin.is_kb:
            content, evidence, meta = _run_kb(state, ctx)
        else:
            content, evidence, meta = _run_cli(state, ctx)
        if state.stop_event.is_set():
            raise RunStopped()
        out = _safe_finish(ctx, "ok", content, evidence, meta)
        if out is None:
            raise AgentRunError("执行完成，但结果保存失败，请重试")
        state.status = "ok"
        state.emit("done", message=out, status="ok")
    except RunStopped:
        ctx = ctx or _safe_context(message_id)
        out = _safe_finish(ctx, "stopped", state.text.strip() or STOPPED_TEXT,
                           [], {"stopped": True}) if ctx else _mark_failed_without_ctx(
            message_id, STOPPED_TEXT, "stopped")
        state.status = "stopped"
        state.emit("stopped", message=out, status="stopped")
    except AgentRunError as exc:
        log.warning("Agent 执行失败（消息 #%s）：%s", message_id, exc)
        ctx = ctx or _safe_context(message_id)
        out = _safe_finish(ctx, "failed", state.text.strip() or str(exc),
                           [], {"error": str(exc)}) if ctx else _mark_failed_without_ctx(
            message_id, str(exc), "failed")
        state.status = "failed"
        state.error = str(exc)
        state.emit("error", message=str(exc), status="failed")
    except BaseException as exc:  # noqa: BLE001 - 后台线程必须自己收尾
        log.exception("Agent 执行异常（消息 #%s）", message_id)
        ctx = ctx or _safe_context(message_id)
        message = f"执行失败：{exc}"
        out = _safe_finish(ctx, "failed", state.text.strip() or message,
                           [], {"error": str(exc)}) if ctx else _mark_failed_without_ctx(
            message_id, message, "failed")
        state.status = "failed"
        state.error = message
        state.emit("error", message=message, status="failed")
    finally:
        state.finished_at = time.time()
        state.terminate = None


def _safe_context(message_id: int) -> RunContext | None:
    try:
        return _load_context(message_id)
    except Exception:  # noqa: BLE001 - 收尾路径不能再抛
        return None


def _safe_finish(ctx: RunContext, status: str, content: str,
                 evidence: list, meta: dict) -> dict | None:
    """收尾落库绝不能让后台线程崩掉；崩了前端会永远停在「执行中」。"""
    try:
        return _finish(ctx, status, content, evidence, meta)
    except Exception:  # noqa: BLE001
        log.exception("保存 Agent 结果失败（消息 #%s）", ctx.message_id)
        return _mark_failed_without_ctx(
            ctx.message_id, f"{content}\n\n（结果保存失败，请重试）", status)


def _mark_failed_without_ctx(message_id: int, text: str, status: str) -> dict | None:
    """上下文都读不出来时，至少把 pending 消息收尾，避免前端无限轮询。"""
    try:
        with SessionLocal() as db:
            message = db.get(models.AgentMessage, message_id)
            if message is None:
                return None
            message.status = status
            message.content = text
            message.meta = json.dumps({"error": text}, ensure_ascii=False)
            session = db.get(models.AgentSession, message.session_id)
            if session is not None:
                session.status = "idle" if status == "stopped" else "error"
                db.add(session)
            db.add(message)
            db.commit()
            return serializers.agent_message_out(message)
    except Exception:  # noqa: BLE001
        log.exception("收尾 Agent 消息失败（消息 #%s）", message_id)
        return None


def _finish(ctx: RunContext, status: str, content: str,
            evidence: list, meta: dict) -> dict:
    """落库并返回给前端的最终消息。写锁冲突时重试几次。"""
    latency_ms = 0
    for attempt in range(4):
        try:
            with SessionLocal() as db:
                message = db.get(models.AgentMessage, ctx.message_id)
                if message is None:
                    raise AgentRunError("运行记录已被删除")
                message.content = content[:MAX_TEXT_CHARS]
                message.status = status
                message.evidence = json.dumps(evidence, ensure_ascii=False)
                message.meta = json.dumps(meta, ensure_ascii=False)
                created = message.created_at or models.utcnow()
                if created.tzinfo is None:
                    created = created.replace(tzinfo=dt.timezone.utc)
                message.latency_ms = max(
                    0, int((models.utcnow() - created).total_seconds() * 1000))
                session = db.get(models.AgentSession, message.session_id)
                if session is not None:
                    session.status = "idle" if status in ("ok", "stopped") else "error"
                    session.updated_at = models.utcnow()
                    db.add(session)
                db.add(message)
                db.commit()
                latency_ms = message.latency_ms
                out = serializers.agent_message_out(message, core_name=ctx.plugin.name)
                out["latency_ms"] = latency_ms
                return out
        except OperationalError as exc:
            if not is_lock_error(exc) or attempt == 3:
                raise
            time.sleep(0.3 * (attempt + 1))
    raise AgentRunError("保存 Agent 结果失败")


# ------------------------------------------------------------------ 知识库核心
def _run_kb(state: RunState, ctx: RunContext) -> tuple[str, list, dict]:
    from ...deps import grant_level

    with SessionLocal() as db:
        user = db.get(models.User, ctx.user_id)
        if user is None:
            raise AgentRunError("用户不存在")
        kbs: list[models.KnowledgeBase] = []
        for kb_id in ctx.kb_ids:
            kb = db.get(models.KnowledgeBase, kb_id)
            if kb is not None and grant_level(db, user, kb_id) is not None:
                kbs.append(kb)
        if not kbs:
            raise AgentRunError("当前会话没有可访问的知识库，请先在顶部选择至少一个知识库")

        cfg = db.get(models.ModelConfig, ctx.model_config_id) if ctx.model_config_id else None
        state.emit("status", text=f"正在检索 {len(kbs)} 个知识库…")
        result = answering.answer(db, ctx.question, kbs, cfg_override=cfg)

        content = result["content"]
        evidence = result["evidence"]
        meta = {
            "core": ctx.plugin.id,
            "model": ctx.model_label,
            "usage": result.get("usage") or {},
            "route": result.get("route") or {},
            "insufficient": bool(result.get("insufficient")),
            "suggestions": result.get("suggestions") or [],
            "refusal_reason": result.get("refusal_reason") or "",
        }
        state.emit_text(content)
        state.emit("evidence", items=evidence)
        return content, evidence, meta


# ------------------------------------------------------------------ CLI 核心
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _build_cli_prompt(ctx: RunContext) -> str:
    lines = [
        "你是运行在「企业知识库文档整理与问答系统」里的本地个人 Agent。",
        f"当前工作目录：{ctx.workspace}",
        "请直接完成用户的请求；需要时可以使用本机工具。回答用中文，除非用户使用其他语言。",
    ]
    transcript = ctx.history
    if transcript and transcript[-1]["role"] == "user" \
            and transcript[-1]["content"] == ctx.question:
        transcript = transcript[:-1]
    if transcript:
        lines.append("")
        lines.append("对话记录：")
        for item in transcript[-config.AGENT_HISTORY_MESSAGES:]:
            text = (item["content"] or "").strip()
            if not text:
                continue
            if len(text) > config.AGENT_HISTORY_CHARS:
                text = text[: config.AGENT_HISTORY_CHARS] + "…"
            role = {"user": "用户", "assistant": "助手", "system": "系统"}.get(
                item["role"], item["role"])
            lines.append(f"{role}：{text}")
    lines += ["", f"用户：{ctx.question}", "", "请回答："]
    return "\n".join(lines)[:20000]


def _clean_env(plugin: plugins.AgentPlugin, binding: dict,
               workspace: str, session_id: int, prompt: str) -> dict[str, str]:
    return plugins.build_env(plugin, binding, workspace, session_id, prompt)


def _safe_workspace(value: str) -> str:
    """运行前兜底：旧会话里可能存着收敛之前写入的越界工作目录。

    路由器在建会话/改会话时已用 `plugins.resolve_workspace` 拦过一次；这里
    再校验一次，避免直接改库或进程重启后的历史会话绕过边界。
    """
    try:
        return plugins.resolve_workspace(value)
    except plugins.PluginError as exc:
        raise AgentRunError(str(exc)) from exc


def _pump(stream, output: "queue.Queue[tuple[str, str | None]]", kind: str) -> None:
    try:
        for line in iter(stream.readline, ""):
            output.put((kind, line))
    except Exception:  # noqa: BLE001 - 管道关闭
        pass
    finally:
        output.put((kind, None))
        try:
            stream.close()
        except Exception:
            pass


def _terminate_process(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True, timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:
            pass


def _extract_jsonl_text(obj: Any, paths: tuple[str, ...]) -> str:
    for path in paths:
        cur = obj
        ok = True
        for part in path.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                ok = False
                break
        if ok and isinstance(cur, str) and cur.strip():
            return cur
    return ""


def _jsonl_error(obj: Any) -> str:
    if not isinstance(obj, dict):
        return ""
    kind = str(obj.get("type") or obj.get("msg", {}).get("type") or "")
    if kind in ("error", "turn.failed", "thread.failed"):
        message = obj.get("message") or obj.get("error") or obj.get("msg")
        return str(message)[:500]
    return ""


# 供应商/CLI 对同一个概念有两种写法：OpenAI Responses 用 input_tokens /
# output_tokens，OpenAI 兼容 chat 用 prompt_tokens / completion_tokens。
_USAGE_INPUT_KEYS = ("input_tokens", "prompt_tokens")
_USAGE_OUTPUT_KEYS = ("output_tokens", "completion_tokens")


def _first_int(usage: dict, keys: tuple[str, ...]) -> int | None:
    """取第一个非负整数；`type(value) is int` 顺带挡掉 bool 与浮点。"""
    for key in keys:
        value = usage.get(key)
        if type(value) is int and value >= 0:
            return value
    return None


def _jsonl_usage(obj: Any, event: str) -> tuple[int, int] | None:
    """从一条 JSONL 事件里读本次调用的 token 用量；不是用量事件时返回 None。

    只认清单里声明的那一类事件（Codex 是 `turn.completed`）与其中的 `usage`
    对象；结构不符就返回 None，由调用方按「未计量」处理，绝不猜数字。
    """
    if not event or not isinstance(obj, dict) or obj.get("type") != event:
        return None
    usage = obj.get("usage")
    if not isinstance(usage, dict):
        return None
    prompt = _first_int(usage, _USAGE_INPUT_KEYS)
    completion = _first_int(usage, _USAGE_OUTPUT_KEYS)
    if prompt is None and completion is None:
        return None
    return prompt or 0, completion or 0


def _usage_meta(prompt_tokens: int, completion_tokens: int, latency_ms: int) -> dict:
    """个人 Agent 一次执行的用量快照，键名与 `llm.CallStats.as_dict()` 对齐。

    为什么调用数固定记 1：这是「一次执行」而不是「一次模型请求」——CLI 内部
    可能调了十几次模型，但应用只知道这一轮跑完了。token 能用就照实记，
    用不上就记 0，并由调用方在 meta 上加 `usage_unmetered` 让用量页计入
    「未计量」，不猜数字。
    """
    return {
        "calls": 1,
        "chat_calls": 1,
        "live_calls": 1,
        "offline_calls": 0,
        "prompt_tokens": max(int(prompt_tokens), 0),
        "completion_tokens": max(int(completion_tokens), 0),
        "embed_tokens": 0,
        "latency_ms": max(int(latency_ms), 0),
    }


def _run_cli(state: RunState, ctx: RunContext) -> tuple[str, list, dict]:
    plugin = ctx.plugin
    started = time.time()
    detection = plugins.detect(plugin)
    if not detection["installed"]:
        raise AgentRunError(
            f"未检测到 {plugin.name}：{detection['detect_error'] or '请先安装对应的命令行工具'}")
    binding = ctx.model_binding
    if not binding:
        raise AgentRunError("这个核心需要先选择模型，请点右上角的模型选择器")

    workspace_str = _safe_workspace(ctx.workspace)
    workspace = Path(workspace_str)
    if not workspace.is_dir():
        raise AgentRunError(f"工作目录不存在：{workspace}")

    # 统一脱敏：密钥只经 env 模板注入，绝不进命令行；命令摘要、运行日志、
    # 正文与错误信息共用同一份替换（P1-7）。
    secrets = plugins.secret_values(binding)
    prompt = _build_cli_prompt(ctx)
    args = plugins.substitute_args(plugin, binding, workspace_str, ctx.session_id, prompt)
    command = [detection["path"], *args]
    env = _clean_env(plugin, binding, workspace_str, ctx.session_id, prompt)
    state.emit("status", text=f"正在启动 {plugin.name}…")
    state.emit("log", text=f"$ {plugins.redacted_command(command, prompt, secrets)}")

    kwargs: dict[str, Any] = dict(
        cwd=workspace_str, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(command, **kwargs)
    except OSError as exc:
        raise AgentRunError(
            f"启动 {plugin.name} 失败：{plugins.redact_text(str(exc), secrets=secrets)}"
        ) from exc

    state.terminate = lambda: _terminate_process(proc)
    output: "queue.Queue[tuple[str, str | None]]" = queue.Queue()
    readers = [
        threading.Thread(target=_pump, args=(proc.stdout, output, "stdout"), daemon=True),
        threading.Thread(target=_pump, args=(proc.stderr, output, "stderr"), daemon=True),
    ]
    for reader in readers:
        reader.start()

    timeout = min(int(plugin.timeout_seconds or config.AGENT_RUN_TIMEOUT_SECONDS),
                  config.AGENT_RUN_TIMEOUT_SECONDS)
    deadline = time.time() + timeout
    stderr_tail: deque[str] = deque(maxlen=40)
    seen: set[str] = set()
    done_streams = 0
    # 一轮执行里 usage 事件可能出现多次（每个 turn 一条），token 累加、调用仍记 1 次。
    usage_events = 0
    prompt_tokens = 0
    completion_tokens = 0
    state.emit("status", text="已启动，等待输出…")

    while done_streams < 2:
        if state.stop_event.is_set():
            _terminate_process(proc)
            raise RunStopped()
        if time.time() > deadline:
            _terminate_process(proc)
            raise AgentRunError(f"执行超过 {timeout} 秒，已终止")
        try:
            kind, line = output.get(timeout=0.2)
        except queue.Empty:
            continue
        if line is None:
            done_streams += 1
            continue
        clean = plugins.redact_text(_strip_ansi(line.rstrip("\r\n")), secrets=secrets)
        if not clean:
            continue
        if kind == "stderr":
            stderr_tail.append(clean[:500])
            state.emit("log", text=clean[:1000])
            continue
        if plugin.output == "jsonl":
            try:
                obj = json.loads(clean)
            except ValueError:
                # 有些 CLI 会先打一行人类可读的提示，再进入 JSONL；原样当日志。
                state.emit("log", text=clean[:1000])
                continue
            error = _jsonl_error(obj)
            if error:
                stderr_tail.append(error)
                state.emit("log", text=error)
                continue
            usage = _jsonl_usage(obj, plugin.usage_event)
            if usage is not None:
                usage_events += 1
                prompt_tokens += usage[0]
                completion_tokens += usage[1]
            text = _extract_jsonl_text(obj, plugin.text_paths)
            if text and text not in seen:
                seen.add(text)
                state.emit_text(text if text.endswith("\n") else text + "\n")
        else:
            state.emit_text(clean + "\n")

    try:
        code = proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        _terminate_process(proc)
        code = proc.wait(timeout=10)

    if state.stop_event.is_set():
        raise RunStopped()
    if code != 0:
        tail = "\n".join(stderr_tail)[-800:]
        raise AgentRunError(
            f"{plugin.name} 退出码 {code}。" + (f"\n{tail}" if tail else ""))
    content = state.text.strip()
    if not content:
        tail = "\n".join(stderr_tail)[-800:]
        raise AgentRunError(EMPTY_OUTPUT_TEXT + (f"\n{tail}" if tail else ""))
    meta = {
        "core": plugin.id,
        "model": ctx.model_label,
        "protocol": binding["protocol"],
        "exit_code": code,
        "command": plugins.redacted_command(command, prompt, secrets),
        "workspace": workspace_str,
        # 用量快照：知识库核心由 answering 直接给出，CLI 核心在这里落库。
        # 解析不到任何用量事件时写零 token 并标注 usage_unmetered，用量页据此
        # 计入「未计量」——不能把「不知道」悄悄算成「没消耗」。
        "usage": _usage_meta(prompt_tokens, completion_tokens,
                             int((time.time() - started) * 1000)),
    }
    if usage_events == 0:
        meta["usage_unmetered"] = True
    return content, [], meta
