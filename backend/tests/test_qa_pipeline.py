"""检索链路与流式思考过程回归测试。

覆盖四件事：
1. 意图分析：四类问题走不同路径，闲聊不进入 RAG；
2. 查询路由：original / rewrite / multi_query 三种策略；
3. 覆盖度评估与重试：证据不足时只重写缺失查询，最多 2 次；
4. SSE：逐步推送思考过程，最终结果与普通 /ask 同形并落库。
"""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import delete

from app import models
from app.services import llm, qa_pipeline, worker

DOC_MD = """# 差旅与审批

## 住宿标准

一线城市住宿上限 600 元每晚，其他城市 400 元每晚。

## 审批流程

提交人提交单据后，依次经过直属主管、部门负责人、财务复核，最后出纳付款。
"""


@pytest.fixture(autouse=True)
def _force_offline(db_session):
    """清掉前面用例留下的默认模型，保证链路测试不依赖网络与端口状态。

    没有模型时意图/路由/评估走规则判断、回答走抽取式，正好覆盖离线降级路径；
    需要模型节点的用例自己 monkeypatch `llm.chat`。
    """
    db_session.execute(delete(models.ModelConfig))
    db_session.commit()


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _kb_with_doc(app_client, admin_headers) -> dict:
    kb = app_client.post("/api/kbs", headers=admin_headers, json={
        "name": _unique("链路测试库"), "description": "", "mode": "basic"}).json()
    app_client.post(f"/api/kbs/{kb['id']}/documents", headers=admin_headers,
                    files=[("files", ("qa-pipeline.md", DOC_MD.encode(), "text/markdown"))])
    while worker.run_once("test"):
        pass
    return kb


def _conversation(app_client, admin_headers, kb: dict) -> int:
    return app_client.post("/api/conversations", headers=admin_headers, json={
        "title": "链路会话", "kb_ids": [kb["id"]]}).json()["id"]


def _events(text: str) -> list[dict]:
    events: list[dict] = []
    for block in text.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


def _stream(app_client, admin_headers, cid: int, question: str) -> list[dict]:
    response = app_client.post(f"/api/conversations/{cid}/ask/stream",
                               headers=admin_headers, json={"question": question})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    return _events(response.text)


# ---------------------------------------------------------------- 规则兜底
def test_heuristic_intent_covers_four_labels():
    assert qa_pipeline._heuristic_intent("你好").label == "闲聊"
    assert qa_pipeline._heuristic_intent("工单号 123456 的状态").label == "精确标识查询"
    assert qa_pipeline._heuristic_intent("报销审批流程是什么").label == "流程咨询"
    assert qa_pipeline._heuristic_intent("住宿标准是多少").label == "规范查询"


def test_heuristic_route_keeps_single_compound_question_together():
    """「住宿标准和加班餐补」是一句完整问题，不能按「和」拆成两个查询。"""
    plan = qa_pipeline._heuristic_plan("住宿标准和加班餐补")
    assert plan.mode == "original"
    assert plan.queries == ["住宿标准和加班餐补"]

    multi = qa_pipeline._heuristic_plan("住宿标准是多少？审批流程是什么？")
    assert multi.mode == "multi_query"
    assert multi.queries == ["住宿标准是多少", "审批流程是什么"]

    rewrite = qa_pipeline._heuristic_plan("请问住宿标准是多少")
    assert rewrite.mode == "rewrite"
    assert rewrite.queries == ["住宿标准是多少"]


