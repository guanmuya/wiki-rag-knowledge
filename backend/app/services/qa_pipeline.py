"""问答检索链路：意图分析 → 查询路由 → 检索 → 覆盖度评估 → 重试 → 回答生成。

这是产品设计里的 LangGraph StateGraph 在现有 FastAPI 服务里的落地实现。节点
与边的关系固定为：

    意图分析 ──闲聊──→ 通用回复（不检索）
        │
        └→ 查询路由 ──original / rewrite / multi_query──→ 检索
                                                       │
                                              覆盖度评估 ──充分──→ 回答生成
                                                   │
                                              不足 → 重写缺失查询（最多 2 次）
                                                       │
                                              达到上限 → 部分回答 / 拒答兜底

为什么单独成模块而不是塞进 `answering.py`：
1. 检索链路有自己的状态机（轮次、重试、缺失查询），和「拒答文案/引用校验」
   是两类职责；混在一起会让两边都难改。
2. 链路需要边跑边推送状态（SSE 思考过程），用生成器 `iter_run()` 表达最自然；
   `answering.answer()` 只是消费它并丢弃事件，保持原有非流式接口不变。

离线降级：没有可用生成模型时，意图/路由/评估节点退回规则判断，回答退回
`answering._extractive_answer()` 的抽取式回答——链路仍然完整可跑，前端仍能
看到每一步在做什么，不会因为没配模型就整块不可用。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Iterator

from sqlalchemy.orm import Session

from .. import config, models
from . import answering, llm, retrieval

log = logging.getLogger("wrk.pipeline")

INTENT_LABELS = ("规范查询", "流程咨询", "精确标识查询", "闲聊")
ROUTE_MODES = ("original", "rewrite", "multi_query")
MAX_RETRIES = 2
MAX_QUERIES = 4
MAX_REWRITE_QUERIES = 3
# 单条查询「有相关证据」的最低覆盖率。跨库的 coverage 是查询词覆盖率（0~1），
# 与库大小、检索通道无关，适合做这条判断；阈值偏保守，宁可多评估一轮，
# 也不要把「只命中一个常见词」当成证据充分。
COVERAGE_OK = 0.25
COVERAGE_WEAK = 0.15


# ------------------------------------------------------------------ 结构定义
@dataclass
class Intent:
    label: str
    needs_retrieval: bool
    reason: str
    source: str = "rule"

    def to_dict(self) -> dict:
        return {"label": self.label, "needs_retrieval": self.needs_retrieval,
                "reason": self.reason, "source": self.source}


@dataclass
class RoutePlan:
    mode: str
    queries: list[str]
    reason: str
    source: str = "rule"

    def to_dict(self) -> dict:
        return {"mode": self.mode, "queries": list(self.queries),
                "reason": self.reason, "source": self.source}


@dataclass
class Evaluation:
    sufficient: bool
    missing_queries: list[str] = field(default_factory=list)
    reason: str = ""
    source: str = "rule"

    def to_dict(self) -> dict:
        return {"sufficient": self.sufficient,
                "missing_queries": list(self.missing_queries),
                "reason": self.reason, "source": self.source}


class _StepLog:
    """把「思考步骤」既发给前端（事件）又留在结果里（历史消息可还原）。

    同 id 的步骤只保留最新状态：前端先收到 running，再收到 done，不会出现
    两行「理解问题」。历史消息读到的就是最终状态列表。
    """

    def __init__(self) -> None:
        self.items: list[dict] = []

    def _event(self, step_id: str, title: str, status: str, detail: str = "") -> dict:
        item = {"id": step_id, "title": title, "status": status, "detail": detail}
        for index, old in enumerate(self.items):
            if old.get("id") == step_id:
                self.items[index] = item
                break
        else:
            self.items.append(item)
        return {"type": "step", **item}

    def start(self, step_id: str, title: str, detail: str = "") -> dict:
        return self._event(step_id, title, "running", detail)

    def done(self, step_id: str, title: str, detail: str = "") -> dict:
        return self._event(step_id, title, "done", detail)


# ------------------------------------------------------------------ 通用工具
def _clean_question(question: str) -> str:
    return re.sub(r"\s+", " ", (question or "").strip())


def _parse_json_object(text: str) -> dict | None:
    """从模型输出里取 JSON 对象；兼容 ```json 代码块和前后解释文字。"""
    raw = (text or "").strip()
    if not raw:
        return None
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I | re.S).strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(raw[start:end + 1])
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _chat_json(db: Session, messages: list[dict], call_stats: llm.CallStats, *,
               cfg_override: models.ModelConfig | None, note: str) -> dict | None:
    """分析节点的模型调用：失败/非 JSON 一律返回 None，由调用方走规则判断。

    这里刻意不把 `call_stats.mode` 标成 offline：分析节点降级不代表回答生成
    也降级。只有最终回答真的没调用到模型时才标 offline，否则界面会误报
    「未调用生成模型」。
    """
    try:
        text = llm.chat(db, messages, temperature=0.0, max_tokens=500,
                        json_mode=True, stats=call_stats, cfg_override=cfg_override)
    except llm.ModelUnavailable as exc:
        call_stats.add_note(f"{note}降级为规则判断：{llm.public_error(exc)}")
        return None
    data = _parse_json_object(text)
    if data is None:
        call_stats.add_note(f"{note}返回非 JSON，已使用规则判断")
    return data


def _offline_reason(exc: llm.ModelUnavailable) -> str:
    """生成降级原因只暴露错误类别；未配置模型不算调用失败。"""
    if isinstance(exc, llm.ModelNotConfigured):
        return ""
    return llm.public_error(exc)


def _normalize_queries(raw, *, limit: int = MAX_QUERIES) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        query = re.sub(r"\s+", " ", str(item or "").strip())
        if len(query) < 2 or len(query) > 200:
            continue
        if query not in out:
            out.append(query)
    return out[:limit]


# ------------------------------------------------------------------ 意图分析
_CHITCHAT_RE = re.compile(
    r"^(?:你好|您好|嗨|哈喽|hi|hello|在吗|在么|早上好|下午好|晚上好|"
    r"谢谢|多谢|感谢|再见|拜拜|晚安|你是谁|你能做什么|怎么用)[!！。.？?~～\s]*$",
    re.I,
)
_PRECISE_RE = re.compile(
    r"(?:第\s*\d+\s*[条章节款项]|[A-Za-z]{2,}[-_]?\d+|"
    r"\b\d{4,}\b|工号|编号|单号|条款号|版本号|sku)",
    re.I,
)
_PROCESS_WORDS = ("流程", "步骤", "审批", "办理", "手续", "申请", "怎么走", "走什么")


def _heuristic_intent(question: str) -> Intent:
    text = _clean_question(question)
    if _CHITCHAT_RE.match(text):
        return Intent("闲聊", False, "问候/寒暄，不需要检索知识库", "rule")
    if _PRECISE_RE.search(text):
        return Intent("精确标识查询", True, "问题包含编号/条款号等精确标识", "rule")
    if any(word in text for word in _PROCESS_WORDS):
        return Intent("流程咨询", True, "问题在询问办理流程或审批步骤", "rule")
    return Intent("规范查询", True, "问题在询问制度、标准或规定", "rule")


def _analyze_intent(db: Session, question: str, call_stats: llm.CallStats, *,
                    cfg_override: models.ModelConfig | None) -> Intent:
    fallback = _heuristic_intent(question)
    data = _chat_json(db, [
        {"role": "system", "content": (
            "你是企业知识库问答系统的意图分析节点。把用户问题归为以下四类之一：\n"
            "- 规范查询：询问制度、标准、规定、政策；\n"
            "- 流程咨询：询问办理流程、审批步骤、手续；\n"
            "- 精确标识查询：包含文档编号、工单号、条款号等精确标识；\n"
            "- 闲聊：问候、感谢、与知识库内容无关的寒暄。\n"
            "只输出 JSON：{\"label\":\"...\",\"reason\":\"...\"}，不要输出其它内容。"
        )},
        {"role": "user", "content": question},
    ], call_stats, cfg_override=cfg_override, note="意图分析节点")
    if not data:
        return fallback
    label = str(data.get("label") or "").strip()
    if label not in INTENT_LABELS:
        return fallback
    reason = str(data.get("reason") or "").strip()[:200]
    return Intent(label, label != "闲聊", reason or fallback.reason, "model")


# ------------------------------------------------------------------ 查询路由
def _strip_fillers(question: str) -> str:
    cleaned = re.sub(
        r"^(?:请问|麻烦问一下|麻烦|帮我|帮忙|我想知道|想问一下|想问下|"
        r"查一下|找一下)[，,。.\s]*", "", _clean_question(question))
    return re.sub(r"[。.\s]+$", "", cleaned).strip()


def _heuristic_plan(question: str) -> RoutePlan:
    text = _clean_question(question)
    # 只在明确的问号/分号处拆分。中文「和/与」既可能是并列主题，也可能是
    # 一个完整问题的一部分（如「住宿标准和加班餐补」），直接拆会把一句话
    # 变成两个查询，评估时反而把「部分资料缺失」误判成整题证据不足。
    parts = [p.strip() for p in re.split(r"[?？]+|[;；]+", text) if len(p.strip()) >= 3]
    if len(parts) >= 2:
        return RoutePlan("multi_query", parts[:MAX_QUERIES],
                         "问题包含多个子问题，拆分为多条查询分别检索", "rule")
    cleaned = _strip_fillers(text)
    if cleaned and cleaned != text:
        return RoutePlan("rewrite", [cleaned],
                         "问题包含口语化表达，已改写为可独立检索的查询", "rule")
    return RoutePlan("original", [text],
                     "问题表述清晰，直接使用原始查询", "rule")


def _plan_queries(db: Session, question: str, intent: Intent,
                  call_stats: llm.CallStats, *,
                  cfg_override: models.ModelConfig | None) -> RoutePlan:
    fallback = _heuristic_plan(question)
    data = _chat_json(db, [
        {"role": "system", "content": (
            "你是企业知识库问答系统的查询路由节点。根据用户问题选择检索策略，"
            "只输出 JSON：{\"mode\":\"original|rewrite|multi_query\","
            "\"queries\":[\"...\"],\"reason\":\"...\"}。\n"
            "- original：问题清晰，直接使用原始问题检索；\n"
            "- rewrite：问题口语化、指代不明或缺少关键词，改写成一条独立可检索的查询；\n"
            "- multi_query：问题包含多个子问题，拆成 2~4 条查询。\n"
            "queries 里的每条查询都必须是完整、可独立检索的中文问题或短语。"
        )},
        {"role": "user", "content": f"意图：{intent.label}\n问题：{question}"},
    ], call_stats, cfg_override=cfg_override, note="查询路由节点")
    if not data:
        return fallback
    mode = str(data.get("mode") or "").strip()
    if mode not in ROUTE_MODES:
        return fallback
    queries = _normalize_queries(data.get("queries"))
    if mode == "original":
        queries = [_clean_question(question)]
    elif mode == "rewrite":
        queries = queries[:1] or [_strip_fillers(question) or _clean_question(question)]
    elif len(queries) < 2:
        # 模型说要多查询却没给出足够查询：用规则拆分兜底，拆不出来就退回
        # original，避免为了凑 multi_query 把一句话重复检索两遍。
        return fallback
    reason = str(data.get("reason") or "").strip()[:200]
    return RoutePlan(mode, queries, reason or fallback.reason, "model")


# ------------------------------------------------------------------ 检索与融合
def _merge_stats(stats_list: list[dict], queries: list[str],
                 evidence: list[retrieval.Evidence]) -> dict:
    base = dict(stats_list[0]) if stats_list else {}
    base["candidate_total"] = sum(int(s.get("candidate_total") or 0) for s in stats_list)
    base["final_total"] = len(evidence)
    base["embedding_ok"] = bool(stats_list) and all(
        bool(s.get("embedding_ok")) for s in stats_list)
    base["embedding_error"] = next(
        (str(s.get("embedding_error")) for s in stats_list if s.get("embedding_error")), "")
    terms: list[str] = []
    for item in stats_list:
        for term in item.get("query_terms") or []:
            if term not in terms:
                terms.append(term)
    base["query_terms"] = terms[:16]
    base["queries"] = list(queries)
    coverage: dict[str, int] = {}
    for item in stats_list:
        for name, count in (item.get("kb_coverage") or {}).items():
            coverage[name] = coverage.get(name, 0) + int(count)
    base["kb_coverage"] = coverage
    return base


def _fuse_query_evidence(per_query: list[tuple[str, list[retrieval.Evidence]]],
                         *, top_k: int | None = None,
                         limit: int | None = None) -> list[retrieval.Evidence]:
    """多条查询的检索结果按 RRF 名次融合，再按证据 key 去重。

    不同查询的原始分数不可比（查询词不同、覆盖率不同），所以这里和库内
    融合一样只看名次；同一条证据被多条查询命中时，保留覆盖率最高的那份
    元数据，并把命中次数写进 channels 供前端解释排序。
    """
    scores: dict[str, float] = {}
    chosen: dict[str, retrieval.Evidence] = {}
    hits: dict[str, int] = {}
    for _query, evidence in per_query:
        for rank, ev in enumerate(evidence, start=1):
            scores[ev.key] = scores.get(ev.key, 0.0) + 1.0 / (config.RRF_K + rank)
            hits[ev.key] = hits.get(ev.key, 0) + 1
            old = chosen.get(ev.key)
            if old is None:
                chosen[ev.key] = ev
                continue
            if ev.coverage > old.coverage:
                old.coverage = ev.coverage
            if ev.score > old.score:
                old.score = ev.score
            for channel, value in ev.channels.items():
                old.channels.setdefault(channel, value)
    ordered = sorted(chosen.values(), key=lambda ev: -scores.get(ev.key, 0.0))
    total = limit or (config.MAX_EVIDENCE if top_k is None else max(top_k, 3))
    ordered = ordered[:total]
    for rank, ev in enumerate(ordered, start=1):
        ev.rank = rank
        ev.channels["query_hits"] = hits.get(ev.key, 1)
    return ordered


def _retrieve_round(db: Session, kbs: list[models.KnowledgeBase], queries: list[str],
                    top_k: int | None, call_stats: llm.CallStats):
    per_query: list[tuple[str, list[retrieval.Evidence]]] = []
    routes_by_id: dict[int, retrieval.KbRoute] = {}
    route_order: list[int] = []
    stats_list: list[dict] = []
    for query in queries:
        evidence, routes, stats = retrieval.retrieve(
            db, kbs, query, top_k=top_k, stats=call_stats)
        per_query.append((query, evidence))
        stats_list.append(stats)
        for route in routes:
            old = routes_by_id.get(route.kb_id)
            if old is None:
                routes_by_id[route.kb_id] = route
                route_order.append(route.kb_id)
                continue
            old.keyword_hits += route.keyword_hits
            old.vector_hits += route.vector_hits
            old.wiki_hits += route.wiki_hits
            old.fallback = old.fallback or route.fallback
            if old.used == "none" and route.used != "none":
                old.used = route.used
            elif old.used == "chunk" and route.used == "wiki":
                old.used = "wiki"
    routes = [routes_by_id[kb_id] for kb_id in route_order]
    if len(queries) > 1:
        for route in routes:
            if "多查询" not in route.reason:
                route.reason += f"；多查询融合（{len(queries)} 条查询）"
    merged = _fuse_query_evidence(per_query, top_k=top_k)
    return merged, routes, _merge_stats(stats_list, queries, merged), per_query


# ------------------------------------------------------------------ 覆盖度评估
def _query_evidence_ok(evidence: list[retrieval.Evidence]) -> bool:
    relevant = [ev for ev in evidence if ev.coverage > 0]
    if not relevant:
        return False
    best = max(ev.coverage for ev in relevant)
    return best >= COVERAGE_OK or (len(relevant) >= 2 and best >= COVERAGE_WEAK)


def _evaluate_round(db: Session, question: str, queries: list[str],
                    per_query: list[tuple[str, list[retrieval.Evidence]]],
                    call_stats: llm.CallStats, *,
                    cfg_override: models.ModelConfig | None) -> Evaluation:
    by_query = {query: evidence for query, evidence in per_query}
    missing = [query for query in queries
               if not _query_evidence_ok(by_query.get(query, []))]
    heuristic = Evaluation(
        sufficient=not missing,
        missing_queries=missing,
        reason=("每条查询都检索到相关证据" if not missing
                else "以下查询缺少相关证据：" + "、".join(missing)),
        source="rule",
    )
    all_evidence = [ev for _query, evidence in per_query for ev in evidence]
    if not all_evidence:
        return heuristic

    lines: list[str] = []
    for index, ev in enumerate(all_evidence[:8], start=1):
        title = ev.page_title or ev.doc_name or ev.heading or "资料"
        lines.append(f"[{index}] 覆盖率 {ev.coverage:.2f} · {title}\n{ev.text[:180]}")
    data = _chat_json(db, [
        {"role": "system", "content": (
            "你是企业知识库检索的相关性与覆盖度评估节点。判断给出的证据是否"
            "足以回答用户问题。只输出 JSON："
            "{\"sufficient\":true/false,\"missing_queries\":[\"...\"],\"reason\":\"...\"}。\n"
            "规则：\n"
            "1. 证据能支撑问题的主要结论才算 sufficient=true；\n"
            "2. 不足时 missing_queries 只列出缺少证据的查询，且必须来自本轮查询；\n"
            "3. 不要因为资料没有提到某个细节就把整题判为不足。"
        )},
        {"role": "user", "content": (
            f"用户问题：{question}\n本轮查询：{json.dumps(queries, ensure_ascii=False)}\n"
            f"证据摘要：\n" + "\n\n".join(lines)
        )},
    ], call_stats, cfg_override=cfg_override, note="覆盖度评估节点")
    if not data:
        return heuristic
    sufficient = bool(data.get("sufficient"))
    raw_missing = data.get("missing_queries")
    missing_model = [str(item).strip() for item in raw_missing] \
        if isinstance(raw_missing, list) else []
    missing_model = [item for item in missing_model if item in queries]
    reason = str(data.get("reason") or "").strip()[:300]
    if sufficient and not missing_model:
        return Evaluation(True, [], reason or "模型判断证据充分", "model")
    if not missing_model:
        missing_model = missing or list(queries)
    return Evaluation(False, missing_model, reason or heuristic.reason, "model")


# ------------------------------------------------------------------ 查询重写 / 重试
def _heuristic_rewrites(missing: list[str], intent: Intent,
                        tried: set[str]) -> list[str]:
    out: list[str] = []
    for query in missing:
        base = _strip_fillers(query) or query
        terms = retrieval.query_terms(base)
        candidates = [
            base,
            " ".join(terms[:8]),
            f"{base} 规定 标准",
            f"{base} 流程 步骤 审批" if intent.label == "流程咨询" else "",
        ]
        for candidate in candidates:
            candidate = re.sub(r"\s+", " ", candidate or "").strip()
            if len(candidate) < 2 or candidate in tried or candidate in out:
                continue
            out.append(candidate)
            if len(out) >= MAX_REWRITE_QUERIES:
                return out
    return out


def _rewrite_missing(db: Session, question: str, missing: list[str], intent: Intent,
                     tried: set[str], call_stats: llm.CallStats, *,
                     cfg_override: models.ModelConfig | None) -> list[str]:
    fallback = _heuristic_rewrites(missing, intent, tried)
    data = _chat_json(db, [
        {"role": "system", "content": (
            "你是企业知识库检索的查询重写节点。只处理证据不足的查询：提取关键"
            "术语，补充同义表达或更具体的说法，生成新的检索查询。"
            "只输出 JSON：{\"queries\":[\"...\"]}，最多 3 条，不要重复已尝试的查询。"
        )},
        {"role": "user", "content": (
            f"用户原始问题：{question}\n"
            f"证据不足的查询：{json.dumps(missing, ensure_ascii=False)}\n"
            f"已尝试的查询：{json.dumps(sorted(tried), ensure_ascii=False)}"
        )},
    ], call_stats, cfg_override=cfg_override, note="查询重写节点")
    if not data:
        return fallback
    queries = _normalize_queries(data.get("queries"), limit=MAX_REWRITE_QUERIES)
    queries = [query for query in queries if query not in tried]
    return queries or fallback


# ------------------------------------------------------------------ 通用回复
GENERAL_REPLY_PROMPT = (
    "你是企业知识库问答助手。用户这句话是问候或寒暄，不需要检索知识库。"
    "请用一两句话简短、友好地回应，并提示用户可以询问制度、流程或文档里的内容。"
    "不要编造企业内部信息。"
)
GENERAL_REPLY_OFFLINE = (
    "你好，我是企业知识库问答助手。你可以直接问我制度、流程或文档里的内容，"
    "我会基于知识库中的资料回答并标注引用。"
)


def _general_reply(db: Session, question: str, call_stats: llm.CallStats, *,
                   cfg_override: models.ModelConfig | None,
                   stream: bool) -> Iterator[dict]:
    messages = [
        {"role": "system", "content": GENERAL_REPLY_PROMPT},
        {"role": "user", "content": question},
    ]
    parts: list[str] = []
    try:
        if stream:
            for delta in llm.chat_stream(db, messages, temperature=0.4, max_tokens=300,
                                         stats=call_stats, cfg_override=cfg_override):
                parts.append(delta)
                yield {"type": "answer", "delta": delta}
        else:
            text = llm.chat(db, messages, temperature=0.4, max_tokens=300,
                            stats=call_stats, cfg_override=cfg_override)
            if text:
                parts.append(text)
                yield {"type": "answer", "delta": text}
    except llm.ModelUnavailable as exc:
        call_stats.mode = "offline"
        call_stats.offline_reason = _offline_reason(exc)
        detail = llm.public_error(exc)
        call_stats.add_note(f"通用回复降级为固定文案：{detail}")
    text = "".join(parts).strip()
    if not text:
        text = GENERAL_REPLY_OFFLINE
        yield {"type": "answer", "delta": text}
    yield {"type": "answer_replace", "content": text}


# ------------------------------------------------------------------ 回答生成
def _partial_note(missing: list[str], retries: int) -> str:
    if not missing:
        return ""
    if retries:
        lead = f"补充说明：补充检索 {retries} 次后，以下内容在现有资料中仍没有找到足够依据："
    else:
        lead = "补充说明：以下内容在现有资料中没有找到足够依据："
    lines = ["", lead]
    lines.extend(f"- {query}" for query in missing)
    return "\n".join(lines)


def _generate_answer(db: Session, question: str, kbs: list[models.KnowledgeBase],
                     evidence: list[retrieval.Evidence],
                     routes: list[retrieval.KbRoute], plan: RoutePlan,
                     evaluation: Evaluation, retries: int,
                     missing_display: list[str], *,
                     call_stats: llm.CallStats,
                     cfg_override: models.ModelConfig | None,
                     stream: bool) -> Iterator[dict]:
    """生成回答，逐段产出 answer 事件，返回与旧 `answer()` 同形的结果字典。"""
    if not evidence:
        block = answering._suggestion_block(
            db, kbs, [], routes,
            detail="\n".join(answering._route_notes(routes)), question=question)
        return {
            "content": block.text, "evidence": [], "route": {},
            "usage": call_stats.as_dict(), "insufficient": True,
            "citations": {"cited": [], "invalid": [], "citation_ok": True},
            "suggestions": block.items,
            "refusal_reason": "检索没有命中任何证据",
        }

    context = retrieval.build_context(evidence)
    prompt_lines = [
        f"资料：\n{context}",
        "",
        f"问题：{question}",
        "",
        f"本轮使用的检索查询：{json.dumps(plan.queries, ensure_ascii=False)}",
    ]
    missing_for_prompt = missing_display or evaluation.missing_queries
    if missing_for_prompt:
        prompt_lines.extend([
            "",
            "注意：本轮检索后仍有一些内容没有找到足够证据："
            + "、".join(missing_for_prompt)
            + "。请只回答资料能支撑的部分，不要编造缺失内容。",
        ])
    prompt_lines.append("")
    prompt_lines.append("请依据资料回答，并在结论后用 [编号] 标注依据。")
    messages = [
        {"role": "system", "content": answering.SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(prompt_lines)},
    ]

    parts: list[str] = []
    if stream:
        try:
            for delta in llm.chat_stream(db, messages, temperature=0.2,
                                         max_tokens=1500, stats=call_stats,
                                         cfg_override=cfg_override):
                parts.append(delta)
                yield {"type": "answer", "delta": delta}
        except llm.ModelUnavailable as exc:
            detail = llm.public_error(exc)
            call_stats.add_note(f"流式生成中断：{detail}")
            if not parts:
                call_stats.mode = "offline"
                call_stats.offline_reason = _offline_reason(exc)
                text = answering._extractive_answer(
                    question, evidence, reason=call_stats.offline_reason)
                parts.append(text)
                yield {"type": "answer", "delta": text}
    else:
        try:
            text = llm.chat(db, messages, temperature=0.2, max_tokens=1500,
                            stats=call_stats, cfg_override=cfg_override)
            if text:
                parts.append(text)
        except llm.ModelUnavailable as exc:
            call_stats.mode = "offline"
            call_stats.offline_reason = _offline_reason(exc)
            detail = llm.public_error(exc)
            call_stats.add_note(f"生成模型不可用：{detail}")
    text = "".join(parts).strip()
    if not text:
        call_stats.mode = "offline"
        if not call_stats.offline_reason:
            call_stats.offline_reason = "生成模型返回空内容"
        call_stats.add_note("生成结果为空，已回退抽取式回答")
        text = answering._extractive_answer(
            question, evidence, reason=call_stats.offline_reason)
        yield {"type": "answer", "delta": text}

    citations = answering._citation_check(text, evidence)
    refusal_reason = answering._refusal_verdict(text, citations)
    insufficient = bool(refusal_reason)
    suggestions: list[dict] = []

    if insufficient:
        detail_parts = [answering._strip_refusal_lead(text)]
        notes = answering._route_notes(routes)
        if notes:
            detail_parts.append("\n".join(notes))
        block = answering._suggestion_block(
            db, kbs, evidence, routes,
            detail="\n\n".join(part for part in detail_parts if part),
            question=question)
        text = block.text
        suggestions = block.items
        yield {"type": "answer_replace", "content": text}
    elif evaluation.missing_queries:
        # 部分回答：保留模型已经答出的内容，再补一段「哪些没找到」。
        # 不把它替换成整块拒答文案，否则用户拿不到已经能回答的那部分。
        # 优先展示第一轮评估里的原始缺失查询；重写后的查询可能是一串检索词，
        # 直接贴给用户看不懂。
        note = _partial_note(missing_display or evaluation.missing_queries, retries)
        if note:
            text = text.rstrip() + note
            yield {"type": "answer_replace", "content": text}
        insufficient = True
        refusal_reason = (f"补充检索 {retries} 次后仍有证据缺口" if retries
                          else "检索后仍有证据缺口")
        suggestions = answering._question_items(
            db, kbs, limit=answering.SUGGESTION_LIMIT, skip_question=question)

    return {
        "content": text,
        "evidence": [ev.to_dict() for ev in evidence],
        "route": {"kb_routes": [route.to_dict() for route in routes]},
        "usage": call_stats.as_dict(),
        "insufficient": insufficient,
        "citations": citations,
        "suggestions": suggestions,
        "refusal_reason": refusal_reason,
    }


# ------------------------------------------------------------------ 主链路
def _empty_stats() -> dict:
    return {
        "query_terms": [], "embedding_ok": False, "embedding_error": "",
        "per_kb_limit": 0, "candidate_total": 0, "final_total": 0,
        "rrf_k": config.RRF_K, "kb_coverage": {}, "queries": [],
        "rounds": 0, "retries": 0,
    }


def iter_run(db: Session, question: str, kbs: list[models.KnowledgeBase], *,
             top_k: int | None = None,
             cfg_override: models.ModelConfig | None = None,
             stream: bool = True) -> Iterator[dict]:
    """执行整条链路，边跑边产出事件；最终结果作为生成器返回值。"""
    question = _clean_question(question)
    call_stats = llm.CallStats()
    steps = _StepLog()
    pipeline: dict = {
        "mode": "rag", "intent": None, "route": None, "rounds": [],
        "retries": 0, "evaluation": None, "steps": steps.items,
    }

    # 1) 意图分析
    yield steps.start("intent", "理解问题", "判断问题类型与是否需要检索…")
    intent = _analyze_intent(db, question, call_stats, cfg_override=cfg_override)
    pipeline["intent"] = intent.to_dict()
    yield steps.done("intent", "理解问题",
                     f"{intent.label} · {intent.reason}")

    # 2) 闲聊：不进入 RAG 检索
    if not intent.needs_retrieval:
        pipeline["mode"] = "chitchat"
        pipeline["route"] = RoutePlan("original", [], "闲聊不进入知识库检索", "rule").to_dict()
        yield steps.start("answer", "生成回答", "直接回复，不检索知识库…")
        content_parts: list[str] = []
        final_content = ""
        for event in _general_reply(db, question, call_stats,
                                    cfg_override=cfg_override, stream=stream):
            if event.get("type") == "answer":
                content_parts.append(str(event.get("delta") or ""))
            elif event.get("type") == "answer_replace":
                final_content = str(event.get("content") or "")
            yield event
        text = (final_content or "".join(content_parts)).strip()
        yield steps.done("answer", "生成回答", "已直接回复，未检索知识库")
        stats = _empty_stats()
        return {
            "content": text, "evidence": [],
            "route": {"kb_routes": [], "stats": stats, "pipeline": pipeline},
            "usage": call_stats.as_dict(), "insufficient": False,
            "citations": {"cited": [], "invalid": [], "citation_ok": True},
            "suggestions": [], "refusal_reason": "", "pipeline": pipeline,
        }

    # 3) 查询路由
    yield steps.start("route", "规划检索", "选择 original / rewrite / multi_query 策略…")
    plan = _plan_queries(db, question, intent, call_stats, cfg_override=cfg_override)
    pipeline["route"] = plan.to_dict()
    yield steps.done("route", "规划检索",
                     f"{plan.mode} · {len(plan.queries)} 条查询："
                     + "；".join(plan.queries))

    # 4) 检索 → 评估 → 不足则重写缺失查询，最多 2 次
    queries = list(plan.queries)
    tried = {query for query in queries}
    all_per_query: list[tuple[str, list[retrieval.Evidence]]] = []
    routes: list[retrieval.KbRoute] = []
    stats: dict = {}
    evaluation = Evaluation(False, list(queries), "尚未评估", "rule")
    first_missing: list[str] = []
    retries = 0
    while True:
        round_no = len(pipeline["rounds"]) + 1
        yield steps.start(f"retrieve-{round_no}", "检索知识库",
                          f"第 {round_no} 轮 · {len(queries)} 条查询检索"
                          "（向量 + 关键词 + RRF）…")
        _evidence, routes, stats, per_query = _retrieve_round(
            db, kbs, queries, top_k, call_stats)
        all_per_query.extend(per_query)
        yield steps.done(
            f"retrieve-{round_no}", "检索知识库",
            f"命中 {sum(len(evs) for _q, evs in per_query)} 条候选，"
            f"去重后保留 {len(_fuse_query_evidence(all_per_query, top_k=top_k))} 条")

        yield steps.start(f"evaluate-{round_no}", "评估证据",
                          "判断证据是否充分、哪些查询还缺资料…")
        evaluation = _evaluate_round(db, question, queries, per_query, call_stats,
                                     cfg_override=cfg_override)
        if round_no == 1 and not evaluation.sufficient:
            # 第一轮的查询就是用户问题 / 模型改写后的自然语言，适合展示给用户；
            # 后续轮次可能是术语串，只留在 pipeline.rounds 里做审计。
            first_missing = list(evaluation.missing_queries or queries)
        pipeline["evaluation"] = evaluation.to_dict()
        pipeline["rounds"].append({
            "round": round_no, "queries": list(queries),
            "hits": {query: len(evidence) for query, evidence in per_query},
            "evidence_total": len(_fuse_query_evidence(all_per_query, top_k=top_k)),
            "evaluation": evaluation.to_dict(),
        })
        yield steps.done(
            f"evaluate-{round_no}", "评估证据",
            ("证据充分，可以生成回答" if evaluation.sufficient
             else "证据不足：" + (evaluation.reason or "缺少相关证据")))

        if evaluation.sufficient or retries >= MAX_RETRIES:
            break
        next_retry = retries + 1
        yield steps.start(f"retry-{next_retry}", "补充检索",
                          f"第 {next_retry}/{MAX_RETRIES} 次重写缺失查询…")
        new_queries = _rewrite_missing(
            db, question, evaluation.missing_queries or queries, intent,
            tried, call_stats, cfg_override=cfg_override)
        new_queries = [query for query in new_queries if query not in tried]
        if not new_queries:
            yield steps.done(f"retry-{next_retry}", "补充检索",
                             "没有生成新的检索表达，停止重试")
            break
        retries = next_retry
        tried.update(new_queries)
        queries = new_queries
        pipeline["retries"] = retries
        yield steps.done(f"retry-{next_retry}", "补充检索",
                         "新增查询：" + "；".join(new_queries))

    # 5) 汇总所有轮次的证据，生成回答
    evidence = _fuse_query_evidence(all_per_query, top_k=top_k)
    stats["rounds"] = len(pipeline["rounds"])
    stats["retries"] = retries
    stats["final_total"] = len(evidence)
    pipeline["retries"] = retries
    pipeline["evaluation"] = evaluation.to_dict()
    pipeline["steps"] = steps.items

    yield steps.start("answer", "生成回答",
                      "使用原始问题、查询列表与参考资料生成答案…")
    result = yield from _generate_answer(
        db, question, kbs, evidence, routes, plan, evaluation, retries,
        first_missing,
        call_stats=call_stats, cfg_override=cfg_override, stream=stream)
    detail = f"已完成 · {len(evidence)} 条参考资料"
    if call_stats.mode == "offline":
        detail += "（生成模型不可用，已降级为离线抽取）"
    yield steps.done("answer", "生成回答", detail)

    result["route"]["stats"] = stats
    result["route"]["pipeline"] = pipeline
    result["pipeline"] = pipeline
    result["usage"] = call_stats.as_dict()
    return result


def run(db: Session, question: str, kbs: list[models.KnowledgeBase], *,
        top_k: int | None = None,
        cfg_override: models.ModelConfig | None = None) -> dict:
    """非流式入口：消费链路事件，只返回最终结果（保持旧接口形状）。"""
    generator = iter_run(db, question, kbs, top_k=top_k,
                         cfg_override=cfg_override, stream=False)
    try:
        while True:
            next(generator)
    except StopIteration as stop:
        return stop.value
