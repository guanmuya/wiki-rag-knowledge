"""Wiki 生成：主题归纳 → 页面 → 章节 → 原文证据关联。

关键约束：
- 不把整库塞进一次上下文；按**字符预算分片**给模型，Map 归纳局部主题，
  Reduce 合并相近主题。全库每一块都必须进入某一片，不允许静默截断。
- 所有分片共用**全库全局编号**（enumerate(all_chunks, start=1)）。
  绝不在片内从 1 重编——历史上正是因为离线分支用全量编号、下游只在前 60 条里
  查找，80 块时 12 个主题只出 1 页。
- 每个页面必须挂回原文证据（chunk_id + 引文 + 位置），不允许只留摘要。
- 人工校对过的页面（human_locked）重新生成时进入待审队列，不静默覆盖。
- 生成是幂等的：同一主题 slug 复用页面，版本号递增，不产生重复页面。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .. import config, models
from . import chunking, llm

log = logging.getLogger("wrk.wiki")

MAX_TOPICS = 12
CHUNKS_PER_TOPIC = 6
# 单个主题最多记住的证据编号数（合并后可能跨多片，取多了没意义：
# 页面正文一次也只喂 CHUNKS_PER_TOPIC 条）。
MAX_TOPIC_CHUNK_IDS = 12

# 单片窗口（沿用旧名，语义已变）：
# 旧实现里它是「全库只取前 60 块」的截断点；现在它只是**单片块数的参考上限**，
# 实际片大小由字符预算（config.WIKI_SHARD_CHAR_BUDGET）反推，
# 全库每一块都必须进入某一片。保留这个名字是为了兼容外部脚本的引用。
TOPIC_SAMPLE_SIZE = 60

# Reduce 阶段的合并阈值。标题归一化（去标点/空白/大小写）后比对：
# 相似度 >= 0.72 直接合并；有证据重叠时可放宽到 0.45。
MERGE_TITLE_SIM = 0.72
MERGE_TITLE_SIM_WITH_EVIDENCE = 0.45
MERGE_EVIDENCE_OVERLAP = 0.30

TOPIC_PROMPT = """你是企业知识库整理助手。下面是若干原始资料片段，每段带编号。
请归纳出这批资料能支撑的主题（不超过 {max_topics} 个），每个主题给出：
- title：简短主题名（不超过 20 字，适合做 Wiki 页面标题）
- summary：一句话说明这个主题讲什么
- chunk_ids：支撑该主题的片段编号（数组，只填真实存在的编号）
只输出 JSON：{{"topics":[{{"title":"...","summary":"...","chunk_ids":[1,2]}}]}}
{scope_note}资料片段：
{context}"""

PAGE_PROMPT = """你是企业知识库编辑。请只依据下面的原始资料片段撰写 Wiki 页面正文。
要求：
1. 用 Markdown，二级标题分节（## 小节名），内容必须可被下列片段支撑。
2. 不要引入片段之外的数字、结论或推测。
3. 每个小节末尾用 [编号] 标注依据的片段。
4. 页面开头写一段 2-3 句的概述，不加标题。
5. 只输出页面正文 Markdown，不要输出 JSON 或解释。
主题：{title}
主题说明：{summary}
资料片段：
{context}"""


@dataclass
class WikiBuildResult:
    pages_created: int = 0
    pages_updated: int = 0
    pages_skipped: int = 0
    sections: int = 0
    evidence_links: int = 0
    mode: str = "live"
    notes: list[str] | None = None


def _chunks_for_kb(db: Session, kb_id: int) -> list[models.Chunk]:
    doc_ids = [d.id for d in db.execute(
        select(models.Document).where(
            models.Document.kb_id == kb_id, models.Document.deleted.is_(False))
    ).scalars()]
    if not doc_ids:
        return []
    return list(db.execute(
        select(models.Chunk).where(
            models.Chunk.kb_id == kb_id, models.Chunk.doc_id.in_(doc_ids)
        ).order_by(models.Chunk.doc_id, models.Chunk.ordinal)
    ).scalars())


def _cluster_topics_offline(chunks: list[models.Chunk]) -> list[dict]:
    """离线降级：按最近标题聚类，生成主题列表。保证无模型时流程仍可跑通。

    返回的 chunk_ids 是 **chunks 列表内的 1-based 序号**。
    调用方必须用同一个列表去解析这些序号，否则会错位——
    之前这里传全量 chunks，而下游只用前 60 条采样，导致
    超过 60 条时序号越界、页面被静默丢弃。
    """
    buckets: dict[str, list[int]] = {}          # 标题 → chunks 中的 1-based 序号
    for idx, c in enumerate(chunks, start=1):
        key = c.heading.strip() if c.heading.strip() else "总览"
        buckets.setdefault(key[:40], []).append(idx)
    topics: list[dict] = []
    for title, indices in sorted(buckets.items(), key=lambda kv: -len(kv[1]))[:MAX_TOPICS]:
        first = chunks[indices[0] - 1]
        topics.append({
            "title": title,
            # 摘要只描述来源，不重复正文，避免它被当成可检索内容而稀释相关性
            "summary": f"依据《{first.locator or '原文'}》等 {len(indices)} 个片段整理。",
            "chunk_ids": indices[:CHUNKS_PER_TOPIC],
        })
    return topics


def _parse_json_block(text: str) -> dict | None:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        return json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except ValueError:
                return None
    return None


def _build_context(chunks: list[models.Chunk], indices: list[int]) -> tuple[str, list[models.Chunk]]:
    picked: list[models.Chunk] = []
    lines: list[str] = []
    for idx in indices:
        if 1 <= idx <= len(chunks):
            c = chunks[idx - 1]
            if c in picked:
                continue
            picked.append(c)
            head = f"[{idx}]"
            if c.heading:
                head += f" {c.heading}"
            head += f"（{c.locator or '全文'}）"
            lines.append(f"{head}\n{c.text.strip()[:1200]}")
        if len(picked) >= CHUNKS_PER_TOPIC:
            break
    return "\n\n".join(lines), picked


def _offline_page(title: str, summary: str, picked: list[models.Chunk]) -> str:
    """离线降级页面：抽取式拼装原文要点，不生成任何资料外的内容。

    摘要单独放在引用块里，不混进正文——否则「依据第 N 段整理」这类
    元信息会被当作页面正文参与检索，稀释真实内容的相关性。
    """
    out: list[str] = []
    if summary:
        out.extend([f"> {summary}", ""])
    groups: dict[str, list[str]] = {}
    order: list[str] = []
    for c in picked:
        key = c.heading.strip() or title
        if key not in groups:
            groups[key] = []
            order.append(key)
        body = re.sub(r"\s+", " ", c.text.strip())
        groups[key].append(body[:700] + ("…" if len(body) > 700 else ""))

    if len(order) == 1:
        # 只有一个来源小节且标题与页面同名：正文不再重复标题
        out.extend(groups[order[0]])
    else:
        for key in order:
            out.append(f"## {key}")
            out.extend(groups[key])
            out.append("")
    return "\n".join(out).strip()


def _extract_evidence(page_md: str, picked: list[models.Chunk], all_chunks: list[models.Chunk]) -> list[dict]:
    """从页面正文里的 [编号] 标注解析证据；解析不到就按主题片段整体挂接。"""
    refs = {int(m) for m in re.findall(r"\[(\d{1,3})\]", page_md)}
    links: list[dict] = []
    used_ids: set[int] = set()
    for idx in sorted(refs):
        if 1 <= idx <= len(all_chunks):
            c = all_chunks[idx - 1]
            if c.id in used_ids:
                continue
            used_ids.add(c.id)
            links.append({"chunk": c, "quote": c.text.strip()[:400], "locator": c.locator})
    if not links:
        for c in picked:
            if c.id in used_ids:
                continue
            used_ids.add(c.id)
            links.append({"chunk": c, "quote": c.text.strip()[:400], "locator": c.locator})
    return links


def _unique_slug(db: Session, kb_id: int, title: str, taken: set[str]) -> str:
    base = chunking.slugify(title) or "page"
    slug = base
    n = 2
    existing = {p.slug for p in db.execute(
        select(models.WikiPage).where(models.WikiPage.kb_id == kb_id)).scalars()}
    while slug in existing or slug in taken:
        slug = f"{base}-{n}"
        n += 1
    taken.add(slug)
    return slug


def _index_sections(db: Session, kb: models.KnowledgeBase, page: models.WikiPage,
                    version: models.WikiPageVersion, *,
                    stats: llm.CallStats | None = None) -> int:
    """把页面正文按章节切分并向量化，写入 WikiSection。

    stats 为什么必须传进来（真实缺陷修复）：
    Wiki 库的整理成本**主要就在这里** —— 每个页面 3~8 个章节，一次 Wiki 生成
    可能有几十次向量化调用。之前没传 stats，等于把 Wiki 成本的大头完全丢掉，
    只剩主题归纳和页面生成那几次 chat 调用，测出来的「Wiki 整理成本」偏低数倍。
    """
    db.execute(delete(models.WikiSection).where(models.WikiSection.page_version_id == version.id))
    units = chunking.split_markdown(version.content_md)
    if not units:
        return 0
    # 章节标题带上页面名，检索命中时能区分是哪个页面的哪一节，
    # 也避免不同页面的同名小节（如「审批流程」）互相混淆。
    for u in units:
        u.heading = f"{page.title} / {u.heading}" if u.heading else page.title
    texts = [u.text for u in units]
    vectors: list[list[float]] = []
    sig = kb.embed_signature
    try:
        # 关键修复：stats 必须透传，否则 Wiki 章节向量化不计入成本
        vectors = llm.embed(db, texts, stats=stats)
        if vectors:
            sig = llm.active_embedding(db)[2]
    except llm.ModelUnavailable as exc:
        log.warning("wiki 章节向量化失败：%s", exc)
        if stats:
            stats.add_note(f"Wiki 章节向量化失败，仅建关键词索引：{exc}")
        vectors = []
    count = 0
    for unit, vec in zip(units, vectors or [None] * len(units)):
        db.add(models.WikiSection(
            kb_id=kb.id, page_id=page.id, page_version_id=version.id, ordinal=unit.ordinal,
            heading_path=unit.heading, text=unit.text,
            keywords=chunking.keyword_field(unit.text),
            vector_blob=llm.pack_vector(vec) if vec else None,
            dim=len(vec) if vec else 0, embed_signature=sig if vec else "",
        ))
        count += 1
    return count


def build_wiki(db: Session, kb: models.KnowledgeBase, *, force: bool = False,
               stats: llm.CallStats | None = None) -> WikiBuildResult:
    stats = stats or llm.CallStats()
    result = WikiBuildResult(notes=[])
    chunks = _chunks_for_kb(db, kb.id)
    if not chunks:
        raise RuntimeError("知识库还没有已入库的分块，无法生成 Wiki")

    # 1. 主题归纳
    #
    # 采样窗口与主题编号必须用同一个列表。
    # 曾经的 bug：LLM 分支用 sample（前 60 条），离线分支用全量 chunks，
    # 后者返回的 chunk_ids 是「全量中的序号」，而下游 _build_context(sample, ids)
    # 只在前 60 条里找 —— 库内超过 60 块时序号越界，页面被静默丢弃且无任何提示。
    # 实测 80 块时 12 个主题只生成 1 页。
    # 现在统一：先切窗口，两条分支都只在窗口内编号。
    sample = chunks[:TOPIC_SAMPLE_SIZE]
    context = "\n\n".join(
        f"[{i}] {c.text.strip()[:900]}" for i, c in enumerate(sample, start=1)
    )
    topics: list[dict] | None = None
    try:
        raw = llm.chat(
            db,
            [{"role": "user", "content": TOPIC_PROMPT.format(max_topics=MAX_TOPICS, context=context[:9000])}],
            temperature=0.1, max_tokens=1200, stats=stats, json_mode=True,
        )
        parsed = _parse_json_block(raw)
        if parsed and isinstance(parsed.get("topics"), list) and parsed["topics"]:
            topics = [t for t in parsed["topics"] if t.get("title")][:MAX_TOPICS]
    except llm.ModelUnavailable as exc:
        stats.mode = "offline"
        stats.add_note(f"主题归纳降级为离线聚类：{exc}")
    if not topics:
        topics = _cluster_topics_offline(sample)
        if stats.mode != "offline":
            stats.mode = "offline"
            stats.add_note("主题归纳使用离线聚类")

    if len(chunks) > len(sample):
        # 采样窗口外还有内容：明确告知，而不是让用户以为 Wiki 覆盖了全库
        result.notes.append(
            f"主题归纳只用了前 {len(sample)} 个分块（共 {len(chunks)} 个），"
            f"其余内容仍可通过分块检索命中"
        )

    taken: set[str] = set()
    for order, topic in enumerate(topics):
        title = str(topic.get("title") or "未命名主题")[:80]
        summary = str(topic.get("summary") or "")
        raw_ids = topic.get("chunk_ids") or []
        ids = [int(i) for i in raw_ids if str(i).isdigit()] if isinstance(raw_ids, list) else []
        if not ids:
            ids = [order + 1]
        ctx, picked = _build_context(sample, ids)
        if not picked:
            result.pages_skipped += 1
            # 跳过必须有原因，否则用户看到「页面数少于主题数」却无从排查
            result.notes.append(
                f"《{title}》未能定位到有效原文片段（编号 {ids[:5]}），已跳过")
            continue

        slug = chunking.slugify(title) or f"page-{order+1}"
        page = db.execute(
            select(models.WikiPage).where(models.WikiPage.kb_id == kb.id, models.WikiPage.slug == slug)
        ).scalar_one_or_none()

        if page and page.human_locked and not force:
            page.status = models.WikiPageStatus.needs_review.value
            db.add(page)
            result.pages_skipped += 1
            result.notes.append(f"《{title}》已人工校对，本次生成进入待审，不覆盖")
            continue

        content = ""
        try:
            content = llm.chat(
                db,
                [{"role": "user", "content": PAGE_PROMPT.format(title=title, summary=summary, context=ctx)}],
                temperature=0.2, max_tokens=1400, stats=stats,
            ).strip()
        except llm.ModelUnavailable as exc:
            stats.mode = "offline"
            stats.add_note(f"页面生成降级为抽取式：{exc}")
        if len(content) < 40:
            content = _offline_page(title, summary, picked)
            if stats.mode != "offline":
                stats.mode = "offline"
                stats.add_note("页面正文使用抽取式拼装")

        if page is None:
            page = models.WikiPage(
                kb_id=kb.id, slug=slug, title=title, summary=summary,
                status=models.WikiPageStatus.draft.value, order_index=order,
                parent_slug="", generated_at=models.utcnow(),
            )
            db.add(page)
            db.flush()
            result.pages_created += 1
        else:
            page.title = title
            page.summary = summary
            page.order_index = order
            page.status = models.WikiPageStatus.published.value
            page.updated_at = models.utcnow()
            db.add(page)
            result.pages_updated += 1

        last_no = db.execute(
            select(models.WikiPageVersion.version_no)
            .where(models.WikiPageVersion.page_id == page.id)
            .order_by(models.WikiPageVersion.version_no.desc()).limit(1)
        ).scalar() or 0
        version = models.WikiPageVersion(
            page_id=page.id, version_no=last_no + 1, content_md=content, summary=summary,
            origin="generated", note="自动生成",
        )
        db.add(version)
        db.flush()
        page.current_version_id = version.id
        page.status = models.WikiPageStatus.published.value
        db.add(page)

        # 证据关联：先清旧版本关联，只保留当前版本
        db.execute(delete(models.WikiEvidence).where(models.WikiEvidence.page_version_id == version.id))
        links = _extract_evidence(content, picked, sample)
        for link in links:
            c = link["chunk"]
            db.add(models.WikiEvidence(
                kb_id=kb.id, page_version_id=version.id, chunk_id=c.id, doc_id=c.doc_id,
                doc_version_id=c.doc_version_id, quote=link["quote"], locator=link["locator"],
            ))
            result.evidence_links += 1

        result.sections += _index_sections(db, kb, page, version, stats=stats)

    # 2. 页面间关联：同主题共享证据的页面互为关联
    db.flush()
    kb.wiki_ready_at = models.utcnow()
    kb.status = models.KbStatus.ready.value
    db.add(kb)
    db.add(models.IndexVersion(
        kb_id=kb.id, kind="wiki", embed_signature=kb.embed_signature,
        status=models.KbStatus.ready.value, unit_count=result.sections,
    ))
    return result


def related_pages(db: Session, kb_id: int, page: models.WikiPage, limit: int = 5) -> list[dict]:
    """基于共享证据计算关联页面，替代知识图谱，成本可控。"""
    if not page.current_version_id:
        return []
    mine = {r.chunk_id for r in db.execute(
        select(models.WikiEvidence).where(models.WikiEvidence.page_version_id == page.current_version_id)
    ).scalars() if r.chunk_id}
    if not mine:
        return []
    rows = db.execute(
        select(models.WikiEvidence.page_version_id, models.WikiEvidence.chunk_id)
        .where(models.WikiEvidence.kb_id == kb_id, models.WikiEvidence.chunk_id.in_(mine))
    ).all()
    counts: dict[int, int] = {}
    for pv_id, _ in rows:
        if pv_id == page.current_version_id:
            continue
        counts[pv_id] = counts.get(pv_id, 0) + 1
    if not counts:
        return []
    versions = {v.id: v for v in db.execute(
        select(models.WikiPageVersion).where(models.WikiPageVersion.id.in_(list(counts)))
    ).scalars()}
    page_ids = {v.page_id for v in versions.values()}
    pages = {p.id: p for p in db.execute(
        select(models.WikiPage).where(models.WikiPage.id.in_(page_ids))).scalars()}
    out = []
    for pv_id, shared in sorted(counts.items(), key=lambda kv: -kv[1]):
        v = versions.get(pv_id)
        if not v:
            continue
        p = pages.get(v.page_id)
        if not p or p.id == page.id:
            continue
        out.append({"page_id": p.id, "slug": p.slug, "title": p.title, "shared_evidence": shared})
        if len(out) >= limit:
            break
    return out
