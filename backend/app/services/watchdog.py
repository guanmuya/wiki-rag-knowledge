"""SQLite 写锁看门狗（常驻诊断，开销极低）。

为什么需要它
------------
桌面版在解析 / 向量化文档时会偶发「整个软件卡住」，事后日志里只有一句
`OperationalError: database is locked` 的 SQLAlchemy 栈尾。那条栈属于
**被挡住的人**，不是持锁的人，所以事后永远查不出「谁把写事务开了几十秒」。
这里做三件事补上现场：

1. 探针：守护线程每 0.5s 用一条**独立连接**执行
   `PRAGMA busy_timeout=300` + `BEGIN IMMEDIATE` + `ROLLBACK`。
   这两句只碰锁、不碰业务数据——拿到写锁立刻放，不写任何一行。
   探针被挡住（耗时 >250ms，或直接抛 `database is locked`）就说明此刻
   有别的连接正握着写锁。
2. 现场：被挡住时用 `sys._current_frames()` 抓本进程**所有**线程的栈，
   连同「正在执行中的语句」和「最近慢语句」一起追加写
   `DATA_DIR/logs/lock-watchdog.log`。同一分钟内只 dump 一次，
   否则卡住 30 秒会写出 60 份几乎一样的现场，反而看不清第一条。
3. 慢语句：在 engine 上挂 before/after_cursor_execute + begin/commit 事件，
   维护「>1s 的语句」环形缓冲（deque，最多 200 条）。正常路径只多两次
   `perf_counter()` 调用，不做字符串拼接、不落盘。

约束：本模块任何异常都必须自吞。看门狗是来救火的，绝不能自己把应用点着。
"""
from __future__ import annotations

import collections
import logging
import os
import sqlite3
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import event

from .. import config
from ..db import engine

log = logging.getLogger("wrk.watchdog")

# ---------------------------------------------------------------- 可调项
# 全部直接读环境变量，不写进 config.py：那是别人正在改的文件，
# 而且这些开关只在排障时需要，属于「现场可调」而不是「产品配置」。
_ENABLED = os.getenv("WRK_LOCK_WATCHDOG", "1") == "1"
_INTERVAL = float(os.getenv("WRK_WATCHDOG_INTERVAL", "0.5"))          # 探针间隔（秒）
_BLOCK_MS = float(os.getenv("WRK_WATCHDOG_BLOCK_MS", "250"))          # 判定「被挡住」的阈值
_BUSY_TIMEOUT_MS = int(os.getenv("WRK_WATCHDOG_BUSY_TIMEOUT", "300"))  # 探针自己的 busy_timeout
_SLOW_MS = float(os.getenv("WRK_WATCHDOG_SLOW_MS", "1000"))           # 慢语句阈值
_DEDUP_SECONDS = float(os.getenv("WRK_WATCHDOG_DEDUP_SECONDS", "60"))  # 同一时间窗只 dump 一次

_SLOW_MAXLEN = 200       # 慢语句环形缓冲长度（需求：最多 200 条）
_EVENT_MAXLEN = 50       # commit 事件环形缓冲长度
_STACK_TAIL = 40         # 每个线程只保留栈尾 40 帧
_SQL_HEAD = 300          # 语句只存前 300 字符
_MAX_LOG_BYTES = 5 * 1024 * 1024   # 日志超过 5MB 就滚动一次，桌面端不能无限长

_LOG_DIR = Path(config.DATA_DIR) / "logs"
_LOG_PATH = _LOG_DIR / "lock-watchdog.log"

# ---------------------------------------------------------------- 状态
_stop = threading.Event()
_thread: threading.Thread | None = None
_started_at: float = 0.0
_last_dump_at: float = 0.0
_installed = False

