"""会话与问答。权限在每次提问时重新校验，撤销授权后旧会话立即失效。"""
from __future__ import annotations

import json
import time

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models, schemas, serializers
from ..db import get_db
from ..deps import audit, get_current_user, grant_level
from ..services import answering, llm, retrieval

router = APIRouter(tags=["chat"])


def _my_conversation(db: Session, user: models.User, cid: int) -> models.Conversation:
    conv = db.get(models.Conversation, cid)
    if not conv or conv.user_id != user.id:
        raise HTTPException(status_code=404, detail="会话不存在")
    return conv


def _allowed_kbs(db: Session, user: models.User, kb_ids: list[int]) -> tuple[list[models.KnowledgeBase], list[dict]]:
    """返回可用的知识库与本次被拒绝的知识库（明确告知用户，而不是静默丢弃）。"""
    allowed: list[models.KnowledgeBase] = []
    denied: list[dict] = []
    seen: set[int] = set()
    for kid in kb_ids:
        if kid in seen:
            continue
        seen.add(kid)
        kb = db.get(models.KnowledgeBase, kid)
        if not kb:
            denied.append({"kb_id": kid, "kb_name": f"#{kid}", "reason": "知识库不存在或已删除"})
            continue
        level = grant_level(db, user, kid)
        if level is None:
            denied.append({"kb_id": kid, "kb_name": kb.name, "reason": "没有该知识库的访问权限"})
            continue
        allowed.append(kb)
    return allowed, denied


@router.get("/conversations")
def list_conversations(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    convs = db.execute(
        select(models.Conversation).where(models.Conversation.user_id == user.id)
        .order_by(models.Conversation.updated_at.desc()).limit(100)
    ).scalars().all()
    counts = dict(db.execute(
        select(models.Message.conversation_id, func.count(models.Message.id))
        .where(models.Message.conversation_id.in_([c.id for c in convs] or [0]))
        .group_by(models.Message.conversation_id)
    ).all())
    return {"items": [serializers.conversation_out(c, message_count=counts.get(c.id, 0)) for c in convs],
            "total": len(convs)}


@router.post("/conversations", status_code=201)
def create_conversation(body: schemas.ConversationIn, db: Session = Depends(get_db),
                        user: models.User = Depends(get_current_user)):
    allowed, denied = _allowed_kbs(db, user, body.kb_ids)
    if body.kb_ids and not allowed:
        raise HTTPException(status_code=403, detail="所选知识库都没有访问权限")
    conv = models.Conversation(
        user_id=user.id, title=body.title.strip() or "新会话",
        kb_ids=json.dumps([kb.id for kb in allowed]),
    )
    db.add(conv)
    db.commit()
    data = serializers.conversation_out(conv)
    if denied:
        data["denied"] = denied
    return data


@router.get("/conversations/{cid}")
def get_conversation(cid: int, db: Session = Depends(get_db),
                     user: models.User = Depends(get_current_user)):
    conv = _my_conversation(db, user, cid)
    messages = db.execute(
        select(models.Message).where(models.Message.conversation_id == cid).order_by(models.Message.id)
    ).scalars().all()
    data = serializers.conversation_out(conv, message_count=len(messages))
    data["messages"] = [serializers.message_out(m) for m in messages]
    # 会话里引用的知识库当前是否仍可访问
    data["kb_access"] = []
    try:
        kb_ids = json.loads(conv.kb_ids or "[]")
    except ValueError:
        kb_ids = []
    for kid in kb_ids:
        kb = db.get(models.KnowledgeBase, kid)
        level = grant_level(db, user, kid) if kb else None
        data["kb_access"].append({
            "kb_id": kid, "kb_name": kb.name if kb else f"#{kid}",
            "accessible": level is not None, "level": level,
        })
    return data


@router.patch("/conversations/{cid}")
def update_conversation(cid: int, body: schemas.ConversationUpdateIn, db: Session = Depends(get_db),
                        user: models.User = Depends(get_current_user)):
    conv = _my_conversation(db, user, cid)
    if body.title is not None:
        conv.title = body.title.strip()[:255] or conv.title
    if body.kb_ids is not None:
        allowed, _ = _allowed_kbs(db, user, body.kb_ids)
        conv.kb_ids = json.dumps([kb.id for kb in allowed])
    db.add(conv)
    db.commit()
    return serializers.conversation_out(conv)


@router.delete("/conversations/{cid}")
def delete_conversation(cid: int, db: Session = Depends(get_db),
                        user: models.User = Depends(get_current_user)):
    conv = _my_conversation(db, user, cid)
    db.delete(conv)
    db.commit()
    return {"ok": True}


@router.post("/conversations/{cid}/ask")
def ask(cid: int, body: schemas.AskIn, db: Session = Depends(get_db),
        user: models.User = Depends(get_current_user)):
    conv = _my_conversation(db, user, cid)
    started = time.time()

    requested = body.kb_ids if body.kb_ids is not None else json.loads(conv.kb_ids or "[]")
    allowed, denied = _allowed_kbs(db, user, requested)
    if not allowed:
        raise HTTPException(status_code=403, detail="当前没有可检索的知识库，请先选择有权限的知识库")

    if body.kb_ids is not None:
        conv.kb_ids = json.dumps([kb.id for kb in allowed])
        db.add(conv)

    result = answering.answer(db, body.question, allowed, top_k=body.top_k)
    if denied:
        result["route"]["denied"] = denied

    db.add(models.Message(
        conversation_id=conv.id, role="user", content=body.question,
        kb_ids=json.dumps([kb.id for kb in allowed]),
    ))
    # route 字段同时存下 insufficient 与 usage，这样刷新页面后
    # 仍能还原「当时是否证据不足」「消耗多少模型调用」，不必重跑。
    persisted_route = dict(result["route"])
    persisted_route["insufficient"] = result["insufficient"]
    persisted_route["usage"] = result["usage"]
    answer_msg = models.Message(
        conversation_id=conv.id, role="assistant", content=result["content"],
        kb_ids=json.dumps([kb.id for kb in allowed]),
        evidence=json.dumps(result["evidence"], ensure_ascii=False),
        route=json.dumps(persisted_route, ensure_ascii=False),
        latency_ms=int((time.time() - started) * 1000),
    )
    db.add(answer_msg)
    if conv.title in {"", "新会话"}:
        conv.title = body.question.strip()[:40]
    conv.updated_at = models.utcnow()
    db.add(conv)
    db.commit()

    result["message"] = serializers.message_out(answer_msg)
    result["denied"] = denied
    return result


@router.post("/search/preview")
def search_preview(body: schemas.SearchPreviewIn, db: Session = Depends(get_db),
                   user: models.User = Depends(get_current_user)):
    allowed, denied = _allowed_kbs(db, user, body.kb_ids)
    if not allowed:
        raise HTTPException(status_code=403, detail="没有可检索的知识库")
    # 检索也有模型开销（查询向量化）。不传 stats 的话，这个接口的成本恒为 0，
    # 实验脚本用它做「分块 vs Wiki」对比时会得出「检索不花钱」的错误结论。
    stats = llm.CallStats()
    evidence, routes, stats_out = retrieval.retrieve(db, allowed, body.query, stats=stats)
    return {
        "evidence": [ev.to_dict() for ev in evidence],
        "routes": [r.to_dict() for r in routes],
        "stats": stats_out,
        "denied": denied,
        "context_preview": retrieval.build_context(evidence),
        # 只加字段：检索阶段自身的模型成本（含查询向量化）
        "usage": stats.as_dict(),
    }