# ---------------------------------------------------------------- 流式链路
def test_stream_emits_steps_and_persists_pipeline(app_client, admin_headers):
    kb = _kb_with_doc(app_client, admin_headers)
    cid = _conversation(app_client, admin_headers, kb)

    events = _stream(app_client, admin_headers, cid, "一线城市住宿上限是多少？")
    types = [event["type"] for event in events]
    assert "step" in types, events
    assert "answer" in types, "流式回答必须逐段推送 answer 事件"
    assert events[-1]["type"] == "done", events[-1]
    done = events[-1]
    assert done["evidence"], "回答必须带证据"
    pipeline = done["route"]["pipeline"]
    assert pipeline["intent"]["label"] in qa_pipeline.INTENT_LABELS
    assert pipeline["route"]["mode"] in qa_pipeline.ROUTE_MODES
    assert pipeline["steps"], "思考步骤必须随结果落库"
    step_ids = {step["id"] for step in pipeline["steps"]}
    assert "intent" in step_ids and "route" in step_ids and "answer" in step_ids
    assert all(step["status"] == "done" for step in pipeline["steps"]), pipeline["steps"]

    detail = app_client.get(f"/api/conversations/{cid}", headers=admin_headers).json()
    assistant = detail["messages"][-1]
    assert assistant["status"] == "ok"
    assert assistant["route"]["pipeline"]["steps"], "刷新后必须能还原思考过程"
    assert assistant["route"]["pipeline"]["route"]["mode"] == pipeline["route"]["mode"]


def test_chitchat_skips_retrieval(app_client, admin_headers):
    kb = _kb_with_doc(app_client, admin_headers)
    cid = _conversation(app_client, admin_headers, kb)

    events = _stream(app_client, admin_headers, cid, "你好")
    done = events[-1]
    assert done["type"] == "done"
    assert done["route"]["pipeline"]["mode"] == "chitchat"
    step_ids = [event.get("id") for event in events if event["type"] == "step"]
    assert not any(str(step_id).startswith("retrieve") for step_id in step_ids), step_ids
    assert "知识库问答助手" in done["message"]["content"]


def test_insufficient_evidence_rewrites_and_retries(app_client, admin_headers):
    """库内没有加班餐补资料：必须触发重写与重试，而不是一次检索就放弃。"""
    kb = _kb_with_doc(app_client, admin_headers)
    cid = _conversation(app_client, admin_headers, kb)

    events = _stream(app_client, admin_headers, cid, "加班餐补标准是多少？")
    done = events[-1]
    pipeline = done["route"]["pipeline"]
    assert pipeline["evaluation"]["sufficient"] is False
    assert pipeline["retries"] >= 1, pipeline
    assert len(pipeline["rounds"]) >= 2, pipeline["rounds"]
    assert len(pipeline["rounds"][0]["queries"]) == 1


# ---------------------------------------------------------------- 模型节点
def test_model_nodes_drive_intent_route_and_evaluation(app_client, admin_headers,
                                                       monkeypatch):
    """分析节点可用时，意图/路由/评估结果要真正进入链路，而不是被规则覆盖。"""
    kb = _kb_with_doc(app_client, admin_headers)
    cid = _conversation(app_client, admin_headers, kb)

    def fake_chat(db, messages, **kwargs):
        system = messages[0].get("content") or ""
        if kwargs.get("json_mode"):
            if "意图分析节点" in system:
                return json.dumps({"label": "流程咨询", "reason": "问审批步骤"},
                                  ensure_ascii=False)
            if "查询路由节点" in system:
                return json.dumps({
                    "mode": "multi_query",
                    "queries": ["住宿标准", "审批流程"],
                    "reason": "两个子问题",
                }, ensure_ascii=False)
            if "覆盖度评估节点" in system:
                return json.dumps({"sufficient": True, "missing_queries": [],
                                   "reason": "两个查询都有证据"}, ensure_ascii=False)
        return "一线城市住宿上限 600 元每晚[1]。"

    monkeypatch.setattr(llm, "chat", fake_chat)
    response = app_client.post(f"/api/conversations/{cid}/ask", headers=admin_headers,
                               json={"question": "住宿标准和审批流程分别是什么"})
    assert response.status_code == 200, response.text
    data = response.json()
    pipeline = data["route"]["pipeline"]
    assert pipeline["intent"]["label"] == "流程咨询"
    assert pipeline["intent"]["source"] == "model"
    assert pipeline["route"]["mode"] == "multi_query"
    assert pipeline["route"]["queries"] == ["住宿标准", "审批流程"]
    assert pipeline["route"]["source"] == "model"
    assert pipeline["rounds"][0]["queries"] == ["住宿标准", "审批流程"]
    assert pipeline["evaluation"]["source"] == "model"
    assert pipeline["evaluation"]["sufficient"] is True
    assert data["insufficient"] is False