_slow: collections.deque[dict[str, Any]] = collections.deque(maxlen=_SLOW_MAXLEN)
_events: collections.deque[dict[str, Any]] = collections.deque(maxlen=_EVENT_MAXLEN)
_blocked_events: collections.deque[dict[str, Any]] = collections.deque(maxlen=_EVENT_MAXLEN)
# thread_ident -> 正在执行中的语句（before 进、after 出）。
# 卡在语句里的线程会一直留在这里，这正是「持锁线索」的来源。
_in_flight: dict[int, dict[str, Any]] = {}
# thread_ident -> 打开中的事务（begin 进、commit/rollback 出）。
# 「某个线程把写事务开了几十秒」是 database is locked 的头号成因，
# 光看栈看不出来（栈里往往是个 HTTP 调用），必须单独计时。
_open_tx: dict[int, dict[str, Any]] = {}
_last_probe: dict[str, Any] = {}
# 当前这轮阻塞的起点 (wall_ts, perf_ts)；None 表示此刻没阻塞。
# 用来在日志里补一行「解锁：阻塞持续 Xms」——卡了多久是最常被问的问题。
_block_started: tuple[float, float] | None = None
_state_lock = threading.Lock()
_tls = threading.local()   # 每线程的语句起始时间栈（支持嵌套执行）


# ---------------------------------------------------------------- 小工具
def _now_iso(ts: float | None = None) -> str:
    dt = datetime.fromtimestamp(ts if ts is not None else time.time()).astimezone()
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _clock(ts: float | None = None) -> str:
    """日志里只写时分秒，行首已经有完整日期了。"""
    dt = datetime.fromtimestamp(ts if ts is not None else time.time())
    return dt.strftime("%H:%M:%S.%f")[:-3]


def _short(statement: Any, limit: int = _SQL_HEAD) -> str:
    try:
        text = " ".join(str(statement).split())
    except Exception:
        return "<unprintable>"
    return text[:limit] + ("…" if len(text) > limit else "")


def _db_file() -> str | None:
    """从 DATABASE_URL 解出 sqlite 文件路径；非 sqlite / 内存库返回 None。"""
    url = config.DATABASE_URL
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        return None
    path = url[len(prefix):]
    if not path or path == ":memory:" or path.endswith("/:memory:"):
        return None
    return path


def _ensure_log_dir() -> None:
    _LOG_DIR.mkdir(parents=True, exist_ok=True)


