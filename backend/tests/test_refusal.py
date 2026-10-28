"""拒答行为回归测试（k55 决策：B + 统一前置文案 + 展示可用资源）。

锁三件事：
1. 模型说「资料不足」时，用户看到的是**固定前置句**开头，且不重复；
2. 拒答文案必须带「本次检索到这些资料 / 你可以试试这样问」，而且这些标题
   来自库内真实文本——不能是编的示例问题；
3. 正常回答不得被误判成拒答（带合法引用就不判），否则用户会莫名其妙收到
   「资料不足」。
"""
from __future__ import annotations

import uuid

import pytest

from app.services import answering, llm, worker


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _kb_with_doc(app_client, admin_headers, mode: str = "wiki") -> dict:
    kb = app_client.post("/api/kbs", headers=admin_headers,
                         json={"name": _unique("拒答测试库"), "description": "",
                               "mode": mode}).json()
    app_client.post(f"/api/kbs/{kb['id']}/documents", headers=admin_headers,
                    files=[("files", ("refuse.md", (
                        "# 差旅报销办法\n\n## 住宿标准\n\n一线城市住宿上限 600 元每晚。\n\n"
                        "## 交通标准\n\n高铁二等座为标准配置。\n\n"
                        "## 审批流程\n\n提交后经直属主管、财务复核。\n"
                    ).encode(), "text/markdown"))])
    while worker.run_once("test"):
        pass
    return kb


def _ask(app_client, admin_headers, kb: dict, question: str) -> dict:
    conv = app_client.post("/api/conversations", headers=admin_headers,
                           json={"title": "拒答会话", "kb_ids": [kb["id"]]}).json()
    r = app_client.post(f"/api/conversations/{conv['id']}/ask", headers=admin_headers,
                        json={"question": question})
    assert r.status_code == 200, r.text
    return r.json()


def test_model_refusal_uses_fixed_lead_and_real_suggestions(app_client, admin_headers,
                                                            monkeypatch):
    """模型拒答 → 固定前置句 + 真实资料/建议问题，且前置句只出现一次。"""
    kb = _kb_with_doc(app_client, admin_headers)

    def fake_chat(db, messages, **kwargs):
        return ("现有资料不足以回答这个问题。资料里没有关于量子计算机退相干时间的内容。"
                "如果问的是差旅住宿标准，可以再具体一些。")

    monkeypatch.setattr(llm, "chat", fake_chat)
    data = _ask(app_client, admin_headers, kb, "量子计算机的退相干时间是多少")

    assert data["insufficient"] is True
    assert data["refusal_reason"], "拒答必须给出可读原因，便于排查误判"
    content = data["content"]
    assert content.startswith(answering.REFUSAL_LEAD), content[:120]
    assert content.count(answering.REFUSAL_LEAD) == 1, (
        f"前置句重复了：{content[:200]}")

    # 建议区块必须来自库内真实文本
    assert "你可以试试这样问" in content, content[:400]
    items = data["suggestions"]
    assert items, "拒答时必须给出可问的方向，否则用户不知道下一步做什么"
    kinds = {i["kind"] for i in items}
    assert "question" in kinds
    for item in items:
        assert item["kb_name"], "建议必须标注来源知识库"
        assert item["title"], "建议标题不能为空"
        if item["kind"] == "question":
            assert item["question"].endswith("？"), item
            assert item["question"] != "量子计算机的退相干时间是多少", (
                "不能把用户刚问过的问题原样再推荐一遍")


def test_refusal_lists_hit_resources(app_client, admin_headers, monkeypatch):
    """检索命中了资料但模型仍判不足时，要把命中的资料列给用户。"""
    kb = _kb_with_doc(app_client, admin_headers)

    def fake_chat(db, messages, **kwargs):
        return "现有资料不足以回答这个问题。资料里只提到住宿上限，没有提到餐补。"

    monkeypatch.setattr(llm, "chat", fake_chat)
    data = _ask(app_client, admin_headers, kb, "加班餐补标准")

    assert data["insufficient"] is True
    content = data["content"]
    assert "本次检索到这些资料" in content, content[:400]
    resources = [i for i in data["suggestions"] if i["kind"] == "resource"]
    assert resources, f"命中的资料没有列出来：{data['suggestions']}"
    assert all(r["title"] for r in resources)


