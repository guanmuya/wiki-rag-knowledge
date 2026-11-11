"""供应商协议适配：三种协议族的请求 payload 构造与响应文本抽取。

支持三种协议（`Provider.protocol`）：
- `openai`（默认）：`POST {base}/chat/completions` + `POST {base}/embeddings`
- `openai-responses`：`POST {base}/responses`（`input` / `max_output_tokens` / `output[].content[].text`）
- `anthropic`：`POST {base}/messages`（`system` 独立字段、`max_tokens` 必填、`content[].text`）

为什么分派放在这一层而不是各调用点：chat/embed/describe_image/test_model 的
调用方（retrieval / wiki / answering / ingest）只关心「给我一段文本」，协议差异
属于供应商适配细节。协议判断集中一处，新增协议时不必改五个业务模块。

本模块只依赖标准库、`models` 与 `llm_common` 的异常，不反向依赖门面 `llm.py`。
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from .. import models
from .llm_common import ModelUnavailable

PROTOCOL_OPENAI = "openai"
PROTOCOL_RESPONSES = "openai-responses"
PROTOCOL_ANTHROPIC = "anthropic"
SUPPORTED_PROTOCOLS = (PROTOCOL_OPENAI, PROTOCOL_RESPONSES, PROTOCOL_ANTHROPIC)

# Anthropic 的 /v1/models 在部分代理/中转实现里不存在，探测失败时回退这份清单。
# 只列稳定别名（不带日期快照），避免内置表过期后导入一堆 404 模型。
ANTHROPIC_BUILTIN_MODELS: tuple[str, ...] = (
    "claude-opus-4-1",
    "claude-opus-4-0",
    "claude-sonnet-4-0",
    "claude-3-7-sonnet-latest",
    "claude-3-5-sonnet-latest",
    "claude-3-5-haiku-latest",
    "claude-3-opus-latest",
    "claude-3-haiku-20240307",
)

ANTHROPIC_VERSION = "2023-06-01"
# anthropic 的 temperature 取值区间是 0..1，OpenAI 是 0..2。
# 统一按更严的区间钳制，避免用户配了 0.7 之外的值直接被 400 打回。
_ANTHROPIC_TEMP_RANGE = (0.0, 1.0)


def provider_protocol(provider: models.Provider) -> str:
    """读供应商协议，未知/空值一律回退 openai。

    为什么回退而不是报错：老库的 `protocol` 列是迁移补出来的，历史行可能是
    空串或 NULL；这类行过去就是按 OpenAI 兼容协议跑的，回退才不破坏存量行为。
    """
    value = (getattr(provider, "protocol", "") or "").strip()
    return value if value in SUPPORTED_PROTOCOLS else PROTOCOL_OPENAI


def _official_https_base(base: str, host: str, path: str | None = None) -> bool:
    """仅识别官方 HTTPS 地址，避免对兼容网关应用供应商特例。"""
    try:
        parsed = urlsplit((base or "").strip())
        return (
            parsed.scheme.lower() == "https"
            and parsed.hostname == host
            and parsed.port in (None, 443)
            and "@" not in parsed.netloc
            and not parsed.query and not parsed.fragment
            and (path is None or parsed.path.rstrip("/") == path)
        )
    except ValueError:
        return False


def _openai_reasoning_chat(provider: models.Provider, model: str) -> bool:
    """官方 o1/o3/o4 系列 Chat Completions 使用不同的 token 参数。"""
    return bool(
        _official_https_base(provider.base_url, "api.openai.com")
        and re.match(r"^o[134](?:-|$)", model or "")
    )


def _dashscope_embedding(provider: models.Provider, model: str) -> bool:
    """中国区同步向量 API 的 v3/v4 模型每次最多接受 10 条文本。"""
    return (
        model in ("text-embedding-v3", "text-embedding-v4")
        and _official_https_base(
            provider.base_url, "dashscope.aliyuncs.com", "/compatible-mode/v1")
    )


def _plain_text(content: Any) -> str:
    """把 OpenAI 风格的 content（str 或 parts 数组）压成纯文本。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for part in content:
            if isinstance(part, str):
                chunks.append(part)
            elif isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    chunks.append(text)
        return "\n".join(chunks)
    return str(content)


