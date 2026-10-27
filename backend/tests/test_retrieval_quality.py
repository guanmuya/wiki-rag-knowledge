"""检索质量回归测试：用固定的中文语料验证排序是否合理。

这些用例的价值在于——只要有人改坏了 IDF、覆盖率、跨库融合或分块边界，
正确条目掉出首位就会被立刻发现，而不是等到演示时才发现答非所问。
"""
from __future__ import annotations

import pytest
from sqlalchemy import delete

from app import models
from app.services import chunking, parser, retrieval


@pytest.fixture(autouse=True)
def _offline(db_session):
    db_session.execute(delete(models.ModelConfig))
    db_session.commit()
    yield


CORPUS = """# 人事制度

## 考勤与工时

标准工时为每周五天、每天八小时。迟到单次超过 30 分钟按事假半天计。

## 请假制度

年假入职满一年 5 天，此后每满一年增加 1 天。年假可跨年顺延至次年 3 月 31 日。

## 报销与费用

报销单据需在费用发生后 30 天内提交。一线城市住宿上限 600 元每晚。

## 离职管理

员工提出离职需提前 30 天提交书面申请，试用期内提前 3 天。
"""

SUPPORT = """# 客户支持

## 支持渠道与响应时效

P0 级工单首次响应 15 分钟，解决目标 4 小时。P1 级首次响应 30 分钟。

## 服务等级与赔付

标准版服务可用性承诺为 99.5%，企业版为 99.9%。

## 备份与恢复

数据库每日 02:00 全量备份，每小时增量备份，保留 30 天。
"""


def _ingest(db, kb, text, filename="doc.md"):
    doc = models.Document(kb_id=kb.id, filename=filename, suffix=".md", status="ready")
    db.add(doc)
    db.commit()
    ver = models.DocumentVersion(doc_id=doc.id, version_no=1, storage_path="/tmp/x", status="success")
    db.add(ver)
    db.commit()

    blocks = [parser.ParsedBlock(text=b, heading=h, locator=f"第 {i+1} 段")
              for i, (b, h) in enumerate(_blocks(text))]
    units = chunking.split_blocks(blocks)
    for u in units:
        db.add(models.Chunk(
            kb_id=kb.id, doc_id=doc.id, doc_version_id=ver.id, ordinal=u.ordinal,
            text=u.text, heading=u.heading, locator=u.locator,
            keywords=chunking.keyword_field(u.text),
            chunk_hash=chunking.chunk_hash(u.text, u.heading),
        ))
    db.commit()
    return doc, ver


def _blocks(md: str):
    heading = ""
    for line in md.splitlines():
        m = line.strip()
        if m.startswith("## "):
            heading = m[3:].strip()
            continue
        if m and not m.startswith("#"):
            yield m, heading


@pytest.fixture()
def corpus(db_session):
    """每个用例独立命名，避免 SQLite 唯一约束在参数化用例间冲突。"""
    import uuid
    tag = uuid.uuid4().hex[:8]
    kb1 = models.KnowledgeBase(name=f"排序回归-人事-{tag}", mode="basic", status="ready")
    kb2 = models.KnowledgeBase(name=f"排序回归-支持-{tag}", mode="basic", status="ready")
    db_session.add_all([kb1, kb2])
    db_session.commit()
    _ingest(db_session, kb1, CORPUS, f"人事-{tag}.md")
    _ingest(db_session, kb2, SUPPORT, f"支持-{tag}.md")
    yield kb1, kb2
    # 清理，避免用例间互相污染检索语料
    for kb in (kb1, kb2):
        db_session.execute(delete(models.Chunk).where(models.Chunk.kb_id == kb.id))
        db_session.execute(delete(models.WikiSection).where(models.WikiSection.kb_id == kb.id))
        db_session.execute(delete(models.Chunk).where(models.Chunk.kb_id == kb.id))
        db_session.delete(kb)
    db_session.commit()


def test_chunking_splits_on_heading_change(db_session):
    """标题变化必须切块，不能把「考勤」和「请假」并进同一块。"""
    blocks = [parser.ParsedBlock(text=b, heading=h, locator=f"p{i}", kind="text")
              for i, (b, h) in enumerate(_blocks(CORPUS))]
    units = chunking.split_blocks(blocks)
    assert len(units) >= 4, "每个二级标题至少一块"
    for u in units:
        assert u.heading, "分块必须带所属标题"
        # 同一块里不应同时出现两个不同章节的标志性内容
        if "迟到" in u.text:
            assert "年假" not in u.text, "考勤内容不应混入请假内容"


def test_no_overlap_across_headings(db_session):
    blocks = [
        parser.ParsedBlock(text="考勤说明内容足够长可以成为一个块。", heading="考勤", locator="p1"),
        parser.ParsedBlock(text="请假说明内容同样足够长可以成为另一个块。", heading="请假", locator="p2"),
    ]
    units = chunking.split_blocks(blocks, size=20, overlap=15)
    for u in units:
        if "请假" in u.text:
            assert "考勤" not in u.text, "overlap 不得把上一章节内容带进新章节"


@pytest.mark.parametrize("question,expected_kb,expected_keyword", [
    ("一线城市住宿上限是多少", 0, "600"),
    ("年假可以顺延到什么时候", 0, "3 月 31 日"),
    ("离职需要提前多久", 0, "30 天"),
    ("P0 工单首次响应时限", 1, "15 分钟"),
    ("标准版可用性承诺是多少", 1, "99.5%"),
    ("数据库备份保留多久", 1, "30 天"),
])
def test_correct_evidence_ranks_first(db_session, corpus, question, expected_kb, expected_keyword):
    kb1, kb2 = corpus
    evidence, routes, stats = retrieval.retrieve(db_session, [kb1, kb2], question)
    assert evidence, f"「{question}」应有检索结果"
    top = evidence[0]
    assert expected_keyword in top.text, (
        f"「{question}」首位应为含「{expected_keyword}」的证据，实际是：{top.text[:80]}")
    assert top.coverage > 0, "首位证据必须命中查询词"


def test_cross_kb_query_covers_both(db_session, corpus):
    """问句同时涉及两个库时，两库都应进入结果。"""
    kb1, kb2 = corpus
    evidence, routes, _ = retrieval.retrieve(db_session, [kb1, kb2], "年假天数和服务可用性承诺")
    kb_ids = {e.kb_id for e in evidence}
    assert kb_ids == {kb1.id, kb2.id}, "跨库问题应同时命中两个知识库"
    assert all(r.used in ("chunk", "wiki") for r in routes)


def test_no_result_for_unrelated_query(db_session, corpus):
    kb1, kb2 = corpus
    evidence, routes, stats = retrieval.retrieve(db_session, [kb1, kb2], "量子计算机的退相干时间")
    if evidence:
        assert all(e.coverage == 0 for e in evidence), "完全不相关的查询不应产生有命中的证据"


def test_coverage_is_comparable_across_retrievers(db_session, corpus):
    """覆盖率是 0~1 的绝对量，与库大小和检索方式无关。"""
    kb1, kb2 = corpus
    evidence, _, _ = retrieval.retrieve(db_session, [kb1, kb2], "住宿上限")
    for e in evidence:
        assert 0.0 <= e.coverage <= 1.0
    scores = [e.coverage for e in evidence]
    assert scores == sorted(scores, reverse=True), "结果应按覆盖率降序"


def test_rank_is_monotonic(db_session, corpus):
    kb1, kb2 = corpus
    evidence, _, _ = retrieval.retrieve(db_session, [kb1, kb2], "年假 报销 工单")
    assert [e.rank for e in evidence] == list(range(1, len(evidence) + 1))
