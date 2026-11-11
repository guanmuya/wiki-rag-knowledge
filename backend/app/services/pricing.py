"""模型参考单价：内置目录匹配 + 管理员为具体模型配置填写的覆盖价。

两件事集中在这一个模块：

1. **归一化匹配**。用户配置里的模型标识是自由字符串（`kz/deepseek-v4.1-flash`），
   公开目录里写的是完整 ID（`deepseek/deepseek-v4.1-flash-1017`）。匹配只做
   「归一化后精确相等」，不做子串或相似度——`gpt-4o` 绝不能命中 `gpt-4o-mini`，
   算错的费用比没有费用更糟。
2. **取价优先级**。用户单价（按 `model_config_id`，见 `models.ModelPrice`）>
   内置参考价（按归一化键）> 无（未计价）。

为什么内置目录是代码模块：见 `price_catalog.py` 开头的打包说明。
"""
from __future__ import annotations

import re
from functools import lru_cache

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, serializers
from . import price_catalog

CURRENCY = price_catalog.CURRENCY
PRICE_UNIT = price_catalog.PRICE_UNIT
TOKENS_PER_PRICE_UNIT = 1_000_000

ORIGIN_USER = "user"          # 管理员为这个模型配置填过价
ORIGIN_BUILTIN = "builtin"    # 命中内置参考价
ORIGIN_NONE = "none"          # 未计价
USER_PRICE_SOURCE = "管理员填写"

# 只剥「纯数字/日期」结尾：`deepseek-v4.1-flash-1017` → `deepseek-v4.1-flash`。
# `-v4.1` 这类段不剥——那可能就是模型名字本身（`gpt-5.6-sol`、`gpt-4o-mini`）。
_NUMERIC_SUFFIX_RE = re.compile(r"-\d{4,8}$")
_DATE_SUFFIX_RE = re.compile(r"-(?:19|20)\d{2}-\d{2}-\d{2}$")


def normalize_model_key(model: str) -> str:
    """把模型标识归一成目录匹配键。

    规则（顺序不能换）：小写去空白 → 取最后一个 `/` 之后的部分（丢掉
    `kz/`、`deepseek/` 这类渠道前缀）→ `_` 换 `-` → 去日期后缀 → 去纯数字后缀。
    日期后缀必须先去：`foo-2024-08-06` 的 `-06` 也是纯数字，先剥数字会留下
    `foo-2024-08`，两条规则就互相打架了。
    """
    key = str(model or "").strip().lower()
    key = key.rsplit("/", 1)[-1]
    key = key.replace("_", "-")
    key = _DATE_SUFFIX_RE.sub("", key)
    key = _NUMERIC_SUFFIX_RE.sub("", key)
    return key


def _entry_keys(entry: dict) -> tuple[str, ...]:
    keys = [normalize_model_key(entry["model_id"])]
    keys.extend(normalize_model_key(alias) for alias in entry.get("aliases") or ())
    return tuple(key for key in dict.fromkeys(keys) if key)


@lru_cache(maxsize=1)
def catalog_index() -> dict[str, dict]:
    """归一化键 → 目录条目；进程内缓存一次（目录是常量）。

    两条不同条目归一化后撞同一个键时直接抛错：这种冲突只能靠维护者写显式
    `aliases` 或合并条目解决，静默取第一条会让费用取决于字典顺序。
    `backend/tests/test_model_prices.py` 在构建期就把这条守住。
    """
    index: dict[str, dict] = {}
    for entry in price_catalog.CATALOG:
        for key in _entry_keys(entry):
            existing = index.get(key)
            if existing is not None and existing["model_id"] != entry["model_id"]:
                raise ValueError(
                    f"参考价目录归一化冲突：{key!r} 同时命中 "
                    f"{existing['model_id']!r} 与 {entry['model_id']!r}，请写显式 aliases 或合并条目")
            index[key] = entry
    return index


def builtin_price(model: str) -> dict | None:
    """按归一化键取内置参考价；没有就返回 None（未计价，不猜）。"""
    key = normalize_model_key(model)
    if not key:
        return None
    return catalog_index().get(key)


def user_prices(db: Session) -> dict[int, models.ModelPrice]:
    """管理员填写过的单价，按 `model_config_id` 索引。

    「填过 0」与「没填」的差别就在这里：判断依据是行是否存在，不是价格是否为 0。
    """
    rows = db.execute(select(models.ModelPrice)).scalars().all()
    return {row.model_config_id: row for row in rows}


