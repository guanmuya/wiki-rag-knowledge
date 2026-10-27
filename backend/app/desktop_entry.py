"""桌面应用入口：由 Electron 主进程拉起，通过 stdout 报告就绪状态。

与 `app.main:app` 的区别：
1. 端口动态分配（0 = 让系统选），避免与用户机器上其他服务冲突；
2. 就绪后向 stdout 打印一行 `WRK_READY {json}`，Electron 据此拿到真实端口；
3. 收到 SIGTERM / stdin EOF 时优雅退出，不留孤儿进程；
4. 数据目录由 Electron 传入（用户 AppData），不写程序安装目录。
"""
from __future__ import annotations

import json
import os
import signal
import socket
import sys
import threading
import time

import uvicorn

from .main import app, APP_NAME, VERSION


def _port_open(host: str, port: int) -> bool:
    """探测端口是否已开始监听。比依赖 uvicorn 内部状态更可靠。"""
    try:
        with socket.create_connection((host, port), timeout=0.2):
            return True
    except OSError:
        return False


def _pick_port(host: str, preferred: int) -> int:
    """优先用固定端口（便于用户手工访问 /docs），被占用则让系统分配。"""
    if preferred:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, preferred))
                return preferred
            except OSError:
                pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def main() -> int:
    host = os.getenv("WRK_HOST", "127.0.0.1")
    preferred = int(os.getenv("WRK_PORT", "0"))
    port = _pick_port(host, preferred)

    config = uvicorn.Config(
        app, host=host, port=port, log_level=os.getenv("WRK_LOG_LEVEL", "info"),
        access_log=False, lifespan="on",
    )
    server = uvicorn.Server(config)

    def announce() -> None:
        # 必须等 uvicorn 真正 bind 成功。只查 server.started 会漏：
        # 它在 lifespan 完成后才置位，而我们要在 bind 后立刻可用。
        for _ in range(600):
            if getattr(server, "started", False) or _port_open(host, port):
                break
            time.sleep(0.05)
        payload = {
            "host": host, "port": port, "version": VERSION, "app": APP_NAME,
            "pid": os.getpid(),
        }
        print(f"WRK_READY {json.dumps(payload, ensure_ascii=False)}", flush=True)

    threading.Thread(target=announce, daemon=True).start()

    def shutdown(*_args) -> None:
        server.should_exit = True

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    def watch_stdin() -> None:
        """父进程（Electron）退出时 stdin 会 EOF，据此自我了断。"""
        try:
            while sys.stdin.readline():
                pass
        except Exception:
            pass
        shutdown()

    if os.getenv("WRK_WATCH_STDIN", "1") == "1":
        threading.Thread(target=watch_stdin, daemon=True).start()

    server.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
