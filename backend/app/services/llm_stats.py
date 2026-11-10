"""模型调用成本统计：`CallStats`。

所有调用点唯一的记账对象。它是一份自洽的数据类，不依赖 `llm.py` 的编排
逻辑，也不被协议适配 / 向量打包 / 离线降级反向引用，因此单独成模块。
"""
from __future__ import annotations

from dataclasses import dataclass, field


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
    # 生成通道降级的原因（供应商报错 / 空响应）。只给界面解释用，完整错误在 notes。
    # 未配置模型时留空：那种情况下界面继续显示「未调用生成模型」，不能误报成调用失败。
    offline_reason: str = ""
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
        if other.offline_reason and not self.offline_reason:
            self.offline_reason = other.offline_reason
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
            "offline_reason": self.offline_reason,
            "degraded": self.degraded,
            "notes": list(self.notes),
        }
