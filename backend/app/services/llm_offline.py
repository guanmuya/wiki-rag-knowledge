"""离线演示模式的向量降级：确定性哈希向量。

未配置 Embedding 供应商时，`llm.embed()` 走这里的 `hash_embed()` 产出
固定维度的本地向量，保证检索链路可跑通、可测试、且不产生费用。
与「向量打包」分开成模块，是为了让「离线降级」这一职责有明确落点。
"""
from __future__ import annotations

import hashlib
import math
import re

from .llm_vectors import normalize

FALLBACK_EMBED_DIM = 256
FALLBACK_EMBED_NAME = "local-hash-embedding"

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
