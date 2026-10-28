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


def _cluster_topics_offline(entries: list[tuple[int, models.Chunk]]) -> list[dict]:
    """离线降级：按最近标题聚类，生成主题列表。保证无模型时流程仍可跑通。

    参数是 (全局编号, 分块) 的列表，编号是**全库统一的 1-based 序号**。
    绝不能在这里从 1 重新编号：分片后重编号会让下游按全局序号取块时错位，
    历史上正是这种错位导致 80 块时 12 个主题只出 1 页。
    """
    buckets: dict[str, list[int]] = {}          # 标题 → 全局编号
    by_idx = {idx: c for idx, c in entries}
    for idx, c in entries:
        key = c.heading.strip() if c.heading.strip() else "总览"
        buckets.setdefault(key[:40], []).append(idx)
    topics: list[dict] = []
    for title, indices in sorted(buckets.items(), key=lambda kv: -len(kv[1]))[:MAX_TOPICS]:
        first = by_idx[indices[0]]
        topics.append({
            "title": title,
            # 摘要只描述来源，不重复正文，避免它被当成可检索内容而稀释相关性
            "summary": f"依据《{first.locator or '原文'}》等 {len(indices)} 个片段整理。",
            "chunk_ids": indices[:MAX_TOPIC_CHUNK_IDS],
            "offline": True,
        })
    return topics


def _shard_chunks(chunks: list[models.Chunk],
                  excerpt: int) -> list[list[tuple[int, models.Chunk]]]:
    """按字符预算把全库分块切成若干片，返回 [(全局编号, 分块)] 的分片列表。

    编号在切片**之前**就用 `enumerate(chunks, start=1)` 固定下来，片内不再重编号：
    下游 `_build_context` / `_extract_evidence` 都按全库序号取块，一旦片内重编号，
    取到的就是别的文档的段落。

    单片块数另受 TOPIC_SAMPLE_SIZE 约束（沿用旧常量）：它曾经是「全库只取前 60 块」
    的截断点，现在只是单片上限，保证单片上下文不会长到模型读不完。
    """
    budget = max(int(config.WIKI_SHARD_CHAR_BUDGET), 1)
    max_chunks = max(int(TOPIC_SAMPLE_SIZE), 1)
    shards: list[list[tuple[int, models.Chunk]]] = []
    current: list[tuple[int, models.Chunk]] = []
    used = 0
    for idx, chunk in enumerate(chunks, start=1):
        cost = min(len(chunk.text.strip()), excerpt) + 60   # 编号/标题/位置等固定开销
        if current and (used + cost > budget or len(current) >= max_chunks):
            shards.append(current)
            current, used = [], 0
        current.append((idx, chunk))
        used += cost
    if current:
        shards.append(current)
    return shards or [[]]


def _plan_shards(chunks: list[models.Chunk]) -> tuple[list[list[tuple[int, models.Chunk]]], int]:
    """切分片；片数超过 WIKI_MAX_MAP_CALLS 时先压缩单块摘录再重切。

    返回 (分片, 单片摘录长度)。压缩有下限（WIKI_TOPIC_EXCERPT_MIN），
    压到下限仍然超上限就**如实多调用**——宁可多花几次请求，也不把库尾内容丢掉。
    这就是「1000 份文档的库也能整理」与「只取前 60 块」的区别。
    """
    excerpt = max(int(config.WIKI_TOPIC_EXCERPT_CHARS), 1)
    floor = max(min(int(config.WIKI_TOPIC_EXCERPT_MIN), excerpt), 1)
    limit = int(config.WIKI_MAX_MAP_CALLS)
    shards = _shard_chunks(chunks, excerpt)
    if limit <= 0:
        return shards, excerpt
    while len(shards) > limit and excerpt > floor:
        ratio = limit / max(len(shards), 1)
        nxt = max(floor, int(excerpt * ratio * 0.9))
        if nxt >= excerpt:
            nxt = floor
        excerpt = nxt
        shards = _shard_chunks(chunks, excerpt)
    return shards, excerpt


def _shard_context(entries: list[tuple[int, models.Chunk]], excerpt: int) -> str:
    """拼单片上下文。编号一律用全局序号，标题与位置一并给出，便于模型对齐原文。"""
    lines: list[str] = []
    for idx, c in entries:
        head = f"[{idx}]"
        if c.heading:
            head += f" {c.heading}"
        head += f"（{c.locator or '全文'}）"
        lines.append(f"{head}\n{c.text.strip()[:excerpt]}")
    return "\n\n".join(lines)


