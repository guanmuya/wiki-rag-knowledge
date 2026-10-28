"""问答生成：证据组织 → 提示词 → 回答 + 引用校验。

原则：证据不足时明确说不知道，不让模型编造企业内部信息。
离线模式下退化为「抽取式回答 + 原文引用」，仍然可追溯。

拒答结构（k55 决策，方案 B：模型判定 + 统一文案，**不引入覆盖率硬阈值**）：

    现有资料不足以回答这个问题。

    <可选：缺什么 / 哪些库当前不可检索>

    本次检索到这些资料：          ← 本次真实命中的证据（heading / doc_name / page_title 去重）
    - 《知识库》文档 · 小节

    你可以试试这样问：            ← 从库内真实标题生成，默认 5 条
    1. 住宿标准是怎么规定的？

全部取不到时退化为「可以补充上传相关文档，或换一个更具体的问题。」——
建议必须来自库内真实内容，取不到就明说，不生成通用话术。
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import config, models
from . import llm, retrieval

log = logging.getLogger("wrk.answer")

SYSTEM_PROMPT = """你是企业内部知识库问答助手。严格遵守：
1. 只依据「资料」作答，不使用资料外的知识，不编造数字、人名、流程或承诺。
2. 每个结论后面用 [编号] 标注依据的资料；编号必须来自资料列表，
   不得为了凑格式而编造引用编号——编造编号比不标更糟。
3. 资料无法支撑这个问题时，必须先原样输出这一句：「现有资料不足以回答这个问题。」
   然后再用一两句话说明缺什么（缺哪类文档、缺哪个字段），
   不要用资料外的知识给出推测性答案，也不要半答半猜。
4. 只要资料确实能回答，就不要拒答；「资料里没提到某个细节」不等于整题无法回答，
   能回答的部分照常回答，缺的那一点在末尾说明即可。
