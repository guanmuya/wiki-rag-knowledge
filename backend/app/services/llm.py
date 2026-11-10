"""模型调用层门面：按供应商协议族分派 LLM / Embedding / 视觉模型。

本文件保留「对外契约 + 编排」：`llm.chat` / `llm.embed` / `llm.describe_image` /
`llm.test_model` / `llm.CallStats` / `llm.pack_vector` 等名字，以及被测试
monkeypatch 的 `_client` / `_post` / `_get` / `resolve_model` / `active_embedding`
都仍定义在（或重导出到）本模块，行为与拆分前完全一致。

职责代码按 P1-4 拆到同目录的兄弟模块（单向依赖、无循环）：
- `llm_protocols.py`：协议适配（三种协议的 payload / 响应抽取 / 协议判定）
- `llm_stats.py`：CallStats 成本统计
- `llm_vectors.py`：向量打包（float32 序列化 / 解包 / 归一化）
- `llm_offline.py`：离线降级（确定性哈希向量与 FALLBACK_* 常量）
- `llm_common.py`：共享基础（异常 / 错误文案 / URL 拼接）

依赖方向：`llm.py` → 各兄弟模块；`llm_offline` → `llm_vectors`；
`llm_protocols` → `llm_common`；兄弟模块之间无反向引用，故无循环依赖。

未配置可用模型时进入「离线演示模式」：LLM 侧由问答链路降级为抽取式整理，
Embedding 侧降级为确定性哈希向量，保证检索链路可跑通、可测试。
"""
from __future__ import annotations

import base64
import json
import logging
import time
from typing import Any, Iterator

import httpx
from sqlalchemy import case, select
from sqlalchemy.orm import Session

from .. import config, models, security

# 门面重导出：保持 `llm.X` 的对外契约不变（含被测试 monkeypatch 的名字）。
# 各职责模块只被本模块单向引用，不反向 import `llm.py`。
from .llm_common import (
    DEFAULT_TIMEOUT, ModelNotConfigured, ModelUnavailable, _ERROR_BODY_RE,
    _join, error_summary, public_error,
)
from .llm_stats import CallStats
from .llm_vectors import normalize, pack_vector, unpack_vector
from .llm_offline import (
    FALLBACK_EMBED_DIM, FALLBACK_EMBED_NAME, _TOKEN_RE, hash_embed,
)
from .llm_protocols import (
    ANTHROPIC_BUILTIN_MODELS, ANTHROPIC_VERSION, PROTOCOL_ANTHROPIC,
    PROTOCOL_OPENAI, PROTOCOL_RESPONSES, SUPPORTED_PROTOCOLS,
    _ANTHROPIC_TEMP_RANGE, _anthropic_payload, _clamp_temperature,
    _dashscope_embedding, _extract_anthropic_text, _extract_responses_text,
    _no_embedding_error, _official_https_base, _openai_reasoning_chat,
    _plain_text, _responses_payload, _split_system, provider_protocol,
)

log = logging.getLogger("wrk.llm")


def _client() -> httpx.Client:
    """按显式配置创建 HTTP 客户端，默认不继承系统代理环境变量。"""
    kwargs: dict = {
        "timeout": httpx.Timeout(connect=10.0, read=config.HTTP_TIMEOUT_READ,
                                 write=30.0, pool=10.0),
        "trust_env": config.HTTP_TRUST_ENV,
    }
    if config.HTTP_PROXY:
        kwargs["proxy"] = config.HTTP_PROXY
    return httpx.Client(**kwargs)


