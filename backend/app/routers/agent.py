"""个人 Agent：核心插件、会话、执行事件。

权限：v1 只对管理员开放。CLI 核心会以应用进程身份执行本机命令，等于把
shell 交给使用者；在单企业桌面部署里这应当是管理员的能力，而不是每个员工
的默认能力。知识库问答本身仍走原有的员工问答页。
"""
from __future__ import annotations

import json
import threading

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import config, models, schemas, serializers
from ..db import SessionLocal, get_db
from ..deps import audit, require_admin
from ..services.agent import plugins, runtime

router = APIRouter(tags=["agent"])

# 同一会话的「检查是否在跑 + 建两条消息」必须串行，否则两个并发请求
# 会各建一个 pending，界面出现两个同时执行的核心。
_session_locks: dict[int, threading.Lock] = {}
_session_locks_guard = threading.Lock()


def _session_lock(session_id: int) -> threading.Lock:
    with _session_locks_guard:
        return _session_locks.setdefault(session_id, threading.Lock())


def _my_session(db: Session, user: models.User, session_id: int) -> models.AgentSession:
    session = db.get(models.AgentSession, session_id)
    if session is None or session.user_id != user.id:
        raise HTTPException(status_code=404, detail="Agent 会话不存在")
    return session


def _core(db: Session, core_id: str, *, require_available: bool = True) -> plugins.AgentPlugin:
    plugin = plugins.find_plugin(core_id)
    if plugin is None:
        raise HTTPException(status_code=404, detail=f"Agent 核心 `{core_id}` 不存在")
    out = plugins.plugin_out(db, plugin)
    if require_available and not out["enabled"]:
        raise HTTPException(status_code=400, detail=f"{plugin.name} 已停用，请先在核心管理里启用")
    if require_available and not out["installed"]:
        raise HTTPException(
            status_code=400,
            detail=f"{plugin.name} 未安装：{out['unavailable_reason']}")
    return plugin


def _validate_model(db: Session, plugin: plugins.AgentPlugin,
                    model_config_id: int | None, *, required: bool) -> models.ModelConfig | None:
    if model_config_id is None:
        if required:
            raise HTTPException(status_code=400, detail=f"{plugin.name} 需要先选择一个模型")
        return None
    cfg = db.get(models.ModelConfig, model_config_id)
    if cfg is None or cfg.purpose != models.ModelPurpose.llm.value or not cfg.enabled:
        raise HTTPException(status_code=400, detail="所选模型不存在或未启用")
    provider = db.get(models.Provider, cfg.provider_id)
    if provider is None or not provider.enabled:
        raise HTTPException(status_code=400, detail="所选模型的供应商已停用")
    if not plugin.is_kb and (provider.protocol or "openai") not in plugin.protocols:
        raise HTTPException(
            status_code=400,
            detail=f"{plugin.name} 不支持 {provider.name} 的协议（{provider.protocol}），请换一个模型")
    return cfg


def _filter_kb_ids(db: Session, user: models.User, kb_ids: list[int]) -> list[int]:
    from ..deps import grant_level

    allowed: list[int] = []
    seen: set[int] = set()
    for kb_id in kb_ids:
        if kb_id in seen:
            continue
        seen.add(kb_id)
        kb = db.get(models.KnowledgeBase, kb_id)
        if kb is not None and grant_level(db, user, kb_id) is not None:
            allowed.append(kb_id)
    return allowed


def _workspace(value: str | None) -> str:
    """会话工作目录必须落在 `AGENT_WORKSPACE_DIR` 内，越界返回 400（P1-7）。

    校验逻辑放在 `services/agent/plugins.resolve_workspace`，这里只负责把
    `PluginError` 翻译成 HTTP 400，保持路由层不重复实现边界规则。
    """
    try:
        return plugins.resolve_workspace(value)
    except plugins.PluginError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _session_out(db: Session, session: models.AgentSession, *,
                 message_count: int | None = None) -> dict:
    plugin = plugins.find_plugin(session.core_id)
    cfg = db.get(models.ModelConfig, session.model_config_id) if session.model_config_id else None
    label = plugins.model_label(cfg, cfg.provider) if cfg is not None else ""
    if message_count is None:
        message_count = db.execute(
            select(func.count(models.AgentMessage.id)).where(
                models.AgentMessage.session_id == session.id)
        ).scalar() or 0
    return serializers.agent_session_out(
        session,
        message_count=message_count,
        core_name=plugin.name if plugin else session.core_id,
        model_label=label,
    )


