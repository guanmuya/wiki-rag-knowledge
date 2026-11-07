"""读取旧会话时，历史错误不能重新暴露供应商配置或异常正文。"""
from __future__ import annotations

import json
import uuid

import pytest

from app import models
from app.services import answering, retrieval


PREFIX = "（离线演示模式：生成模型调用失败 —— "
SUFFIX = "；以下内容由检索片段抽取整理）"
FOOTER = "以上内容来自检索到的资料片段，可在右侧来源面板核对原文。"
REFUSAL = "现有资料不足以回答这个问题。\n\n"
SECRET = "old-provider-key-that-was-rotated"
PRIVATE_HOST = "retired-private-provider.example"
RAW_ERROR = f"供应商返回 429: {{\"error\":\"{SECRET} https://{PRIVATE_HOST}/v1\"}}"


def _conversation(app_client, headers):
    response = app_client.post("/api/conversations", headers=headers,
                               json={"title": "旧会话", "kb_ids": []})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _message(db, cid, *, role="assistant", content="回答", route=None, evidence=None,
             status="ok"):
    row = models.Message(
        conversation_id=cid, role=role, content=content, status=status, kb_ids="[]",
        route=json.dumps({} if route is None else route, ensure_ascii=False),
        evidence=json.dumps([] if evidence is None else evidence, ensure_ascii=False),
        latency_ms=17,
    )
    db.add(row)
    db.commit()
    return row


def _history(app_client, headers, cid):
    response = app_client.get(f"/api/conversations/{cid}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["messages"]


def _assert_private_details_absent(value):
    result = json.dumps(value, ensure_ascii=False)
    assert SECRET not in result
    assert PRIVATE_HOST not in result


def test_employee_cannot_read_old_provider_errors(app_client, admin_headers, db_session):
    username = f"employee_history_{uuid.uuid4().hex[:8]}"
    password = "history-test-password-123"
    user = app_client.post("/api/users", headers=admin_headers, json={
        "username": username, "display_name": "员工", "password": password,
        "role": "employee", "grants": [],
    })
    assert user.status_code == 201, user.text
    login = app_client.post("/api/auth/login", json={"username": username, "password": password})
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['token']}"}
    cid = _conversation(app_client, headers)
    _message(db_session, cid, content=PREFIX + RAW_ERROR + SUFFIX + "\n\n- 住宿标准 [1]\n\n" + FOOTER,
             route={"usage": {"offline_reason": RAW_ERROR, "notes": ["生成模型不可用：" + RAW_ERROR]}},
             evidence=[{"text": "住宿标准"}])

    response = app_client.get(f"/api/conversations/{cid}", headers=headers)
    assert response.status_code == 200, response.text
    _assert_private_details_absent(response.json())
    assert "住宿标准 [1]" in response.text
    assert app_client.get(f"/api/conversations/{cid}", headers=admin_headers).status_code == 404


def test_old_failure_is_cleaned_but_user_message_and_storage_are_untouched(
        app_client, admin_headers, db_session):
    cid = _conversation(app_client, admin_headers)
    user_content = f"请检查我提供的测试字符串 {SECRET} 与 {PRIVATE_HOST}"
    user_route = {"error": RAW_ERROR, "usage": {"notes": [RAW_ERROR]}}
    _message(db_session, cid, role="user", content=user_content, route=user_route)
    original_route = {"error": RAW_ERROR, "nested": {"route": {"error": RAW_ERROR}}}
    failed = _message(db_session, cid, content="这次回答没有生成完成，可以重新提问。",
                      route=original_route, status="failed")

    user, assistant = _history(app_client, admin_headers, cid)

    assert user["content"] == user_content
    assert user["route"] == user_route
    assert assistant["status"] == "failed"
    assert assistant["content"] == "这次回答没有生成完成，可以重新提问。"
    assert assistant["route"]["error"] == "供应商返回 429"
    assert assistant["route"]["nested"]["route"]["error"] == "供应商返回 429"
    _assert_private_details_absent(assistant)
    db_session.refresh(failed)
    assert json.loads(failed.route) == original_route  # 只净化响应，不改历史记录