def _write_line(line: str) -> None:
    """追加一行到看门狗日志。任何失败都咽掉：写不了文件也不能影响业务。"""
    try:
        _ensure_log_dir()
        _rotate_if_needed()
        with open(_LOG_PATH, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")
            fh.flush()
    except Exception as exc:      # pragma: no cover - 磁盘满/权限等
        log.debug("锁看门狗写日志失败（忽略）：%s", exc)


def _rotate_if_needed() -> None:
    try:
        if _LOG_PATH.exists() and _LOG_PATH.stat().st_size > _MAX_LOG_BYTES:
            backup = _LOG_PATH.with_suffix(".log.1")
            if backup.exists():
                backup.unlink()
            _LOG_PATH.rename(backup)
    except Exception:
        pass


# ---------------------------------------------------------------- engine 事件
# 这些回调全部「只记录、不阻塞、不抛」。SQLAlchemy 事件回调里抛异常会
# 直接把业务语句带崩，所以每个入口都包了 try/except。
def _on_begin(conn) -> None:
    try:
        t0 = time.perf_counter()
        conn.info["wrk_tx0"] = t0
        conn.info["wrk_tx0_at"] = time.time()
        ident = threading.get_ident()
        with _state_lock:
            if ident not in _open_tx:
                _open_tx[ident] = {
                    "t0": t0, "ts": time.time(),
                    "thread": threading.current_thread().name, "ident": ident,
                }
    except Exception:
        pass


def _on_rollback(conn) -> None:
    try:
        conn.info.pop("wrk_tx0", None)
        conn.info.pop("wrk_tx0_at", None)
        ident = threading.get_ident()
        with _state_lock:
            _open_tx.pop(ident, None)
    except Exception:
        pass


def _on_commit(conn) -> None:
    """记录一次提交。

    SQLAlchemy 没有 before_commit 事件，拿不到「COMMIT 本身耗时」，
    但「事务从 begin 到 commit 开了多久」恰恰是写锁被占多久的直接证据，
    所以这里记录 tx_ms，并明确标注这是事务时长而不是提交时长。
    """
    try:
        t0 = conn.info.pop("wrk_tx0", None)
        at = conn.info.pop("wrk_tx0_at", None)
        tx_ms = (time.perf_counter() - t0) * 1000.0 if t0 else 0.0
        rec = {
            "at": _now_iso(at), "ts": at or time.time(), "ms": round(tx_ms, 1),
            "sql": "COMMIT", "thread": threading.current_thread().name,
            "kind": "commit", "note": "事务从 begin 到 commit 的时长",
        }
        with _state_lock:
            _open_tx.pop(threading.get_ident(), None)
            _events.append(rec)
            if tx_ms >= _SLOW_MS:
                _slow.append(rec)
    except Exception:
        pass


def _on_before_cursor(conn, cursor, statement, parameters, context, executemany) -> None:
    try:
        t0 = time.perf_counter()
        stack = getattr(_tls, "starts", None)
        if stack is None:
            stack = _tls.starts = []
        stack.append(t0)
        if len(stack) > 8:      # 防御：万一漏了 after，栈也不无限涨
            del stack[:-8]
        ident = threading.get_ident()
        entry = {
            "ts": time.time(), "t0": t0, "sql": _short(statement),
            "thread": threading.current_thread().name, "ident": ident,
        }
        with _state_lock:
            _in_flight[ident] = entry
    except Exception:
        pass


def _on_after_cursor(conn, cursor, statement, parameters, context, executemany) -> None:
    try:
        ident = threading.get_ident()
        stack = getattr(_tls, "starts", None)
        t0 = stack.pop() if stack else None
        if t0 is None:
            with _state_lock:
                _in_flight.pop(ident, None)
            return
        ms = (time.perf_counter() - t0) * 1000.0
        with _state_lock:
            _in_flight.pop(ident, None)
            if ms >= _SLOW_MS:
                _slow.append({
                    "at": _now_iso(), "ts": time.time(), "ms": round(ms, 1),
                    "sql": _short(statement), "thread": threading.current_thread().name,
                    "kind": "cursor",
                })
    except Exception:
        pass


def _on_error(exception_context) -> None:
    """语句失败时 SQLAlchemy 不会发 after_cursor_execute。

    不清理的话，一次 `database is locked` 之后这个线程会永远挂在
    「正在执行中」列表里，下次真卡住时反而看不出是谁。这里把这条语句
    转成一条带耗时的事件（往往就是被锁挡住的真实耗时），再清掉在飞记录。
    """
    try:
        ident = threading.get_ident()
        with _state_lock:
            item = _in_flight.pop(ident, None)
            if item is not None:
                ms = (time.perf_counter() - item.get("t0", 0)) * 1000.0
                rec = {
                    "at": _now_iso(), "ts": time.time(), "ms": round(ms, 1),
                    "sql": item.get("sql", ""), "thread": item.get("thread", ""),
                    "kind": "error",
                    "note": str(getattr(exception_context, "original_exception", ""))[:200],
                }
                _events.append(rec)
                if ms >= _SLOW_MS:
                    _slow.append(rec)
    except Exception:
        pass


def _install_listeners() -> None:
    global _installed
    if _installed:
        return
    try:
        event.listen(engine, "before_cursor_execute", _on_before_cursor)
        event.listen(engine, "after_cursor_execute", _on_after_cursor)
        event.listen(engine, "handle_error", _on_error)
        event.listen(engine, "begin", _on_begin)
        event.listen(engine, "commit", _on_commit)
        event.listen(engine, "rollback", _on_rollback)
        _installed = True
    except Exception as exc:   # pragma: no cover - 取决于 engine 状态
        log.debug("锁看门狗事件注册失败（忽略）：%s", exc)


# ---------------------------------------------------------------- 探针
def _open_probe_conn() -> sqlite3.Connection | None:
    """独立连接：不经过 SQLAlchemy 连接池，不会被业务事务牵连。

    也不复用 SessionLocal——池里的连接可能正卡在别的线程手里，
    探针要的是「此刻锁是不是被占着」这个客观事实。
    """
    path = _db_file()
    if not path:
        return None
    if not os.path.exists(path):
        return None     # 库还没建：不要去凭空创建一个空文件
    conn = sqlite3.connect(path, timeout=_BUSY_TIMEOUT_MS / 1000.0,
                           isolation_level=None, check_same_thread=False)
    return conn


def _probe_once(conn: sqlite3.Connection) -> None:
    """拿一次写锁立刻放。

    只做 BEGIN IMMEDIATE + ROLLBACK：IMMEDIATE 会去抢 RESERVED 锁，
    抢到就说明「此刻没有别的写事务」，于是立刻回滚——不写任何业务数据。
    """
    t0 = time.perf_counter()
    blocked = False
    err = ""
    try:
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ROLLBACK")
    except sqlite3.OperationalError as exc:
        blocked = True
        err = str(exc)[:200]
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
    ms = (time.perf_counter() - t0) * 1000.0
    blocked = blocked or ms >= _BLOCK_MS

    with _state_lock:
        _last_probe.clear()
        _last_probe.update({
            "at": _now_iso(), "ms": round(ms, 1), "blocked": blocked, "error": err,
        })
        if blocked:
            _blocked_events.append({"at": _now_iso(), "ms": round(ms, 1), "error": err})

    _track_block_window(blocked)

    if blocked and _should_dump():
        _dump(ms, err)


def _track_block_window(blocked: bool) -> None:
    """记录这轮阻塞从什么时候开始、持续了多久。

    只在「结束且持续 >=1s」时补一行日志：正常写竞争那种几百毫秒的擦碰
    不写，否则日志会被刷满；真卡住的几十秒则一定要有个总时长。
    """
    global _block_started
    try:
        if blocked:
            if _block_started is None:
                _block_started = (time.time(), time.perf_counter())
            return
        if _block_started is None:
            return
        _started_wall, started_perf = _block_started
        _block_started = None
        dur_ms = (time.perf_counter() - started_perf) * 1000.0
        if dur_ms >= _SLOW_MS:
            _write_line(f"----- {_now_iso()} 解锁：写锁阻塞持续 {dur_ms:.0f}ms "
                        f"pid={os.getpid()} -----")
    except Exception:
        pass


def _should_dump() -> bool:
    """同一时间窗只 dump 一次：卡住时每 0.5s 一条现场会把日志淹掉。"""
    global _last_dump_at
    now = time.time()
    with _state_lock:
        if now - _last_dump_at < _DEDUP_SECONDS:
            return False
        _last_dump_at = now
        return True


# ---------------------------------------------------------------- 现场 dump
def _thread_snapshot(limit: int = 32) -> list[dict[str, Any]]:
    """所有线程的栈。用 sys._current_frames() 才能抓到别的线程。"""
    frames = sys._current_frames()
    out: list[dict[str, Any]] = []
    for t in threading.enumerate():
        frame = frames.get(t.ident)
        stack: list[str] = []
        if frame is not None:
            try:
                stack = [line.rstrip("\n") for line in
                         traceback.format_stack(frame)[-_STACK_TAIL:]]
            except Exception:
                stack = ["<stack unavailable>"]
        out.append({
            "name": t.name, "ident": t.ident, "daemon": bool(t.daemon),
            "alive": t.is_alive(), "stack": stack,
        })
    return out[:limit]


def _dump(probe_ms: float, probe_err: str) -> None:
    try:
        now_perf = time.perf_counter()
        with _state_lock:
            slow = list(_slow)[-10:]
            in_flight = [dict(v) for v in _in_flight.values()]
            open_tx = [dict(v) for v in _open_tx.values()]
        threads = _thread_snapshot()
        now = time.time()
        in_flight_detail = []
        for item in in_flight:
            held = (now_perf - item.get("t0", now_perf)) * 1000.0
            in_flight_detail.append({
                "ident": item.get("ident"), "thread": item.get("thread"),
                "ms": round(held, 1), "sql": item.get("sql"),
            })
        tx_detail = []
        for item in open_tx:
            tx_detail.append({
                "ident": item.get("ident"), "thread": item.get("thread"),
                "ms": round((now_perf - item.get("t0", now_perf)) * 1000.0, 1),
            })
        in_flight_idents = {it["ident"] for it in in_flight_detail}

        lines: list[str] = []
        lines.append(
            f"===== {_now_iso(now)} pid={os.getpid()} ppid={os.getppid()} "
            f"探针被阻塞 {probe_ms:.0f}ms（阈值 {_BLOCK_MS:.0f}ms） ====="
        )
        lines.append(
            f"probe: PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}; BEGIN IMMEDIATE; ROLLBACK "
            f"耗时 {probe_ms:.0f}ms" + (f" err={probe_err}" if probe_err else "")
        )
        if tx_detail:
            # 这一节是排障的主菜：写事务开得越久，别人越容易撞 database is locked。
            lines.append("本进程打开中的事务（从 begin 算起，时长最久的优先怀疑）:")
            for it in sorted(tx_detail, key=lambda x: -x["ms"])[:10]:
                tag = ("正在执行语句（也可能只是被挡住的一方）"
                       if it["ident"] in in_flight_idents
                       else "没在执行语句（典型持锁方：事务开着去干别的了）")
                lines.append(f"  - [{it['ms']:.0f}ms] tid={it['ident']} "
                             f"{it['thread']} | {tag}")
        else:
            lines.append("本进程打开中的事务: 无")
        # 一句结论，省得看日志的人自己去比对：本进程线程全都卡在语句上，
        # 说明它们只是等待方，真正持锁的要么是别的进程（孤儿 sidecar），
        # 要么是本进程里「开着事务但没在执行语句」的那个线程。
        idle_holders = [it for it in tx_detail if it["ident"] not in in_flight_idents]
        if idle_holders:
            lines.append("结论: 写锁大概率在本进程这些线程手里——看它们栈里在做什么"
                         "（embedding HTTP 调用 / 大文件读写等）")
        elif tx_detail:
            lines.append("结论: 本进程线程都在等语句，持锁方可能在本进程之外"
                         "（另一个进程 / 残留 sidecar）")
        else:
            lines.append("结论: 本进程没有打开的事务，持锁方在本进程之外"
                         "（另一个进程 / 残留 sidecar）")
        if in_flight_detail:
            lines.append("正在执行中的语句（长语句会自己占住写锁；被挡住的一方也会留在这里）:")
            for it in sorted(in_flight_detail, key=lambda x: -x["ms"])[:10]:
                lines.append(f"  - [{it['ms']:.0f}ms] tid={it['ident']} "
                             f"{it['thread']} | {it['sql']}")
        else:
            lines.append("正在执行中的语句: 无")
        if slow:
            lines.append(f"最近慢语句（>={_SLOW_MS:.0f}ms，最多 10 条）:")
            for it in slow:
                kind = {"commit": "COMMIT", "error": "ERROR"}.get(it.get("kind"), "SQL")
                extra = f" | {it.get('note')}" if it.get("note") and kind != "COMMIT" else ""
                lines.append(f"  - [{_clock(it.get('ts'))}] {it.get('ms')}ms "
                             f"{it.get('thread')} | {kind} | {it.get('sql')}{extra}")
        else:
            lines.append("最近慢语句: 无")
        lines.append(f"线程快照（{len(threads)} 个线程）:")
        for t in threads:
            lines.append(f"--- thread {t['name']} ident={t['ident']} "
                         f"daemon={t['daemon']}")
            for line in t["stack"]:
                lines.append("  " + line)
        lines.append("")

        payload = "\n".join(lines)
        _write_line(payload)
        log.warning("检测到 SQLite 写锁阻塞 %.0fms，现场已写入 %s", probe_ms, _LOG_PATH)
    except Exception as exc:          # pragma: no cover - 极端防御
        log.debug("锁看门狗 dump 失败（忽略）：%s", exc)


# ---------------------------------------------------------------- 生命周期
def _loop() -> None:
    conn: sqlite3.Connection | None = None
    backoff = max(1.0, _INTERVAL * 4)
    while not _stop.is_set():
        try:
            if conn is None:
                conn = _open_probe_conn()
            if conn is None:
                _stop.wait(backoff)     # 库还没建 / 非 sqlite：慢点再看
                continue
            _probe_once(conn)
        except Exception:
            # 连接坏了就重开；看门狗自己不能死，也不能把异常抛给主线程。
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass
            conn = None
            _stop.wait(backoff)
            continue
        _stop.wait(_INTERVAL)
    try:
        if conn is not None:
            conn.close()
    except Exception:
        pass


def start() -> None:
    """启动守护线程。默认开启；WRK_LOCK_WATCHDOG=0 可整体关掉。"""
    global _thread, _started_at
    if not _ENABLED:
        log.info("锁看门狗已禁用（WRK_LOCK_WATCHDOG=0）")
        return
    if _thread is not None and _thread.is_alive():
        return
    _install_listeners()
    _stop.clear()
    _started_at = time.time()
    _thread = threading.Thread(target=_loop, name="wrk-lock-watchdog", daemon=True)
    _thread.start()
    log.info("锁看门狗已启动：间隔 %.2fs，阻塞阈值 %.0fms，日志 %s",
             _INTERVAL, _BLOCK_MS, _LOG_PATH)


def stop() -> None:
    """停守护线程。事件监听保留——重新 start() 时不必重复注册。"""
    _stop.set()
    t = _thread
    if t is not None and t.is_alive():
        t.join(timeout=3)


def running() -> bool:
    return bool(_thread is not None and _thread.is_alive())


# ---------------------------------------------------------------- 对外快照
def status() -> dict[str, Any]:
    """配置与运行状态。给诊断接口用，免得路由去摸模块私有变量。"""
    return {
        "enabled": _ENABLED,
        "running": running(),
        "started_at": _now_iso(_started_at) if _started_at else "",
        "interval_seconds": _INTERVAL,
        "block_ms_threshold": _BLOCK_MS,
        "slow_ms_threshold": _SLOW_MS,
        "log_path": str(_LOG_PATH),
        "log_exists": _LOG_PATH.exists(),
    }


def now_iso() -> str:
    """当前时间的带时区字符串（日志与接口共用同一种格式）。"""
    return _now_iso()


def snapshot() -> dict[str, Any]:
    """给诊断接口用的内存快照。只读内存，不查库——库正卡着的时候它也得能返回。"""
    with _state_lock:
        slow = list(_slow)
        events = list(_events)
        blocked = list(_blocked_events)
        probe = dict(_last_probe)
        in_flight = [dict(v) for v in _in_flight.values()]
        open_tx = [dict(v) for v in _open_tx.values()]
    now = time.perf_counter()
    for item in in_flight:
        item["ms"] = round((now - item.pop("t0", now)) * 1000.0, 1)
        item.pop("ts", None)
    for item in open_tx:
        item["ms"] = round((now - item.pop("t0", now)) * 1000.0, 1)
        item["at"] = _now_iso(item.pop("ts", None))
    threads = _thread_snapshot()
    with _state_lock:
        last_dump = _last_dump_at
    return {
        **status(),
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "db_file": _db_file() or "",
        "last_dump_at": _now_iso(last_dump) if last_dump else "",
        "blocking": _block_started is not None,
        "blocking_since": _now_iso(_block_started[0]) if _block_started else "",
        "blocking_ms": (round((time.perf_counter() - _block_started[1]) * 1000.0, 1)
                        if _block_started else 0.0),
        "last_probe": probe,
        "blocked_events": blocked[-20:],
        "slow_statements": slow[-50:],
        "commit_events": events[-20:],
        "in_flight": in_flight,
        "open_transactions": open_tx,
        "threads": threads,
        "thread_count": len(threads),
    }
