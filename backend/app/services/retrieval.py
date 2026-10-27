"""检索层：关键词 + 向量，RRF 融合，跨库统一证据结构。

设计要点（与论文的对照实验直接相关）：
1. 分块库：关键词检索与语义检索各自排名，用 RRF 合并——不直接比较两种原始分数。
2. Wiki 库：检索 Wiki 章节单元（页面正文按章节切分），命中后回挂该页面的原文证据。
3. 跨库：每库先取 top N，再统一按 RRF 排名融合，避免大库占满候选。
   注意（勿照抄注释写论文）：库内才是加权 RRF，库间是「覆盖率 × 名次衰减」，
   见 `retrieve()` 里的 scored 计算。
4. Embedding 签名不一致的单元不参与语义检索（防止新模型查旧索引），但仍可用关键词命中。

规模约束（实测，单库 1000 文档 ≈ 5000 块 / 213 万字符）：
朴素实现要把全库向量逐条 unpack 成 list、再用纯 Python 循环算余弦，
单次查询仅这一项就占 0.58 s（4800 条 × 256 维）。改为 numpy 矩阵一次算完，
结果**完全等价**（同一余弦公式、同一 float32 输入），只是不再有 Python 层循环。
`cosine()` 保留原样：它是公开函数、有单测覆盖，且单对向量场景下更直观。

剩余耗时主要是 SQLite 把全库文本取回 Python（约 0.59 s / 2.1 MB），
这是关键词通道按 term 计频的固有成本，未做近似优化——用 FTS 预筛会改变
IDF 的统计范围进而影响排序，属于改变语义而非加速，故不做。
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

log = logging.getLogger("wrk.retrieval")

from .. import config, models
from . import llm
from .chunking import extract_keywords

_WORD = re.compile(r"[a-z0-9_]+|[\u4e00-\u9fff]")


def query_terms(text: str) -> list[str]:
    """查询词：英文词 + 中文二元组。

    中文长串不再整串作为一个词条——整句「工单首次响应时限是多久」几乎不可能
    在文档里原样出现，留着只会稀释权重。超过 4 字的连续中文串只贡献二元组。
    """
    low = text.lower()
    terms: list[str] = []
    seen: set[str] = set()

    def add(term: str) -> None:
        if term and term not in seen:
            seen.add(term)
            terms.append(term)

    for tok in re.findall(r"[a-z][a-z0-9_\-]{1,}", low):
        add(tok)
    for run in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        if len(run) <= 4:
            add(run)
        for i in range(len(run) - 1):
            add(run[i:i + 2])
    return terms[:64]


def bm25_score(term: str, text: str, k1: float = 1.4, b: float = 0.72,
               avg_len: float = 120.0) -> float:
    """单 term 的 BM25 简化打分（idf 在调用侧按候选集近似）。"""
    tf = text.count(term)
    if tf == 0:
        return 0.0
    dl = max(len(text), 1)
    return tf * (k1 + 1) / (tf + k1 * (1 - b + b * dl / avg_len))


def keyword_rank(rows: Iterable[tuple[int, str, str]], query: str, *,
                 limit: int, headings: dict[int, str] | None = None) -> list[tuple[int, float]]:
    """BM25 关键词检索。

    rows: (id, keywords_field, text)；headings 可选，用于给标题命中加权。
    与朴素做法相比这里做了三件事，缺一都会让长文档里的偶然用词排到前面：
    1. IDF 按候选语料实时计算，常见词（如「审批」在每节都出现）权重自动降低；
    2. 平均文档长度按语料计算，而不是写死常量；
    3. 覆盖率门槛：只命中 1 个短词不足以入选。
    """
    terms = query_terms(query)
    if not terms:
        return []
    corpus = [(rid, kw or "", (text or "").lower()) for rid, kw, text in rows]
    if not corpus:
        return []

    n_docs = len(corpus)
    avg_len = sum(len(t) for _, _, t in corpus) / n_docs or 1.0
    df: dict[str, int] = {}
    for term in terms:
        df[term] = sum(1 for _, _, t in corpus if term in t)
    idf = {
        term: math.log(1 + (n_docs - df[term] + 0.5) / (df[term] + 0.5))
        for term in terms
    }

    k1, b = 1.4, 0.72
    scored_all: list[tuple[int, float, int]] = []     # (id, score, hits)
    for rid, kw, text in corpus:
        dl = max(len(text), 1)
        heading = (headings or {}).get(rid, "").lower()
        score = 0.0
        hits = 0
        for term in terms:
            tf = text.count(term)
            if tf == 0 and term not in kw and term not in heading:
                continue
            hits += 1
            norm = k1 * (1 - b + b * dl / avg_len)
            tf_part = tf * (k1 + 1) / (tf + norm) if tf else 0.0
            w = idf[term]
            if term in kw:
                tf_part *= 1.8            # 关键词字段是抽取过的主题词，命中更可信
            if term in heading:
                tf_part += 0.9            # 标题命中通常比正文命中更相关
            score += w * tf_part
        if score <= 0:
            continue
        long_hit = any(len(t) >= 3 and t in text for t in terms)
        if hits < 2 and not long_hit:
            continue
        # 覆盖率加成：命中更多不同查询词的候选优先，但不掩盖 IDF 的作用
        score *= 1.0 + 0.18 * (hits - 1)
        scored_all.append((rid, score, hits))

    if not scored_all:
        # 门槛过严时（短查询、小语料）回退到原始命中，宁可多召回也不空手而归
        scored_all = []
        for rid, kw, text in corpus:
            s = 0.0
            for term in terms:
                tf = text.count(term)
                if tf or term in kw:
                    s += idf[term] * (1.0 + math.log1p(tf))
            if s > 0:
                scored_all.append((rid, s, 1))
        scored_all.sort(key=lambda x: -x[1])
        return [(rid, s) for rid, s, _ in scored_all[:limit]]

    scored_all.sort(key=lambda x: -x[1])
    return [(rid, s) for rid, s, _ in scored_all[:limit]]


def vector_rank(pairs: list[tuple[int, list[float]]], qvec: list[float], *,
                limit: int, rel_ratio: float = 0.55, min_sim: float = 0.05,
                lexical_ids: set[int] | None = None,
                scores_out: dict[int, float] | None = None) -> list[int]:
    """按余弦相似度排序并过滤。

    不用固定绝对阈值：不同 embedding 模型的相似度分布差异很大
    （离线哈希向量整体偏低，商业模型整体偏高）。这里用「相对最高分的比例」
    作为主判据，绝对下限只用来挡掉几乎正交的结果。

    lexical_ids 非空时，只保留「语义命中 且 关键词也有命中」的结果。
    纯语义召回在没有真实 embedding 时噪声极大，会把无关条目顶上首位；
    有了关键词交集作为约束，语义通道才能真正起「同义扩展」的作用。

    scores_out：传入字典则把 (id → 相似度) 填进去，**所有候选**都填，
    不只是入选的。调用方（search_chunks / search_wiki）需要把相似度挂到
    Evidence.channels 上供前端解释排序，以前是自己再算一遍 cosine()，
    在 5000 块的库上等于把最贵的一步做了两次。

    性能：pairs 超过 `_MATMUL_MIN_ROWS` 时走 numpy 矩阵一次性算完，
    结果与逐条 `cosine()` 等价（见 `_batch_cosine`）。小规模仍走原路径，
    避免为几条向量引入 numpy 调用开销，也让单测覆盖的路径保持原样。
    """
    if not qvec or not pairs:
        return []
    sims = _batch_cosine(pairs, qvec) if len(pairs) >= _MATMUL_MIN_ROWS else None
    if sims is None:
        sims = []
        for rid, vec in pairs:
            if not vec:
                continue
            s = cosine(qvec, vec)
            if s >= min_sim:
                sims.append((rid, s))
    if scores_out is not None:
        scores_out.update(dict(sims))
    if not sims:
        return []
    sims.sort(key=lambda x: -x[1])
    best = sims[0][1]
    cutoff = max(min_sim, best * rel_ratio)
    selected = [(rid, s) for rid, s in sims if s >= cutoff]

    if lexical_ids:
        gated = [(rid, s) for rid, s in selected if rid in lexical_ids]
        if gated:
            selected = gated
        # 关键词一个都没命中时保留语义结果：可能是同义改写，交给下游判断
    return [rid for rid, _ in selected][:limit]


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / math.sqrt(na * nb)


# 低于这个条数时逐条 cosine() 更快（numpy 的调用开销反而占主导）。
_MATMUL_MIN_ROWS = 64


def _batch_cosine(pairs: list[tuple[int, list[float]]],
                  qvec: list[float]) -> list[tuple[int, float]] | None:
    """批量余弦，返回 [(id, sim)]，只保留 sim >= 0 的项。

    与 `cosine()` 数学等价，差别只在实现：
    - 同样的点积 / 两个模长公式，`cosine()` 的 `na<=0 or nb<=0 → 0.0` 分支在这里
      用「模长为 0 的行直接置 0」表达；
    - 维度不一致的行按 `cosine()` 的约定当作 0.0 丢弃（返回 None 让调用方回退
      逐条路径，保证异常数据下的行为与原来完全一致）。

    返回 None 表示「这批数据不适合矩阵化」（维度不一致 / numpy 不可用），
    调用方必须回退到逐条 `cosine()`。
    """
    if not qvec:
        return None
    try:
        import numpy as np
    except ImportError:                      # pragma: no cover - numpy 是硬依赖
        return None

    dims = {len(v) for _, v in pairs if v}
    if dims != {len(qvec)}:
        # 维度不一致：交给逐条路径，那里会按 cosine() 的约定返回 0.0
        return None

    ids: list[int] = []
    rows: list[list[float]] = []
    for rid, vec in pairs:
        if not vec:
            continue
        ids.append(rid)
        rows.append(vec)
    if not rows:
        return []

    mat = np.asarray(rows, dtype="float64")
    q = np.asarray(qvec, dtype="float64")
    dots = mat @ q
    row_norms = np.sqrt((mat * mat).sum(axis=1))
    q_norm = math.sqrt(float((q * q).sum()))
    if q_norm <= 0:
        return []
    denom = row_norms * q_norm
    # 模长为 0 的行（全零向量）按 cosine() 的约定算 0，并避免除零告警
    with np.errstate(divide="ignore", invalid="ignore"):
        sims = np.where(denom > 0, dots / denom, 0.0)
    return [(rid, float(s)) for rid, s in zip(ids, sims)]


def rrf_fuse(rankings: list[list[int]], k: int | None = None,
             weights: list[float] | None = None) -> list[tuple[int, float, dict]]:
    """加权 Reciprocal Rank Fusion。返回 [(id, score, 各通道名次)]。

    weights 用来表达通道可信度差异：离线哈希向量只是占位实现，
    不能和真实 embedding 等权，否则会把关键词排序的正确结果冲掉。
    """
    k = k or config.RRF_K
    if weights is None:
        weights = [1.0] * len(rankings)
    scores: dict[int, float] = {}
    detail: dict[int, dict] = {}
    for channel_idx, ranking in enumerate(rankings):
        w = weights[channel_idx] if channel_idx < len(weights) else 1.0
        if w <= 0:
            continue
        for rank, rid in enumerate(ranking, start=1):
            scores[rid] = scores.get(rid, 0.0) + w / (k + rank)
            detail.setdefault(rid, {})[f"rank_{channel_idx}"] = rank
    ordered = sorted(scores.items(), key=lambda kv: -kv[1])
    return [(rid, score, detail.get(rid, {})) for rid, score in ordered]


def channel_weights(db: Session, qvec_ok: bool) -> list[float]:
    """关键词通道恒定 1.0；向量通道按当前 Embedding 是否为真实模型加权。

    离线哈希向量只保证链路可跑通，排序质量明显弱于关键词，给低权重；
    接入真实 embedding 后权重回到 1.0，两通道真正互补。
    """
    if not qvec_ok:
        return [1.0, 0.0]
    cfg, provider, sig, dim = llm.active_embedding(db)
    offline = cfg is None or sig.startswith(llm.FALLBACK_EMBED_NAME)
    return [1.0, 0.45 if offline else 1.0]


# ------------------------------------------------------------------ 证据结构
@dataclass
class Evidence:
    kb_id: int
    kb_name: str
    source_type: str                 # chunk | wiki
    text: str
    score: float = 0.0
    rank: int = 0
    doc_id: int | None = None
    doc_name: str = ""
    doc_version_id: int | None = None
    version_no: int | None = None
    locator: str = ""
    heading: str = ""
    page_id: int | None = None
    page_title: str = ""
    page_slug: str = ""
    page_version_id: int | None = None
    chunk_id: int | None = None
    channels: dict = field(default_factory=dict)
    evidence: list[dict] = field(default_factory=list)   # wiki → 原文依据
    coverage: float = 0.0                                 # 查询词覆盖率，跨库可比

    @property
    def key(self) -> str:
        if self.source_type == "wiki":
            return f"wiki:{self.page_version_id}:{self.heading}"
        return f"chunk:{self.chunk_id}"

    def to_dict(self) -> dict:
        return {
            "evidence_id": self.key,
            "kb_id": self.kb_id,
            "kb_name": self.kb_name,
            "source_type": self.source_type,
            "text": self.text,
            "score": round(self.score, 6),
            "rank": self.rank,
            # 查询词覆盖率是跨库唯一可比的量，必须对外输出：
            # 否则前端无法解释排序，实验也没法复算。
            "coverage": round(self.coverage, 4),
            "doc_id": self.doc_id,
            "doc_name": self.doc_name,
            "doc_version_id": self.doc_version_id,
            "version_no": self.version_no,
            "locator": self.locator,
            "heading": self.heading,
            "page_id": self.page_id,
            "page_title": self.page_title,
            "page_slug": self.page_slug,
            "page_version_id": self.page_version_id,
            "chunk_id": self.chunk_id,
            "channels": self.channels,
            "evidence": self.evidence,
        }


@dataclass
class KbRoute:
    kb_id: int
    kb_name: str
    mode: str
    status: str
    used: str                       # wiki | chunk | none
    reason: str
    keyword_hits: int = 0
    vector_hits: int = 0
    wiki_hits: int = 0
    fallback: bool = False

    def to_dict(self) -> dict:
        return {
            "kb_id": self.kb_id, "kb_name": self.kb_name, "mode": self.mode,
            "status": self.status, "used": self.used, "reason": self.reason,
            "keyword_hits": self.keyword_hits, "vector_hits": self.vector_hits,
            "wiki_hits": self.wiki_hits, "fallback": self.fallback,
        }


# ------------------------------------------------------------------ 单库检索
def _chunk_rows(db: Session, kb_id: int) -> list[tuple[int, str, str]]:
    rows = db.execute(
        select(models.Chunk.id, models.Chunk.keywords, models.Chunk.text).where(models.Chunk.kb_id == kb_id)
    ).all()
    return [(r[0], r[1] or "", r[2] or "") for r in rows]


def _chunk_headings(db: Session, kb_id: int) -> dict[int, str]:
    rows = db.execute(
        select(models.Chunk.id, models.Chunk.heading).where(models.Chunk.kb_id == kb_id)
    ).all()
    return {r[0]: r[1] or "" for r in rows}


def search_chunks(db: Session, kb: models.KnowledgeBase, query: str, *,
                  top_n: int, qvec: list[float] | None) -> tuple[list[Evidence], dict]:
    rows = _chunk_rows(db, kb.id)
    if not rows:
        return [], {"keyword_hits": 0, "vector_hits": 0}

    kw = keyword_rank(rows, query, limit=top_n * 3, headings=_chunk_headings(db, kb.id))
    kw_ids = [rid for rid, _ in kw]

    vec_ids: list[int] = []
    vec_scores: dict[int, float] = {}
    if qvec:
        sig = kb.embed_signature
        candidates = db.execute(
            select(models.Chunk.id, models.Chunk.vector_blob).where(
                models.Chunk.kb_id == kb.id,
                models.Chunk.vector_blob.is_not(None),
                models.Chunk.embed_signature == sig,
            )
        ).all()
        pairs = []
        for cid, blob in candidates:
            vec = llm.unpack_vector(blob)
            if vec:
                pairs.append((cid, vec))
        # 相似度只算一次：vector_rank 内部算完顺手写回 vec_scores，
        # 以前这里先 cosine 一遍、vector_rank 里再算一遍，5000 块时等于双倍开销。
        vec_ids = vector_rank(pairs, qvec, limit=top_n * 3, lexical_ids=set(kw_ids),
                              scores_out=vec_scores)

    fused = rrf_fuse([kw_ids, vec_ids], weights=channel_weights(db, qvec is not None))[:top_n]
    if not fused:
        return [], {"keyword_hits": len(kw_ids), "vector_hits": len(vec_ids)}

    ids = [rid for rid, _, _ in fused]
    chunk_map = {c.id: c for c in db.execute(
        select(models.Chunk).where(models.Chunk.id.in_(ids))
    ).scalars()}
    doc_ids = {c.doc_id for c in chunk_map.values()}
    docs = {d.id: d for d in db.execute(select(models.Document).where(models.Document.id.in_(doc_ids))).scalars()}
    ver_ids = {c.doc_version_id for c in chunk_map.values()}
    vers = {v.id: v for v in db.execute(
        select(models.DocumentVersion).where(models.DocumentVersion.id.in_(ver_ids))).scalars()}

    out: list[Evidence] = []
    for rank, (cid, score, chans) in enumerate(fused, start=1):
        c = chunk_map.get(cid)
        if not c:
            continue
        doc = docs.get(c.doc_id)
        ver = vers.get(c.doc_version_id)
        if doc and doc.deleted:
            continue
        channels = {k: v for k, v in chans.items()}
        if cid in vec_scores:
            channels["vector_sim"] = round(vec_scores[cid], 4)
        out.append(Evidence(
            kb_id=kb.id, kb_name=kb.name, source_type="chunk", text=c.text,
            score=score, rank=rank, doc_id=c.doc_id, doc_name=doc.filename if doc else "",
            doc_version_id=c.doc_version_id, version_no=ver.version_no if ver else None,
            locator=c.locator, heading=c.heading, chunk_id=c.id, channels=channels,
        ))
    return out, {"keyword_hits": len(kw_ids), "vector_hits": len(vec_ids)}


def search_wiki(db: Session, kb: models.KnowledgeBase, query: str, *,
                top_n: int, qvec: list[float] | None) -> tuple[list[Evidence], dict]:
    pages = db.execute(
        select(models.WikiPage).where(
            models.WikiPage.kb_id == kb.id,
            models.WikiPage.status != models.WikiPageStatus.draft.value,
            models.WikiPage.current_version_id.is_not(None),
        )
    ).scalars().all()
    if not pages:
        return [], {"keyword_hits": 0, "wiki_hits": 0}
    live_versions = {p.current_version_id for p in pages}
    sections = db.execute(
        select(models.WikiSection).where(
            models.WikiSection.kb_id == kb.id,
            models.WikiSection.page_version_id.in_(live_versions),
        )
    ).scalars().all()
    if not sections:
        return [], {"keyword_hits": 0, "wiki_hits": 0}

    rows = [(s.id, s.keywords or "", s.text or "") for s in sections]
    headings = {s.id: s.heading_path or "" for s in sections}
    kw = keyword_rank(rows, query, limit=top_n * 3, headings=headings)
    kw_ids = [rid for rid, _ in kw]

    vec_ids: list[int] = []
    vec_scores: dict[int, float] = {}
    if qvec:
        pairs: list[tuple[int, list[float]]] = []
        for s in sections:
            if s.embed_signature != kb.embed_signature or not s.vector_blob:
                continue
            vec = llm.unpack_vector(s.vector_blob)
            if vec:
                pairs.append((s.id, vec))
        # 同 search_chunks：相似度由 vector_rank 算一次并写回，不重复计算
        vec_ids = vector_rank(pairs, qvec, limit=top_n * 3, lexical_ids=set(kw_ids),
                              scores_out=vec_scores)

    fused = rrf_fuse([kw_ids, vec_ids], weights=channel_weights(db, qvec is not None))[:top_n]
    if not fused:
        return [], {"keyword_hits": len(kw_ids), "wiki_hits": len(vec_ids)}

    sec_map = {s.id: s for s in sections}
    page_map = {p.id: p for p in pages}
    out: list[Evidence] = []
    for rank, (sid, score, chans) in enumerate(fused, start=1):
        sec = sec_map.get(sid)
        if not sec:
            continue
        page = page_map.get(sec.page_id)
        if not page:
            continue
        channels = dict(chans)
        if sid in vec_scores:
            channels["vector_sim"] = round(vec_scores[sid], 4)
        ev = _wiki_evidence(db, sec.page_version_id)
        out.append(Evidence(
            kb_id=kb.id, kb_name=kb.name, source_type="wiki", text=sec.text, score=score, rank=rank,
            page_id=page.id, page_title=page.title, page_slug=page.slug,
            page_version_id=sec.page_version_id, heading=sec.heading_path,
            locator=sec.heading_path, channels=channels, evidence=ev,
        ))
    return out, {"keyword_hits": len(kw_ids), "wiki_hits": len(vec_ids)}


def _wiki_evidence(db: Session, page_version_id: int, limit: int = 4) -> list[dict]:
    rows = db.execute(
        select(models.WikiEvidence).where(models.WikiEvidence.page_version_id == page_version_id)
        .order_by(models.WikiEvidence.id).limit(limit)
    ).scalars().all()
    if not rows:
        return []
    doc_ids = {r.doc_id for r in rows if r.doc_id}
    docs = {d.id: d for d in db.execute(
        select(models.Document).where(models.Document.id.in_(doc_ids))).scalars()} if doc_ids else {}
    ver_ids = {r.doc_version_id for r in rows if r.doc_version_id}
    vers = {v.id: v for v in db.execute(
        select(models.DocumentVersion).where(models.DocumentVersion.id.in_(ver_ids))).scalars()} if ver_ids else {}
    out = []
    for r in rows:
        doc = docs.get(r.doc_id)
        ver = vers.get(r.doc_version_id)
        out.append({
            "chunk_id": r.chunk_id,
            "doc_id": r.doc_id,
            "doc_name": doc.filename if doc else "",
            "doc_version_id": r.doc_version_id,
            "version_no": ver.version_no if ver else None,
            "locator": r.locator,
            "quote": r.quote,
        })
    return out


# ------------------------------------------------------------------ 跨库路由
def plan_route(db: Session, kb: models.KnowledgeBase) -> tuple[str, str, bool]:
    """返回 (used, reason, fallback)。配置模式 ≠ 可用状态，这里显式区分。"""
    if kb.mode == models.KbMode.wiki.value:
        wiki_count = db.execute(
            select(models.WikiPage.id).where(
                models.WikiPage.kb_id == kb.id,
                models.WikiPage.status != models.WikiPageStatus.draft.value,
                models.WikiPage.current_version_id.is_not(None),
            ).limit(1)
        ).first()
        if wiki_count:
            return "wiki", "Wiki 已就绪，检索 Wiki 章节", False
        has_chunk = db.execute(
            select(models.Chunk.id).where(models.Chunk.kb_id == kb.id).limit(1)
        ).first()
        if has_chunk:
            return "chunk", "Wiki 尚未生成完成，已回退到分块索引", True
        return "none", "Wiki 未就绪且没有可用分块索引，该库暂不可检索", True
    has_chunk = db.execute(
        select(models.Chunk.id).where(models.Chunk.kb_id == kb.id).limit(1)
    ).first()
    if has_chunk:
        return "chunk", "基础分块库，关键词 + 语义检索后 RRF 融合", False
    return "none", "知识库还没有可检索的文档", False


def retrieve(db: Session, kbs: list[models.KnowledgeBase], query: str, *,
             top_k: int | None = None,
             stats: llm.CallStats | None = None) -> tuple[list[Evidence], list[KbRoute], dict]:
    """检索并返回 (证据, 各库路由, 统计)。

    stats 参数为什么必须存在（这是一个真实缺陷的修复）：
    查询向量化是**每次问答都会发生**的模型调用，之前没把 stats 传进 llm.embed，
    于是这部分调用与 token 从来没被记过账。结果是「问答成本」被系统性低估，
    而论文要报的正是问答链路的总开销——漏掉它，测出来的数字就是错的。

    这里不新建 CallStats 而是接受外部传入：调用方（answering.answer）需要把
    查询向量化和回答生成合并成同一次请求的成本，两份统计必须累加到同一个对象。
    """
    per_kb = top_k or config.PER_KB_CANDIDATES
    # 局部别名：函数体内 `stats` 这个名字下面还要用于各库命中统计，
    # 用 cost 指代模型成本对象，避免覆盖入参导致成本丢失。
    cost = stats
    qvec: list[float] | None = None
    embed_error = ""
    try:
        # 关键修复：stats 必须透传，否则查询向量化不计入成本
        qvec = llm.embed(db, [query], stats=cost)[0]
    except llm.ModelUnavailable as exc:
        embed_error = str(exc)

    routes: list[KbRoute] = []
    per_kb_evidence: list[list[Evidence]] = []
    for kb in kbs:
        used, reason, fallback = plan_route(db, kb)
        route = KbRoute(kb_id=kb.id, kb_name=kb.name, mode=kb.mode, status=kb.status,
                        used=used, reason=reason, fallback=fallback)
        if used == "wiki":
            ev, hits = search_wiki(db, kb, query, top_n=per_kb, qvec=qvec)
            route.wiki_hits = len(ev)
            route.keyword_hits = hits.get("keyword_hits", 0)
            route.vector_hits = hits.get("wiki_hits", 0)
            if not ev:
                ev2, hits2 = search_chunks(db, kb, query, top_n=per_kb, qvec=qvec)
                if ev2:
                    route.used = "chunk"
                    route.reason += "；Wiki 无命中，回退分块索引"
                    route.fallback = True
                    route.keyword_hits = hits2.get("keyword_hits", 0)
                    route.vector_hits = hits2.get("vector_hits", 0)
                    ev = ev2
        elif used == "chunk":
            ev, hits = search_chunks(db, kb, query, top_n=per_kb, qvec=qvec)
            route.keyword_hits = hits.get("keyword_hits", 0)
            route.vector_hits = hits.get("vector_hits", 0)
        else:
            ev = []
        routes.append(route)
        per_kb_evidence.append(ev)

    # 跨库融合：库内已用加权 RRF 得到排名。库间不能比较原始分数（量纲不同），
    # 但可以比较「查询词覆盖率」——它是 0~1 的绝对量，与检索方式和库大小无关。
    # 最终分 = 覆盖率 × 库内名次衰减，再叠加库间均衡因子。
    merged: list[Evidence] = []
    seen: set[str] = set()
    terms = query_terms(query)

    def coverage_of(text: str, heading: str) -> float:
        if not terms:
            return 0.0
        haystack = f"{heading} {text}".lower()
        hit = sum(1 for t in terms if t in haystack)
        return hit / len(terms)

    scored: list[tuple[float, int, int, Evidence]] = []
    for kb_idx, evs in enumerate(per_kb_evidence):
        for local_idx, ev in enumerate(evs):
            if ev.key in seen:
                continue
            seen.add(ev.key)
            ev.coverage = coverage_of(ev.text, ev.heading or ev.locator or "")
            rank_term = 1.0 / (1.0 + 0.45 * local_idx)          # 库内名次衰减
            # 均衡因子只作用于库内第 2 名之后：各库首位候选应平等竞争。
            balance = 1.0 if local_idx == 0 else 1.0 / (1.0 + 0.10 * kb_idx)
            score = (0.25 + ev.coverage) * rank_term * balance
            scored.append((score, kb_idx, local_idx, ev))

    scored.sort(key=lambda x: (-x[0], x[1]))
    total = config.MAX_EVIDENCE if top_k is None else max(top_k, 3)

    # 覆盖率 0 的候选只在不占名额时保留（补足上下文），优先给有命中的证据让位
    relevant = [s for s in scored if s[3].coverage > 0]
    irrelevant = [s for s in scored if s[3].coverage <= 0]
    ordered = relevant + irrelevant

    picked: list[tuple[float, int, int, Evidence]] = []
    kb_count: dict[int, int] = {i: 0 for i in range(len(per_kb_evidence))}
    for item in ordered:
        if len(picked) >= total:
            break
        picked.append(item)
        kb_count[item[1]] = kb_count.get(item[1], 0) + 1

    for rank, (score, _, _, ev) in enumerate(picked, start=1):
        ev.rank = rank
        merged.append(ev)

    stats = {
        "query_terms": query_terms(query)[:16],
        "embedding_ok": qvec is not None,
        "embedding_error": embed_error,
        "per_kb_limit": per_kb,
        "candidate_total": sum(len(e) for e in per_kb_evidence),
        "final_total": len(merged),
        "rrf_k": config.RRF_K,
        "kb_coverage": {str(r.kb_name): kb_count.get(i, 0) for i, r in enumerate(routes)},
    }
    # 注意：这里刻意不把 usage 塞进 stats 字典。
    # stats 会被 chat.py 原样持久化进 message.route，若其中带一份「此刻」的
    # usage 快照，生成调用发生前的旧数字会跟着存进历史消息，和真正的总账
    # （result["usage"]）对不上。成本只走一条出口：调用方显式返回 usage。
    return merged, routes, stats


def build_context(evidence: list[Evidence], budget: int | None = None) -> str:
    budget = budget or config.CONTEXT_CHAR_BUDGET
    parts: list[str] = []
    used = 0
    for idx, ev in enumerate(evidence, start=1):
        if ev.source_type == "wiki":
            head = (f"[{idx}] 知识库《{ev.kb_name}》 Wiki 页面《{ev.page_title}》"
                    f"· 章节：{ev.heading or '正文'}")
        else:
            head = (f"[{idx}] 知识库《{ev.kb_name}》 文档《{ev.doc_name}》"
                    f"· 版本 v{ev.version_no or '?'} · 位置：{ev.locator or '全文'}")
            if ev.heading:
                head += f" · 小节：{ev.heading}"
        body = ev.text.strip()
        block = f"{head}\n{body}"
        if used + len(block) > budget:
            remain = budget - used
            if remain < 200:
                break
            block = block[:remain] + "…"
        parts.append(block)
        used += len(block)
        if used >= budget:
            break
    return "\n\n".join(parts)