5. 用中文回答，结构清晰；涉及流程时用有序步骤；涉及对比时用表格。
6. 不要复述资料原文的大段内容，要归纳。"""

# 拒答判定用的语气标记。顺序上把最具体的放在前面，便于在原因里写出命中的是哪一句。
REFUSAL_MARKERS = (
    "现有资料不足以回答",
    "资料不足以回答",
    "不足以回答这个问题",
    "不足以回答该问题",
    "资料不足",
    "未收录",
    "没有收录",
    "知识库中没有",
    "知识库未收录",
    "知识库里没有",
    "资料中没有",
    "资料里没有",
    "资料未提及",
    "资料未涉及",
    "没有相关资料",
    "无相关资料",
    "没有相关记录",
    "无法回答",
    "无法确定",
    "无法提供",
    "未找到",
    "没有找到",
    "暂无相关",
)

# 固定前置句。用户明确要求「拒答答案可以有通用的前置文本」，
# 因此所有拒答路径（模型拒答 / 无证据 / 空库）都必须以这一句开头。
REFUSAL_LEAD = "现有资料不足以回答这个问题。"

# 完全取不到真实资源与真实标题时的兜底文案（明确、可执行，不假装有内容）。
FALLBACK_HINT = "可以补充上传相关文档，或换一个更具体的问题。"


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, "") or default))
    except (TypeError, ValueError):
        return default


# 建议问题条数上限（任务要求「默认 5，可配」）。
# 放在 answering 本地而不是 config.py：config.py 由别的任务维护，这里不越界改动。
SUGGESTION_LIMIT = _env_int("WRK_SUGGESTION_LIMIT", 5)
# 「本次检索到这些资料」最多列几条，避免拒答文案比正常回答还长。
RESOURCE_LIMIT = _env_int("WRK_SUGGESTION_RESOURCE_LIMIT", 6)
# 判定「开头就是拒答」时看多少字符：模型被要求在回答最前面原样输出前置句，
# 所以只检查开头，避免正文里一句「资料里没写」把正常回答误判成拒答。
_LEAD_WINDOW = 80


# ------------------------------------------------------------------ 结构定义
@dataclass
class SuggestionBlock:
    """拒答正文 + 结构化建议。

    items 每项的字段语义（接口契约，只加字段不删）：
      kb_name  来源知识库名，前端用于标注「这条建议出自哪个库」；
      kind     "resource" = 本次检索命中的资料；"question" = 可点击填入输入框的建议问题；
      title    资料来源名（文档名 / Wiki 页面名 / 章节 heading），**一定是库内真实文本**；
      question 仅 kind="question" 时有值，前端点击后填入输入框。
    """

    text: str
    items: list[dict] = field(default_factory=list)


# ------------------------------------------------------------------ 拒答判定
def _strip_lead_noise(text: str) -> str:
    """去掉开头的空白、引用符、列表符、加粗符，便于判断「是不是以拒答开头」。"""
    return re.sub(r"^[\s>#*\-\u3000\"'“”‘’「」【】]+", "", text or "")


def _refusal_verdict(text: str, citations: dict) -> str:
    """判定这段回答是不是拒答，返回可读原因；返回空字符串表示「不是拒答」。

    为什么要重写判定（这是本次要修的漏洞）：
    旧逻辑 `(not cited) and any(marker in text)` 只要模型带一个合法引用编号就
    永远不会被判 insufficient——模型只要在硬答后面补个 [1]，拒答检测就失效了。

    新逻辑分两级，都不依赖覆盖率阈值：
    1. **开头就是拒答**：开头 80 字内命中拒答语句 → 一律判拒答，**不看引用**。
       模型被要求在回答最前面原样输出前置句，所以「带引用但开头就拒答」必须拦住。
    2. **没有任何有效引用 + 全文有拒答语气** → 判拒答（覆盖模型没按格式开头的情况）。

    已知取舍：像「资料中没有提到加班餐补，但住宿标准是 600 元[1]」这种半答半拒的
    回答也会被判为拒答（因为拒答语句出现在开头）。这是刻意选择的保守方向——
    宁可多提示一次「证据不足」，也不要放过模型用资料外知识硬答。
    """
    body = _strip_lead_noise(text)
    if not body:
        return ""
    lead = body[:_LEAD_WINDOW]
    for marker in REFUSAL_MARKERS:
        if marker in lead:
            return f"回答开头即声明资料不足（命中「{marker}」）"
    if not citations.get("cited"):
        for marker in REFUSAL_MARKERS:
            if marker in body:
                return f"没有有效引用且含拒答语气（命中「{marker}」）"
    return ""


def _strip_refusal_lead(text: str) -> str:
    """把模型回答开头的拒答句剥掉，只留下「缺什么」的说明。

    否则拼出来的文案会变成「现有资料不足以回答这个问题。现有资料不足以回答这个问题。」。
    """
    body = (text or "").strip()
    for _ in range(3):
        stripped = _strip_lead_noise(body)
        cut = False
        for marker in REFUSAL_MARKERS:
            idx = stripped.find(marker)
            if idx != 0:
                continue
            end = len(stripped)
            for sep in ("。", "！", "？", "\n"):
                pos = stripped.find(sep, idx)
                if pos != -1:
                    end = min(end, pos + 1)
            rest = stripped[end:].lstrip()
            if rest != stripped:
                body = rest
                cut = True
            break
        if not cut:
            break
    return body.strip()


# ------------------------------------------------------------------ 建议来源
def _route_notes(routes: list[retrieval.KbRoute]) -> list[str]:
    """知识库层面的可读说明：哪些库当前不可检索 / 发生了检索方式回退。"""
    lines: list[str] = []
    unusable = [r for r in routes if r.used == "none"]
    fallback = [r for r in routes if r.fallback]
    if unusable:
        lines.append("以下知识库当前不可检索：")
        lines.extend(f"- 《{r.kb_name}》：{r.reason}" for r in unusable)
    if fallback:
        lines.append("以下知识库发生了检索方式回退：")
        lines.extend(f"- 《{r.kb_name}》：{r.reason}" for r in fallback)
    return lines


def _resource_items(evidence: list[retrieval.Evidence],
                    limit: int = RESOURCE_LIMIT) -> list[dict]:
    """本次真实命中的资料：page_title / doc_name / heading 去重后列出。"""
    out: list[dict] = []
    seen: set[str] = set()
    for ev in evidence:
        page = (ev.page_title or "").strip()
        doc = (ev.doc_name or "").strip()
        head = (ev.heading or "").strip()
        name = page or doc
        # Wiki 章节的 heading 在建索引时已经拼上了页面名（wiki.py 里 page.title + " / " + heading），
        # 再拼一次会出现「差旅报销 · 差旅报销 / 住宿标准」这种重复。
        parts = [head] if (page and head.startswith(page)) else [p for p in (name, head) if p]
        title = " · ".join(parts)
        if not title:
            continue
        key = f"{ev.kb_id}|{title}"
        if key in seen:
            continue
        seen.add(key)
        out.append({"kb_name": ev.kb_name, "kind": "resource", "title": title})
        if len(out) >= limit:
            break
    return out


_TOPIC_NOISE = re.compile(
    r"^(?:第[0-9一二三四五六七八九十百]+[章节条部分篇]\s*"
    r"|[0-9]+(?:\.[0-9]+)*[.、)）]\s*"
    r"|[一二三四五六七八九十]+[、.．]\s*"
    r"|[（(][0-9一二三四五六七八九十]+[)）]\s*)"
)


def _topic_label(raw: str) -> str:
    """把标题清洗成可读题材：取 heading_path 最末一级，去掉编号前缀。"""
    text = (raw or "").strip()
    if not text:
        return ""
    if " / " in text:
        text = text.split(" / ")[-1].strip()
    text = _TOPIC_NOISE.sub("", text).strip(" ：:。·-—")
    return text


def _topic_to_question(topic: str) -> str:
    """由真实标题生成一句能直接发出去的问题（不引入标题里没有的信息）。"""
    t = topic.strip()
    if not t:
        return ""
    if t.endswith(("？", "?")):
        return t
    if any(k in t for k in ("流程", "步骤", "手续", "环节")):
        return f"{t}的流程是什么？"
    if any(k in t for k in ("标准", "要求", "规定", "规范", "制度", "政策", "办法", "细则")):
        return f"{t}是怎么规定的？"
    if any(k in t for k in ("时间", "期限", "时限", "周期", "截止")):
        return f"{t}的时限要求是什么？"
    if any(k in t for k in ("清单", "列表", "目录", "名单", "范围")):
        return f"{t}包含哪些内容？"
    return f"关于「{t}」，资料里是怎么规定的？"


def _wiki_page_titles(db: Session, kb_ids: list[int], limit: int) -> list[tuple[int, str]]:
    if not kb_ids or limit <= 0:
        return []
    rows = db.execute(
        select(models.WikiPage.kb_id, models.WikiPage.title).where(
            models.WikiPage.kb_id.in_(kb_ids),
            models.WikiPage.status != models.WikiPageStatus.draft.value,
            models.WikiPage.current_version_id.is_not(None),
        ).order_by(models.WikiPage.order_index, models.WikiPage.id).limit(limit)
    ).all()
    return [(r[0], r[1] or "") for r in rows]


def _wiki_section_topics(db: Session, kb_ids: list[int], limit: int) -> list[tuple[int, str]]:
    if not kb_ids or limit <= 0:
        return []
    live = select(models.WikiPage.current_version_id).where(
        models.WikiPage.kb_id.in_(kb_ids),
        models.WikiPage.status != models.WikiPageStatus.draft.value,
        models.WikiPage.current_version_id.is_not(None),
    )
    rows = db.execute(
        select(models.WikiSection.kb_id, models.WikiSection.heading_path).where(
            models.WikiSection.kb_id.in_(kb_ids),
            models.WikiSection.page_version_id.in_(live),
            models.WikiSection.heading_path != "",
        ).order_by(models.WikiSection.page_id, models.WikiSection.ordinal).limit(limit)
    ).all()
    return [(r[0], r[1] or "") for r in rows]


def _chunk_topics(db: Session, kb_ids: list[int], limit: int) -> list[tuple[int, str]]:
    if not kb_ids or limit <= 0:
        return []
    rows = db.execute(
        select(models.Chunk.kb_id, models.Chunk.heading).where(
            models.Chunk.kb_id.in_(kb_ids),
            models.Chunk.heading != "",
        ).order_by(models.Chunk.kb_id, models.Chunk.ordinal).limit(limit * 4)
    ).all()
    return [(r[0], r[1] or "") for r in rows]


def _question_items(db: Session, kbs: list[models.KnowledgeBase], *, limit: int,
                    skip_question: str = "") -> list[dict]:
    """从库内真实标题生成建议问题：Wiki 页面标题 → Wiki 章节 → 分块 heading。

    顺序即优先级：页面标题最像「一个可问的主题」，分块 heading 只是兜底。
    """
    if limit <= 0 or not kbs:
        return []
    kb_ids = [kb.id for kb in kbs]
    name_of = {kb.id: kb.name for kb in kbs}
    out: list[dict] = []
    seen_topic: set[str] = set()
    seen_question: set[str] = set()
    skip = (skip_question or "").strip()

    for fetch in (_wiki_page_titles, _wiki_section_topics, _chunk_topics):
        if len(out) >= limit:
            break
        for kb_id, raw in fetch(db, kb_ids, limit * 2):
            if len(out) >= limit:
                break
            topic = _topic_label(raw)
            if len(topic) < 2 or len(topic) > 40 or topic in seen_topic:
                continue
            question = _topic_to_question(topic)
            if not question or question in seen_question or question == skip:
                continue
            seen_topic.add(topic)
            seen_question.add(question)
            out.append({
                "kb_name": name_of.get(kb_id, ""),
                "kind": "question",
                "title": topic,
                "question": question,
            })
    return out[:limit]


def _suggestion_block(db: Session, kbs: list[models.KnowledgeBase],
                      evidence: list[retrieval.Evidence],
                      routes: list[retrieval.KbRoute], *,
                      detail: str = "", question: str = "") -> SuggestionBlock:
    """构造「前置拒答句 → 空行 → 你可以问什么」的完整文本与结构化建议。

    detail   插在前置句与建议之间的补充说明（模型自己说缺什么 / 哪些库不可检索）。
    question 本次用户提问，用于避免把原问题原样再推荐一遍。
    """
    resources = _resource_items(evidence)
    questions = _question_items(db, kbs, limit=SUGGESTION_LIMIT, skip_question=question)

    lines: list[str] = [REFUSAL_LEAD, ""]
    extra = (detail or "").strip()
    if extra:
        lines.extend([extra, ""])
    if resources:
        lines.append("本次检索到这些资料：")
        for item in resources:
            kb_name = item.get("kb_name") or ""
            title = item.get("title") or ""
            lines.append(f"- 《{kb_name}》{title}" if kb_name else f"- {title}")
        lines.append("")
    if questions:
        lines.append("你可以试试这样问：")
        for idx, item in enumerate(questions, start=1):
            lines.append(f"{idx}. {item['question']}")
        lines.append("")
    if not resources and not questions:
        lines.append(FALLBACK_HINT)
    return SuggestionBlock(text="\n".join(lines).strip() + "\n", items=resources + questions)


# ------------------------------------------------------------------ 抽取式降级
def _looks_like_table_row(text: str) -> bool:
    """判断一句话是不是 Markdown 表格行。

    离线抽取式回答会把整段原文按句切分后挑一句贴进列表项。表格行贴进去
    只会得到一长串 `| 假期类型 | 天数 |` 的竖线噪音（实测截图里就是这样），
    既不可读也不像回答，所以候选句里先把表格行排掉。
    """
    s = (text or "").strip()
    return s.count("|") >= 3 or bool(re.match(r"^\|?[\s:\-]*\|", s))


def _extractive_answer(question: str, evidence: list[retrieval.Evidence]) -> str:
    """离线降级：从证据中抽取最相关句子拼装，不做生成。"""
    if not evidence:
        return REFUSAL_LEAD
    terms = retrieval.query_terms(question)
    parts = ["（离线演示模式：以下内容由检索片段抽取整理，未调用生成模型）", ""]
    for idx, ev in enumerate(evidence[:4], start=1):
        sentences = re.split(r"(?<=[。！？；\n])", ev.text)
        scored = []
        for s in sentences:
            s = s.strip()
            if len(s) < 10:
                continue
            hit = sum(1 for t in terms if t in s)
            if hit:
                scored.append((hit, s))
        scored.sort(key=lambda x: -x[0])
        readable = [(hit, s) for hit, s in scored if not _looks_like_table_row(s)]
        if readable:
            body = readable[0][1]
        elif scored:
            # 整段都是表格：把竖线压成空格，至少读得懂，不丢内容
            body = re.sub(r"\s*\|\s*", " · ", ev.text.strip())[:220]
        else:
            body = ev.text.strip()[:200]
        parts.append(f"- {body} [{idx}]")
    parts.append("")
    parts.append("以上内容来自检索到的资料片段，可在右侧来源面板核对原文。")
    return "\n".join(parts)


def _citation_check(answer: str, evidence: list[retrieval.Evidence]) -> dict:
    cited = {int(m) for m in re.findall(r"\[(\d{1,2})\]", answer)}
    valid = {i for i in cited if 1 <= i <= len(evidence)}
    return {
        "cited": sorted(valid),
        "invalid": sorted(cited - valid),
        "citation_ok": bool(valid) or not evidence,
    }


# ------------------------------------------------------------------ 主流程
def answer(db: Session, question: str, kbs: list[models.KnowledgeBase], *,
           top_k: int | None = None) -> dict:
    """生成回答并返回完整成本快照。

    成本口径（修复后）：一次问答的成本 = 查询向量化 + 回答生成，两者累加到同一个
    CallStats 上。修复前查询向量化没有传 stats，导致「问答成本」少算一次 embed 调用，
    模型调用量被系统性低估；论文要对比整理代价，这个数字必须是全的。

    返回的 usage 里 chat_calls / embed_calls 分列，方便区分「生成花了多少」
    和「检索花了多少」，而不是只看到一个笼统的 calls。

    拒答时额外返回（只加字段不删）：
      suggestions     [{kb_name, kind, title, question?}]，语义见 SuggestionBlock；
      refusal_reason  判定为拒答的可读原因，便于排查「为什么这次判成证据不足」。
    """
    call_stats = llm.CallStats()
    evidence, routes, stats = retrieval.retrieve(db, kbs, question, top_k=top_k,
                                                 stats=call_stats)

    # 无证据路径与模型拒答路径共用同一套文案结构，用户看到的东西保持一致。
    if not evidence:
        block = _suggestion_block(db, kbs, [], routes,
                                  detail="\n".join(_route_notes(routes)),
                                  question=question)
        return {
            "content": block.text, "evidence": [], "route": {
                "kb_routes": [r.to_dict() for r in routes], "stats": stats},
            "usage": call_stats.as_dict(), "insufficient": True,
            "citations": {"cited": [], "invalid": [], "citation_ok": True},
            "suggestions": block.items,
            "refusal_reason": "检索没有命中任何证据",
        }

    context = retrieval.build_context(evidence)
    prompt = f"资料：\n{context}\n\n问题：{question}\n\n请依据资料回答，并在结论后用 [编号] 标注依据。"
    try:
        text = llm.chat(db, [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ], temperature=0.2, max_tokens=1500, stats=call_stats)
    except llm.ModelUnavailable as exc:
        call_stats.mode = "offline"
        call_stats.add_note(f"生成模型不可用：{exc}")
        text = _extractive_answer(question, evidence)

    text = (text or "").strip()
    if not text:
        text = _extractive_answer(question, evidence)
        call_stats.mode = "offline"
        call_stats.add_note("生成结果为空，已回退抽取式回答")

    citations = _citation_check(text, evidence)
    refusal_reason = _refusal_verdict(text, citations)
    insufficient = bool(refusal_reason)
    suggestions: list[dict] = []

    if insufficient:
        # 模型自己说的「缺什么」保留下来（去掉重复的前置句），再拼统一结构的建议区块。
        detail_parts = [_strip_refusal_lead(text)]
        notes = _route_notes(routes)
        if notes:
            detail_parts.append("\n".join(notes))
        block = _suggestion_block(db, kbs, evidence, routes,
                                  detail="\n\n".join(p for p in detail_parts if p),
                                  question=question)
        text = block.text
        suggestions = block.items

    return {
        "content": text,
        "evidence": [ev.to_dict() for ev in evidence],
        "route": {"kb_routes": [r.to_dict() for r in routes], "stats": stats},
        "usage": call_stats.as_dict(),
        "insufficient": insufficient,
        "citations": citations,
        "suggestions": suggestions,
        "refusal_reason": refusal_reason,
    }