# ------------------------------------------------------------------ 解析配置
def resolve_model(db: Session, purpose: str) -> tuple[models.ModelConfig, models.Provider] | None:
    """按用途取默认模型。

    排序规则（「明明配了模型却走离线」的修复点）：
    1. 显式默认优先；
    2. 其次优先最近一次连接测试成功的模型：用户测过说明当前网络/密钥下可用，
       批量导入的免费/限流模型不能因为「最后导入」就抢占问答；
    3. 其余沿用原来的「最新添加优先」。
    """
    test_rank = case(
        (models.ModelConfig.last_test_ok.is_(True), 0),
        (models.ModelConfig.last_test_ok.is_(None), 1),
        else_=2,
    )
    stmt = (
        select(models.ModelConfig)
        .join(models.Provider, models.Provider.id == models.ModelConfig.provider_id)
        .where(
            models.ModelConfig.purpose == purpose,
            models.ModelConfig.enabled.is_(True),
            models.Provider.enabled.is_(True),
        )
        .order_by(
            models.ModelConfig.is_default.desc(),
            test_rank,
            models.ModelConfig.last_test_at.desc().nulls_last(),
            models.ModelConfig.id.desc(),
        )
    )
    cfg = db.execute(stmt).scalars().first()
    if not cfg:
        return None
    return cfg, cfg.provider


def embed_signature(cfg: models.ModelConfig, provider: models.Provider, dim: int) -> str:
    return f"{provider.name}:{cfg.model}:{dim}"


def active_embedding(db: Session) -> tuple[models.ModelConfig | None, models.Provider | None, str, int]:
    """返回当前全局 Embedding 配置及签名。未配置时返回离线哈希向量签名。"""
    resolved = resolve_model(db, models.ModelPurpose.embedding.value)
    if not resolved:
        return None, None, f"{FALLBACK_EMBED_NAME}:{FALLBACK_EMBED_DIM}", FALLBACK_EMBED_DIM
    cfg, provider = resolved
    dim = cfg.embed_dim or int(json.loads(cfg.params or "{}").get("dim") or 0)
    return cfg, provider, embed_signature(cfg, provider, dim), dim


# ------------------------------------------------------------------ 调用实现
def _headers(provider: models.Provider, protocol: str | None = None) -> dict[str, str]:
    """按协议族构造鉴权头。

    OpenAI 系走 `Authorization: Bearer`；Anthropic 走 `x-api-key`，
    且必须带 `anthropic-version`（缺失会被直接 400）。
    """
    proto = protocol or provider_protocol(provider)
    key = security.decrypt_secret(provider.api_key_enc)
    headers = {"Content-Type": "application/json"}
    if proto == PROTOCOL_ANTHROPIC:
        if key:
            headers["x-api-key"] = key
        headers["anthropic-version"] = ANTHROPIC_VERSION
    elif key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _post(url: str, payload: dict, provider: models.Provider,
          protocol: str | None = None) -> dict:
    try:
        with _client() as client:
            resp = client.post(url, headers=_headers(provider, protocol), json=payload)
    except httpx.InvalidURL as exc:
        raise ModelUnavailable(f"供应商接口地址不合法：{url}（{exc}）") from exc
    except httpx.HTTPError as exc:
        raise ModelUnavailable(f"网络请求失败：{exc.__class__.__name__}: {exc}") from exc
    if resp.status_code >= 400:
        body = resp.text[:600]
        raise ModelUnavailable(f"供应商返回 {resp.status_code}: {body}")
    try:
        return resp.json()
    except ValueError as exc:
        raise ModelUnavailable(f"响应不是合法 JSON：{resp.text[:300]}") from exc


def _get(url: str, provider: models.Provider, protocol: str | None = None) -> dict:
    """GET 探活/列表。与 `_post` 同样的中文错误语义。"""
    try:
        with _client() as client:
            resp = client.get(url, headers=_headers(provider, protocol))
    except httpx.InvalidURL as exc:
        raise ModelUnavailable(f"供应商接口地址不合法：{url}（{exc}）") from exc
    except httpx.HTTPError as exc:
        raise ModelUnavailable(f"网络请求失败：{exc.__class__.__name__}: {exc}") from exc
    if resp.status_code >= 400:
        body = resp.text[:600]
        raise ModelUnavailable(f"供应商返回 {resp.status_code}: {body}")
    try:
        return resp.json()
    except ValueError as exc:
        raise ModelUnavailable(f"响应不是合法 JSON：{resp.text[:300]}") from exc


