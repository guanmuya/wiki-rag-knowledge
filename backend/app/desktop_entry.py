"""桌面应用入口：由 Electron 主进程拉起，通过 stdout 报告就绪状态。

与 `app.main:app` 的区别：
1. 端口动态分配（0 = 让系统选），避免与用户机器上其他服务冲突；
2. 就绪后向 stdout 打印一行 `WRK_READY {json}`，Electron 据此拿到真实端口；
3. 收到 SIGTERM / stdin EOF 时退出，不留孤儿进程；
4. 数据目录由 Electron 传入（用户 AppData），不写程序安装目录。

关于「不留孤儿进程」：
打包后是 PyInstaller 单文件程序，实际有两个 pid（bootloader + 真进程）。
Electron 那边的 taskkill /T 只是兜底，真正可靠的是这里的两道守护：
  * procguard.watch_parent：Electron 一消失就 os._exit(0)；
  * procguard.acquire_data_lock：同一数据目录只允许一个后端实例。
两者都走硬退出 —— 会卡死的场景恰恰是优雅关闭在等 SQLite 写锁，等不到的。
"""
from __future__ import annotations

import json
import os
import signal
import socket
import sys
import threading
import time


def _parse_data_dir_arg(argv: list) -> str:
    """从命令行取 --data-dir。

    必须在 import .main 之前生效：config 模块在 import 时就会按
    WRK_DATA_DIR 建目录，晚一步数据就落到默认目录去了。
    Electron 显式传参还有个好处 —— 进程命令行里带上了本应用的数据目录，
    main.cjs 的 killStaleBackends() 能据此精确匹配残留进程。
    """
    for i, item in enumerate(argv):
        if item == "--data-dir" and i + 1 < len(argv):
            return argv[i + 1]
        if item.startswith("--data-dir="):
            return item.split("=", 1)[1]
    return ""


_DATA_DIR_ARG = _parse_data_dir_arg(sys.argv[1:])
if _DATA_DIR_ARG:
    os.environ["WRK_DATA_DIR"] = os.path.abspath(_DATA_DIR_ARG)

import uvicorn  # noqa: E402  （必须在环境变量定型之后再导入）

from . import config  # noqa: E402
from .main import app, APP_NAME, VERSION  # noqa: E402
from .services import procguard  # noqa: E402

# 收到退出请求后的硬退出预算：uvicorn 正常收尾几百毫秒，超过 5 秒基本可以
# 认定它卡住了（最常见是等 SQLite 写锁），这时必须 os._exit 走人。
HARD_EXIT_TIMEOUT = 5.0

# 故意用无锁的普通标志：这个函数会被信号处理器调用，而信号可能在主线程
# 正持有某把锁时打断它 —— 用 Lock 反而有极小概率自锁。最坏情况只是多起
# 一个定时器，两个定时器都调 os._exit(0)，先到先得，没有副作用。
_exit_timer_armed = False


def _hard_exit(code: int = 0) -> None:
    """立刻退出：不跑 atexit、不 join 线程、不等 SQLite 锁。

    用 os._exit 而不是 sys.exit：sys.exit 只是抛异常，主线程卡在锁等待里时
    根本执行不到。这是退出路径上唯一可靠的收尾手段。
    """
    os._exit(code)


def _arm_hard_exit_timer(delay: float = HARD_EXIT_TIMEOUT) -> None:
    """启动「若干秒还没退就硬退」的兜底定时器。"""
    global _exit_timer_armed
    if _exit_timer_armed:
        return
    _exit_timer_armed = True
    timer = threading.Timer(delay, _hard_exit)
    timer.daemon = True
    timer.start()


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


class _DesktopServer(uvicorn.Server):
    """uvicorn 自己会接管 SIGTERM/SIGINT（capture_signals → handle_exit），

    所以「5 秒硬退出」的兜底必须挂在这里；挂在自己的 signal.signal 上会被
    uvicorn 覆盖掉，等于没挂。
    """

    def handle_exit(self, sig, frame=None) -> None:
        super().handle_exit(sig, frame)
        _arm_hard_exit_timer()