def _split_system(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """把 system 消息从消息数组里剥出来。

    Responses 用独立的 `instructions`，Anthropic 用独立的 `system`；
    两者都不接受 messages 里出现 role=system。业务侧仍按 OpenAI 习惯传
    system 消息，所以这里统一剥一次，调用方不必知道协议差异。
    """
    system_parts: list[str] = []
    rest: list[dict[str, Any]] = []
    for msg in messages:
        if (msg.get("role") or "").lower() == "system":
            text = _plain_text(msg.get("content")).strip()
            if text:
                system_parts.append(text)
            continue
        rest.append(msg)
    return "\n\n".join(system_parts), rest


def _clamp_temperature(value: Any) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return 0.2
    low, high = _ANTHROPIC_TEMP_RANGE
    return min(max(num, low), high)


def _responses_payload(cfg: models.ModelConfig, messages: list[dict[str, Any]], *,
                       temperature: Any, max_tokens: int, json_mode: bool) -> dict:
    """OpenAI Responses API：`input` 取代 `messages`，`max_output_tokens` 取代 `max_tokens`。"""
    instructions, rest = _split_system(messages)
    payload: dict[str, Any] = {
        "model": cfg.model,
        "input": [
            {"role": m.get("role") or "user", "content": _plain_text(m.get("content"))}
            for m in rest
        ],
    }
    if instructions:
        payload["instructions"] = instructions
    if temperature is not None:
        payload["temperature"] = temperature
    if max_tokens:
        payload["max_output_tokens"] = int(max_tokens)
    if json_mode:
        payload["text"] = {"format": {"type": "json_object"}}
    return payload


def _anthropic_payload(cfg: models.ModelConfig, messages: list[dict[str, Any]], *,
                       temperature: Any, max_tokens: int, json_mode: bool) -> dict:
    """Anthropic Messages：`system` 独立字段、`max_tokens` 必填。"""
    system, rest = _split_system(messages)
    if json_mode:
        # Anthropic 没有 response_format，只能靠指令约束输出形态。
        # 只追加约束、不删原有 system，尽量不改变业务提示词的语义。
        hint = "只输出合法 JSON 对象，不要包含 Markdown 代码块或解释性文字。"
        system = f"{system}\n\n{hint}".strip()
    payload: dict[str, Any] = {
        "model": cfg.model,
        # max_tokens 是 Anthropic 的必填项，业务侧传 0（不限制）时兜底一个上限。
        "max_tokens": max(int(max_tokens or 0), 1) if max_tokens else 1600,
        "messages": [
            {"role": m.get("role") or "user", "content": _plain_text(m.get("content"))}
            for m in rest
        ],
    }
    if system:
        payload["system"] = system
    if temperature is not None:
        payload["temperature"] = _clamp_temperature(temperature)
    return payload


def _extract_responses_text(data: dict) -> str:
    """从 Responses 响应取文本：`output[].content[].text`，兼容聚合字段 `output_text`。"""
    aggregated = data.get("output_text")
    if isinstance(aggregated, str) and aggregated.strip():
        return aggregated
    parts: list[str] = []
    for item in data.get("output") or []:
        if not isinstance(item, dict):
            continue
        for part in item.get("content") or []:
            if isinstance(part, dict) and part.get("type") in ("output_text", "text"):
                text = part.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
    return "\n".join(parts)


def _extract_anthropic_text(data: dict) -> str:
    """从 Anthropic 响应取文本：`content[].text`（type=text 的块）。"""
    parts: list[str] = []
    for part in data.get("content") or []:
        if isinstance(part, dict) and part.get("type") == "text":
            text = part.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
    return "\n".join(parts)


def _no_embedding_error(protocol: str) -> ModelUnavailable:
    name = {PROTOCOL_RESPONSES: "OpenAI Responses", PROTOCOL_ANTHROPIC: "Anthropic Messages"}
    return ModelUnavailable(
        f"该协议（{name.get(protocol, protocol)}）不提供 Embedding 接口，"
        "请为向量用途单独配置一个 OpenAI 兼容协议的供应商"
    )
