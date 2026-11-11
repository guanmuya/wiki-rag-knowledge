"""登录失败限流（P1-8）。

为什么需要
----------
`POST /api/auth/login` 原先对任何失败都只回 401，没有计数、没有等待：初始管理员
口令是部署方用 `WRK_ADMIN_PASSWORD` 给的，密码强度规则也挡不住「对着接口硬试」
——攻击者换着用户名撞库，几千次请求里中一次就拿到 admin 令牌。前端弹窗和
强度校验都发生在浏览器里，边界只能落在后端。

两个维度分别计数
----------------
- **账号维度**：同一用户名在窗口内失败 N 次就锁一段时间，保护单个账号；
- **来源维度**：同一来源在窗口内失败 M 次（M 远大于 N）同样锁住，挡「换用户名
  继续试」的同一来源。两类阈值都可通过环境变量调整，默认见 config.py。

为什么放在内存里、不落库
------------------------
1. 这是「防爆破的临时状态」，不是业务数据：进程重启即清零，也正是我们想要的
   行为——一次误锁（用户自己敲错太多次）不该把人永久挡在门外，而桌面版重启
   应用对用户来说是零成本操作。
2. 登录是**没有会话**的写路径，再给它加一张表意味着每次失败都要拿写锁；而
   写锁被后台任务占着时，登录本来就该尽快失败而不是排队。
3. 代价明确：多进程部署时各进程独立计数（本项目的桌面形态是单进程常驻），
   且重启清零。这两个取舍都写在这里，免得被当成疏漏。

不引入新依赖：只用标准库的 deque + threading.Lock。
"""
from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass

from .. import config

# 内存上限：被扫的随机用户名不能把进程撑爆。超过就整批丢最旧的一半——
# 计数是可丢失的防护状态，不值得为它做精确淘汰。
_MAX_KEYS = 4096
# 浮点减法会带来 1e-9 级的尾巴；算剩余秒数时先抹掉，避免 Retry-After 多报 1 秒。
_SECONDS_EPSILON = 1e-6


@dataclass(frozen=True)
class Decision:
    """一次登录尝试的判定结果。`allowed=False` 时按 `retry_after` 秒拒绝。"""

    allowed: bool
    retry_after: int = 0        # 秒；allowed=True 时为 0
    reason: str = ""            # account / source（命中哪一维度的锁定）
    failures: int = 0           # 账号维度窗口内的失败次数，写审计用

    def detail(self) -> str:
        """给用户看的中文说明（前端直接展示 detail）。"""
        if self.allowed:
            return ""
        minutes = max(1, math.ceil(self.retry_after / 60))
        who = "该账号" if self.reason == "account" else "当前来源"
        return f"登录失败次数过多，{who}已被临时锁定，请约 {minutes} 分钟后再试"


_failures: dict[str, deque[float]] = {}
_lock = threading.Lock()
_clock = time.monotonic          # 测试可替换成假时钟，避免真的 sleep


# ---------------------------------------------------------------- 内部工具
def _account_key(username: str) -> str:
    return "u:" + (username or "").strip().lower()


def _source_key(source: str) -> str:
    return "i:" + (source or "unknown")


def _thresholds() -> tuple[int, int, int, int]:
    """(账号阈值, 来源阈值, 窗口秒, 锁定时长秒)。每次现读 config，便于测试调小。"""
    return (
        max(1, int(config.LOGIN_MAX_FAILURES)),
        max(1, int(config.LOGIN_MAX_FAILURES_PER_SOURCE)),
        max(1, int(config.LOGIN_FAIL_WINDOW_SECONDS)),
        max(1, int(config.LOGIN_LOCK_SECONDS)),
    )


def _retry_after(locked_until: float, now: float) -> int:
    """还有几秒解锁，向上取整。

    为什么要先减 `_SECONDS_EPSILON`：锁定时刻通常是 `now + lock_seconds`，
    再减回同一个 `now` 时，浮点减法可能得到 900.0000000001，直接 `ceil`
    就会把 `Retry-After` 报成 901（客户端白等一秒）。抹掉 1 微秒的浮点尾巴
    不影响「还剩多少秒」的语义——那是远小于 1 秒的误差。
    """
    return max(1, math.ceil(locked_until - now - _SECONDS_EPSILON))


def _prune(bucket: deque[float], *, now: float, window: int) -> None:
    cutoff = now - window
    while bucket and bucket[0] < cutoff:
        bucket.popleft()


def _trim_storage() -> None:
    """键数超上限时丢掉最旧的一半（按最近一次失败时间排序）。"""
    if len(_failures) <= _MAX_KEYS:
        return
    ordered = sorted(_failures.items(), key=lambda kv: kv[1][-1] if kv[1] else 0.0)
    for key, _ in ordered[: max(1, len(ordered) // 2)]:
        _failures.pop(key, None)


def _decision(username: str, source: str, *, now: float) -> Decision:
    account_limit, source_limit, window, lock_seconds = _thresholds()
    account = _failures.get(_account_key(username))
    if account:
        _prune(account, now=now, window=window)
    failures = len(account) if account else 0

    for key, limit, reason in (
        (_account_key(username), account_limit, "account"),
        (_source_key(source), source_limit, "source"),
    ):
        bucket = _failures.get(key)
        if not bucket:
            continue
        _prune(bucket, now=now, window=window)
        if len(bucket) < limit:
            continue
        locked_until = bucket[-1] + lock_seconds
        if locked_until > now:
            return Decision(False, retry_after=_retry_after(locked_until, now),
                            reason=reason, failures=failures)
    return Decision(True, failures=failures)


# ---------------------------------------------------------------- 对外接口
def check(username: str, source: str) -> Decision:
    """这次尝试现在能不能进（只读，不计数）。"""
    with _lock:
        return _decision(username, source, now=_clock())


def register_failure(username: str, source: str) -> Decision:
    """记一次失败，返回记录之后的判定（刚达到阈值时这里就已经能给出锁定信息）。"""
    account_limit, source_limit, _, _ = _thresholds()
    with _lock:
        now = _clock()
        for key, limit in ((_account_key(username), account_limit),
                           (_source_key(source), source_limit)):
            bucket = _failures.setdefault(key, deque(maxlen=limit))
            bucket.append(now)
        _trim_storage()
        return _decision(username, source, now=now)


def register_success(username: str, source: str) -> None:
    """登录成功即清零：合法用户不该被别人的撞库拖累。"""
    with _lock:
        _failures.pop(_account_key(username), None)
        _failures.pop(_source_key(source), None)


def reset() -> None:
    """清空全部计数。给测试用，也可在运维手动解锁时调用。"""
    with _lock:
        _failures.clear()


def status() -> dict:
    """给诊断接口用的只读状态：只给数量与阈值，不回显用户名/来源。"""
    account_limit, source_limit, window, lock_seconds = _thresholds()
    now = _clock()
    with _lock:
        locked = 0
        for key, bucket in list(_failures.items()):
            _prune(bucket, now=now, window=window)
            limit = account_limit if key.startswith("u:") else source_limit
            if len(bucket) >= limit and bucket[-1] + lock_seconds > now:
                locked += 1
        tracked = len(_failures)
    return {
        "tracked_keys": tracked,
        "locked_keys": locked,
        "account_threshold": account_limit,
        "source_threshold": source_limit,
        "window_seconds": window,
        "lock_seconds": lock_seconds,
    }