def _parent_pids() -> list:
    """要盯的父进程：Electron 的 pid（环境变量）+ 直接的父进程（getppid）。

    packaged 时 os.getppid() 是 bootloader 而不是 Electron，两个都得盯：
    Electron 被 taskkill /F 时靠 WRK_PARENT_PID 触发，bootloader 先死时
    靠 getppid 触发。dev 模式下两者相同，下面会去重。
    """
    pids = []
    try:
        env_pid = int(os.getenv("WRK_PARENT_PID", "0") or 0)
    except ValueError:
        env_pid = 0
    for pid in (env_pid, os.getppid()):
        if pid > 0 and pid != os.getpid() and pid not in pids:
            pids.append(pid)
    return pids


def main() -> int:
    host = os.getenv("WRK_HOST", "127.0.0.1")
    preferred = int(os.getenv("WRK_PORT", "0"))
    port = _pick_port(host, preferred)
    data_dir = os.getenv("WRK_DATA_DIR") or str(config.DATA_DIR)

    # 1) 数据目录单实例锁。第二个实例在这里就会 attach 到已有后端后退出，
    #    不会拉起第二个 Worker 去抢同一条任务、抢同一把 SQLite 写锁。
    lock_info = procguard.acquire_data_lock(data_dir, port=port, host=host)
    print("WRK_LOCK " + json.dumps(lock_info, ensure_ascii=False), file=sys.stderr, flush=True)

    # 2) 父进程守护。父进程一消失立刻 os._exit(0)：这是「Electron 被强杀后
    #    后端变孤儿、继续跑 Worker 占着写锁」的根因修复。
    watched = _parent_pids()
    for pid in watched:
        procguard.watch_parent(pid, _hard_exit)
    # 打到 stderr（Electron 会写进 main.log）：出问题时一眼能看出盯的是谁
    print("WRK_GUARD " + json.dumps(
        {"pid": os.getpid(), "ppid": os.getppid(),
         "env_parent": os.getenv("WRK_PARENT_PID", ""), "watched": watched},
        ensure_ascii=False), file=sys.stderr, flush=True)

    uv_config = uvicorn.Config(
        app, host=host, port=port, log_level=os.getenv("WRK_LOG_LEVEL", "info"),
        access_log=False, lifespan="on",
    )
    server = _DesktopServer(uv_config)

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

    def request_shutdown(reason: str) -> None:
        """优雅收尾 + 硬退出兜底。

        先置 should_exit 让 uvicorn 自己收尾（关 Worker、关库），同时立刻
        上闹钟 —— 收尾卡住时闹钟会把进程带走，绝不允许「等锁等到天荒地老」。
        """
        print(f"WRK_SHUTDOWN {reason}", file=sys.stderr, flush=True)
        server.should_exit = True
        _arm_hard_exit_timer()

    # 这两个 handler 只在 uvicorn 接管信号之前有效（接管后由 _DesktopServer
    # .handle_exit 兜底），保留是为了覆盖「刚启动就被要求退出」的窗口期。
    signal.signal(signal.SIGTERM, lambda *_: request_shutdown("SIGTERM"))
    signal.signal(signal.SIGINT, lambda *_: request_shutdown("SIGINT"))

    def watch_stdin() -> None:
        """父进程（Electron）退出时 stdin 会 EOF，据此自我了断。

        packaged 时 bootloader 自己握着一份 stdin 句柄，EOF 不一定传得到
        这里，所以它只是「父进程守护」之外的补充手段，不是主路径。
        """
        try:
            while sys.stdin.readline():
                pass
        except Exception:
            pass
        request_shutdown("stdin-eof")

    if os.getenv("WRK_WATCH_STDIN", "1") == "1":
        threading.Thread(target=watch_stdin, daemon=True).start()

    try:
        server.run()
    finally:
        # 正常收尾时主动放锁，「重启后端」不用等旧进程彻底消失
        procguard.release_data_lock()
    return 0


if __name__ == "__main__":
    sys.exit(main())