def _empty_effective() -> dict:
    return {
        "origin": ORIGIN_NONE,
        "currency": CURRENCY,
        "price_unit": PRICE_UNIT,
        "input_per_1m": None,
        "output_per_1m": None,
        "model_id": "",
        "source": "",
        "source_url": "",
        "verified_at": "",
        "note": "",
    }


def effective_price(model_config_id: int | None, model: str,
                    user: models.ModelPrice | None = None) -> dict:
    """生效单价：用户单价 > 内置参考价 > 未计价。

    `model_config_id` 只用于说明「这条价属于哪个模型配置」，不参与匹配——
    价格永远挂在一个具体配置上，不同渠道的同名模型可以各自填价。
    """
    if user is not None:
        return {
            "origin": ORIGIN_USER,
            "currency": user.currency or CURRENCY,
            "price_unit": PRICE_UNIT,
            "input_per_1m": float(user.input_per_1m or 0.0),
            "output_per_1m": float(user.output_per_1m or 0.0),
            "model_id": model or "",
            "source": USER_PRICE_SOURCE,
            "source_url": "",
            "verified_at": "",
            "note": "",
        }
    entry = builtin_price(model)
    if entry is None:
        return _empty_effective()
    return {
        "origin": ORIGIN_BUILTIN,
        "currency": CURRENCY,
        "price_unit": PRICE_UNIT,
        "input_per_1m": float(entry["input_per_1m"]),
        "output_per_1m": float(entry["output_per_1m"]),
        "model_id": entry["model_id"],
        "source": entry["source"],
        "source_url": entry["source_url"],
        "verified_at": entry["verified_at"],
        "note": entry.get("note") or "",
    }


def _builtin_brief(entry: dict | None) -> dict | None:
    if entry is None:
        return None
    return {
        "model_id": entry["model_id"],
        "currency": CURRENCY,
        "price_unit": PRICE_UNIT,
        "input_per_1m": float(entry["input_per_1m"]),
        "output_per_1m": float(entry["output_per_1m"]),
        "source": entry["source"],
        "source_url": entry["source_url"],
        "verified_at": entry["verified_at"],
        "note": entry.get("note") or "",
    }


def _user_brief(row: models.ModelPrice | None) -> dict | None:
    if row is None:
        return None
    return {
        "currency": row.currency or CURRENCY,
        "price_unit": PRICE_UNIT,
        "input_per_1m": float(row.input_per_1m or 0.0),
        "output_per_1m": float(row.output_per_1m or 0.0),
        "updated_by": row.updated_by,
        "updated_at": serializers.iso(row.updated_at),
    }


def item_payload(cfg: models.ModelConfig, provider_name: str,
                 user: models.ModelPrice | None = None) -> dict:
    """一个模型配置的价格视图：内置价、用户价与最终生效价。"""
    return {
        "model_config_id": cfg.id,
        "model": cfg.model,
        "label": (cfg.label or "").strip() or cfg.model,
        "provider": provider_name,
        "purpose": cfg.purpose,
        "match_key": normalize_model_key(cfg.model),
        "builtin": _builtin_brief(builtin_price(cfg.model)),
        "user": _user_brief(user),
        "effective": effective_price(cfg.id, cfg.model, user),
    }


def catalog_payload(db: Session) -> dict:
    """价格目录接口的完整载荷：所有模型配置 + 内置/用户/生效价。"""
    rows = db.execute(
        select(models.ModelConfig, models.Provider.name)
        .join(models.Provider, models.Provider.id == models.ModelConfig.provider_id)
        .order_by(models.ModelConfig.purpose, models.ModelConfig.id)
    ).all()
    user_rows = user_prices(db)
    return {
        "currency": CURRENCY,
        "price_unit": PRICE_UNIT,
        # 明确标注「参考价」：前端与后台都应把它当估算值，不能当账单。
        "reference": True,
        "note": price_catalog.CATALOG_NOTE,
        "sources": [dict(source) for source in price_catalog.SOURCES],
        "items": [
            item_payload(cfg, provider_name, user_rows.get(cfg.id))
            for cfg, provider_name in rows
        ],
    }
