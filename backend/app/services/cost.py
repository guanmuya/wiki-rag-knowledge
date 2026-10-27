"""模型调用成本的持久化与聚合。

为什么单独建一层，而不是各调用点自己写库：
1. 只有一个写入口，就不会出现「某个路径忘了记」——这正是本次缺陷的成因
   （三处 llm.embed 漏传 stats）。写入口收敛后，统计口径只有一处可改。
2. 成本要能按**阶段**拆：一次文档入库包含「分块向量化」和「Wiki 生成」两段，
   论文里这两段必须分列（一个是检索索引成本，一个是知识整理成本）。
3. 聚合要能兜住历史数据：CostRecord 是本版本新增的表，老库里的任务只有
   Task.result["usage"]。查询时按 task_id 去重回退，保证升级前后数字连续。
"""
from __future__ import annotations

import json
import logging
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from . import llm

log = logging.getLogger("wrk.cost")

# 阶段/任务类型 → 中文标签。新增阶段时这里必须同步，否则前端只能显示英文键。
STAGE_LABEL = {
    models.TaskKind.ingest_document.value: "文档入库",
    models.TaskKind.rebuild_wiki.value: "重建 Wiki",
    models.TaskKind.reindex_kb.value: "重建索引",
    models.TaskKind.embedding_rebuild.value: "换模型全量重建",
    "wiki_edit": "人工校对重建索引",
    "failed_attempt": "失败任务（已消耗）",
}

# 两个「大头」成本分组：论文要的就是这两列的对比。
GROUP_OF_STAGE = {
    models.TaskKind.ingest_document.value: "ingest",
    models.TaskKind.reindex_kb.value: "ingest",
    models.TaskKind.embedding_rebuild.value: "ingest",
    models.TaskKind.rebuild_wiki.value: "wiki",
    "wiki_edit": "wiki",
    "failed_attempt": "other",
}
GROUP_LABEL = {
    "ingest": "文档入库",
    "wiki": "Wiki 生成",
    "other": "其他",
}

_STAT_FIELDS = (
    "calls", "chat_calls", "embed_calls", "vision_calls", "live_calls", "offline_calls",
    "prompt_tokens", "completion_tokens", "embed_tokens", "embed_texts",
    "latency_ms",
)


def empty_totals() -> dict:
    """聚合桶的初始值。total_tokens 由三部分相加得到，不单独累加，避免口径漂移。"""
    return {f: 0 for f in _STAT_FIELDS} | {"total_tokens": 0, "record_count": 0}


def _accumulate(bucket: dict, row: dict) -> None:
    for f in _STAT_FIELDS:
        bucket[f] = bucket.get(f, 0) + int(row.get(f) or 0)
    bucket["total_tokens"] = (bucket.get("prompt_tokens", 0)
                              + bucket.get("completion_tokens", 0)
                              + bucket.get("embed_tokens", 0))
    bucket["record_count"] = bucket.get("record_count", 0) + 1


def record(db: Session, *, kb_id: int | None, kind: str, stats: llm.CallStats | None,
           source: str = "task", source_id: int | None = None,
           doc_id: int | None = None) -> models.CostRecord | None:
    """写一条成本流水。stats 为空或全零时返回 None，不制造空记录。

    为什么全零也要跳过：任务在排队阶段就被取消、或解析直接失败时，
    stats 全是 0。写一堆零记录会让「成本记录条数」失去意义，
    也会让按阶段分组的查询出现大量无意义的键。
    """
    if stats is None or (stats.calls == 0 and stats.embed_texts == 0
                         and stats.latency_ms == 0):
        return None
    if kb_id is None:
        # 没有知识库归属的成本（例如连接测试）不进业务成本表：
        # 它不属于任何库的整理代价，混进来会污染论文数据。
        return None
    row = models.CostRecord(
        kb_id=kb_id, kind=kind, source=source, source_id=source_id, doc_id=doc_id,
        calls=stats.calls, chat_calls=stats.chat_calls, embed_calls=stats.embed_calls,
        vision_calls=stats.vision_calls,
        live_calls=stats.live_calls, offline_calls=stats.offline_calls,
        prompt_tokens=stats.prompt_tokens, completion_tokens=stats.completion_tokens,
        embed_tokens=stats.embed_tokens, embed_texts=stats.embed_texts,
        latency_ms=stats.latency_ms, mode=stats.mode, embed_mode=stats.embed_mode,
    )
    db.add(row)
    return row


def record_phases(db: Session, *, kb_id: int | None, task_kind: str,
                  task_id: int | None, doc_id: int | None,
                  phases: dict[str, dict] | None, total: llm.CallStats | None) -> list[models.CostRecord]:
    """按阶段写多条流水；没有阶段拆分时退回一条总量流水。

    phases 的形态是 {阶段名: usage dict}，由 ingest 侧提供。之所以传 dict 而不是
    CallStats：阶段统计在 ingest 里已经序列化进任务 result，worker 直接复用，
    不必再维护第二份对象，避免两处口径不一致。
    """
    rows: list[models.CostRecord] = []
    if phases:
        for stage, usage in phases.items():
            if not isinstance(usage, dict):
                continue
            sub = llm.CallStats(
                calls=int(usage.get("calls") or 0),
                chat_calls=int(usage.get("chat_calls") or 0),
                embed_calls=int(usage.get("embed_calls") or 0),
                vision_calls=int(usage.get("vision_calls") or 0),
                live_calls=int(usage.get("live_calls") or 0),
                offline_calls=int(usage.get("offline_calls") or 0),
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                embed_tokens=int(usage.get("embed_tokens") or 0),
                embed_texts=int(usage.get("embed_texts") or 0),
                latency_ms=int(usage.get("latency_ms") or 0),
                mode=str(usage.get("mode") or "live"),
                embed_mode=str(usage.get("embed_mode") or "live"),
            )
            row = record(db, kb_id=kb_id, kind=stage, stats=sub, source="task",
                         source_id=task_id, doc_id=doc_id)
            if row is not None:
                rows.append(row)
        if rows:
            return rows
    row = record(db, kb_id=kb_id, kind=task_kind, stats=total, source="task",
                 source_id=task_id, doc_id=doc_id)
    return [row] if row is not None else []


