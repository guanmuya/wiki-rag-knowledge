"""检索层单元测试：RRF 融合、关键词打分、跨库不霸占、路由回退。"""
from __future__ import annotations

import pytest
from sqlalchemy import delete

from app import config, models
from app.services import retrieval


@pytest.fixture(autouse=True)
def _force_offline_embedding(db_session):
    """清掉其他用例留下的模型配置，保证检索走确定性哈希向量、不发网络请求。"""
    db_session.execute(delete(models.ModelConfig))
    db_session.commit()
    yield


def test_rrf_fuse_prefers_consensus():
    # 通道 A：1,2,3  通道 B：2,1,4
    fused = retrieval.rrf_fuse([[1, 2, 3], [2, 1, 4]])
    order = [rid for rid, _, _ in fused]
    assert order[0] in (1, 2), "两通道都靠前的候选应排在最前"
    assert order[0] == 1, "1 在 A 排第 1、B 排第 2，总分高于 2（A2/B1）的对称情况需稳定"
    assert set(order) == {1, 2, 3, 4}


def test_rrf_scores_are_rank_based_not_raw():
    """不同检索方式的原始分数不参与比较：只传排名也应得到合理结果。"""
    fused = retrieval.rrf_fuse([[10], [11]])
    assert len(fused) == 2
    scores = [s for _, s, _ in fused]
    assert abs(scores[0] - scores[1]) < 1e-9, "各通道第 1 名应同分"


def test_keyword_rank_hits_relevant_first():
    rows = [
        (1, "住宿 上限 一线城市 600", "一线城市住宿上限 600 元每晚。"),
        (2, "交通 高铁 二等座", "高铁二等座为标准配置。"),
        (3, "审批 财务 付款", "财务在收到完整单据后 5 个工作日内完成付款。"),
    ]
    ranked = retrieval.keyword_rank(rows, "一线城市住宿上限多少", limit=3)
    assert ranked, "应有关键词命中"
    assert ranked[0][0] == 1


def test_keyword_rank_empty_query():
    rows = [(1, "a", "b")]
    assert retrieval.keyword_rank(rows, "", limit=3) == []


def test_cosine_basic():
    assert retrieval.cosine([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert retrieval.cosine([1.0, 0.0], [0.0, 1.0]) == 0.0
    assert retrieval.cosine([], [1.0]) == 0.0


def test_query_terms_covers_chinese_bigrams():
    terms = retrieval.query_terms("差旅报销标准")
    assert "差旅" in terms and "报销" in terms


def _make_kb(db, name, mode):
    kb = models.KnowledgeBase(name=name, mode=mode, status="ready")
    db.add(kb)
    db.commit()
    return kb


def test_retrieve_respects_per_kb_limit(db_session, sample_text):
    from app.services import ingest  # noqa: F401  确保模块可导入
    kb = _make_kb(db_session, "限流测试库", "basic")
    # 手工灌入 20 个分块，验证单库上限
    doc = models.Document(kb_id=kb.id, filename="a.md", suffix=".md", status="ready")
    db_session.add(doc)
    db_session.commit()
    ver = models.DocumentVersion(doc_id=doc.id, version_no=1, storage_path="/tmp/x",
                                 status="success")
    db_session.add(ver)
    db_session.commit()
    for i in range(20):
        db_session.add(models.Chunk(
            kb_id=kb.id, doc_id=doc.id, doc_version_id=ver.id, ordinal=i,
            text=f"差旅报销标准 第 {i} 条说明内容。", keywords="差旅 报销 标准",
            chunk_hash=f"h{i}",
        ))
    db_session.commit()

    evidence, routes, stats = retrieval.retrieve(db_session, [kb], "差旅报销标准", top_k=5)
    assert len(evidence) <= config.MAX_EVIDENCE
    assert stats["per_kb_limit"] == 5
    assert routes[0].used == "chunk"
    assert all(ev.source_type == "chunk" for ev in evidence)


def test_retrieve_wiki_fallback_to_chunk(db_session):
    """Wiki 模式但页面未生成 → 回退分块并标记 fallback。"""
    kb = _make_kb(db_session, "Wiki未就绪库", "wiki")
    doc = models.Document(kb_id=kb.id, filename="b.md", suffix=".md", status="ready")
    db_session.add(doc)
    db_session.commit()
    ver = models.DocumentVersion(doc_id=doc.id, version_no=1, storage_path="/tmp/y",
                                 status="success")
    db_session.add(ver)
    db_session.commit()
    db_session.add(models.Chunk(
        kb_id=kb.id, doc_id=doc.id, doc_version_id=ver.id, ordinal=0,
        text="年假入职满一年 5 天，此后每满一年增加 1 天。", keywords="年假 入职 增加",
        chunk_hash="hv1",
    ))
    db_session.commit()

    evidence, routes, _ = retrieval.retrieve(db_session, [kb], "年假有几天")
    assert routes[0].used == "chunk"
    assert routes[0].fallback is True
    assert "回退" in routes[0].reason
    assert evidence and evidence[0].source_type == "chunk"


def test_retrieve_empty_kb_reports_none(db_session):
    kb = _make_kb(db_session, "空库", "basic")
    evidence, routes, _ = retrieval.retrieve(db_session, [kb], "任何问题")
    assert evidence == []
    assert routes[0].used == "none"
    assert "没有可检索" in routes[0].reason


def test_cross_kb_balance_does_not_let_big_kb_dominate(db_session):
    """大库有很多命中时，小库仍应获得代表位。"""
    big = _make_kb(db_session, "大库", "basic")
    small = _make_kb(db_session, "小库", "basic")
    for kb, count in ((big, 12), (small, 1)):
        doc = models.Document(kb_id=kb.id, filename=f"{kb.name}.md", suffix=".md", status="ready")
        db_session.add(doc)
        db_session.commit()
        ver = models.DocumentVersion(doc_id=doc.id, version_no=1, storage_path="/tmp/z",
                                     status="success")
        db_session.add(ver)
        db_session.commit()
        for i in range(count):
            db_session.add(models.Chunk(
                kb_id=kb.id, doc_id=doc.id, doc_version_id=ver.id, ordinal=i,
                text=f"信息安全 数据 脱敏 要求 第 {i} 条。", keywords="信息安全 数据 脱敏",
                chunk_hash=f"{kb.id}-{i}",
            ))
        db_session.commit()

    evidence, _, _ = retrieval.retrieve(db_session, [big, small], "信息安全数据脱敏", top_k=4)
    kb_ids = {ev.kb_id for ev in evidence}
    assert small.id in kb_ids, "小库不应被大库完全挤掉"


def test_build_context_respects_budget():
    ev = retrieval.Evidence(
        kb_id=1, kb_name="库", source_type="chunk", text="x" * 5000,
        doc_name="a.md", version_no=1, locator="第 1 页", chunk_id=1,
    )
    ctx = retrieval.build_context([ev], budget=1000)
    assert len(ctx) <= 1100
