"""内置参考单价目录：公开价目表 + 来源链接 + 查证日期。

为什么是 Python 模块而不是 JSON 数据文件：桌面版用 PyInstaller 打包后端
（`frontend/scripts/build-backend.mjs`），打包命令没有 `--add-data`，外部数据
文件进不了产物；写在模块里随代码一起打包，运行时零配置。

维护约定（详见 `docs/PRICING.md`）：
- 只录「查得到公开价、说得清来源」的模型；查不到就**不录**——用量的
  `unpriced_records` 会如实显示「这些调用没有单价」，不编数字。
- 每条必须有 `source` + `source_url` + `verified_at`（YYYY-MM-DD），否则
  `backend/tests/test_model_prices.py` 直接失败。
- 同一个模型的两个公开来源数值不同时，只录一个并在 `note` 里写明另一个；
  金额只按 `input_per_1m` / `output_per_1m` 计算，缓存价不参与。
- CI 与运行时都不联网：联网只发生在维护时人工核对价目页之后。

单位固定为「每 100 万 token、美元」。`output_per_1m = 0` 只用于只按输入
token 计价的模型（Embedding），不是「免费」的意思。
"""
from __future__ import annotations

CURRENCY = "USD"
PRICE_UNIT = "per_1m_tokens"
# 界面上必须原样展示这句话：单价是公开参考价，不是账单。
CATALOG_NOTE = (
    "参考价：取自公开价目页，按每 100 万 token 的美元标价估算，"
    "不用于账单、余额或对账；同一模型不同渠道的实际价格可能不同，"
    "管理员可以为具体模型配置填写自己的价格来覆盖。"
)

# 价目页清单：给界面与文档一个可点开的出处，条目里的 source_url 必须与之一致。
SOURCES = (
    {
        "id": "openai-pricing",
        "name": "OpenAI 官方价目表（标准档、短上下文）",
        "url": "https://developers.openai.com/api/docs/pricing",
    },
    {
        "id": "vercel-ai-gateway",
        "name": "Vercel AI Gateway 模型页",
        "url": "https://vercel-docs.vercel.sh/ai-gateway/models/",
    },
)

_OPENAI_URL = "https://developers.openai.com/api/docs/pricing"
_OPENAI_SOURCE = "OpenAI 官方价目表（标准档、短上下文）"


def _gateway_url(model_id: str) -> str:
    return f"https://vercel-docs.vercel.sh/ai-gateway/models/{model_id}.md"