def _has_pending(db: Session, session_id: int) -> bool:
    return db.execute(
        select(models.AgentMessage.id).where(
            models.AgentMessage.session_id == session_id,
            models.AgentMessage.role == "assistant",
            models.AgentMessage.status == "pending",
        ).limit(1)
    ).first() is not None


def _ensure_idle(db: Session, session: models.AgentSession) -> None:
    if session.status == "running" or _has_pending(db, session.id):
        raise HTTPException(status_code=409, detail="当前这一轮还在执行，请先停止或等它结束")


# ------------------------------------------------------------------ 核心与插件
@router.get("/agent/cores")
def list_cores(db: Session = Depends(get_db), admin: models.User = Depends(require_admin)):
    data = plugins.list_plugins(db)
    by_id = {p.id: p for p in plugins.all_plugins()[0]}
    for item in data["items"]:
        plugin = by_id.get(item["id"])
        if plugin is None:
            continue
        models_list = plugins.list_models(db, plugin)
        item["compatible_model_count"] = len([m for m in models_list if m["compatible"]])
        item["default_model_id"] = plugins.default_model_id(db, plugin)
        item["model_ready"] = plugin.is_kb or item["compatible_model_count"] > 0
    return data


@router.get("/agent/models")
def list_agent_models(core_id: str | None = None,
                      db: Session = Depends(get_db),
                      admin: models.User = Depends(require_admin)):
    plugin = plugins.find_plugin(core_id) if core_id else None
    if core_id and plugin is None:
        raise HTTPException(status_code=404, detail=f"Agent 核心 `{core_id}` 不存在")
    return {"items": plugins.list_models(db, plugin), "core_id": core_id}


@router.post("/agent/plugins/rescan")
def rescan_plugins(db: Session = Depends(get_db),
                   admin: models.User = Depends(require_admin)):
    plugins.clear_detection_cache()
    data = plugins.list_plugins(db)
    audit(db, admin, "agent_plugin_rescan", target=str(config.AGENT_PLUGIN_DIR),
          detail=json.dumps([e["file"] for e in data["errors"]], ensure_ascii=False))
    db.commit()
    return data


@router.patch("/agent/plugins/{plugin_id}")
def toggle_plugin(plugin_id: str, body: schemas.AgentPluginToggleIn,
                  db: Session = Depends(get_db),
                  admin: models.User = Depends(require_admin)):
    plugin = plugins.find_plugin(plugin_id)
    if plugin is None:
        raise HTTPException(status_code=404, detail=f"Agent 核心 `{plugin_id}` 不存在")
    if plugin_id == "kb" and not body.enabled:
        raise HTTPException(status_code=400, detail="内置知识库核心不能停用，它是最小可用回退")
    plugins.set_enabled(db, plugin_id, body.enabled)
    audit(db, admin, "agent_plugin_toggle", target=plugin_id,
          detail="enabled" if body.enabled else "disabled")
    db.commit()
    return plugins.plugin_out(db, plugin)


# ------------------------------------------------------------------ 会话
@router.get("/agent/sessions")
def list_sessions(db: Session = Depends(get_db),
                  admin: models.User = Depends(require_admin)):
    sessions = db.execute(
        select(models.AgentSession)
        .where(models.AgentSession.user_id == admin.id)
        .order_by(models.AgentSession.updated_at.desc())
        .limit(100)
    ).scalars().all()
    counts = dict(db.execute(
        select(models.AgentMessage.session_id, func.count(models.AgentMessage.id))
        .where(models.AgentMessage.session_id.in_([s.id for s in sessions] or [0]))
        .group_by(models.AgentMessage.session_id)
    ).all())
    return {
        "items": [_session_out(db, s, message_count=counts.get(s.id, 0)) for s in sessions],
        "total": len(sessions),
    }


