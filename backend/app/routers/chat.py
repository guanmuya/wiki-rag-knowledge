"""会话与问答。权限在每次提问时重新校验，撤销授权后旧会话立即失效。"""
from __future__ import annotations

import json
import logging
import time

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models, schemas, serializers
from ..db import SessionLocal, get_db
from ..deps import audit, get_current_user, grant_level
from ..services import answering, llm, qa_pipeline, retrieval

router = APIRouter(tags=["chat"])
log = logging.getLogger("wrk.chat")

# 生成失败/被中断时给用户看的话。技术细节写进 route.error，只留在库里给排障用。
ASK_FAILED_TEXT = "这次回答没有生成完成，可以重新提问。"
# 同一会话又来了新提问时，上一条没写完的回答按「已跳过」收尾。
SUPERSEDED_TEXT = "上一次回答没有生成完成，已跳过。"
# 进程重启后，上个进程里未完成的回答不可能再写完，统一标成失败。
RECOVERED_TEXT = "上次回答没有生成完成（应用已重启），请重新提问。"


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


def _close_pending(db: Session, conversation_id: int, text: str) -> int:
    """把某会话里还没写完的回答收尾成 failed（空内容才补提示文案）。

    为什么需要：一次问答是「先落库 pending，再生成，最后改成 ok」的两段写。
    如果进程被杀、或者用户直接又提了一个新问题，旧的 pending 就再也不会更新了；
    不收尾的话前端会一直转圈，用户永远等不到结果。
    """
    rows = db.execute(
        select(models.Message).where(
            models.Message.conversation_id == conversation_id,
            models.Message.role == "assistant",
            models.Message.status == "pending",
        )
    ).scalars().all()
    for row in rows:
        row.status = "failed"
        if not (row.content or "").strip():
            row.content = text
        db.add(row)
    return len(rows)


def recover_pending_messages() -> int:
    """启动时收尾：上个进程没写完的回答标成失败，返回处理条数。

    必须在开始服务之前调用（见 main.lifespan）：这些 pending 是上一个进程的
    内存里在跑的生成，进程没了就不可能再完成，留着只会让页面一直「正在生成」。
    """
    with SessionLocal() as db:
        rows = db.execute(
            select(models.Message).where(
                models.Message.role == "assistant",
                models.Message.status == "pending",
            )
        ).scalars().all()
        for row in rows:
            row.status = "failed"
            if not (row.content or "").strip():
                row.content = RECOVERED_TEXT
            db.add(row)
        if rows:
            db.commit()
        return len(rows)


