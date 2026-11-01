"""诊断接口：卡住时用来「取证」，不是给前端页面用的日常接口。

为什么单独一个路由文件：`database is locked` 发生时，管理员需要在不重启、
不装工具的前提下拿到三样东西——谁卡着（线程栈）、卡在哪条语句（慢语句）、
这个进程到底是哪个（pid/端口/数据目录）。以前这些只能靠猜。

两个接口都只允许 admin：里面含完整线程栈与文件路径，属于运维信息。
注意 `snapshot()` 只读内存、不查库，所以库被写锁卡住时它照样能返回；
但鉴权本身要读一次用户表（`require_admin` → `get_current_user`），
库被写锁长期占住时这个请求会等 busy_timeout。真卡死时更快的路径是直接看
`DATA_DIR/logs/lock-watchdog.log`，接口是给「事后复盘 / 前端提示」用的。
"""
from __future__ import annotations

import os
import platform
import re
import sys
import threading

from fastapi import APIRouter, Depends, Request

from .. import config, models
from ..deps import require_admin
from ..services import threadpool, warmup, watchdog

router = APIRouter(tags=["diagnostics"])


def _db_hint() -> str:
    """只暴露「是哪个库」，不暴露连接串里的口令。"""
    url = config.DATABASE_URL
    if url.startswith("sqlite:///"):
        return "sqlite:" + url[len("sqlite:///"):]
    return re.sub(r"://[^/@]*@", "://***@", url)


def _server_from_request(request: Request) -> tuple[str, int | None]:
    """从 ASGI scope 取真实的监听地址。

    桌面版端口是 uvicorn 动态分配的（WRK_PORT=0），环境变量里查不到；
    但每个请求的 scope 里带着「哪个 socket 接的这条连接」，这才是真端口。
    """
    server = request.scope.get("server") if request.scope else None
    if isinstance(server, (tuple, list)) and len(server) >= 2:
        host, port = server[0], server[1]
        try:
            return str(host), int(port)
        except (TypeError, ValueError):
            return str(host), None
    return "", None


def _process_info(request: Request) -> dict:
    host, port = _server_from_request(request)
    status = watchdog.status()
    return {
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "host": host,
        # 配置里写的首选端口（0 表示让系统分配）；实际端口看上面 host/port
        "configured_port": os.getenv("WRK_PORT", ""),
        "port": port,
        "cwd": os.getcwd(),
        "python": sys.version.split()[0],
        "python_full": sys.version.replace("\n", " "),
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "data_dir": str(config.DATA_DIR),
        "db": _db_hint(),
        "threads": threading.active_count(),
        "thread_names": [t.name for t in threading.enumerate()],
        # 这条请求实际由哪条线程执行：正常应始终是 wrk-http-N（常驻池），
        # 出现其它名字说明有代码绕过了线程池、事件循环又开始自己建线程了。
        "served_by": threading.current_thread().name,
        "watchdog": status,
        # 「有任务在处理时整个软件卡住」的两道防线，出问题时先看这里：
        # threadpool.started=False 说明常驻池没起来（事件循环又会自己建线程）；
        # warmup.done=False 说明重模块还没预热完，首次任务仍要现场加载 DLL。
        "threadpool": threadpool.stats(),
        "warmup": warmup.status(),
        "env": {
            "WRK_LOCK_WATCHDOG": os.getenv("WRK_LOCK_WATCHDOG", ""),
            "WRK_WATCHDOG_INTERVAL": os.getenv("WRK_WATCHDOG_INTERVAL", ""),
            "WRK_WORKER_ENABLED": os.getenv("WRK_WORKER_ENABLED", ""),
            "WRK_DATA_DIR": os.getenv("WRK_DATA_DIR", ""),
            "WRK_HTTP_THREADS": os.getenv("WRK_HTTP_THREADS", ""),
            "WRK_WARMUP": os.getenv("WRK_WARMUP", ""),
        },
        "at": watchdog.now_iso(),
    }


@router.get("/diagnostics/lock")
def lock_diagnostics(request: Request, admin: models.User = Depends(require_admin)):
    """写锁现场：最近阻塞事件、慢语句、正在执行中的语句、全部线程栈。"""
    snap = watchdog.snapshot()
    snap["process"] = _process_info(request)
    return snap


@router.get("/diagnostics/process")
def process_diagnostics(request: Request, admin: models.User = Depends(require_admin)):
    """进程身份：pid/ppid/真实端口/工作目录/数据目录/python 版本。"""
    return _process_info(request)