@router.post("/agent/sessions", status_code=201)
def create_session(body: schemas.AgentSessionIn,
                   db: Session = Depends(get_db),
                   admin: models.User = Depends(require_admin)):
    core_id = (body.core_id or "").strip()
    if core_id:
        plugin = _core(db, core_id)
    else:
        candidates = plugins.list_plugins(db)["items"]
        usable = [c for c in candidates if c["available"]]
        if not usable:
            raise HTTPException(status_code=400, detail="没有可用的 Agent 核心，请先安装 CLI 或启用插件")
        core_id = usable[0]["id"]
        plugin = _core(db, core_id)

    model_id = body.model_config_id
    if model_id is None:
        model_id = plugins.default_model_id(db, plugin)
    _validate_model(db, plugin, model_id, required=not plugin.is_kb)

    kb_ids = _filter_kb_ids(db, admin, body.kb_ids)
    session = models.AgentSession(
        user_id=admin.id,
        title=(body.title or "").strip()[:255] or "新会话",
        core_id=plugin.id,
        model_config_id=model_id,
        kb_ids=json.dumps(kb_ids),
        workspace=_workspace(body.workspace),
        status="idle",
    )
    db.add(session)
    db.commit()
    return _session_out(db, session)


@router.get("/agent/sessions/{session_id}")
def get_session(session_id: int, db: Session = Depends(get_db),
                admin: models.User = Depends(require_admin)):
    session = _my_session(db, admin, session_id)
    messages = db.execute(
        select(models.AgentMessage)
        .where(models.AgentMessage.session_id == session.id)
        .order_by(models.AgentMessage.id)
    ).scalars().all()
    core_map = {p.id: p.name for p in plugins.all_plugins()[0]}
    data = _session_out(db, session, message_count=len(messages))
    data["messages"] = [
        serializers.agent_message_out(m, core_name=core_map.get(m.core_id, m.core_id))
        for m in messages
    ]
    plugin = plugins.find_plugin(session.core_id)
    data["core"] = plugins.plugin_out(db, plugin) if plugin else None
    data["running"] = _has_pending(db, session.id)
    return data


@router.patch("/agent/sessions/{session_id}")
def update_session(session_id: int, body: schemas.AgentSessionUpdateIn,
                   db: Session = Depends(get_db),
                   admin: models.User = Depends(require_admin)):
    session = _my_session(db, admin, session_id)
    _ensure_idle(db, session)

    old_plugin = plugins.find_plugin(session.core_id)
    plugin = old_plugin
    if "core_id" in body.model_fields_set and body.core_id is not None:
        plugin = _core(db, body.core_id)
    if plugin is None:
        # 内置核心被删除/改名属于异常状态：给用户一个可恢复的错误，而不是 500。
        raise HTTPException(status_code=400, detail="当前会话的 Agent 核心已不存在，请重新选择核心")

    if body.title is not None:
        session.title = body.title.strip()[:255] or session.title

    core_changed = plugin.id != session.core_id
    if core_changed:
        session.core_id = plugin.id

    # 核心换了以后，原来那条模型可能协议不兼容：显式传了模型就按新核心校验，
    # 没传就自动挑一条兼容的，避免用户切到 Claude Code 还挂着 OpenAI 模型。
    if "model_config_id" in body.model_fields_set:
        _validate_model(db, plugin, body.model_config_id, required=not plugin.is_kb)
        session.model_config_id = body.model_config_id
    elif core_changed:
        current = db.get(models.ModelConfig, session.model_config_id) if session.model_config_id else None
        compatible = False
        if current is not None:
            provider = db.get(models.Provider, current.provider_id)
            compatible = provider is not None and (
                plugin.is_kb or (provider.protocol or "openai") in plugin.protocols)
        if not compatible:
            session.model_config_id = plugins.default_model_id(db, plugin)
            if not plugin.is_kb and session.model_config_id is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"没有与 {plugin.name} 协议兼容的模型，请先在模型设置里添加")

    if body.kb_ids is not None:
        session.kb_ids = json.dumps(_filter_kb_ids(db, admin, body.kb_ids))
    if body.workspace is not None:
        session.workspace = _workspace(body.workspace)

    if core_changed:
        old_name = old_plugin.name if old_plugin else session.core_id
        db.add(models.AgentMessage(
            session_id=session.id, role="system", status="ok",
            core_id=plugin.id,
            content=f"已切换 Agent 核心：{old_name} → {plugin.name}。下一轮开始使用新核心。",
        ))
    session.updated_at = models.utcnow()
    db.add(session)
    db.commit()
    return _session_out(db, session)


@router.delete("/agent/sessions/{session_id}")
def delete_session(session_id: int, db: Session = Depends(get_db),
                   admin: models.User = Depends(require_admin)):
    session = _my_session(db, admin, session_id)
    _ensure_idle(db, session)
    audit(db, admin, "agent_session_delete", target=str(session.id), detail=session.title)
    db.delete(session)
    db.commit()
    return {"ok": True}


