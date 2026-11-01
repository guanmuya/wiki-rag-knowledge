"""模型调用层：按供应商协议族分派 LLM / Embedding / 视觉模型。

支持三种协议（`Provider.protocol`）：
- `openai`（默认）：`POST {base}/chat/completions` + `POST {base}/embeddings`
- `openai-responses`：`POST {base}/responses`（`input` / `max_output_tokens` / `output[].content[].text`）
- `anthropic`：`POST {base}/messages`（`system` 独立字段、`max_tokens` 必填、`content[].text`）

为什么分派放在这一层而不是各调用点：chat/embed/describe_image/test_model 的
调用方（retrieval / wiki / answering / ingest）只关心「给我一段文本」，协议差异
属于供应商适配细节。协议判断集中一处，新增协议时不必改五个业务模块。

未配置可用模型时进入“离线演示模式”：
- LLM 侧降级为基于原文的抽取式整理（不编造），并在响应中标注 offline。
- Embedding 侧降级为确定性的哈希向量，保证检索链路可跑通、可测试。
这样首版在没有 API Key 的环境里也能完整演示，接入真实供应商后行为一致。
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import config, models, security

log = logging.getLogger("wrk.llm")

DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0)
FALLBACK_EMBED_DIM = 256
FALLBACK_EMBED_NAME = "local-hash-embedding"


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


class ModelUnavailable(RuntimeError):
    """没有可用模型配置，或供应商调用失败。"""


@dataclass
class CallStats:
    """模型调用成本统计。

    为什么必须把「生成调用」和「向量调用」分开记（而不是混在一个 calls 里）：
    1. 计费口径不同：生成按输入/输出 token 分别计价，向量通常只按输入 token 计价，
       单价往往低一个数量级。混进同一个 prompt_tokens 就算不出真实成本。
    2. 归因不同：一次问答只有 1 次生成调用，但索引阶段可能有成百上千次向量调用。
       合并计数会让「问答成本」被索引成本淹没，论文里没法分列对比。
    3. 离线降级必须可识别：离线哈希向量不产生费用，但确实是一次向量化操作。
       所以 calls/embed_calls 照记，同时用 live_calls/offline_calls 分开——
       论文里只报 live_calls 就是真实开销，不会把本地哈希算成钱。

    字段语义（全部是累计值）：
    - calls：总调用次数 = chat_calls + embed_calls + vision_calls（含离线降级）
    - chat_calls / embed_calls / vision_calls：分通道调用次数
    - live_calls / offline_calls：真实供应商调用 / 离线降级调用，二者之和等于 calls
    - prompt_tokens / completion_tokens：生成侧输入 / 输出 token
    - embed_tokens：向量侧 token（单独计费，**不**并入 prompt_tokens）
    - embed_texts：被向量化的文本条数（离线模式也有值，用来衡量索引工作量）
    - latency_ms：总耗时；latency_chat_ms / latency_embed_ms 为分通道耗时
    - mode：生成通道模式；embed_mode：向量通道模式；degraded：任一通道降级

    为什么 mode 只描述生成通道：前端按 mode === 'offline' 显示「未调用生成模型」。
    如果把「只有 embedding 走离线」也算成 mode=offline，回答明明由真实模型生成，
    界面却说没调模型——这是对用户撒谎。所以向量通道单独用 embed_mode 表达。
    """

    # ---- 计数
    calls: int = 0              # 总调用次数（含离线降级）
    chat_calls: int = 0         # 生成调用
    embed_calls: int = 0        # 向量调用
    vision_calls: int = 0       # 视觉调用
    live_calls: int = 0         # 真实供应商调用（可计费）
    offline_calls: int = 0      # 离线降级（不计费）
    # ---- token
    prompt_tokens: int = 0
    completion_tokens: int = 0
    embed_tokens: int = 0
    embed_texts: int = 0        # 被向量化的文本条数（离线模式也记）
    # ---- 耗时
    latency_ms: int = 0
    latency_chat_ms: int = 0
    latency_embed_ms: int = 0
    # ---- 模式
    mode: str = "live"          # 生成通道：live | offline
    embed_mode: str = "live"    # 向量通道：live | offline
    notes: list[str] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        """全通道 token 合计。成本换算用这个，别再自己加。"""
        return self.prompt_tokens + self.completion_tokens + self.embed_tokens

    @property
    def degraded(self) -> bool:
        """任一通道降级为离线实现。"""
        return self.mode == "offline" or self.embed_mode == "offline"

    def add_note(self, note: str) -> None:
        """追加说明，相同内容只记一次——批量索引时同一提示会重复上百次。"""
        if note and note not in self.notes:
            self.notes.append(note)

    # ---- 记录入口：所有调用点都走这里，避免各处手写自增导致再次漏计
    def record_chat(self, *, prompt_tokens: int = 0, completion_tokens: int = 0,
                    latency_ms: int = 0) -> None:
        self.calls += 1
        self.chat_calls += 1
        self.live_calls += 1
        self.prompt_tokens += max(int(prompt_tokens), 0)
        self.completion_tokens += max(int(completion_tokens), 0)
        self.latency_ms += max(int(latency_ms), 0)
        self.latency_chat_ms += max(int(latency_ms), 0)

    def record_embed(self, *, texts: int = 0, tokens: int = 0, latency_ms: int = 0,
                     live: bool = True, note: str | None = None) -> None:
        """记录一次向量化。

        离线哈希向量同样记一次调用：它不是模型调用，但它是真实的向量化工作量，
        论文里要报「索引阶段向量化了多少个单元」。是否计费看 live_calls。
        """
        self.calls += 1
        self.embed_calls += 1
        self.embed_texts += max(int(texts), 0)
        self.embed_tokens += max(int(tokens), 0)
        self.latency_ms += max(int(latency_ms), 0)
        self.latency_embed_ms += max(int(latency_ms), 0)
        if live:
            self.live_calls += 1
        else:
            self.offline_calls += 1
            self.embed_mode = "offline"
            if note:
                self.add_note(note)

    def record_vision(self, *, prompt_tokens: int = 0, completion_tokens: int = 0,
                      latency_ms: int = 0, live: bool = True, note: str | None = None) -> None:
        self.calls += 1
        self.vision_calls += 1
        self.prompt_tokens += max(int(prompt_tokens), 0)
        self.completion_tokens += max(int(completion_tokens), 0)
        self.latency_ms += max(int(latency_ms), 0)
        if live:
            self.live_calls += 1
        else:
            self.offline_calls += 1
            self.mode = "offline"
            if note:
                self.add_note(note)

    def merge(self, other: "CallStats | None") -> None:
        """把另一份统计并入本份。

        用途：把「分块向量化」和「Wiki 生成」拆成两个子统计各自累计，
        既能在任务 result 里分别留痕（成本归因），又能合并成任务总用量。
        降级状态是「粘」的：任一子统计离线，合并后整体也标记降级。
        """
        if other is None:
            return
        self.calls += other.calls
        self.chat_calls += other.chat_calls
        self.embed_calls += other.embed_calls
        self.vision_calls += other.vision_calls
        self.live_calls += other.live_calls
        self.offline_calls += other.offline_calls
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.embed_tokens += other.embed_tokens
        self.embed_texts += other.embed_texts
        self.latency_ms += other.latency_ms
        self.latency_chat_ms += other.latency_chat_ms
        self.latency_embed_ms += other.latency_embed_ms
        if other.mode == "offline":
            self.mode = "offline"
        if other.embed_mode == "offline":
            self.embed_mode = "offline"
        for note in other.notes:
            self.add_note(note)

    def as_dict(self) -> dict:
        return {
            "calls": self.calls,
            "chat_calls": self.chat_calls,
            "embed_calls": self.embed_calls,
            "vision_calls": self.vision_calls,
            "live_calls": self.live_calls,
            "offline_calls": self.offline_calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "embed_tokens": self.embed_tokens,
            "embed_texts": self.embed_texts,
            "total_tokens": self.total_tokens,
            "latency_ms": self.latency_ms,
            "latency_chat_ms": self.latency_chat_ms,
            "latency_embed_ms": self.latency_embed_ms,
            "mode": self.mode,
            "embed_mode": self.embed_mode,
            "degraded": self.degraded,
            "notes": list(self.notes),
        }


# ------------------------------------------------------------------ 解析配置
def _join(base: str, path: str) -> str:
    """拼接 OpenAI 兼容端点。兼容 base_url 带/不带 /v1，以及误带端口方括号的写法。"""
    base = (base or "").strip().rstrip("/")
    if not base:
        raise ModelUnavailable("供应商接口地址为空")
    if not re.match(r"^https?://", base, re.I):
        raise ModelUnavailable(f"供应商接口地址需以 http:// 或 https:// 开头：{base}")
    if re.search(r"/v\d+$", base):
        return f"{base}/{path.lstrip('/')}"
    return f"{base}/v1/{path.lstrip('/')}"


def resolve_model(db: Session, purpose: str) -> tuple[models.ModelConfig, models.Provider] | None:
    """按用途取默认模型；没有显式默认则取最新可用的一条。"""
    stmt = (
        select(models.ModelConfig)
        .join(models.Provider, models.Provider.id == models.ModelConfig.provider_id)
        .where(
            models.ModelConfig.purpose == purpose,
            models.ModelConfig.enabled.is_(True),
            models.Provider.enabled.is_(True),
        )
        .order_by(models.ModelConfig.is_default.desc(), models.ModelConfig.id.desc())
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


# ------------------------------------------------------------------ 向量工具
def pack_vector(vec: list[float]) -> str:
    """把向量打包成 base64（float32 小端，与历史数据逐字节兼容）。

    为什么不用 numpy：这条函数在入库循环里逐块调用，而 numpy 是**懒加载**的。
    打包版首次 `import numpy` 要去加载 `_multiarray_umath.pyd` 和 OpenBLAS，
    Windows 上必须拿 loader lock —— 实测曾把 SQLite 写锁占了 155 秒，用户看到
    的就是「任务处理中整个软件卡住」（见 services/threadpool.py 的事故说明）。
    这里只需要把 float32 写进字节串，stdlib 的 array 就够了：
    同样 4 字节、同样本机小端，读回来的值与 numpy 版本完全一致。
    """
    import array

    arr = array.array("f", (float(v) for v in vec))
    return base64.b64encode(arr.tobytes()).decode("ascii")


def unpack_vector(blob: str | None) -> list[float] | None:
    """pack_vector 的逆操作；同样不依赖 numpy（原因见上）。"""
    if not blob:
        return None
    import array

    arr = array.array("f")
    arr.frombytes(base64.b64decode(blob.encode("ascii")))
    return arr.tolist()


def normalize(vec: Iterable[float]) -> list[float]:
    vals = [float(v) for v in vec]
    norm = math.sqrt(sum(v * v for v in vals)) or 1.0
    return [v / norm for v in vals]


_TOKEN_RE = re.compile(r"[a-z0-9_]+|[\u4e00-\u9fff]")


def hash_embed(text: str, dim: int = FALLBACK_EMBED_DIM) -> list[float]:
    """确定性哈希向量：字符/词 n-gram 哈希到固定维度。离线模式与单测使用。"""
    vec = [0.0] * dim
    low = text.lower()
    tokens = _TOKEN_RE.findall(low)
    grams: list[str] = []
    grams.extend(tokens)
    for i in range(len(tokens) - 1):
        grams.append(tokens[i] + tokens[i + 1])
    for i in range(len(low) - 2):
        grams.append(low[i:i + 3])
    for g in grams:
        h = int(hashlib.md5(g.encode("utf-8")).hexdigest()[:8], 16)
        idx = h % dim
        sign = 1.0 if (h >> 8) % 2 == 0 else -1.0
        vec[idx] += sign * (1.0 + 0.2 * math.log1p(len(g)))
    return normalize(vec)


# ------------------------------------------------------------------ 协议适配
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
            raise ModelUnavailable("未配置可用的对话模型")
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
            "temperature": temp,
        }
        if limit:
            payload["max_tokens"] = limit
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


def embed(db: Session, texts: list[str], *, stats: CallStats | None = None,
          cfg_override: models.ModelConfig | None = None) -> list[list[float]]:
    """返回与 texts 等长的向量列表。未配置模型时使用离线哈希向量。

    统计口径：无论 live 还是 offline 都会记一次 embed 调用与 embed_texts 条数，
    因为「向量化了多少文本」本身就是整理成本的一部分（论文要报这个数）。
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
    started = time.time()
    data = _post(_join(provider.base_url, "embeddings"), payload, provider, protocol)
    try:
        rows = sorted(data["data"], key=lambda r: r.get("index", 0))
        vectors = [normalize(r["embedding"]) for r in rows]
    except (KeyError, TypeError) as exc:
        raise ModelUnavailable(f"Embedding 响应结构异常：{str(data)[:300]}") from exc
    if len(vectors) != len(texts):
        raise ModelUnavailable(f"Embedding 数量不匹配：请求 {len(texts)} 条，返回 {len(vectors)} 条")
    real_dim = len(vectors[0])
    if stats:
        usage = data.get("usage") or {}
        stats.record_embed(
            texts=len(texts),
            tokens=int(usage.get("prompt_tokens") or usage.get("total_tokens") or 0),
            latency_ms=int((time.time() - started) * 1000),
            live=True,
        )
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
            "max_tokens": 500,
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
            text = chat(db, [{"role": "user", "content": "只回复 OK 两个字符。"}],
                        max_tokens=16, cfg_override=cfg)
            ok = bool(text.strip())
            message = f"模型返回：{text.strip()[:60]}"
    except ModelUnavailable as exc:
        message = str(exc)
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