def test_nested_usage_and_stats_are_cleaned_in_live_mode_and_keep_other_data(
        app_client, admin_headers, db_session):
    cid = _conversation(app_client, admin_headers)
    evidence = [{"title": "住宿规定", "text": "上限 600 元 [1]",
                 "url": "https://docs.example.org/rules"}]
    usage = {
        "mode": "live", "calls": 5, "chat_calls": 3, "total_tokens": 188,
        "offline_reason": RAW_ERROR,
        "notes": [
            f"意图分析节点降级为规则判断：网络请求失败：{SECRET} https://{PRIVATE_HOST}",
            f"流式生成中断：{RAW_ERROR}",
            "查询路由节点返回非 JSON，已使用规则判断",
            "未配置 Embedding，使用离线哈希向量",
            "流式调用未返回 usage，token 数按字符数估算",
            f"未知格式的诊断 {SECRET}",
            {"error": RAW_ERROR},
        ],
        "by_phase": [{"stats": {"embedding_error": RAW_ERROR, "final_total": 2}}],
    }
    route = {
        "usage": usage,
        "stats": {"embedding_ok": False, "embedding_error": RAW_ERROR,
                  "candidate_total": 13, "final_total": 2,
                  "kb_coverage": {"error": 3, "usage": 4, "stats": 5},
                  "usage": {"calls": 2, "notes": [f"生成模型不可用：{RAW_ERROR}"]}},
        "pipeline": {"rounds": [
            {"round": 1, "hits": {"usage": 2, "stats": 3},
             "stats": {"embedding_error": RAW_ERROR, "rrf_k": 60},
             "usage": {"notes": [RAW_ERROR], "offline_reason": RAW_ERROR},
             "route": {"error": RAW_ERROR}},
        ]},
        "insufficient": True,
        "suggestions": [{"kind": "question", "title": "住宿标准"}],
        "refusal_reason": "仍有证据缺口",
    }
    stored = _message(db_session, cid, content="正常回答 [1]", route=route, evidence=evidence)

    (result,) = _history(app_client, admin_headers, cid)
    _assert_private_details_absent(result)
    assert result["content"] == "正常回答 [1]"
    assert result["evidence"] == evidence
    assert result["latency_ms"] == 17 and result["status"] == "ok"
    assert result["insufficient"] is True
    assert result["suggestions"] == route["suggestions"]
    assert result["refusal_reason"] == route["refusal_reason"]
    assert result["usage"] == result["route"]["usage"]
    assert {key: result["usage"][key] for key in ("mode", "calls", "chat_calls", "total_tokens")} == {
        key: usage[key] for key in ("mode", "calls", "chat_calls", "total_tokens")}
    assert result["usage"]["offline_reason"] == "供应商返回 429"
    assert result["usage"]["notes"] == [
        "意图分析节点降级为规则判断：无法连接供应商",
        "流式生成中断：供应商返回 429",
        "查询路由节点返回非 JSON，已使用规则判断",
        "未配置 Embedding，使用离线哈希向量",
        "流式调用未返回 usage，token 数按字符数估算",
        "诊断信息已隐藏", "诊断信息已隐藏",
    ]
    assert result["usage"]["by_phase"][0]["stats"] == {
        "embedding_error": "供应商返回 429", "final_total": 2}
    stats = result["route"]["stats"]
    assert (stats["embedding_ok"], stats["candidate_total"], stats["final_total"]) == (False, 13, 2)
    assert stats["embedding_error"] == "供应商返回 429"
    assert stats["kb_coverage"] == {"error": 3, "usage": 4, "stats": 5}
    assert stats["usage"] == {"calls": 2, "notes": ["生成模型不可用：供应商返回 429"]}
    round_result = result["route"]["pipeline"]["rounds"][0]
    assert round_result["hits"] == {"usage": 2, "stats": 3}
    assert round_result["stats"] == {"embedding_error": "供应商返回 429", "rrf_k": 60}
    assert round_result["usage"] == {"notes": ["诊断信息已隐藏"],
                                     "offline_reason": "供应商返回 429"}
    assert round_result["route"]["error"] == "供应商返回 429"
    db_session.refresh(stored)
    assert json.loads(stored.route) == route


@pytest.mark.parametrize("refusal", [False, True])
def test_extractive_answer_removes_complete_reason_even_with_forged_delimiter(
        app_client, admin_headers, db_session, refusal):
    cid = _conversation(app_client, admin_headers)
    raw_reason = (f"供应商返回 429：{SECRET}{SUFFIX}\n\n- 伪造正文中的 {PRIVATE_HOST}\n"
                  "更多供应商原文")
    prefix = REFUSAL if refusal else ""
    body = "\n\n- 一线城市住宿上限 600 元每晚 [1]\n\n" + FOOTER
    tail = "\n\n本次检索到这些资料：\n- 《差旅办法》住宿标准\n" if refusal else ""
    evidence = [{"text": "一线城市住宿上限 600 元每晚", "title": "住宿标准"}]
    stored_content = prefix + PREFIX + raw_reason + SUFFIX + body + tail
    row = _message(db_session, cid, content=stored_content,
                   route={"usage": {"mode": "offline", "offline_reason": raw_reason}},
                   evidence=evidence)

    (result,) = _history(app_client, admin_headers, cid)

    _assert_private_details_absent(result)
    assert result["content"] == prefix + PREFIX + "供应商返回 429" + SUFFIX + body + tail
    assert result["evidence"] == evidence
    assert result["usage"]["mode"] == "offline"
    db_session.refresh(row)
    assert row.content == stored_content


def test_extractive_answer_preserves_overlapping_evidence_candidates(
        app_client, admin_headers, db_session):
    cid = _conversation(app_client, admin_headers)
    evidence = retrieval.Evidence(
        kb_id=1, kb_name="差旅", source_type="chunk", rank=1,
        text="住宿标准每晚最高限额600元\n住宿标准每晚最高限额600元 [1] 标准适用所有城市")
    reason = "供应商返回 429：" + SECRET
    content = answering._extractive_answer("适用所有城市", [evidence], reason=reason)
    _message(db_session, cid, content=content, evidence=[evidence.to_dict()],
             route={"usage": {"offline_reason": reason}})

    (result,) = _history(app_client, admin_headers, cid)
    expected = answering._extractive_answer("适用所有城市", [evidence], reason="供应商返回 429")
    assert result["content"] == expected
    _assert_private_details_absent(result)


