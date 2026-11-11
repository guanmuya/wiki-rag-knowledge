"""Read-only, global aggregation of recorded processing, chat and agent usage."""
from __future__ import annotations

import datetime as dt
import json

from sqlalchemy import Integer, Text, func, literal, select, union_all
from sqlalchemy.orm import Session

from .. import models
from . import pricing
from .agent import plugins as agent_plugins

# Only these counters are exposed. In particular, do not copy arbitrary data from
# stored task results, message routes or agent metadata into the response.
_FIELDS = (
    "calls", "live_calls", "offline_calls", "chat_calls", "embed_calls", "vision_calls",
    "prompt_tokens", "completion_tokens", "embed_tokens", "latency_ms",
)
_TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "embed_tokens")
_SOURCE_LABELS = {"processing": "知识库处理", "chat": "问答", "agent": "个人 Agent"}
# 个人 Agent 跑完但没留下任何 token 时只记「发生了一次调用」：执行确实发生过，
# 不能因为供应商/CLI 没回报用量就从统计里消失，也不能凭空填一个 token 数。
_UNMETERED_AGENT_COUNTERS = {"calls": 1, "chat_calls": 1, "live_calls": 1}


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


def _empty() -> dict:
    return {field: 0 for field in _FIELDS} | {
        "total_tokens": 0,
        "record_count": 0,
        # 有调用记录、但一条 token 都没记下来的行数（供应商没回报 / 纯文本 CLI）。
        "unmetered_records": 0,
        "cost": {
            "currency": pricing.CURRENCY,
            "amount": 0.0,
            "priced_records": 0,
            "unpriced_records": 0,
            "unpriced_tokens": 0,
        },
    }


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


def _cost_delta(counters: dict[str, int], price: dict | None) -> dict:
    """一条记录对金额的贡献。

    只给生成 token 计价（输入价 × prompt + 输出价 × completion）；向量 token
    一律计入未计价——记录里没有保存向量模型，拿生成模型的价去估向量费用会
    系统性算错。找不到单价的行整行计入未计价，不做任何推测。
    """
    prompt = counters.get("prompt_tokens", 0)
    completion = counters.get("completion_tokens", 0)
    embed = counters.get("embed_tokens", 0)
    tokens = prompt + completion + embed
    delta = {"amount": 0.0, "priced_records": 0, "unpriced_records": 0,
             "unpriced_tokens": 0}
    if tokens <= 0:
        return delta
    if price is None:
        delta["unpriced_records"] = 1
        delta["unpriced_tokens"] = tokens
        return delta
    delta["amount"] = (prompt * price["input_per_1m"]
                       + completion * price["output_per_1m"]) / pricing.TOKENS_PER_PRICE_UNIT
    delta["priced_records"] = 1
    delta["unpriced_tokens"] = embed
    return delta


def _add(bucket: dict, counters: dict[str, int], cost: dict | None = None) -> None:
    for field, value in counters.items():
        bucket[field] += value
    bucket["total_tokens"] = (bucket["prompt_tokens"] + bucket["completion_tokens"]
                              + bucket["embed_tokens"])
    bucket["record_count"] += 1
    if not any(counters.get(field, 0) for field in _TOKEN_FIELDS):
        bucket["unmetered_records"] += 1
    if cost:
        bucket["cost"]["amount"] += cost["amount"]
        bucket["cost"]["priced_records"] += cost["priced_records"]
        bucket["cost"]["unpriced_records"] += cost["unpriced_records"]
        bucket["cost"]["unpriced_tokens"] += cost["unpriced_tokens"]


def _prices_by_config(db: Session) -> dict[int, dict]:
    """所有模型配置的生效单价（用户价优先，其次内置参考价）。

    一次性查完再开始流式读用量：SQLite 同一连接上边读结果集边发查询会互相
    干扰，而模型配置只有几十行，提前查的开销可以忽略。用户单价按
    `model_config_id` 精确命中，内置价按归一化键匹配（见 `services/pricing.py`）。
    """
    user_rows = pricing.user_prices(db)
    prices: dict[int, dict] = {}
    for cfg_id, model in db.execute(
        select(models.ModelConfig.id, models.ModelConfig.model)
    ).all():
        price = pricing.effective_price(cfg_id, model, user_rows.get(cfg_id))
        if price["origin"] != pricing.ORIGIN_NONE:
            prices[cfg_id] = price
    return prices


def _core_label(core_id: str) -> str:
    """个人 Agent 核心的展示名：内置核心取清单里的名字，插件/未知核心回落 id。"""
    plugin = agent_plugins.BUILTIN_BY_ID.get(core_id or "")
    if plugin is not None:
        return plugin.name
    return core_id or "未知核心"


