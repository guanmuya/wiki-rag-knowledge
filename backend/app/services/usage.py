"""Read-only, global aggregation of recorded processing and chat usage."""
from __future__ import annotations

import datetime as dt
import json

from sqlalchemy import Integer, Text, func, literal, select, union_all
from sqlalchemy.orm import Session

from .. import models

# Only these counters are exposed. In particular, do not copy arbitrary data from
# stored task results or message routes into the response.
_FIELDS = (
    "calls", "live_calls", "offline_calls", "chat_calls", "embed_calls", "vision_calls",
    "prompt_tokens", "completion_tokens", "embed_tokens", "latency_ms",
)
_SOURCE_LABELS = {"processing": "知识库处理", "chat": "问答"}


def _today_utc() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()


def _utc_date(value: dt.datetime | None) -> dt.date | None:
    if not isinstance(value, dt.datetime):
        return None
    # Existing database timestamps are stored as naive UTC; preserve an offset
    # when a database driver does return an aware datetime.
    if value.tzinfo is not None:
        value = value.astimezone(dt.timezone.utc)
    return value.date()


def _empty() -> dict[str, int]:
    return {field: 0 for field in _FIELDS} | {"total_tokens": 0, "record_count": 0}


def _valid_counters(usage: object) -> dict[str, int]:
    if not isinstance(usage, dict):
        return {}
    # CallStats writes non-negative integers. Ignore corrupt or missing fields
    # without inventing counts from modes, other counters, or total_tokens.
    return {
        field: value for field in _FIELDS
        if type(value := usage.get(field)) is int and value >= 0
    }


def _json_counters(raw: str | None) -> dict[str, int]:
    try:
        data = json.loads(raw or "{}")
    except (TypeError, ValueError, RecursionError):
        return {}
    return _valid_counters(data.get("usage")) if isinstance(data, dict) else {}


def _add(bucket: dict[str, int], counters: dict[str, int]) -> None:
    for field, value in counters.items():
        bucket[field] += value
    bucket["total_tokens"] = (bucket["prompt_tokens"] + bucket["completion_tokens"]
                              + bucket["embed_tokens"])
    bucket["record_count"] += 1


def summarize(db: Session, days: int) -> dict:
    """Aggregate source rows once using UTC dates; zero-fill bounded windows."""
    today = _today_utc()
    start = today - dt.timedelta(days=days - 1) if days else None
    daily = {
        (start + dt.timedelta(days=offset)): _empty()
        for offset in range(days)
    } if start is not None else {}
    totals = _empty()
    by_source = {source: _empty() for source in _SOURCE_LABELS}

    def date_in_period(timestamp: dt.datetime | None) -> dt.date | None:
        date = _utc_date(timestamp)
        if date is None or (start is not None and not start <= date <= today):
            return None
        return date

    def feed(source: str, date: dt.date, counters: dict[str, int],
             *, cost_record: bool = False) -> None:
        # CostRecord rows always count, even if every stored counter is invalid.
        # JSON sources require at least one valid recorded counter.
        if not cost_record and not counters:
            return
        _add(totals, counters)
        _add(by_source[source], counters)
        _add(daily.setdefault(date, _empty()), counters)

    def bounded(query, timestamp):
        if start is None:
            return query
        # App-written timestamps are naive UTC. Allow one extra day on either
        # side for databases returning timezone-aware values, then check exact
        # UTC dates in Python before reading any JSON usage.
        lower = dt.datetime.combine(start - dt.timedelta(days=1), dt.time.min)
        upper = dt.datetime.combine(today + dt.timedelta(days=2), dt.time.min)
        return query.where(timestamp >= lower, timestamp < upper)

    # Read exactly the fields needed by the contract, never contents or metadata.
    # A single statement gives all three sources (including the deduplication
    # subquery) the same database snapshot, even while workers commit new rows.
    fields = [getattr(models.CostRecord, field) for field in _FIELDS]
    empty_counters = [literal(None, type_=Integer) for _ in _FIELDS]
    record_query = bounded(
        select(literal("record"), models.CostRecord.created_at,
               literal(None, type_=Text), *fields),
        models.CostRecord.created_at,
    )

    # This subquery deliberately has no date predicate: even a phase recorded
    # outside the selected window suppresses the task's aggregate snapshot.
    covered_task_ids = select(models.CostRecord.source_id).where(
        models.CostRecord.source == "task", models.CostRecord.source_id.is_not(None)
    )
    task_timestamp = func.coalesce(models.Task.finished_at, models.Task.created_at)
    task_query = select(literal("task"), task_timestamp, models.Task.result, *empty_counters).where(
        models.Task.kb_id.is_not(None), ~models.Task.id.in_(covered_task_ids)
    )
    task_query = bounded(task_query, task_timestamp)

    # A conversation may use several KBs, but usage belongs to one message.
    message_query = select(
        literal("chat"), models.Message.created_at, models.Message.route, *empty_counters
    ).where(models.Message.role == "assistant", models.Message.status == "ok")
    message_query = bounded(message_query, models.Message.created_at)

    # Do not flush a caller's unrelated pending writes just to build a dashboard.
    with db.no_autoflush:
        rows = db.execute(union_all(record_query, task_query, message_query)).yield_per(500)
        for kind, timestamp, raw, *values in rows:
            date = date_in_period(timestamp)
            if date is None:
                continue
            if kind == "record":
                feed("processing", date, _valid_counters(dict(zip(_FIELDS, values))),
                     cost_record=True)
            else:
                feed("processing" if kind == "task" else "chat", date, _json_counters(raw))

    return {
        "period_days": days,
        "timezone": "UTC",
        "totals": totals,
        "daily": [{"date": date.isoformat(), **bucket} for date, bucket in sorted(daily.items())],
        "by_source": [
            {"source": source, "label": label, **by_source[source]}
            for source, label in _SOURCE_LABELS.items()
        ],
    }
