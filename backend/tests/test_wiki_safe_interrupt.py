"""Wiki 停止检查点及每页原子发布的回归测试。"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app import models
from app.db import SessionLocal
from app.services import llm, wiki


class StopWiki(Exception):
    pass


def _seed(db):
    tag = uuid.uuid4().hex
    kb = models.KnowledgeBase(name=f"安全中断-{tag}", mode="wiki", status="ready")
    db.add(kb)
    db.flush()
    doc = models.Document(kb_id=kb.id, filename="source.md", status="ready")
    db.add(doc)
    db.flush()
    version = models.DocumentVersion(doc_id=doc.id, version_no=1, storage_path="source.md",
                                     status="success")
    db.add(version)
    db.flush()
    for ordinal in range(2):
        db.add(models.Chunk(kb_id=kb.id, doc_id=doc.id, doc_version_id=version.id,
                            ordinal=ordinal, text=f"来源内容 {ordinal}，供 Wiki 引用的完整文字。" * 4,
                            heading=f"章节{ordinal}", locator=f"第 {ordinal + 1} 段"))
    db.commit()
    return kb


def _topics(monkeypatch, *, two=True):
    topics = [{"title": "Alpha handbook", "summary": "first", "chunk_ids": [1]}]
    if two:
        topics.append({"title": "Beta security", "summary": "second", "chunk_ids": [2]})
    monkeypatch.setattr(wiki, "_map_topics", lambda *args, **kwargs: topics)

    def chat(_db, _messages, *, stats, **_kwargs):
        stats.record_chat(prompt_tokens=7, completion_tokens=11)
        return "## 流程\n" + "完整页面正文，用于检索和展示。" * 5 + " [1]"

    def embed(_db, texts, *, stats):
        stats.record_embed(texts=len(texts), tokens=13)
        return [[0.25, 0.5] for _ in texts]

    monkeypatch.setattr(llm, "chat", chat)
    monkeypatch.setattr(llm, "embed", embed)
    monkeypatch.setattr(llm, "active_embedding", lambda _db: (None, None, "test-embedding", 2))


def _page_snapshot(db, kb_id):
    pages = list(db.execute(select(models.WikiPage).where(models.WikiPage.kb_id == kb_id)
                            .order_by(models.WikiPage.id)).scalars())
    return [(page.id, page.current_version_id, page.status) for page in pages]


def _assert_complete(db, page_id, version_id):
    version = db.get(models.WikiPageVersion, version_id)
    assert version is not None and version.page_id == page_id and version.content_md
    sections = db.execute(select(models.WikiSection).where(
        models.WikiSection.page_version_id == version_id)).scalars().all()
    evidence = db.execute(select(models.WikiEvidence).where(
        models.WikiEvidence.page_version_id == version_id)).scalars().all()
    assert sections and all(section.page_id == page_id for section in sections)
    assert evidence and all(link.chunk_id for link in evidence)


def test_stop_after_complete_page_preserves_page_and_usage(db_session, monkeypatch):
    kb = _seed(db_session)
    _topics(monkeypatch)
    stats = llm.CallStats()

    def check_cancel():
        # 使用不同连接观察，避免将会话中尚未提交的数据误认为已发布。
        with SessionLocal() as observer:
            pages = _page_snapshot(observer, kb.id)
            if pages:
                assert len(pages) == 1
                page_id, version_id, status = pages[0]
                assert status == models.WikiPageStatus.published.value
                _assert_complete(observer, page_id, version_id)
                raise StopWiki("用户请求停止")

    with pytest.raises(StopWiki, match="用户请求停止"):
        wiki.build_wiki(db_session, kb, stats=stats, check_cancel=check_cancel)
    db_session.rollback()
    with SessionLocal() as observer:
        pages = _page_snapshot(observer, kb.id)
        assert len(pages) == 1
        _assert_complete(observer, pages[0][0], pages[0][1])
    assert stats.chat_calls == 1 and stats.embed_calls == 1
    assert stats.prompt_tokens == 7 and stats.embed_tokens == 13


def test_stop_between_map_shards_never_starts_next_shard(db_session, monkeypatch):
    kb = _seed(db_session)
    chunks = wiki._chunks_for_kb(db_session, kb.id)
    monkeypatch.setattr(wiki, "_plan_shards",
                        lambda _chunks: ([[(1, chunks[0])], [(2, chunks[1])]], 100))
    calls = []

    def map_topic(_db, _entries, **kwargs):
        calls.append(kwargs["shard_no"])
        return []

    monkeypatch.setattr(wiki, "_map_topics", map_topic)

    def check_cancel():
        if calls:
            raise StopWiki()

    with pytest.raises(StopWiki):
        wiki.build_wiki(db_session, kb, check_cancel=check_cancel)
    assert calls == [1]
    assert _page_snapshot(db_session, kb.id) == []


def test_failed_publication_leaves_old_version_intact(db_session, monkeypatch):
    kb = _seed(db_session)
    _topics(monkeypatch, two=False)
    wiki.build_wiki(db_session, kb)
    db_session.commit()
    old_page, old_version, _ = _page_snapshot(db_session, kb.id)[0]
    real_flush = db_session.flush

    def fail_when_sections_are_flushed(*args, **kwargs):
        if any(isinstance(obj, models.WikiSection) for obj in db_session.new):
            # 新页面版本已在写事务中 flush，其他连接仍必须只看到旧版本。
            with SessionLocal() as observer:
                assert _page_snapshot(observer, kb.id)[0][1] == old_version
                _assert_complete(observer, old_page, old_version)
            raise RuntimeError("模拟章节写入失败")
        return real_flush(*args, **kwargs)

    monkeypatch.setattr(db_session, "flush", fail_when_sections_are_flushed)
    with pytest.raises(RuntimeError, match="模拟章节写入失败"):
        wiki.build_wiki(db_session, kb)
    db_session.rollback()
    with SessionLocal() as observer:
        assert _page_snapshot(observer, kb.id) == [(old_page, old_version, "published")]
        _assert_complete(observer, old_page, old_version)
        versions = observer.execute(select(models.WikiPageVersion).where(
            models.WikiPageVersion.page_id == old_page)).scalars().all()
        assert len(versions) == 1


def test_preparation_does_not_hold_write_lock_or_publish_early(db_session, monkeypatch):
    kb = _seed(db_session)
    _topics(monkeypatch, two=False)
    wiki.build_wiki(db_session, kb)
    db_session.commit()
    old_page, old_version, _ = _page_snapshot(db_session, kb.id)[0]
    real_embed = llm.embed
    real_pack = llm.pack_vector
    observations = []

    def assert_concurrent_write(phase):
        with SessionLocal() as observer:
            assert _page_snapshot(observer, kb.id)[0][1] == old_version
            _assert_complete(observer, old_page, old_version)
            observer.add(models.AppSetting(key=f"wiki-{phase}-{uuid.uuid4().hex}", value="ok"))
            observer.commit()
        observations.append(phase)

    def embed(db, texts, *, stats):
        assert_concurrent_write("embed")
        # 真实 llm.embed 可能提交模型配置，不能因此提前发布页面。
        db.commit()
        return real_embed(db, texts, stats=stats)

    def pack(vector):
        assert_concurrent_write("pack")
        return real_pack(vector)

    monkeypatch.setattr(llm, "embed", embed)
    monkeypatch.setattr(llm, "pack_vector", pack)
    result = wiki.build_wiki(db_session, kb)
    assert result.pages_updated == 1 and result.sections > 0
    assert "embed" in observations and "pack" in observations
    with SessionLocal() as observer:
        page_id, version_id, _ = _page_snapshot(observer, kb.id)[0]
        assert page_id == old_page and version_id != old_version
        _assert_complete(observer, page_id, version_id)
        _assert_complete(observer, old_page, old_version)


def test_human_lock_set_during_embedding_is_respected(db_session, monkeypatch):
    kb = _seed(db_session)
    _topics(monkeypatch, two=False)
    wiki.build_wiki(db_session, kb)
    db_session.commit()
    old_page, old_version, _ = _page_snapshot(db_session, kb.id)[0]
    real_embed = llm.embed

    def embed(db, texts, *, stats):
        with SessionLocal() as other:
            page = other.get(models.WikiPage, old_page)
            page.human_locked = True
            other.commit()
        return real_embed(db, texts, stats=stats)

    monkeypatch.setattr(llm, "embed", embed)
    result = wiki.build_wiki(db_session, kb)
    assert result.pages_skipped == 1 and result.pages_updated == 0
    with SessionLocal() as observer:
        page = observer.get(models.WikiPage, old_page)
        assert page.human_locked and page.status == models.WikiPageStatus.needs_review.value
        assert page.current_version_id == old_version
        _assert_complete(observer, old_page, old_version)