# 每条：model_id、input_per_1m、output_per_1m、source、source_url、verified_at，
# 可选 aliases（同一模型在别处出现的写法）与 note。
CATALOG = (
    # ---------------------------------------------------------- OpenAI 官方价目表
    {
        "model_id": "gpt-6-astra",
        "input_per_1m": 10.0,
        "output_per_1m": 50.0,
        "source": _OPENAI_SOURCE,
        "source_url": _OPENAI_URL,
        "verified_at": "2026-11-11",
        "aliases": ("openai/gpt-6-astra",),
        "note": "缓存输入 1.00；Vercel AI Gateway 模型页给同一模型也是 10 / 50。缓存价不参与计算。",
    },
    {
        "model_id": "gpt-6.1-sol",
        "input_per_1m": 2.0,
        "output_per_1m": 10.0,
        "source": _OPENAI_SOURCE,
        "source_url": _OPENAI_URL,
        "verified_at": "2026-11-11",
        "aliases": (),
        "note": "缓存输入 0.10；缓存价不参与计算。",
    },
    {
        "model_id": "gpt-6.1-terra",
        "input_per_1m": 1.0,
        "output_per_1m": 5.0,
        "source": _OPENAI_SOURCE,
        "source_url": _OPENAI_URL,
        "verified_at": "2026-11-11",
        "aliases": (),
        "note": "缓存输入 0.05；缓存价不参与计算。",
    },
    {
        "model_id": "gpt-6-luna",
        "input_per_1m": 0.10,
        "output_per_1m": 0.50,
        "source": _OPENAI_SOURCE,
        "source_url": _OPENAI_URL,
        "verified_at": "2026-11-11",
        "aliases": (),
        "note": "缓存输入 0.01；缓存价不参与计算。",
    },
    {
        "model_id": "gpt-6-sol",
        "input_per_1m": 2.0,
        "output_per_1m": 10.0,
        "source": _OPENAI_SOURCE,
        "source_url": _OPENAI_URL,
        "verified_at": "2026-11-11",
        "aliases": (),
        "note": "缓存输入 0.20；缓存价不参与计算。",
    },
    {
        "model_id": "gpt-5.6-sol",
        "input_per_1m": 4.0,
        "output_per_1m": 20.0,
        "source": _OPENAI_SOURCE,
        "source_url": _OPENAI_URL,
        "verified_at": "2026-11-11",
        "aliases": (),
        "note": "缓存输入 0.40；价目页标注为促销价，截止 2026-11-30，之后以官方页为准。",
    },
    {
        "model_id": "gpt-5.6-terra",
        "input_per_1m": 2.0,
        "output_per_1m": 12.0,
        "source": _OPENAI_SOURCE,
        "source_url": _OPENAI_URL,
        "verified_at": "2026-11-11",
        "aliases": (),
        "note": "缓存输入 0.20；缓存价不参与计算。",
    },
    {
        "model_id": "gpt-5.6-luna",
        "input_per_1m": 0.20,
        "output_per_1m": 1.20,
        "source": _OPENAI_SOURCE,
        "source_url": _OPENAI_URL,
        "verified_at": "2026-11-11",
        "aliases": (),
        "note": "缓存输入 0.02；缓存价不参与计算。",
    },
    {
        "model_id": "gpt-5.3-codex",
        "input_per_1m": 1.75,
        "output_per_1m": 14.0,
        "source": _OPENAI_SOURCE,
        "source_url": _OPENAI_URL,
        "verified_at": "2026-11-11",
        "aliases": (),
        "note": "缓存输入 0.175；缓存价不参与计算。",
    },
    {
        "model_id": "text-embedding-3-small",
        "input_per_1m": 0.02,
        "output_per_1m": 0.0,
        "source": _OPENAI_SOURCE,
        "source_url": _OPENAI_URL,
        "verified_at": "2026-11-11",
        "aliases": (),
        "note": "Embedding 模型只按输入 token 计价，输出价固定为 0。",
    },
    {
        "model_id": "text-embedding-3-large",
        "input_per_1m": 0.13,
        "output_per_1m": 0.0,
        "source": _OPENAI_SOURCE,
        "source_url": _OPENAI_URL,
        "verified_at": "2026-11-11",
        "aliases": (),
        "note": "Embedding 模型只按输入 token 计价，输出价固定为 0。",
    },
    # ------------------------------------------------- Vercel AI Gateway 模型页
    {
        "model_id": "anthropic/claude-sonnet-4.6",
        "input_per_1m": 3.0,
        "output_per_1m": 15.0,
        "source": "Vercel AI Gateway 模型页",
        "source_url": _gateway_url("anthropic/claude-sonnet-4.6"),
        "verified_at": "2026-11-11",
        "aliases": (),
        "note": "网关模型页标价；同一模型在 Anthropic 直连的实际价以官方页为准。",
    },
    {
        "model_id": "google/gemini-3.8-flash",
        "input_per_1m": 0.75,
        "output_per_1m": 3.75,
        "source": "Vercel AI Gateway 模型页",
        "source_url": _gateway_url("google/gemini-3.8-flash"),
        "verified_at": "2026-11-11",
        "aliases": (),
        "note": "网关模型页标价。",
    },
    {
        "model_id": "zai/glm-5.3",
        "input_per_1m": 0.0703,
        "output_per_1m": 1.4,
        "source": "Vercel AI Gateway 模型页",
        "source_url": _gateway_url("zai/glm-5.3"),
        "verified_at": "2026-11-11",
        "aliases": (),
        "note": "网关模型页标价。",
    },
    {
        "model_id": "deepseek/deepseek-v4.1-flash-1017",
        "input_per_1m": 0.022,
        "output_per_1m": 0.294,
        "source": "Vercel AI Gateway 模型页",
        "source_url": _gateway_url("deepseek/deepseek-v4.1-flash-1017"),
        "verified_at": "2026-11-11",
        # 用户自己的模型标识常带别的前缀或没有日期后缀，这里显式登记同一模型的写法。
        "aliases": ("deepseek/deepseek-v4.1-flash",),
        "note": (
            "网关目录接口（https://ai-gateway.vercel.sh/v1/models/"
            "deepseek/deepseek-v4.1-flash-1017）对同一模型给的是输入 0.30 / "
            "输出 1.20 / 缓存读取 0.007，并标注 varies_by_provider；本目录只取"
            "模型页的 0.022 / 0.294，两者差一个数量级，所以金额只能当参考价。"
        ),
    },
)