def test_hard_answer_with_citation_is_not_refused(app_client, admin_headers, monkeypatch):
    """带合法引用的正常回答不得被判成拒答。"""
    kb = _kb_with_doc(app_client, admin_headers)

    def fake_chat(db, messages, **kwargs):
        return "一线城市住宿上限为 600 元每晚，超标部分由个人承担。[1]"

    monkeypatch.setattr(llm, "chat", fake_chat)
    data = _ask(app_client, admin_headers, kb, "一线城市住宿上限是多少")

    assert data["insufficient"] is False, data.get("refusal_reason")
    assert data["content"].startswith("一线城市住宿上限")
    assert data["suggestions"] == []


def test_refusal_phrase_later_in_body_is_not_treated_as_lead(app_client, admin_headers,
                                                             monkeypatch):
    """拒答语气出现在正文后段、且带合法引用时，不算拒答。

    判定只看开头 80 字（`_LEAD_WINDOW`）。正常回答结尾补一句「资料里没提到 X」
    不该让整条回答被替换成拒答文案。
    """
    kb = _kb_with_doc(app_client, admin_headers)

    def fake_chat(db, messages, **kwargs):
        return ("一线城市住宿上限为 600 元每晚，超标部分由个人承担；其他城市为 400 元每晚。"
                "报销时需要提供住宿发票与行程单，发票抬头必须是公司全称，"
                "金额与实际入住天数一致。单据由直属主管审批后交财务复核，"
                "复核通过后由出纳在五个工作日内付款[1]。资料里没有提到加班餐补。")

    monkeypatch.setattr(llm, "chat", fake_chat)
    data = _ask(app_client, admin_headers, kb, "住宿标准和加班餐补")

    assert data["insufficient"] is False, data.get("refusal_reason")
    assert "600 元" in data["content"]


def test_half_answer_starting_with_refusal_is_still_refused(app_client, admin_headers,
                                                            monkeypatch):
    """开头就声明资料不足的「半答半拒」按拒答处理——这是刻意选择的保守方向。

    宁可多提示一次「证据不足」，也不要放过模型用资料外知识硬答。
    这条用例把该取舍固定下来，避免以后有人「顺手放宽」判定。
    """
    kb = _kb_with_doc(app_client, admin_headers)

    def fake_chat(db, messages, **kwargs):
        return "资料里没有提到加班餐补。住宿标准是一线城市 600 元每晚[1]。"

    monkeypatch.setattr(llm, "chat", fake_chat)
    data = _ask(app_client, admin_headers, kb, "住宿标准和加班餐补")

    assert data["insufficient"] is True
    assert data["content"].startswith(answering.REFUSAL_LEAD)
    assert data["content"].count(answering.REFUSAL_LEAD) == 1
    # 前置拒答句被剥离后，正文里已经给出的依据仍要保留，不能被整条丢弃
    assert "住宿标准是一线城市 600 元每晚[1]" in data["content"]


def test_empty_kb_refusal_gives_fallback_hint(app_client, admin_headers):
    """空库提问：仍以固定前置句开头，并给出可执行的下一步提示。"""
    kb = app_client.post("/api/kbs", headers=admin_headers,
                         json={"name": _unique("空库拒答"), "description": "",
                               "mode": "basic"}).json()
    data = _ask(app_client, admin_headers, kb, "公司年假有几天")

    assert data["insufficient"] is True
    assert data["content"].startswith(answering.REFUSAL_LEAD)
    assert answering.FALLBACK_HINT in data["content"]
    assert data["evidence"] == []
