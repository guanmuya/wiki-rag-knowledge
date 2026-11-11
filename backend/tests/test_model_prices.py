"""内置参考单价目录：字段完整性、来源可追溯、归一化匹配规则。

这组用例是价格数据的「门禁」：目录里每一条都必须说清来源与查证日期，
归一化后不允许撞键，匹配也不允许做子串/相似度——算错的费用比没有费用更糟。
联网只在维护时人工核对价目页，这里一个请求都不发。
"""
from __future__ import annotations

import re

import pytest

from app.services import price_catalog, pricing

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_REQUIRED = ("model_id", "input_per_1m", "output_per_1m", "source", "source_url",
             "verified_at")


def _entry(model_id: str, *, input_per_1m: float = 1.0, output_per_1m: float = 2.0,
           **extra) -> dict:
    entry = {
        "model_id": model_id,
        "input_per_1m": input_per_1m,
        "output_per_1m": output_per_1m,
        "source": "测试价目页",
        "source_url": "https://example.test/pricing",
        "verified_at": "2026-01-01",
    }
    entry.update(extra)
    return entry


@pytest.fixture()
def catalog_swap(monkeypatch):
    """临时替换内置目录；结束后清掉进程内缓存，避免影响其它用例。"""
    def apply(entries):
        monkeypatch.setattr(price_catalog, "CATALOG", tuple(entries))
        pricing.catalog_index.cache_clear()
    yield apply
    pricing.catalog_index.cache_clear()


# ------------------------------------------------------------------ 目录数据校验
def test_catalog_entries_are_complete_and_sourced():
    assert price_catalog.CATALOG, "内置目录不能为空"
    source_urls = [source["url"] for source in price_catalog.SOURCES]
    assert source_urls and all(url.startswith("https://") for url in source_urls)
    for entry in price_catalog.CATALOG:
        for field in _REQUIRED:
            assert entry.get(field) not in (None, ""), f"{entry.get('model_id')} 缺 {field}"
        assert isinstance(entry["input_per_1m"], (int, float))
        assert isinstance(entry["output_per_1m"], (int, float))
        assert entry["input_per_1m"] >= 0 and entry["output_per_1m"] >= 0
        assert entry["source_url"].startswith("https://")
        # 条目必须指向 SOURCES 里登记过的价目页，不能出现「查不到出处」的价。
        assert any(entry["source_url"].startswith(url) for url in source_urls), \
            f"{entry['model_id']} 的 source_url 不在 SOURCES 里"
        assert _DATE_RE.match(entry["verified_at"]), entry["verified_at"]
        for alias in entry.get("aliases") or ():
            assert isinstance(alias, str) and alias.strip() == alias and alias


def test_catalog_normalized_keys_are_unique():
    index = pricing.catalog_index()
    assert len(index) == len(price_catalog.CATALOG)
    # 反向确认：每个归一化键只对应一条目录条目。
    by_model = {}
    for key, entry in index.items():
        by_model.setdefault(entry["model_id"], set()).add(key)
    assert len(by_model) == len(price_catalog.CATALOG)


def test_duplicate_keys_fail_loudly(catalog_swap):
    catalog_swap([
        _entry("deepseek/deepseek-v4.1-flash-1017"),
        _entry("kz/deepseek-v4.1-flash"),
    ])
    with pytest.raises(ValueError) as exc:
        pricing.catalog_index()
    assert "归一化冲突" in str(exc.value)


def test_explicit_alias_resolves_same_entry(catalog_swap):
    catalog_swap([
        _entry("deepseek/deepseek-v4.1-flash-1017",
               aliases=("deepseek/deepseek-v4.1-flash",)),
    ])
    assert pricing.builtin_price("kz/deepseek-v4.1-flash")["model_id"] == \
        "deepseek/deepseek-v4.1-flash-1017"
    assert pricing.builtin_price("deepseek/deepseek-v4.1-flash-1017")["model_id"] == \
        "deepseek/deepseek-v4.1-flash-1017"


# ------------------------------------------------------------------ 归一化与匹配
@pytest.mark.parametrize("model, expected", [
    ("kz/deepseek-v4.1-flash", "deepseek-v4.1-flash"),
    ("deepseek/deepseek-v4.1-flash-1017", "deepseek-v4.1-flash"),
    ("  OpenAI/GPT-6-Astra  ", "gpt-6-astra"),
    ("gpt_6_astra", "gpt-6-astra"),
    ("gpt-5.6-luna-2024-08-06", "gpt-5.6-luna"),
    # -v4.1 这类段可能是名字本身，不能当版本后缀剥掉
    ("deepseek-v4.1-flash", "deepseek-v4.1-flash"),
    ("", ""),
])
def test_normalize_model_key(model, expected):
    assert pricing.normalize_model_key(model) == expected


def test_user_and_catalog_ids_match_each_other():
    """用户模型 ID 带渠道前缀，公开目录 ID 带日期后缀，两边都要命中同一条。"""
    user_side = pricing.builtin_price("kz/deepseek-v4.1-flash")
    catalog_side = pricing.builtin_price("deepseek/deepseek-v4.1-flash-1017")
    assert user_side is not None and user_side is catalog_side
    assert user_side["input_per_1m"] == 0.022
    assert user_side["output_per_1m"] == 0.294
    assert user_side["source_url"].startswith("https://")


def test_matching_is_exact_not_prefix(catalog_swap):
    catalog_swap([_entry("gpt-4o", input_per_1m=2.5, output_per_1m=10.0)])
    assert pricing.builtin_price("gpt-4o") is not None
    # 只做归一化后的精确相等：短名字不能命中长名字，长名字也不能命中短名字。
    assert pricing.builtin_price("gpt-4o-mini") is None
    assert pricing.builtin_price("openai/gpt-5.6-luna") is None
    assert pricing.builtin_price("my-gpt-4o-custom") is None


def test_unknown_and_empty_models_are_unpriced():
    for model in ("", "   ", "totally-unknown-model", "kz/"):
        assert pricing.builtin_price(model) is None


def test_effective_price_priority(catalog_swap):
    from app import models

    catalog_swap([_entry("kz/demo-model", input_per_1m=1.0, output_per_1m=3.0)])

    builtin = pricing.effective_price(7, "kz/demo-model")
    assert builtin["origin"] == pricing.ORIGIN_BUILTIN
    assert (builtin["input_per_1m"], builtin["output_per_1m"]) == (1.0, 3.0)

    # 用户价按 model_config_id 覆盖内置价——即使填的是 0。
    user_row = models.ModelPrice(model_config_id=7, currency="USD",
                                 input_per_1m=0.0, output_per_1m=0.0)
    override = pricing.effective_price(7, "kz/demo-model", user_row)
    assert override["origin"] == pricing.ORIGIN_USER
    assert (override["input_per_1m"], override["output_per_1m"]) == (0.0, 0.0)

    missing = pricing.effective_price(8, "unknown-model")
    assert missing["origin"] == pricing.ORIGIN_NONE
    assert missing["input_per_1m"] is None and missing["output_per_1m"] is None