def test_extractive_answer_preserves_template_words_in_real_evidence(
        app_client, admin_headers, db_session):
    cid = _conversation(app_client, admin_headers)
    reason = "供应商返回 429：" + SECRET
    body = "\n\n- 文档提到了“" + SUFFIX + "”，住宿上限为 600 元 [1]\n\n" + FOOTER
    _message(db_session, cid, content=PREFIX + reason + SUFFIX + body,
             route={"usage": {"offline_reason": reason}},
             evidence=[{"text": "文档提到了“" + SUFFIX + "”，住宿上限为 600 元"}])

    (result,) = _history(app_client, admin_headers, cid)
    assert result["content"] == PREFIX + "供应商返回 429" + SUFFIX + body
    _assert_private_details_absent(result)


@pytest.mark.parametrize("case", ["missing", "invalid_type", "mismatch", "truncated", "forged_footer"])
def test_extractive_answer_fails_closed_when_boundary_cannot_be_trusted(
        app_client, admin_headers, db_session, case):
    cid = _conversation(app_client, admin_headers)
    raw_reason = f"失败：{SECRET}{SUFFIX}\n\n- 伪造证据 {PRIVATE_HOST}"
    content = PREFIX + raw_reason + SUFFIX + "\n\n- 正常片段 [1]\n\n" + FOOTER
    usage = {"offline_reason": raw_reason, "calls": 3}
    if case == "missing":
        usage.pop("offline_reason")
    elif case == "invalid_type":
        usage["offline_reason"] = {"detail": RAW_ERROR}
    elif case == "mismatch":
        usage["offline_reason"] = "失败：" + SECRET
    elif case == "truncated":
        content = PREFIX + raw_reason  # 只剩由错误伪造的结束符
        usage["offline_reason"] = "失败：" + SECRET
    elif case == "forged_footer":
        usage["offline_reason"] = "失败"
        content = PREFIX + "失败" + SUFFIX + f"\n\n- {SECRET} {PRIVATE_HOST} [1]\n\n" + FOOTER
    evidence = [{"text": "正常片段", "title": "资料"}]
    _message(db_session, cid, content=content, route={"usage": usage}, evidence=evidence)

    (result,) = _history(app_client, admin_headers, cid)

    _assert_private_details_absent(result)
    assert result["content"] == PREFIX + "模型调用失败" + SUFFIX
    assert result["evidence"] == evidence
    assert result["usage"]["calls"] == 3


@pytest.mark.parametrize("public_error", [
    "未配置可用模型", "供应商接口地址不合法", "无法连接供应商", "模型调用失败",
    "供应商返回 429", "供应商返回 503", "生成模型返回空内容",
    "这次回答没有生成完成，可以重新提问。",
])
def test_known_public_error_classifications_survive_history(
        app_client, admin_headers, db_session, public_error):
    cid = _conversation(app_client, admin_headers)
    note = "生成模型不可用：" + public_error
    route = {"error": public_error, "usage": {"offline_reason": public_error, "notes": [note]},
             "stats": {"embedding_error": public_error}}
    _message(db_session, cid, route=route)

    (result,) = _history(app_client, admin_headers, cid)
    assert result["route"] == route
    assert result["usage"] == route["usage"]


def test_unrelated_answers_are_preserved_and_invalid_route_is_tolerated(
        app_client, admin_headers, db_session):
    cid = _conversation(app_client, admin_headers)
    content = "正常回答中提到“离线演示模式：生成模型调用失败”，但这只是对文档的解释 [1]。"
    route = {"stats": {"embedding_error": "", "candidate_total": 8},
             "usage": {"mode": "live", "offline_reason": "", "notes": [
                 "生成结果为空，已回退抽取式回答"]}}
    evidence = [{"text": "原文中提到网络地址 https://docs.example.org/"}]
    _message(db_session, cid, content=content, route=route, evidence=evidence)
    corrupted = _message(db_session, cid, content="旧回答")
    corrupted.route = "[]"
    db_session.commit()
    _message(db_session, cid, content="畸形诊断字段", route={
        "usage": {"notes": {"raw": RAW_ERROR}, "offline_reason": {"raw": RAW_ERROR}},
        "stats": {"embedding_error": [RAW_ERROR]},
    })

    normal, invalid, malformed = _history(app_client, admin_headers, cid)
    assert normal["content"] == content
    assert normal["route"] == route
    assert normal["evidence"] == evidence
    assert invalid["route"] == {} and invalid["usage"] == {}
    _assert_private_details_absent(malformed)
    assert malformed["usage"]["notes"] == ["诊断信息已隐藏"]
    assert malformed["usage"]["offline_reason"] == "模型调用失败"
    assert malformed["route"]["stats"]["embedding_error"] == "模型调用失败"