def chat(
    db: Session,
    messages: list[dict[str, Any]],
    *,
    purpose: str = "llm",
    temperature: float = 0.2,
    max_tokens: int = 1600,
    stats: CallStats | None = None,
    json_mode: bool = False,
    cfg_override: models.ModelConfig | None = None,
) -> str:
    """生成一段文本。`cfg_override` 只给「连接测试」用：测哪条就调哪条。

    业务侧（retrieval / wiki / answering / ingest）永远不传 override，
    行为与改造前完全一致。
    """
    if cfg_override is not None:
        cfg, provider = cfg_override, cfg_override.provider
    else:
        resolved = resolve_model(db, purpose)
        if not resolved:
            raise ModelNotConfigured("未配置可用的对话模型")
        cfg, provider = resolved
    protocol = provider_protocol(provider)
    params = json.loads(cfg.params or "{}")
    temp = params.get("temperature", temperature)
    limit = int(params.get("max_tokens", max_tokens)) if max_tokens else 0

    if protocol == PROTOCOL_RESPONSES:
        url = _join(provider.base_url, "responses")
        payload = _responses_payload(cfg, messages, temperature=temp,
                                     max_tokens=limit, json_mode=json_mode)
    elif protocol == PROTOCOL_ANTHROPIC:
        url = _join(provider.base_url, "messages")
        payload = _anthropic_payload(cfg, messages, temperature=temp,
                                     max_tokens=limit, json_mode=json_mode)
    else:
        url = _join(provider.base_url, "chat/completions")
        payload = {
            "model": cfg.model,
            "messages": messages,
        }
        reasoning = _openai_reasoning_chat(provider, cfg.model)
        if not reasoning:
            payload["temperature"] = temp
        if limit:
            payload["max_completion_tokens" if reasoning else "max_tokens"] = limit
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

    started = time.time()
    data = _post(url, payload, provider, protocol)
    elapsed = int((time.time() - started) * 1000)

    if protocol == PROTOCOL_RESPONSES:
        try:
            content = _extract_responses_text(data)
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelUnavailable(f"响应结构异常：{str(data)[:300]}") from exc
        if not content:
            raise ModelUnavailable(f"响应结构异常：未取到文本内容 {str(data)[:300]}")
        usage = data.get("usage") or {}
        if stats:
            stats.record_chat(
                prompt_tokens=int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0),
                completion_tokens=int(
                    usage.get("output_tokens") or usage.get("completion_tokens") or 0),
                latency_ms=elapsed,
            )
        return content

    if protocol == PROTOCOL_ANTHROPIC:
        try:
            content = _extract_anthropic_text(data)
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelUnavailable(f"响应结构异常：{str(data)[:300]}") from exc
        if not content:
            raise ModelUnavailable(f"响应结构异常：未取到文本内容 {str(data)[:300]}")
        usage = data.get("usage") or {}
        if stats:
            stats.record_chat(
                prompt_tokens=int(usage.get("input_tokens") or 0),
                completion_tokens=int(usage.get("output_tokens") or 0),
                latency_ms=elapsed,
            )
        return content

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ModelUnavailable(f"响应结构异常：{str(data)[:300]}") from exc
    if stats:
        usage = data.get("usage") or {}
        stats.record_chat(
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            latency_ms=elapsed,
        )
    return content or ""


