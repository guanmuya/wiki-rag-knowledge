"""启动后台预热：把「首次 import 原生模块」的成本从用户任务里挪走。

背景（同 `services/threadpool.py` 里的事故记录）：打包版第一次 import
lxml / pypdf / docx / pptx 时要去加载 DLL，Windows 上必须拿 loader lock；
杀软首次扫描新解压的 `_MEI` 目录时，这一步能拖到几十秒。

这段成本本身无法消除，但可以决定**由谁付**：

- 付在用户任务里：上传完文档，任务卡在「解析文档 / 建立索引」几十秒不动，
  用户以为软件坏了（v1.0.9 的真实反馈）；
- 付在启动后台线程里：用户还在登录、看首页，模块已经就位。

所以这里在 lifespan 里起一个 daemon 线程做预热，**不阻塞启动**、不碰数据库、
失败也只记一行日志 —— 它只是把首次成本提前，不影响任何功能。

注意：numpy / openpyxl **不在**预热名单里，原因见下面 WARM_MODULES 的注释
（它们的首次加载会在打包版里卡住 loader lock，把整个进程的线程创建一起拖死）。
"""
from __future__ import annotations

import importlib
import logging
import os
import threading
import time
from typing import Any

log = logging.getLogger("wrk.warmup")

# 是否启用（测试里关掉，避免后台线程干扰「请求不创建线程」这类断言）
ENABLED = os.getenv("WRK_WARMUP", "1") == "1"

# 预热顺序：解析库（每次上传都会用）→ 向量库（检索/大库才用）。
#
# 为什么**不预热 numpy / openpyxl**（v1.0.14 事故）：openpyxl 会 import numpy，
# 而 numpy 的 `_multiarray_umath.pyd` + OpenBLAS 是打包版里最大的原生加载。
# 实测（Windows 桌面版）后台线程加载它时会卡在 `create_module` 的 loader lock
# 上不返回，进而让进程里**所有** `Thread.start()` 一起卡死：CLI 版本探测、
# Agent 子进程输出线程、请求收尾全部挂住，界面停在「正在加载 Agent 核心…」。
# 解析库（pypdf/docx/pptx/lxml）没有这个问题，仍照常预热；numpy 只在解析
# xlsx、或候选向量超过 64 条时才需要，首次使用再加载（实测正常，约 1 秒）。
WARM_MODULES: tuple[str, ...] = (
    "pypdf",
    "docx",
    "pptx",
    "lxml.html",
)

_thread: threading.Thread | None = None
_state_lock = threading.Lock()
_state: dict[str, Any] = {"started": False, "done": False, "modules": {}}


def _run(modules: tuple[str, ...]) -> None:
    results: dict[str, Any] = {}
    started = time.perf_counter()
    for name in modules:
        t0 = time.perf_counter()
        try:
            importlib.import_module(name)
        except Exception as exc:       # pragma: no cover - 依赖缺失不该影响启动
            results[name] = {"ok": False, "ms": round((time.perf_counter() - t0) * 1000),
                             "error": f"{type(exc).__name__}: {exc}"}
            log.debug("预热 %s 失败（不影响功能）：%s", name, exc)
        else:
            results[name] = {"ok": True, "ms": round((time.perf_counter() - t0) * 1000)}
    with _state_lock:
        _state["modules"] = results
        _state["done"] = True
        _state["total_ms"] = round((time.perf_counter() - started) * 1000)
    slow = {k: v["ms"] for k, v in results.items() if v["ms"] >= 300}
    log.info("重模块预热完成：共 %dms%s", _state["total_ms"],
             f"（较慢：{slow}）" if slow else "")


def start(modules: tuple[str, ...] | None = None) -> bool:
    """启动预热线程；返回是否真的启动了（已启动/已关闭时返回 False）。"""
    global _thread
    if not ENABLED:
        return False
    with _state_lock:
        if _state["started"]:
            return False
        _state["started"] = True
    _thread = threading.Thread(
        target=_run, args=(modules or WARM_MODULES,), name="wrk-warmup", daemon=True)
    _thread.start()
    return True


def wait(timeout: float = 30.0) -> bool:
    """等预热结束（测试用）。返回是否已完成。"""
    thread = _thread
    if thread is None:
        return True
    thread.join(timeout=timeout)
    return not thread.is_alive()


def status() -> dict[str, Any]:
    """给诊断接口用：预热是否完成、各模块耗时。"""
    with _state_lock:
        return {
            "enabled": ENABLED,
            "started": _state["started"],
            "done": _state["done"],
            "total_ms": _state.get("total_ms"),
            "modules": dict(_state["modules"]),
        }