def _legacy_rows(db: Session, kb_id: int, covered: set[int]) -> list[tuple[str, dict]]:
    """老版本任务没有 CostRecord，只能从 Task.result 里回读。

    按 task_id 去重：已经有流水的任务不再读 result，否则新数据会被算两遍。
    解析失败的任务（result 是 "{}" 或被截断）直接跳过，不抛异常——
    一个坏任务不能让整个成本查询 500。
    """
    tasks = db.execute(
        select(models.Task).where(models.Task.kb_id == kb_id)
        .order_by(models.Task.id)
    ).scalars().all()
    out: list[tuple[str, dict]] = []
    for t in tasks:
        if t.id in covered:
            continue
        try:
            result = json.loads(t.result or "{}")
        except ValueError:
            continue
        usage = result.get("usage")
        if not isinstance(usage, dict):
            continue
        out.append((t.kind, usage))
    return out


def kb_summary(db: Session, kb: models.KnowledgeBase) -> dict:
    """聚合一个知识库的累计整理成本。

    返回结构（对外契约，只加字段不删）：
    - totals：全库累计
    - by_group：ingest / wiki / other 三组，论文对比用的主指标
    - by_stage：按阶段/任务类型分组，含中文标签
    - by_mode：live / offline 分别累计，用来剔除「离线演示」的假开销
    - records：最近 50 条明细，便于核对某次操作花了多少
    """
    rows = db.execute(
        select(models.CostRecord).where(models.CostRecord.kb_id == kb.id)
        .order_by(models.CostRecord.id)
    ).scalars().all()

    covered = {r.source_id for r in rows if r.source == "task" and r.source_id}
    legacy = _legacy_rows(db, kb.id, covered)

    totals = empty_totals()
    by_group = {g: empty_totals() for g in GROUP_LABEL}
    by_stage: dict[str, dict] = {}
    by_mode = {"live": empty_totals(), "offline": empty_totals()}

    def feed(kind: str, usage: dict, *, live: bool) -> None:
        _accumulate(totals, usage)
        stage = by_stage.setdefault(kind, empty_totals() | {"label": STAGE_LABEL.get(kind, kind)})
        _accumulate(stage, usage)
        group = GROUP_OF_STAGE.get(kind, "other")
        _accumulate(by_group.setdefault(group, empty_totals()), usage)
        _accumulate(by_mode["live" if live else "offline"], usage)

    for r in rows:
        usage = {
            "calls": r.calls, "chat_calls": r.chat_calls, "embed_calls": r.embed_calls,
            "vision_calls": r.vision_calls,
            "live_calls": r.live_calls, "offline_calls": r.offline_calls,
            "prompt_tokens": r.prompt_tokens, "completion_tokens": r.completion_tokens,
            "embed_tokens": r.embed_tokens, "embed_texts": r.embed_texts,
            "latency_ms": r.latency_ms,
        }
        # 一条流水可能混合了 live 与 offline 调用（例如生成走真实模型、
        # 向量走离线哈希）。按 live_calls 是否占多数归档，明细里仍保留两个计数。
        feed(r.kind, usage, live=r.live_calls >= r.offline_calls)

    for kind, usage in legacy:
        feed(kind, usage, live=int(usage.get("live_calls")
                                   if usage.get("live_calls") is not None
                                   else usage.get("calls") or 0) > 0)

    by_group = {g: v for g, v in by_group.items()
                if v["record_count"] > 0 or g != "other"}

    return {
        "kb_id": kb.id,
        "kb_name": kb.name,
        "mode": kb.mode,
        "mode_label": "Wiki 增强" if kb.mode == models.KbMode.wiki.value else "基础分块",
        "totals": totals,
        "by_group": by_group,
        "group_labels": GROUP_LABEL,
        "by_stage": [
            {"stage": k, **v} for k, v in
            sorted(by_stage.items(), key=lambda kv: -kv[1]["calls"])
        ],
        "by_mode": by_mode,
        "legacy_records": len(legacy),
        "records": [{
            "id": r.id, "kind": r.kind, "kind_label": STAGE_LABEL.get(r.kind, r.kind),
            "source": r.source, "source_id": r.source_id, "doc_id": r.doc_id,
            "calls": r.calls, "chat_calls": r.chat_calls, "embed_calls": r.embed_calls,
            "vision_calls": r.vision_calls,
            "live_calls": r.live_calls, "offline_calls": r.offline_calls,
            "prompt_tokens": r.prompt_tokens, "completion_tokens": r.completion_tokens,
            "embed_tokens": r.embed_tokens, "embed_texts": r.embed_texts,
            "total_tokens": r.prompt_tokens + r.completion_tokens + r.embed_tokens,
            "latency_ms": r.latency_ms, "mode": r.mode, "embed_mode": r.embed_mode,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        } for r in rows[-50:]][::-1],
    }