def chat_stream(
    db: Session,
    messages: list[dict[str, Any]],
    *,
    purpose: str = "llm",
    temperature: float = 0.2,
    max_tokens: int = 1600,
    stats: CallStats | None = None,
    cfg_override: models.ModelConfig | None = None,
) -> Iterator[str]:
    """流式生成：逐段产出文本增量。

    为什么只有 OpenAI 兼容协议走原生流式：Responses / Anthropic 的流事件结构
    完全不同，而这两个协议在本产品里属于少数配置。它们退化为「一次性调用后
    整段产出」——界面仍能收到回答，只是没有逐字效果，不会因为协议差异报错。

    失败语义与 `chat()` 一致：供应商不可用/返回异常一律抛 `ModelUnavailable`。
    若流式请求在**尚未产出任何文本**时失败（例如部分中转不支持
    `stream_options`），自动回退到 `chat()`，避免为了流式把可用供应商打死。
    """
    if cfg_override is not None:
        cfg, provider = cfg_override, cfg_override.provider
    else:
        resolved = resolve_model(db, purpose)
        if not resolved:
            raise ModelNotConfigured("未配置可用的对话模型")
        cfg, provider = resolved
    protocol = provider_protocol(provider)

    if protocol != PROTOCOL_OPENAI:
        text = chat(db, messages, purpose=purpose, temperature=temperature,
                    max_tokens=max_tokens, stats=stats, cfg_override=cfg_override)
        if text:
            yield text
        return

    params = json.loads(cfg.params or "{}")
    temp = params.get("temperature", temperature)
    limit = int(params.get("max_tokens", max_tokens)) if max_tokens else 0
    url = _join(provider.base_url, "chat/completions")
    reasoning = _openai_reasoning_chat(provider, cfg.model)
    payload: dict[str, Any] = {
        "model": cfg.model,
        "messages": messages,
        "stream": True,
        # 要求供应商在流末尾补 usage；不支持的中转会 400，由下面的回退兜住。
        "stream_options": {"include_usage": True},
    }
    if not reasoning:
        payload["temperature"] = temp
    if limit:
        payload["max_completion_tokens" if reasoning else "max_tokens"] = limit

    started = time.time()
    emitted = 0
    usage: dict = {}
    try:
        with _client() as client:
            with client.stream("POST", url, headers=_headers(provider, protocol),
                               json=payload) as resp:
                if resp.status_code >= 400:
                    body = resp.read().decode("utf-8", "replace")[:600]
                    raise ModelUnavailable(f"供应商返回 {resp.status_code}: {body}")
                for line in resp.iter_lines():
                    line = (line or "").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if raw == "[DONE]":
                        break
                    try:
                        chunk = json.loads(raw)
                    except ValueError:
                        continue
                    if isinstance(chunk.get("usage"), dict):
                        usage = chunk["usage"]
                    for choice in chunk.get("choices") or []:
                        delta = (choice or {}).get("delta") or {}
                        text = delta.get("content")
                        if isinstance(text, list):
                            text = _plain_text(text)
                        if isinstance(text, str) and text:
                            emitted += len(text)
                            yield text
    except ModelUnavailable:
        if emitted:
            # 已经吐出一部分再失败：不能静默重来，否则用户会看到重复内容。
            raise
        text = chat(db, messages, purpose=purpose, temperature=temperature,
                    max_tokens=max_tokens, stats=stats, cfg_override=cfg_override)
        if text:
            yield text
        return
    except (httpx.InvalidURL, httpx.HTTPError) as exc:
        # 流式路径没有经过 `_post()`，网络异常必须在这里归一成 ModelUnavailable
        # 的语义，否则调用方（问答链路）只捕获 ModelUnavailable，会把连接失败
        # 当成未处理异常，整条 SSE 直接中断。
        if emitted:
            raise ModelUnavailable(
                f"网络请求失败：{exc.__class__.__name__}: {exc}") from exc
        text = chat(db, messages, purpose=purpose, temperature=temperature,
                    max_tokens=max_tokens, stats=stats, cfg_override=cfg_override)
        if text:
            yield text
        return

    elapsed = int((time.time() - started) * 1000)
    if not emitted:
        # 流正常结束却没有任何文本：回退一次普通调用，避免返回空回答。
        text = chat(db, messages, purpose=purpose, temperature=temperature,
                    max_tokens=max_tokens, stats=stats, cfg_override=cfg_override)
        if text:
            yield text
        return

    if stats:
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        if not prompt_tokens and not completion_tokens:
            # 供应商没有回 usage（或不支持 stream_options）：按字符数粗估，
            # 并在 notes 里写明是估算值，避免把估算当成真实账单。
            prompt_tokens = max(1, sum(
                len(_plain_text(m.get("content"))) for m in messages) // 4)
            completion_tokens = max(1, emitted // 4)
            stats.add_note("流式调用未返回 usage，token 数按字符数估算")
        stats.record_chat(prompt_tokens=prompt_tokens,
                          completion_tokens=completion_tokens,
                          latency_ms=elapsed)


def _embed_dashscope_batches(texts: list[str], payload: dict, provider: models.Provider,
                             protocol: str, stats: CallStats | None) -> list[list[float]]:
    """将中国区 DashScope 的单次同步请求限制为 10 条，并校验所有批次。"""
    url = _join(provider.base_url, "embeddings")
    vectors: list[list[float]] = []
    expected_dim = payload.get("dimensions")
    for start in range(0, len(texts), 10):
        batch = texts[start:start + 10]
        batch_payload = {**payload, "input": batch}
        started = time.time()
        data = _post(url, batch_payload, provider, protocol)
        try:
            rows = data["data"]
            if not isinstance(rows, list):
                raise TypeError("data 不是数组")
            if len(rows) != len(batch):
                raise ModelUnavailable(
                    f"Embedding 数量不匹配：请求 {len(batch)} 条，返回 {len(rows)} 条")
            indexes = [row["index"] for row in rows]
            if (any(type(index) is not int for index in indexes)
                    or sorted(indexes) != list(range(len(batch)))):
                raise ModelUnavailable("Embedding 索引不匹配")
            rows = sorted(rows, key=lambda row: row["index"])
            batch_vectors = [normalize(row["embedding"]) for row in rows]
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ModelUnavailable(f"Embedding 响应结构异常：{str(data)[:300]}") from exc
        for vector in batch_vectors:
            if expected_dim is None:
                expected_dim = len(vector)
            if not vector or len(vector) != expected_dim:
                raise ModelUnavailable(
                    f"Embedding 维度不匹配：预期 {expected_dim}，返回 {len(vector)}")
        if stats:
            usage = data.get("usage") or {}
            stats.record_embed(
                texts=len(batch),
                tokens=int(usage.get("prompt_tokens") or usage.get("total_tokens") or 0),
                latency_ms=int((time.time() - started) * 1000),
                live=True,
            )
        vectors.extend(batch_vectors)
    return vectors


def embed(db: Session, texts: list[str], *, stats: CallStats | None = None,
          cfg_override: models.ModelConfig | None = None) -> list[list[float]]:
    """返回与 texts 等长的向量列表。未配置模型时使用离线哈希向量。

    统计口径：live 按成功的供应商请求计数，offline 记一次调用；两者都记录
    embed_texts 条数，因为「向量化了多少文本」本身就是整理成本的一部分。
    但只有 live 调用进 live_calls —— 离线哈希是本地计算，不产生费用。

    协议约束：`openai-responses` 与 `anthropic` 都没有 Embedding 端点。
    这里**不**静默降级成离线哈希向量——那会让用户以为向量检索是通的，
    实际检索质量已经悄悄塌掉。宁可抛错让人去配一个 OpenAI 兼容供应商。
    """
    if not texts:
        return []
    if cfg_override is not None:
        cfg, provider = cfg_override, cfg_override.provider
        dim = cfg.embed_dim or int(json.loads(cfg.params or "{}").get("dim") or 0)
    else:
        cfg, provider, sig, dim = active_embedding(db)
        if cfg is None:
            if stats:
                stats.record_embed(texts=len(texts), live=False,
                                   note="未配置 Embedding，使用离线哈希向量")
            return [hash_embed(t, dim) for t in texts]

    protocol = provider_protocol(provider)
    if protocol != PROTOCOL_OPENAI:
        raise _no_embedding_error(protocol)

    params = json.loads(cfg.params or "{}")
    payload: dict[str, Any] = {"model": cfg.model, "input": texts}
    if params.get("dimensions"):
        payload["dimensions"] = int(params["dimensions"])
    if _dashscope_embedding(provider, cfg.model):
        vectors = _embed_dashscope_batches(texts, payload, provider, protocol, stats)
    else:
        started = time.time()
        data = _post(_join(provider.base_url, "embeddings"), payload, provider, protocol)
        try:
            rows = sorted(data["data"], key=lambda r: r.get("index", 0))
            vectors = [normalize(r["embedding"]) for r in rows]
        except (KeyError, TypeError) as exc:
            raise ModelUnavailable(f"Embedding 响应结构异常：{str(data)[:300]}") from exc
        if len(vectors) != len(texts):
            raise ModelUnavailable(
                f"Embedding 数量不匹配：请求 {len(texts)} 条，返回 {len(vectors)} 条")
        if stats:
            usage = data.get("usage") or {}
            stats.record_embed(
                texts=len(texts),
                tokens=int(usage.get("prompt_tokens") or usage.get("total_tokens") or 0),
                latency_ms=int((time.time() - started) * 1000),
                live=True,
            )
    real_dim = len(vectors[0])
    if cfg.embed_dim != real_dim or not cfg.embed_signature:
        cfg.embed_dim = real_dim
        cfg.embed_signature = embed_signature(cfg, provider, real_dim)
        db.add(cfg)
        db.commit()
    return vectors


def describe_image(db: Session, image_bytes: bytes, mime: str, filename: str,
                   *, stats: CallStats | None = None) -> str:
    """视觉模型理解图片；未配置时返回占位说明，避免静默丢内容。"""
    resolved = resolve_model(db, models.ModelPurpose.vision.value)
    if not resolved:
        if stats:
            stats.record_vision(
                live=False,
                note=f"未配置视觉模型，图片 {filename} 仅记录元信息",
            )
        return f"[图片 {filename}，未配置视觉模型，未做内容理解]"
    cfg, provider = resolved
    protocol = provider_protocol(provider)
    b64 = base64.b64encode(image_bytes).decode("ascii")
    started = time.time()

    if protocol == PROTOCOL_ANTHROPIC:
        url = _join(provider.base_url, "messages")
        payload = {
            "model": cfg.model,
            "max_tokens": 500,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text",
                     "text": "请用中文客观描述这张图片中的图表、表格或关键信息，不要推测文档未给出的结论。"},
                    {"type": "image",
                     "source": {"type": "base64", "media_type": mime, "data": b64}},
                ],
            }],
        }
        data = _post(url, payload, provider, protocol)
        text = _extract_anthropic_text(data) or f"[图片 {filename} 解析失败]"
    elif protocol == PROTOCOL_RESPONSES:
        url = _join(provider.base_url, "responses")
        payload = {
            "model": cfg.model,
            "max_output_tokens": 500,
            "input": [{
                "role": "user",
                "content": [
                    {"type": "input_text",
                     "text": "请用中文客观描述这张图片中的图表、表格或关键信息，不要推测文档未给出的结论。"},
                    {"type": "input_image", "image_url": f"data:{mime};base64,{b64}"},
                ],
            }],
        }
        data = _post(url, payload, provider, protocol)
        text = _extract_responses_text(data) or f"[图片 {filename} 解析失败]"
    else:
        url = _join(provider.base_url, "chat/completions")
        payload = {
            "model": cfg.model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "请用中文客观描述这张图片中的图表、表格或关键信息，不要推测文档未给出的结论。"},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                ],
            }],
            "max_completion_tokens" if _openai_reasoning_chat(provider, cfg.model)
            else "max_tokens": 500,
        }
        data = _post(url, payload, provider, protocol)
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            text = f"[图片 {filename} 解析失败]"
    if stats:
        usage = data.get("usage") or {}
        stats.record_vision(
            prompt_tokens=int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
            completion_tokens=int(
                usage.get("completion_tokens") or usage.get("output_tokens") or 0),
            latency_ms=int((time.time() - started) * 1000),
            live=True,
        )
    return text