def _map_topics(db: Session, entries: list[tuple[int, models.Chunk]], *,
                shard_no: int, shard_total: int, total_chunks: int,
                excerpt: int, stats: llm.CallStats) -> list[dict]:
    """Map：单片归纳主题；模型不可用或返回空时退回本片的离线聚类。

    降级是**按片**发生的：某一片的模型调用失败，不影响其它片已归纳出的主题。
    但统计口径必须诚实——只要有一片降级，stats.mode 就标记 offline。
    """
    context = _shard_context(entries, excerpt)
    scope_note = ""
    if shard_total > 1 and entries:
        first, last = entries[0][0], entries[-1][0]
        scope_note = (
            f"注意：这是全库 {total_chunks} 个片段中的第 {first}~{last} 个"
            f"（第 {shard_no}/{shard_total} 片）。只归纳本片出现的主题，"
            f"不要推测本片以外的内容；其它片的主题稍后统一合并。\n"
        )
    try:
        raw = llm.chat(
            db,
            [{"role": "user", "content": TOPIC_PROMPT.format(
                max_topics=MAX_TOPICS, scope_note=scope_note, context=context)}],
            temperature=0.1, max_tokens=1200, stats=stats, json_mode=True,
        )
        parsed = _parse_json_block(raw)
        if parsed and isinstance(parsed.get("topics"), list) and parsed["topics"]:
            valid = {idx for idx, _ in entries}
            out: list[dict] = []
            for t in parsed["topics"]:
                if not t.get("title"):
                    continue
                raw_ids = t.get("chunk_ids") or []
                ids = ([int(i) for i in raw_ids if str(i).isdigit() and int(i) in valid]
                       if isinstance(raw_ids, list) else [])
                out.append({
                    "title": str(t["title"])[:80],
                    "summary": str(t.get("summary") or ""),
                    # 模型偶尔编出不存在的编号：过滤后至少挂回本片首块，不丢主题
                    "chunk_ids": ids or [entries[0][0]],
                    "offline": False,
                })
            if out:
                return out[:MAX_TOPICS]
    except llm.ModelUnavailable as exc:
        stats.mode = "offline"
        stats.add_note(f"主题归纳降级为离线聚类：{exc}")
    if stats.mode != "offline":
        stats.mode = "offline"
        stats.add_note("主题归纳使用离线聚类")
    return _cluster_topics_offline(entries)


def _norm_title(title: str) -> str:
    """标题归一化：去掉空白/标点/大小写差异，只留可比较的字符。"""
    return re.sub(r"[\s\W_]+", "", (title or "").lower())


def _evidence_overlap(a: list[int], b: list[int]) -> float:
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    return len(sa & sb) / max(1, min(len(sa), len(sb)))


def _digit_signature(title: str) -> tuple[int, ...]:
    """标题里的数字序列。用于区分「第 1 节」和「第 2 节」这种只差数字的主题。"""
    return tuple(int(x) for x in re.findall(r"\d+", title or ""))


def _should_merge(a: dict, b: dict) -> bool:
    """判断两个主题是不是「同一件事的不同说法」。

    规则从严到宽：
    1. 归一化标题完全相同 → 合并（离线聚类跨片必然命中这条）；
    2. 短标题被长标题包含（≥4 字）→ 合并，如「住宿标准」并入「住宿标准与报销」；
    3. 数字序列相同、且较短标题 ≥5 字时，才允许按相似度合并
       （≥0.72，或 ≥0.45 且证据重叠 ≥30%）。

    为什么规则 3 要卡「数字序列相同」：`第1节 主题内容` 与 `第2节 主题内容`
    的字符串相似度高达 0.9，纯相似度会把 12 个编号小节并成 1 个页面——
    实测就是这么丢掉 11 个页面的。短标题（<5 字）同理，两个字里改一个
    相似度就掉到 0.5 以下，合并判断没有意义。
    """
    na, nb = _norm_title(a.get("title", "")), _norm_title(b.get("title", ""))
    if not na or not nb:
        return False
    if na == nb:
        return True
    short, long = (na, nb) if len(na) <= len(nb) else (nb, na)
    if len(short) >= 4 and short in long:
        return True
    if len(short) < 5:
        return False
    if _digit_signature(a.get("title", "")) != _digit_signature(b.get("title", "")):
        return False
    sim = SequenceMatcher(None, na, nb).ratio()
    if sim >= MERGE_TITLE_SIM:
        return True
    return (sim >= MERGE_TITLE_SIM_WITH_EVIDENCE
            and _evidence_overlap(a.get("chunk_ids", []), b.get("chunk_ids", []))
            >= MERGE_EVIDENCE_OVERLAP)


