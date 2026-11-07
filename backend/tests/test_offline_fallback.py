"""离线降级：模型选择顺序与「为什么离线」的可解释性。"""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import models
from app.db import Base
from app.services import answering, llm, retrieval


def _model(db: Session, name: str, *, is_default=False, tested=None):
    provider = models.Provider(name=f"p-{name}", base_url="http://127.0.0.1:9",
                               enabled=True, protocol=models.DEFAULT_PROTOCOL)
    db.add(provider)
    db.flush()
    cfg = models.ModelConfig(
        provider_id=provider.id, purpose="llm", model=name, label=name,
        is_default=is_default, enabled=True, last_test_ok=tested,
        last_test_at=models.utcnow() if tested else None,
    )
    db.add(cfg)
    db.flush()
    return cfg


def test_resolve_model_prefers_tested_then_default_then_newest():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        tested = _model(db, "tested", tested=True)
        _model(db, "newer-untested")
        assert llm.resolve_model(db, "llm")[0].id == tested.id

        default = _model(db, "default", is_default=True)
        assert llm.resolve_model(db, "llm")[0].id == default.id

        db.query(models.ModelConfig).update({"is_default": False})
        db.query(models.ModelConfig).filter(models.ModelConfig.id == tested.id).update(
            {"last_test_ok": None, "last_test_at": None})
        db.commit()
        newest = db.query(models.ModelConfig).order_by(models.ModelConfig.id.desc()).first()
        assert llm.resolve_model(db, "llm")[0].id == newest.id


def test_error_summary_extracts_message_and_cuts_at_sentence():
    raw = ('供应商返回 429: {"error":{"message":"Free usage limit reached. '
           'Subscribe at https://example.com/go","type":"rate_limit_error"}}')
    assert llm.error_summary(raw, limit=60) == "供应商返回 429：Free usage limit reached."
    assert llm.error_summary("x" * 500, limit=20).endswith("…")


def test_extractive_answer_reports_why_the_model_was_not_used():
    ev = retrieval.Evidence(kb_id=1, kb_name="库", source_type="chunk", rank=1,
                            chunk_id=1, text="一线城市住宿上限 600 元每晚。")
    text = answering._extractive_answer("住宿标准", [ev], reason="供应商返回 429：限流")
    assert "生成模型调用失败" in text and "429" in text
    assert "未调用生成模型" not in text
    assert "未调用生成模型" in answering._extractive_answer("住宿标准", [ev])


def test_call_stats_exposes_offline_reason():
    stats = llm.CallStats(mode="offline", offline_reason="供应商返回 429：限流")
    assert stats.as_dict()["offline_reason"] == "供应商返回 429：限流"