# ------------------------------------------------------------------ 模型探测
def _extract_model_ids(data: Any) -> list[tuple[str, str]]:
    """从各家 /models 响应里抽 (id, display_name)。

    兼容三种常见形状（真实供应商 + 各类中转代理都见过）：
    - OpenAI / Anthropic：`{"data": [{"id": ...}]}`
    - 部分代理：`{"models": [{"id": ...}]}`
    - 少数实现：裸数组 `[{"id": ...}]` 或裸字符串数组
    """
    rows: Any = data
    if isinstance(data, dict):
        for key in ("data", "models", "items"):
            if isinstance(data.get(key), list):
                rows = data[key]
                break
        else:
            rows = []
    if not isinstance(rows, list):
        return []
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        if isinstance(row, str):
            mid, label = row, ""
        elif isinstance(row, dict):
            mid = row.get("id") or row.get("name") or row.get("model") or ""
            label = row.get("display_name") or row.get("label") or ""
            mid, label = str(mid), str(label or "")
        else:
            continue
        mid = mid.strip()
        if not mid or mid in seen:
            continue
        seen.add(mid)
        out.append((mid, label))
    return out


def _purpose_hint(model_id: str) -> str:
    """给探测结果一个默认用途，前端下拉只是预选，用户可以改。

    只做最保守的一条判断：名字里带 embed 的才认为是向量模型。
    视觉/对话无法从名字可靠区分（gpt-4o 两用），一律给 llm，
    让用户自己在导入弹窗里改，不猜。
    """
    low = model_id.lower()
    if "embed" in low:
        return models.ModelPurpose.embedding.value
    return models.ModelPurpose.llm.value