def _final(bucket: dict) -> dict:
    """输出前抹掉浮点尾差；金额保留 8 位小数（一分钱 = 0.01）。"""
    bucket["cost"]["amount"] = round(bucket["cost"]["amount"], 8)
    return bucket


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
    # 个人 Agent 再按核心拆一层；其它来源没有「核心」概念，不进这张表。
    by_core: dict[str, dict] = {}
    prices: dict[int, dict] = {}

    def date_in_period(timestamp: dt.datetime | None) -> dt.date | None:
        date = _utc_date(timestamp)
        if date is None or (start is not None and not start <= date <= today):
            return None
        return date

    def feed(source: str, date: dt.date, counters: dict[str, int],
             *, cost_record: bool = False, model_config_id: int | None = None,
             core_id: str = "") -> None:
        # CostRecord rows always count, even if every stored counter is invalid.
        # JSON sources require at least one valid recorded counter.
        if not cost_record and not counters:
            return
        price = prices.get(model_config_id) if model_config_id else None
        cost = _cost_delta(counters, price)
        _add(totals, counters, cost)
        _add(by_source[source], counters, cost)
        _add(daily.setdefault(date, _empty()), counters, cost)
        if source == "agent":
            _add(by_core.setdefault(core_id or "", _empty()), counters, cost)

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
    # A single statement gives all four sources (including the deduplication
    # subquery) the same database snapshot, even while workers commit new rows.
    # 每行都带 model_config_id（计价用）与 core_id（个人 Agent 按核心拆分用）；
    # 没有这两个概念的来源填 NULL，不另开查询。
    fields = [getattr(models.CostRecord, field) for field in _FIELDS]
    empty_counters = [literal(None, type_=Integer) for _ in _FIELDS]
    record_query = bounded(
        select(literal("record"), models.CostRecord.created_at,
               literal(None, type_=Text), literal(None, type_=Integer),
               literal(None, type_=Text), *fields),
        models.CostRecord.created_at,
    )

    # This subquery deliberately has no date predicate: even a phase recorded
    # outside the selected window suppresses the task's aggregate snapshot.
    covered_task_ids = select(models.CostRecord.source_id).where(
        models.CostRecord.source == "task", models.CostRecord.source_id.is_not(None)
    )
    task_timestamp = func.coalesce(models.Task.finished_at, models.Task.created_at)
    task_query = select(literal("task"), task_timestamp, models.Task.result,
                        literal(None, type_=Integer), literal(None, type_=Text),
                        *empty_counters).where(
        models.Task.kb_id.is_not(None), ~models.Task.id.in_(covered_task_ids)
    )
    task_query = bounded(task_query, task_timestamp)

    # A conversation may use several KBs, but usage belongs to one message.
    # model_config_id 让问答也能按模型计价（chat.py 在落库时写入）。
    message_query = select(
        literal("chat"), models.Message.created_at, models.Message.route,
        models.Message.model_config_id, literal(None, type_=Text), *empty_counters,
    ).where(models.Message.role == "assistant", models.Message.status == "ok")
    message_query = bounded(message_query, models.Message.created_at)

    # 个人 Agent：一次执行 = 一条 assistant 消息，用量写在 meta.usage 里
    # （知识库核心由 answering 回填，CLI 核心由 runtime 解析 JSONL 用量事件）。
    # 只有 status=ok 的执行计入，与问答同口径；失败/停止的执行没有落库用量。
    agent_query = select(
        literal("agent"), models.AgentMessage.created_at, models.AgentMessage.meta,
        models.AgentSession.model_config_id, models.AgentMessage.core_id, *empty_counters,
    ).join(
        models.AgentSession, models.AgentSession.id == models.AgentMessage.session_id
    ).where(
        models.AgentMessage.role == "assistant", models.AgentMessage.status == "ok"
    )
    agent_query = bounded(agent_query, models.AgentMessage.created_at)

    # Do not flush a caller's unrelated pending writes just to build a dashboard.
    with db.no_autoflush:
        prices.update(_prices_by_config(db))
        rows = db.execute(
            union_all(record_query, task_query, message_query, agent_query)
        ).yield_per(500)
        for kind, timestamp, raw, model_config_id, core_id, *values in rows:
            date = date_in_period(timestamp)
            if date is None:
                continue
            if kind == "record":
                feed("processing", date, _valid_counters(dict(zip(_FIELDS, values))),
                     cost_record=True)
            elif kind == "agent":
                feed("agent", date, _json_counters(raw) or dict(_UNMETERED_AGENT_COUNTERS),
                     model_config_id=model_config_id, core_id=core_id or "")
            else:
                feed("processing" if kind == "task" else "chat", date, _json_counters(raw),
                     model_config_id=model_config_id)

    return {
        "period_days": days,
        "timezone": "UTC",
        "currency": pricing.CURRENCY,
        "price_unit": pricing.PRICE_UNIT,
        "totals": _final(totals),
        "daily": [{"date": date.isoformat(), **_final(bucket)}
                  for date, bucket in sorted(daily.items())],
        "by_source": [
            {"source": source, "label": label, **_final(by_source[source])}
            for source, label in _SOURCE_LABELS.items()
        ],
        "by_core": [
            {"core_id": core_id, "label": _core_label(core_id), **_final(bucket)}
            for core_id, bucket in sorted(by_core.items())
        ],
    }