# ------------------------------------------------------------------ 执行
@router.post("/agent/sessions/{session_id}/messages")
def send_message(session_id: int, body: schemas.AgentMessageIn,
                 db: Session = Depends(get_db),
                 admin: models.User = Depends(require_admin)):
    lock = _session_lock(session_id)
    with lock:
        session = _my_session(db, admin, session_id)
        _ensure_idle(db, session)
        plugin = _core(db, session.core_id)
        cfg = _validate_model(db, plugin, session.model_config_id, required=not plugin.is_kb)
        content = body.content.strip()
        if not content:
            raise HTTPException(status_code=400, detail="消息不能为空")

        label = plugins.model_label(cfg, cfg.provider) if cfg is not None else ""
        user_message = models.AgentMessage(
            session_id=session.id, role="user", content=content,
            core_id=plugin.id, model_label=label, status="ok",
        )
        assistant = models.AgentMessage(
            session_id=session.id, role="assistant", content="",
            core_id=plugin.id, model_label=label, status="pending",
        )
        db.add_all([user_message, assistant])
        if session.title in ("", "新会话"):
            session.title = content[:40]
        session.status = "running"
        session.updated_at = models.utcnow()
        db.add(session)
        db.commit()
        run_id = assistant.id
        user_out = serializers.agent_message_out(user_message, core_name=plugin.name)
        pending_out = serializers.agent_message_out(assistant, core_name=plugin.name)

    try:
        runtime.start_run(run_id, session_id)
    except Exception as exc:  # noqa: BLE001 - 启动失败也要给用户一个可读结果
        with SessionLocal() as fail_db:
            row = fail_db.get(models.AgentMessage, run_id)
            if row is not None:
                row.status = "failed"
                row.content = f"启动 Agent 核心失败：{exc}"
                fail_db.add(row)
            fail_session = fail_db.get(models.AgentSession, session_id)
            if fail_session is not None:
                fail_session.status = "error"
                fail_db.add(fail_session)
            fail_db.commit()
        raise HTTPException(status_code=500, detail=f"启动 Agent 核心失败：{exc}") from exc
    return {
        "run_id": run_id,
        "user_message": user_out,
        "assistant_message": pending_out,
        "session": _session_out(db, session),
    }


@router.get("/agent/runs/{run_id}/events")
def run_events(run_id: int, after: int = 0, db: Session = Depends(get_db),
               admin: models.User = Depends(require_admin)):
    message = db.get(models.AgentMessage, run_id)
    if message is None:
        raise HTTPException(status_code=404, detail="运行记录不存在")
    session = _my_session(db, admin, message.session_id)
    state = runtime.get_run(run_id)
    if state is not None:
        data = state.snapshot(after)
        if state.status != "running":
            fresh = db.get(models.AgentMessage, run_id)
            if fresh is not None:
                core_map = {p.id: p.name for p in plugins.all_plugins()[0]}
                data["message"] = serializers.agent_message_out(
                    fresh, core_name=core_map.get(fresh.core_id, fresh.core_id))
        data["run_id"] = run_id
        data["session_id"] = session.id
        return data

    # 内存里没有这次运行：通常是进程重启后前端还挂着旧轮询。
    # pending 在启动时已被 recover_pending 收尾成 failed，这里照实返回。
    core_map = {p.id: p.name for p in plugins.all_plugins()[0]}
    return {
        "events": [],
        "next": after,
        "status": message.status if message.status != "pending" else "failed",
        "error": "" if message.status != "pending" else runtime.RECOVERED_TEXT,
        "message": serializers.agent_message_out(
            message, core_name=core_map.get(message.core_id, message.core_id)),
        "run_id": run_id,
        "session_id": session.id,
    }


@router.post("/agent/runs/{run_id}/stop")
def stop_run(run_id: int, db: Session = Depends(get_db),
             admin: models.User = Depends(require_admin)):
    message = db.get(models.AgentMessage, run_id)
    if message is None:
        raise HTTPException(status_code=404, detail="运行记录不存在")
    _my_session(db, admin, message.session_id)
    if not runtime.stop_run(run_id):
        raise HTTPException(status_code=409, detail="这次执行已经结束")
    return {"ok": True, "run_id": run_id}