def discover_models(db: Session, provider: models.Provider) -> dict:
    """探测供应商侧可用模型。**任何失败都返回空列表 + error，不抛异常。**

    为什么不抛：这是管理界面的辅助按钮，供应商没实现 /models（很多中转、
    自建网关都没有）是完全正常的事。抛 500 只会让用户以为系统坏了，
    前端还得再包一层 try。这里统一成「结果对象」，前端照常渲染空态+原因。
    """
    protocol = provider_protocol(provider)
    items: list[dict] = []
    source = "api"
    error = ""

    try:
        data = _get(_join(provider.base_url, "models"), provider, protocol)
        pairs = _extract_model_ids(data)
        if not pairs:
            raise ModelUnavailable("供应商返回的模型列表为空")
        items = [{"model": mid, "label": label or mid, "purpose_hint": _purpose_hint(mid)}
                 for mid, label in pairs]
    except ModelUnavailable as exc:
        error = str(exc)
        if protocol == PROTOCOL_ANTHROPIC:
            # Anthropic 官方 /v1/models 需要较新的 key 权限，中转实现经常没有；
            # 回退内置清单，让用户仍能一键导入常用模型。
            source = "builtin"
            items = [{"model": mid, "label": mid, "purpose_hint": _purpose_hint(mid)}
                     for mid in ANTHROPIC_BUILTIN_MODELS]
    except Exception as exc:  # pragma: no cover - 兜底，探测绝不冒泡成 500
        error = f"{exc.__class__.__name__}: {exc}"

    # 已存在的（同供应商 + 同用途 + 同模型）标记出来，前端可置灰，导入也幂等。
    existing = {
        (m.purpose, m.model)
        for m in db.execute(
            select(models.ModelConfig).where(models.ModelConfig.provider_id == provider.id)
        ).scalars().all()
    }
    for item in items:
        item["already_imported"] = (item["purpose_hint"], item["model"]) in existing

    return {"provider_id": provider.id, "protocol": protocol,
            "items": items, "source": source, "error": error, "total": len(items)}


