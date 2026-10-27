"""Wiki 生成的回归测试：采样窗口与主题编号必须一致。

曾经的 bug：LLM 分支用前 60 条采样，离线分支用全量 chunks，
后者返回的 chunk_ids 是「全量序号」，而下游只在采样里查找 ——
库内超过 60 块时序号越界，页面被静默丢弃且无任何提示。
实测 80 块时 12 个主题只生成 1 页。
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from app import models
from app.services import wiki


def _make_kb_with_chunks(db, count: int, headings: int = 12) -> models.KnowledgeBase:
    tag = uuid.uuid4().hex[:8]
    kb = models.KnowledgeBase(name=f"Wiki采样-{tag}", mode="wiki", status="ready")
    db.add(kb)
    db.commit()
    doc = models.Document(kb_id=kb.id, filename="big.md", suffix=".md", status="ready")
    db.add(doc)
    db.commit()
    ver = models.DocumentVersion(doc_id=doc.id, version_no=1, storage_path="/tmp/big",
                                 status="success")
    db.add(ver)
    db.commit()
    for i in range(count):
        h = f"第{i % headings + 1}节 主题内容"
        db.add(models.Chunk(
            kb_id=kb.id, doc_id=doc.id, doc_version_id=ver.id, ordinal=i,
            text=f"{h}。这是第 {i} 个分块的正文内容，用于验证主题归纳不会丢页。",
            heading=h, locator=f"第 {i} 段",
            keywords=f"主题 内容 {i}", chunk_hash=f"{tag}-{i}",
        ))
    db.commit()
    return kb


def test_wiki_covers_all_topics_beyond_sample_window(db_session):
    """80 块（超过 60 采样窗口）时，12 个主题都应生成页面。"""
    kb = _make_kb_with_chunks(db_session, count=80, headings=12)

    result = wiki.build_wiki(db_session, kb)
    db_session.commit()

    pages = db_session.execute(
        select(func.count(models.WikiPage.id)).where(models.WikiPage.kb_id == kb.id)
    ).scalar() or 0

    assert pages >= 10, (
        f"12 个主题应生成接近 12 个页面，实际只有 {pages} 个"
        f"（created={result.pages_created} updated={result.pages_updated} "
        f"skipped={result.pages_skipped} notes={result.notes}）"
    )
    assert result.pages_skipped == 0, (
        f"不应有页面被静默跳过，实际跳过 {result.pages_skipped}：{result.notes}")


def test_wiki_reports_truncated_sample(db_session):
    """分块数超过采样窗口时，必须在 notes 里说明，不能假装覆盖全库。"""
    kb = _make_kb_with_chunks(db_session, count=80, headings=6)
    result = wiki.build_wiki(db_session, kb)
    db_session.commit()

    joined = " ".join(result.notes or [])
    assert "只用了前" in joined or "共" in joined, (
        f"应说明只用了采样窗口，实际 notes={result.notes}")


def test_wiki_pages_have_evidence_links(db_session):
    """每个生成的页面都必须挂回原文证据，不能只有摘要。"""
    kb = _make_kb_with_chunks(db_session, count=30, headings=5)
    wiki.build_wiki(db_session, kb)
    db_session.commit()

    pages = db_session.execute(
        select(models.WikiPage).where(models.WikiPage.kb_id == kb.id)
    ).scalars().all()
    assert pages
    for p in pages:
        assert p.current_version_id
        links = db_session.execute(
            select(func.count(models.WikiEvidence.id)).where(
                models.WikiEvidence.page_version_id == p.current_version_id)
        ).scalar() or 0
        assert links > 0, f"页面《{p.title}》没有原文证据关联"


def test_skipped_pages_always_have_reason(db_session):
    """跳过的页面必须有可读原因，否则用户无法排查。"""
    kb = _make_kb_with_chunks(db_session, count=20, headings=4)
    result = wiki.build_wiki(db_session, kb)
    db_session.commit()
    if result.pages_skipped:
        assert result.notes, "有页面被跳过却没有任何说明"
