"""Wiki 分片归纳的规模测试。

为什么单独一组：用户明确要求「一个知识库要能装 1000 份左右文档」，
单份 2000-5000 字，折合 3000-5000 个分块。旧实现只把前 60 块送进主题归纳，
库尾内容永远进不了 Wiki——这种「静默截断」在演示时看不出来，
但在论文对比实验里会直接让「Wiki vs 分块」不公平。

这里锁三件事：
1. 切片必须**恰好覆盖全库每一块**，不重不漏（编号是全库统一的）；
2. 片数超上限时先压缩摘录，压到下限仍超就如实多调用，绝不丢内容；
3. 千块级的库能跑完，且跳过的页面必须带原因。
"""
from __future__ import annotations

import uuid

from sqlalchemy import select

from app import config, models
from app.services import wiki


def _make_kb(db, count: int, headings: int = 12, docs: int = 3,
             text_len: int = 600) -> models.KnowledgeBase:
    """造一个带 count 个分块的库，按 docs 份文档均分。

    text_len 模拟真实文档的段落长度（单份 2000-5000 字，块约 600-800 字）。
    太短的假分块会让字符预算失效，测不出「按预算切片」的行为。
    """
    tag = uuid.uuid4().hex[:8]
    kb = models.KnowledgeBase(name=f"规模测试-{tag}", mode="wiki", status="ready")
    db.add(kb)
    db.commit()

    per_doc = max(count // docs, 1)
    made = 0
    for d in range(docs):
        doc = models.Document(kb_id=kb.id, filename=f"big-{d}.md", suffix=".md", status="ready")
        db.add(doc)
        db.commit()
        ver = models.DocumentVersion(doc_id=doc.id, version_no=1,
                                     storage_path=f"/tmp/big-{d}", status="success")
        db.add(ver)
        db.commit()
        take = per_doc if d < docs - 1 else count - made
        for i in range(take):
            ordinal = made + i
            h = f"第{ordinal % headings + 1}节 主题内容"
            body = f"{h}。这是第 {ordinal} 个分块的正文，用于验证分片归纳不丢内容。"
            if len(body) < text_len:
                body += "本节说明相关制度的适用范围与执行口径。" * 20
                body = body[:text_len]
            db.add(models.Chunk(
                kb_id=kb.id, doc_id=doc.id, doc_version_id=ver.id, ordinal=ordinal,
                text=body, heading=h, locator=f"第 {ordinal} 段",
                keywords="主题 内容", chunk_hash=f"{tag}-{ordinal}",
            ))
        made += take
        db.commit()
    return kb


# ------------------------------------------------------------------ 切片本身
def test_plan_shards_covers_every_chunk_exactly_once(db_session):
    """全库每一块必须恰好进入一个分片：不重、不漏、顺序不乱。"""
    kb = _make_kb(db_session, count=500, headings=20)
    chunks = wiki._chunks_for_kb(db_session, kb.id)

    shards, excerpt = wiki._plan_shards(chunks)
    seen = [idx for entries in shards for idx, _ in entries]

    assert seen == list(range(1, len(chunks) + 1)), (
        "分片编号必须是全库连续的 1..N，否则下游按全局序号取块会错位")
    assert all(entries for entries in shards), "不应出现空分片"
    assert excerpt == config.WIKI_TOPIC_EXCERPT_CHARS, (
        "片数没超上限时不应压缩摘录，保证小库行为与改动前一致")


def test_plan_shards_compresses_excerpt_before_overrunning(db_session, monkeypatch):
    """片数超上限时先压缩摘录；压到下限仍超就如实多调用，不丢块。"""
    monkeypatch.setattr(config, "WIKI_MAX_MAP_CALLS", 4)
    kb = _make_kb(db_session, count=120, headings=8)
    chunks = wiki._chunks_for_kb(db_session, kb.id)

    shards, excerpt = wiki._plan_shards(chunks)
    seen = [idx for entries in shards for idx, _ in entries]

    assert len(shards) <= 4, f"压缩后片数应回到上限内，实际 {len(shards)}"
    assert excerpt < config.WIKI_TOPIC_EXCERPT_CHARS, "应压缩摘录长度而不是丢分块"
    assert seen == list(range(1, len(chunks) + 1)), "压缩后仍必须覆盖全库"


def test_plan_shards_never_drops_content_below_floor(db_session, monkeypatch):
    """上限小到压不下去时，宁可多调用也不能丢块。"""
    monkeypatch.setattr(config, "WIKI_MAX_MAP_CALLS", 2)
    monkeypatch.setattr(config, "WIKI_TOPIC_EXCERPT_MIN", 200)
    kb = _make_kb(db_session, count=300, headings=10)
    chunks = wiki._chunks_for_kb(db_session, kb.id)

    shards, excerpt = wiki._plan_shards(chunks)
    seen = [idx for entries in shards for idx, _ in entries]

    assert excerpt == 200, "应压到下限为止"
    assert len(shards) > 2, "压到下限仍超上限时，允许超上限（如实多调用）"
    assert seen == list(range(1, len(chunks) + 1)), "超上限也不能丢任何分块"


# ------------------------------------------------------------------ 千块级整库
def test_wiki_builds_on_thousand_chunk_library(db_session):
    """1000 个分块（约 1000 份小文档的规模）能跑完，且不静默丢主题。"""
    kb = _make_kb(db_session, count=1000, headings=12)

    result = wiki.build_wiki(db_session, kb)
    db_session.commit()

    pages = db_session.execute(
        select(models.WikiPage).where(models.WikiPage.kb_id == kb.id)
    ).scalars().all()

    assert pages, "千块级库必须能生成 Wiki"
    assert result.pages_skipped == 0, f"不应静默跳过页面：{result.notes}"
    notes = " ".join(result.notes or [])
    assert "共 1000 个分块" in notes, f"必须如实报告覆盖范围，实际 notes={result.notes}"
    assert "未丢弃任何分块" in notes
    # 每个页面都必须挂证据，否则「Wiki 页面」只是没有出处的摘要
    for page in pages:
        assert page.current_version_id
        links = db_session.execute(
            select(models.WikiEvidence).where(
                models.WikiEvidence.page_version_id == page.current_version_id)
        ).scalars().all()
        assert links, f"页面《{page.title}》没有原文证据"