# ------------------------------------------------------------------ 连接测试
def test_model(db: Session, cfg: models.ModelConfig) -> dict:
    """测试单个模型配置。

    关键点：`chat`/`embed` 原本按用途取**默认**模型，直接调用会「点 A 测 B」。
    这里传 `cfg_override` 明确测被点的那一条，且不修改任何 is_default 状态
    （不写库、不抢默认、失败也不留下副作用）。
    """
    provider = cfg.provider
    protocol = provider_protocol(provider)
    started = time.time()
    ok = False
    message = ""
    try:
        if cfg.purpose == models.ModelPurpose.embedding.value:
            if protocol != PROTOCOL_OPENAI:
                raise _no_embedding_error(protocol)
            vecs = embed(db, ["连接测试"], cfg_override=cfg)
            ok = bool(vecs and vecs[0])
            message = f"返回向量维度 {len(vecs[0])}" if ok else "返回空向量"
        else:
            # o 系列的输出配额包含推理 token；16 常在生成可见文本之前就用完。
            test_budget = (2048 if protocol == PROTOCOL_OPENAI
                           and _openai_reasoning_chat(provider, cfg.model) else 16)
            text = chat(db, [{"role": "user", "content": "只回复 OK 两个字符。"}],
                        max_tokens=test_budget, cfg_override=cfg)
            ok = bool(text.strip())
            message = f"模型返回：{text.strip()[:60]}"
    except ModelUnavailable as exc:
        message = error_summary(exc, limit=300)
    except Exception as exc:  # pragma: no cover - 兜底
        message = f"{exc.__class__.__name__}: {exc}"
    latency = int((time.time() - started) * 1000)
    cfg.last_test_ok = ok
    cfg.last_test_at = models.utcnow()
    cfg.last_test_message = message[:1000]
    db.add(cfg)
    db.commit()
    return {"ok": ok, "message": message, "latency_ms": latency, "purpose": cfg.purpose,
            "model": cfg.model, "provider": provider.name, "protocol": protocol}


