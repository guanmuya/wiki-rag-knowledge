"""问答会话在离开页面后仍能继续：提问先落库、回答带状态、失败/中断可收尾。

背景（用户报的缺陷）：旧实现把「提问」和「回答」两条消息都留到生成结束才写库。
生成要等模型（本地 Ollama 可能几十秒到几分钟），用户中途切到别的页面再回来，
会话里一个字都没有，看起来就像整轮问答被中止了——只能守在页面上等。

修复后：
1. 提问与一条 status='pending' 的占位回答先落库，生成结束再更新成 ok；
2. 生成失败/进程重启留下的 pending 会被收尾成 failed，不会永远「正在生成」；
3. 同一会话来了新提问时，上一条没写完的回答按「已跳过」收尾。
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app import models
from app.db import SessionLocal
from app.routers import chat as chat_router

CANNED = {
    "content": "一线城市住宿上限 600 元每晚 [1]。",
    "evidence": [],
    "route": {"kb_routes": [], "stats": {}},
    "usage": {"calls": 0},
    "insufficient": False,
    "citations": {"cited": [1], "invalid": [], "citation_ok": True},
    "suggestions": [],
    "refusal_reason": "",
}


def _new_conversation(app_client, admin_headers, name: str) -> tuple[dict, dict]:
    kb = app_client.post("/api/kbs", headers=admin_headers, json={
        "name": name, "description": "", "mode": "basic"}).json()
    conv = app_client.post("/api/conversations", headers=admin_headers, json={
        "title": "新会话", "kb_ids": [kb["id"]]}).json()
    return kb, conv


def _messages(app_client, admin_headers, cid: int) -> list[dict]:
    detail = app_client.get(f"/api/conversations/{cid}", headers=admin_headers).json()
    return detail["messages"]


def _insert_pending(conversation_id: int, content: str = "") -> int:
    with SessionLocal() as db:
        row = models.Message(
            conversation_id=conversation_id, role="assistant",
            content=content, kb_ids="[]", status="pending")
        db.add(row)
        db.commit()
        return row.id


def test_question_and_pending_answer_are_visible_while_generating(
        app_client, admin_headers, monkeypatch):
    """生成还没结束时，另一个连接就能读到「提问 + pending 回答」。"""
    kb, conv = _new_conversation(app_client, admin_headers, "会话恢复-可见性")
    seen: list[tuple[str, str]] = []

    def fake_answer(db, question, kbs, **kwargs):
        # 独立会话读取：等价于用户在生成过程中刷新/重进会话页发出的 GET
        with SessionLocal() as probe:
            rows = probe.execute(
                select(models.Message)
                .where(models.Message.conversation_id == conv["id"])
                .order_by(models.Message.id)
            ).scalars().all()
            seen.extend((r.role, r.status) for r in rows)
        return dict(CANNED)

    monkeypatch.setattr(chat_router.answering, "answer", fake_answer)
    r = app_client.post(f"/api/conversations/{conv['id']}/ask", headers=admin_headers,
                        json={"question": "住宿标准是多少？", "kb_ids": [kb["id"]]})
    assert r.status_code == 200, r.text

    assert seen == [("user", "ok"), ("assistant", "pending")], \
        f"生成开始时库里应有提问与 pending 回答，实际 {seen}"

    messages = _messages(app_client, admin_headers, conv["id"])
    assert [(m["role"], m["status"]) for m in messages] == [("user", "ok"), ("assistant", "ok")]
    assert messages[0]["content"] == "住宿标准是多少？"
    assert messages[1]["content"] == CANNED["content"]
    assert r.json()["message"]["status"] == "ok"


def test_generation_failure_keeps_question_and_marks_answer_failed(
        app_client, admin_headers, monkeypatch):
    """生成失败不能把提问一起丢掉，也不能留下永远 pending 的占位。"""
    kb, conv = _new_conversation(app_client, admin_headers, "会话恢复-失败")

    def boom(db, question, kbs, **kwargs):
        raise RuntimeError("模型连接被重置")

    monkeypatch.setattr(chat_router.answering, "answer", boom)
    with pytest.raises(RuntimeError):
        app_client.post(f"/api/conversations/{conv['id']}/ask", headers=admin_headers,
                        json={"question": "会失败的提问", "kb_ids": [kb["id"]]})

    messages = _messages(app_client, admin_headers, conv["id"])
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "会失败的提问", "提问必须保留，否则用户重进会话就看不到问过什么"
    assert messages[1]["status"] == "failed"
    assert messages[1]["content"] == chat_router.ASK_FAILED_TEXT
    assert "模型连接被重置" in messages[1]["route"].get("error", "")


def test_new_ask_supersedes_stale_pending(app_client, admin_headers, monkeypatch):
    """同一会话里上一条没写完的回答，会被新提问收尾成 failed。"""
    kb, conv = _new_conversation(app_client, admin_headers, "会话恢复-顶替")
    stale_id = _insert_pending(conv["id"])

    monkeypatch.setattr(chat_router.answering, "answer",
                        lambda db, question, kbs, **kwargs: dict(CANNED))
    r = app_client.post(f"/api/conversations/{conv['id']}/ask", headers=admin_headers,
                        json={"question": "新的提问", "kb_ids": [kb["id"]]})
    assert r.status_code == 200, r.text

    messages = _messages(app_client, admin_headers, conv["id"])
    stale = next(m for m in messages if m["id"] == stale_id)
    assert stale["status"] == "failed"
    assert stale["content"] == chat_router.SUPERSEDED_TEXT
    assert messages[-1]["status"] == "ok"


def test_startup_recovery_marks_pending_as_failed(app_client, admin_headers):
    """进程重启后，上个进程没写完的回答必须收尾，不能永远「正在生成」。"""
    _kb, conv = _new_conversation(app_client, admin_headers, "会话恢复-重启")
    pending_id = _insert_pending(conv["id"])

    recovered = chat_router.recover_pending_messages()
    assert recovered >= 1

    messages = _messages(app_client, admin_headers, conv["id"])
    row = next(m for m in messages if m["id"] == pending_id)
    assert row["status"] == "failed"
    assert row["content"] == chat_router.RECOVERED_TEXT
    # 再跑一次不应重复处理
    assert chat_router.recover_pending_messages() == 0