def _merge_topics(topics: list[dict]) -> list[dict]:
    """Reduce：把各片归纳出的相近主题合并成一份主题表。

    合并保留首次出现的标题与顺序（文档顺序更接近人的阅读顺序），
    证据编号取并集并去重。`offline` 标记只有在两个主题都来自离线聚类时才保留，
    用于之后重算摘要——离线摘要里带片段数，合并后必须重新生成，否则数字是旧的。
    """
    merged: list[dict] = []
    for topic in topics:
        target = next((kept for kept in merged if _should_merge(kept, topic)), None)
        if target is None:
            merged.append({
                "title": topic["title"],
                "summary": topic.get("summary", ""),
                "chunk_ids": list(dict.fromkeys(topic.get("chunk_ids") or []))[:MAX_TOPIC_CHUNK_IDS],
                "offline": bool(topic.get("offline")),
            })
            continue
        target["chunk_ids"] = list(dict.fromkeys(
            target["chunk_ids"] + list(topic.get("chunk_ids") or [])))[:MAX_TOPIC_CHUNK_IDS]
        if not target.get("summary") and topic.get("summary"):
            target["summary"] = topic["summary"]
        target["offline"] = bool(target.get("offline")) and bool(topic.get("offline"))
    return merged


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

    摘要不再写进正文：页面详情页本来就会单独展示 `page.summary`，
    正文再放一遍会变成「同一段话出现两次」（实测截图里就是这样）。
    正文段落之间用空行分隔，否则 Markdown 的 `breaks: true` 会把它们
    渲染成一整块没有段距的密文。
    """
    out: list[str] = []
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
        out.append("\n\n".join(groups[order[0]]))
    else:
        for key in order:
            out.append(f"## {key}")
            out.append("\n\n".join(groups[key]))
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
    """生成整库 Wiki：分片 Map 归纳 → Reduce 合并 → 逐主题写页面。

    覆盖范围是这里最重要的语义：**全库每一个分块都必须进入某一分片**。
    旧实现只取前 TOPIC_SAMPLE_SIZE(60) 块，库尾内容永远进不了 Wiki 且无提示，
    扩料做「Wiki vs 分块」对比时 Wiki 天然少看到 2/3 资料，结论不可用。

    `stats` 必须透传：Wiki 的成本大头是章节向量化（每页 3~8 节），
    不传就等于把整理成本记成 0。
    """
    stats = stats or llm.CallStats()
    result = WikiBuildResult(notes=[])
    chunks = _chunks_for_kb(db, kb.id)
    if not chunks:
        raise RuntimeError("知识库还没有已入库的分块，无法生成 Wiki")

    # 1. 分片 Map：全库每一块都必须进入某一片
    #
    # 旧实现只把前 TOPIC_SAMPLE_SIZE(60) 块送进主题归纳，库内超过 60 块的部分
    # 永远进不了 Wiki，而且没有任何提示——扩到 150-300 块做对比实验时，
    # 「Wiki 只看到 1/3 资料」，对比天然不公平。
    # 现在按字符预算切片，逐片归纳，片内编号一律用全库序号。
    shards, excerpt = _plan_shards(chunks)
    topics: list[dict] = []
    for shard_no, entries in enumerate(shards, start=1):
        if not entries:
            continue
        topics.extend(_map_topics(
            db, entries, shard_no=shard_no, shard_total=len(shards),
            total_chunks=len(chunks), excerpt=excerpt, stats=stats,
        ))

    # 2. Reduce：合并各片归纳出的相近主题
    topics = _merge_topics(topics)

    if len(shards) > 1:
        # 覆盖范围必须如实说明：用户要知道 Wiki 是「全库分片归纳」的结果
        result.notes.append(
            f"全库共 {len(chunks)} 个分块，按字符预算分 {len(shards)} 片归纳后合并"
            f"（单片摘录 {excerpt} 字，未丢弃任何分块）"
        )
    if len(topics) > config.WIKI_MAX_PAGES:
        # 页面数不设上限会让大库的 Wiki 生成变成几百次模型调用。
        # 超限时按证据数量保留，并明确告知被略过的主题——不静默截断。
        keep = sorted(range(len(topics)),
                      key=lambda i: -len(topics[i]["chunk_ids"]))[:config.WIKI_MAX_PAGES]
        result.notes.append(
            f"合并后共 {len(topics)} 个主题，按证据数量保留前 {config.WIKI_MAX_PAGES} 个；"
            f"其余内容仍可通过分块检索命中"
        )
        topics = [topics[i] for i in sorted(keep)]

    taken: set[str] = set()
    for order, topic in enumerate(topics):
        title = str(topic.get("title") or "未命名主题")[:80]
        ids = [int(i) for i in topic.get("chunk_ids") or []
               if isinstance(i, int) and 1 <= i <= len(chunks)]
        summary = str(topic.get("summary") or "")
        if topic.get("offline") and ids:
            # 离线摘要里带「等 N 个片段」，合并后片段数变了，必须按并集重算
            first = chunks[ids[0] - 1]
            summary = f"依据《{first.locator or '原文'}》等 {len(ids)} 个片段整理。"
        ctx, picked = _build_context(chunks, ids)
        if not picked:
            result.pages_skipped += 1
            # 跳过必须有原因，否则用户看到「页面数少于主题数」却无从排查
            result.notes.append(
                f"《{title}》未能定位到有效原文片段（编号 {ids[:5]}），已跳过")
            continue

        slug = chunking.slugify(title) or f"page-{order+1}"
        if slug in taken:
            n = 2
            while f"{slug}-{n}" in taken:
                n += 1
            slug = f"{slug}-{n}"
        taken.add(slug)
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
        links = _extract_evidence(content, picked, chunks)
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