# ------------------------------------------------------------------ 批量导入
def import_models(db: Session, provider: models.Provider,
                  items: list[dict]) -> tuple[list[dict], list[dict]]:
    """批量导入模型配置，返回 (created, skipped)。**幂等**。

    去重键是 (provider_id, purpose, model)：同一个模型既可以是对话模型也可以是
    视觉模型（gpt-4o 两用），所以不能只按 model 去重，否则用户导入第二个用途
    会被静默丢掉。
    """
    existing = {
        (m.purpose, m.model)
        for m in db.execute(
            select(models.ModelConfig).where(models.ModelConfig.provider_id == provider.id)
        ).scalars().all()
    }
    created: list[dict] = []
    skipped: list[dict] = []
    for raw in items:
        model = str(raw.get("model") or "").strip()
        purpose = str(raw.get("purpose") or "").strip()
        if not model or purpose not in {p.value for p in models.ModelPurpose}:
            skipped.append({"model": model, "purpose": purpose, "reason": "参数不合法"})
            continue
        if (purpose, model) in existing:
            skipped.append({"model": model, "purpose": purpose, "reason": "已存在"})
            continue
        label = str(raw.get("label") or "").strip() or model
        cfg = models.ModelConfig(
            provider_id=provider.id, purpose=purpose, model=model, label=label[:128],
            params="{}", is_default=False, enabled=True,
        )
        db.add(cfg)
        db.flush()
        existing.add((purpose, model))
        created.append({"id": cfg.id, "model": model, "purpose": purpose, "label": cfg.label})
    if created:
        db.commit()
    return created, skipped