@router.get("/conversations")
def list_conversations(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    convs = db.execute(
        select(models.Conversation).where(models.Conversation.user_id == user.id)
        .order_by(models.Conversation.pinned.desc(), models.Conversation.updated_at.desc()).limit(100)
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
    if body.pinned is not None:
        conv.pinned = body.pinned
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


def _mark_answer_failed(db: Session, answer_id: int, started: float,
                        exc: BaseException) -> None:
    """生成失败时把 pending 占位收尾成 failed；本身绝不再往外抛。

    先 rollback：`answering.answer` 失败后会话可能停在「必须先回滚」的状态，
    不回滚的话这次收尾写也会失败，pending 就永远留在库里了。
    """
    log.warning("回答生成失败（消息 #%s）：%s", answer_id, exc)
    try:
        db.rollback()
        answer = db.get(models.Message, answer_id)
        if answer is None or answer.status != "pending":
            return
        answer.status = "failed"
        answer.content = ASK_FAILED_TEXT
        answer.latency_ms = int((time.time() - started) * 1000)
        answer.route = json.dumps({"error": str(exc)[:500]}, ensure_ascii=False)
        db.add(answer)
        db.commit()
    except Exception:  # noqa: BLE001 - 收尾失败不能覆盖原始异常
        log.warning("标记回答失败状态时又出错（忽略）", exc_info=True)
        try:
            db.rollback()
        except Exception:  # pragma: no cover - 连接已坏
            pass


def _prepare_turn(db: Session, conv: models.Conversation, user: models.User,
                  body: schemas.AskIn) -> tuple[list[models.KnowledgeBase], list[int],
                                                list[dict], int]:
    """校验权限并把「提问 + pending 回答」先落库，返回可检索的库与占位 id。

    两个问答入口（普通 /ask 与流式 /ask/stream）共用这段逻辑，避免一边改了
    权限校验、另一边忘了改，出现「流式入口能检索到没权限的库」这种漏洞。
    """
    requested = body.kb_ids if body.kb_ids is not None else json.loads(conv.kb_ids or "[]")
    allowed, denied = _allowed_kbs(db, user, requested)
    if not allowed:
        raise HTTPException(status_code=403, detail="当前没有可检索的知识库，请先选择有权限的知识库")

    kb_ids_json = json.dumps([kb.id for kb in allowed])
    if body.kb_ids is not None:
        conv.kb_ids = kb_ids_json

    # 提问和「正在生成」的占位先落库，再开始生成。
    # 旧实现把两条消息都留到生成结束才写：用户中途离开页面（或刷新）回来，
    # 会话里一个字都没有，看起来就像整轮问答被中止了。现在离开页面回来
    # 至少能看到「问题 + 正在生成」，生成结束再由下面的更新改成完整回答。
    _close_pending(db, conv.id, SUPERSEDED_TEXT)
    pending_answer = models.Message(
        conversation_id=conv.id, role="assistant",
        content="", kb_ids=kb_ids_json, status="pending")
    db.add_all([
        models.Message(conversation_id=conv.id, role="user",
                       content=body.question, kb_ids=kb_ids_json),
        pending_answer,
    ])
    if conv.title in {"", "新会话"}:
        conv.title = body.question.strip()[:40]
    conv.updated_at = models.utcnow()
    db.add(conv)
    db.commit()
    return allowed, [kb.id for kb in allowed], denied, pending_answer.id


def _persist_result(db: Session, answer_id: int, result: dict, denied: list[dict],
                    started: float) -> dict:
    """把链路结果写回 pending 消息；流式与非流式共用，保证历史可还原。"""
    if denied:
        result["route"]["denied"] = denied

    # route 字段同时存下 insufficient 与 usage，这样刷新页面后
    # 仍能还原「当时是否证据不足」「消耗多少模型调用」，不必重跑。
    persisted_route = dict(result["route"])
    persisted_route["insufficient"] = result["insufficient"]
    persisted_route["usage"] = result["usage"]
    # 拒答的结构化建议与判定原因同样要落库：只存 insufficient 的话，
    # 刷新页面后「本次检索到这些资料 / 你可以试试这样问」整块消失，
    # 前端只能拿「evidence 为空」去猜，猜错了就把正常回答也显示成拒答。
    # 只加键，不动已有键。
    persisted_route["suggestions"] = result.get("suggestions") or []
    persisted_route["refusal_reason"] = result.get("refusal_reason") or ""
    answer_msg = db.get(models.Message, answer_id)
    if answer_msg is None:
        raise RuntimeError(f"回答占位消息 #{answer_id} 不存在")
    answer_msg.content = result["content"]
    answer_msg.evidence = json.dumps(result["evidence"], ensure_ascii=False)
    answer_msg.route = json.dumps(persisted_route, ensure_ascii=False)
    answer_msg.latency_ms = int((time.time() - started) * 1000)
    answer_msg.status = "ok"
    conv = db.get(models.Conversation, answer_msg.conversation_id)
    if conv is not None:
        conv.updated_at = models.utcnow()
        db.add(conv)
    db.add(answer_msg)
    db.commit()

    result["message"] = serializers.message_out(answer_msg)
    result["denied"] = denied
    return result


@router.post("/conversations/{cid}/ask")
def ask(cid: int, body: schemas.AskIn, db: Session = Depends(get_db),
        user: models.User = Depends(get_current_user)):
    conv = _my_conversation(db, user, cid)
    started = time.time()
    allowed, _kb_ids, denied, answer_id = _prepare_turn(db, conv, user, body)

    try:
        result = answering.answer(db, body.question, allowed, top_k=body.top_k)
    except BaseException as exc:      # 含客户端断开导致的取消：都要把占位收尾
        _mark_answer_failed(db, answer_id, started, exc)
        raise

    return _persist_result(db, answer_id, result, denied, started)


def _sse(payload: dict) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False, default=str) + "\n\n"


@router.post("/conversations/{cid}/ask/stream")
def ask_stream(cid: int, body: schemas.AskIn, db: Session = Depends(get_db),
               user: models.User = Depends(get_current_user)):
    """流式问答：SSE 推送思考步骤、答案增量与最终结果。

    事件格式（全部是 `data: {json}`）：
      step          思考步骤开始/结束（id/title/status/detail）
      answer        回答文本增量
      answer_replace 最终正文被替换（拒答兜底 / 部分回答补说明）
      done          最终结果，字段与普通 /ask 返回一致
      error         生成失败（消息已按 failed 收尾）

    为什么用 POST + fetch 而不是 EventSource：EventSource 不能带
    `Authorization` 头，也不支持 POST body。前端用 fetch 读 ReadableStream，
    鉴权与普通接口完全一致。
    """
    conv = _my_conversation(db, user, cid)
    started = time.time()
    _allowed, kb_ids, denied, answer_id = _prepare_turn(db, conv, user, body)

    def event_stream():
        completed = False
        generator = None
        # 请求级 Session 在响应开始后可能已被关闭，生成器必须用自己的会话。
        with SessionLocal() as sdb:
            try:
                kbs = [kb for kb in (sdb.get(models.KnowledgeBase, kid) for kid in kb_ids)
                       if kb is not None]
                generator = qa_pipeline.iter_run(
                    sdb, body.question, kbs, top_k=body.top_k, stream=True)
                while True:
                    try:
                        event = next(generator)
                    except StopIteration as stop:
                        result = stop.value
                        break
                    yield _sse(event)

                result = _persist_result(sdb, answer_id, result, denied, started)
                completed = True
                yield _sse({"type": "done", **result})
            except GeneratorExit:
                # 客户端断开：回答没写完就标 failed，别让它永远 pending。
                if not completed:
                    _mark_answer_failed(sdb, answer_id, started,
                                        RuntimeError("客户端断开连接"))
                raise
            except BaseException as exc:
                _mark_answer_failed(sdb, answer_id, started, exc)
                yield _sse({"type": "error", "message": ASK_FAILED_TEXT,
                            "detail": str(exc)[:500]})
            finally:
                if generator is not None:
                    try:
                        generator.close()
                    except Exception:  # noqa: BLE001 - 关闭失败不能影响已发送内容
                        pass

    return StreamingResponse(
        event_stream(), media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            # 避免 nginx 等反代把 SSE 缓冲成一整块，导致「流式」名存实亡。
            "X-Accel-Buffering": "no",
        },
    )


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
