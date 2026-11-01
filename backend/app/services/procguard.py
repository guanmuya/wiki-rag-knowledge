"""桌面版后端的进程守护：数据目录单实例锁 + 父进程存活监听。

为什么需要这个模块（两次线上卡死都源于「进程没死干净」）：

1. 孤儿后端抢任务、占着 SQLite 写锁
   桌面版后端是 PyInstaller 单文件程序，启动时实际有两个 pid：
   `bootloader`（Electron 直接 spawn 的那个）+ 真正的 Python 进程。
   Electron 退出时只杀掉了 bootloader，真进程被系统过继后继续跑后台
   Worker：它照样轮询任务表、领取入库任务，并且长期持有 SQLite 写锁。
   用户侧看到的就是「应用已经关了但任务还在跑」「重新打开 database is locked」。
   解法：真进程必须自己盯着父进程，父进程一消失就 `os._exit(0)`。
   这里刻意不走优雅关闭 —— 卡住的场景恰恰是优雅关闭在等 SQLite 锁。

2. 双实例重复消费同一个任务
   两个后端指向同一个数据目录时，两个 Worker 会把同一条 queued 任务
   各自改成 running（重复写分块、重复计费）。用数据目录级文件锁保证
   同一时刻只有一个后端实例真正持有数据目录。

平台差异（Windows 的两个坑，写错就是事故）：
- Windows 的字节范围锁是强制的（mandatory）：别的进程读被锁住的字节会
  直接报错，不像 POSIX 那样只是君子协定。所以这里只锁文件第 0 个字节，
  真正的 JSON 内容从第 1 个字节开始写，读取方也从 offset 1 读。
- `os.kill(pid, 0)` 在 Windows 上不是「探活」，而是 TerminateProcess，
  会真的把进程干掉。探活必须走 OpenProcess + GetExitCodeProcess。

另一条与平台无关的硬规矩：**永远不删 backend.lock**。
POSIX 上 unlink 一个正被 flock 持有的文件是合法的，下一次 open 会创建新
inode，flock 直接成功 —— 第二个实例就拿到了「锁」，单实例语义当场失效
（unlink-while-locked）。残留文件也不需要删：进程崩了锁已经随句柄释放，
下一次直接就能锁上。
"""
from __future__ import annotations

import ctypes
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

# 锁文件名与内容布局：第 0 个字节只用来加锁，JSON 从第 1 个字节开始
LOCK_FILENAME = "backend.lock"
_CONTENT_OFFSET = 1
_LOCK_BYTES = 1

# 必须挂在模块级：文件对象一旦被 GC 回收，锁就跟着没了（POSIX close 即解锁）
_lock_fh = None
_lock_payload = None

# Windows 常量
_SYNCHRONIZE = 0x00100000
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_INFINITE = 0xFFFFFFFF
_WAIT_TIMEOUT = 0x00000102


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def lock_path(data_dir) -> Path:
    """锁文件固定放在数据目录根上，和数据库同生命周期。"""
    return Path(data_dir) / LOCK_FILENAME


# ------------------------------------------------------------------ 进程探活


def _pid_alive_windows(pid: int) -> bool:
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    kernel32.GetExitCodeProcess.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int

    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return False
    try:
        code = ctypes.c_ulong(0)
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def pid_alive(pid: int) -> bool:
    """进程是否还活着。Windows 上绝对不能退化成 os.kill(pid, 0)。"""
    try:
        pid = int(pid or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            return _pid_alive_windows(pid)
        except OSError:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # 进程存在，只是当前用户没权限给它发信号
        return True
    except OSError:
        return False
    return True


# ------------------------------------------------------------------ 健康探测


def probe_health(host: str, port: int, timeout: float = 1.5) -> bool:
    """探测已有实例是否真的在服务。

    只看锁文件不够：锁文件可能是崩溃进程留下的，也可能持有者活着但已经
    卡死（正是要避免的状态）。只有 /api/health 能通才算可用。
    """
    try:
        port = int(port or 0)
    except (TypeError, ValueError):
        return False
    if not port:
        return False
    url = f"http://{host or '127.0.0.1'}:{port}/api/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if getattr(resp, "status", 0) != 200:
                return False
            body = resp.read(4096).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError):
        return False
    return '"ok"' in body.replace(" ", "")


# ------------------------------------------------------------------ 数据目录锁


def _open_lock_file(path: Path):
    """打开（必要时创建）锁文件。

    不能用 'w+b'：那会截断另一个进程正在持有的锁文件。
    """
    try:
        return open(path, "r+b")
    except FileNotFoundError:
        return open(path, "w+b")


def _ensure_lock_byte(fh) -> None:
    """保证文件至少有 1 个字节：Windows 的 _locking 要求被锁的字节真实存在。"""
    fh.seek(0, os.SEEK_END)
    if fh.tell() == 0:
        fh.write(b"\n")
        fh.flush()


def _try_lock(fh) -> None:
    """非阻塞独占锁；拿不到抛 OSError（调用方按「已被占用」处理）。"""
    fh.seek(0)
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, _LOCK_BYTES)
    else:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def read_lock_info(data_dir) -> dict:
    """读锁文件里的持有者信息；文件不存在或内容损坏都返回 {}。"""
    try:
        with open(lock_path(data_dir), "rb") as fh:
            fh.seek(_CONTENT_OFFSET)
            raw = fh.read().decode("utf-8", "replace").strip()
        if not raw:
            return {}
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_lock_info(fh, payload: dict) -> None:
    blob = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    fh.seek(_CONTENT_OFFSET)
    fh.write(blob)
    fh.truncate()
    fh.flush()
    try:
        os.fsync(fh.fileno())
    except OSError:
        pass


def acquire_data_lock(data_dir, port: int, host: str = "127.0.0.1", attempts: int = 3) -> dict:
    """抢数据目录独占锁。

    抢到：把 {pid, port, started_at} 写进 backend.lock 并返回这份 payload。
    抢不到：
      * 旧实例还活着（/api/health 通）→ 打印 `WRK_ATTACH {json}` 后 sys.exit(0)，
        Electron 会把这条当成「后端已就绪」，直接复用已有实例；
      * 旧实例已死（pid 不在了）→ 重新抢一次（注意：**不删锁文件**，见下）；
      * 重试用尽 → 打印 `WRK_LOCKED {json}` 到 stderr，退出码 1。
    """
    global _lock_fh, _lock_payload

    path = lock_path(data_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"WRK_LOCK_ERROR {json.dumps({'error': str(exc)}, ensure_ascii=False)}",
              file=sys.stderr, flush=True)
        sys.exit(1)

    payload = {
        "pid": os.getpid(),
        "port": int(port or 0),
        "host": host or "127.0.0.1",
        "started_at": _now_iso(),
    }

    # 探测过一次就不再探测：健康探测失败要等满超时（1.5s），重复探测只会让
    # 第二个实例白等好几秒 —— 而它本该「拿不到锁就立刻返回」。
    probed = False

    for attempt in range(1, attempts + 1):
        # 先读后锁：Windows 上被锁住的那 1 个字节读不了，内容必须提前读出来
        holder = read_lock_info(data_dir)
        try:
            fh = _open_lock_file(path)
        except OSError as exc:
            print(f"WRK_LOCK_ERROR {json.dumps({'error': str(exc)}, ensure_ascii=False)}",
                  file=sys.stderr, flush=True)
            sys.exit(1)

        locked = False
        try:
            _ensure_lock_byte(fh)
            _try_lock(fh)
            locked = True
        except OSError:
            locked = False

        if not locked:
            try:
                fh.close()
            except OSError:
                pass
            hport = 0
            try:
                hport = int(holder.get("port") or 0)
            except (TypeError, ValueError):
                hport = 0
            hhost = str(holder.get("host") or host or "127.0.0.1")
            try:
                hpid = int(holder.get("pid") or 0)
            except (TypeError, ValueError):
                hpid = 0

            # 这里**绝不删除锁文件**（早期版本会删，是经典的 unlink-while-locked）：
            # POSIX 上 unlink 一个正被 flock 持有的文件是允许的，下一次 open 会
            # 创建**新 inode**，flock 立刻成功 —— 第二个实例就「抢到锁」了，
            # 单实例语义当场失效。而残留文件本来就不需要删：进程崩了锁已经随
            # 句柄释放，下一轮直接就能锁上；Windows 上文件被别的句柄占着时也删不掉。
            # 所以这里只重开、不删文件，重试对任何平台都无副作用。
            if hpid and not pid_alive(hpid):
                print(
                    "WRK_STALE_LOCK " + json.dumps(
                        {"attempt": attempt, "reason": "holder-pid-dead",
                         "holder": holder, "path": str(path)},
                        ensure_ascii=False),
                    file=sys.stderr, flush=True)
                continue

            if not probed:
                probed = True
                if probe_health(hhost, hport):
                    attach = {
                        "host": hhost,
                        "port": hport,
                        "pid": holder.get("pid") or 0,
                        "started_at": holder.get("started_at") or "",
                        "attached": True,
                    }
                    # Electron 认这一行：复用已有后端，不再拉起第二个 Worker
                    print("WRK_ATTACH " + json.dumps(attach, ensure_ascii=False), flush=True)
                    sys.exit(0)

            print(
                "WRK_STALE_LOCK " + json.dumps(
                    {"attempt": attempt, "reason": "holder-unhealthy",
                     "holder": holder, "path": str(path)},
                    ensure_ascii=False),
                file=sys.stderr, flush=True)
            continue

        _write_lock_info(fh, payload)
        _lock_fh = fh
        _lock_payload = payload
        return payload

    print("WRK_LOCKED " + json.dumps(
        {"path": str(path), "holder": read_lock_info(data_dir)}, ensure_ascii=False),
        file=sys.stderr, flush=True)
    sys.exit(1)


def release_data_lock() -> None:
    """主动放锁。

    进程退出时操作系统也会放锁，这里是为了「重启后端」这种场景能立刻
    让新实例拿到锁，而不用等旧进程彻底消失。
    """
    global _lock_fh, _lock_payload
    fh, _lock_fh = _lock_fh, None
    _lock_payload = None
    if fh is None:
        return
    try:
        if sys.platform == "win32":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, _LOCK_BYTES)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        fh.close()
    except OSError:
        pass


# ------------------------------------------------------------------ 父进程守护


def _wait_windows(pid: int, fire) -> None:
    """Windows：拿父进程的同步句柄，父进程一退出 WaitForSingleObject 立刻返回。

    比轮询好：零延迟、零 CPU。注意必须显式声明 restype/argtypes，
    否则 64 位下 HANDLE 会被 ctypes 截成 32 位 int。
    """
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int

    handle = kernel32.OpenProcess(_SYNCHRONIZE, False, int(pid))
    if not handle:
        # 打不开句柄：进程已经不在了，或者根本没权限。两种情况都按「父进程已死」处理
        fire()
        return
    try:
        kernel32.WaitForSingleObject(handle, _INFINITE)
    finally:
        kernel32.CloseHandle(handle)


def _watch_parent_loop(pid: int, on_dead, poll_interval: float) -> None:
    fired = False

    def fire() -> None:
        nonlocal fired
        if fired:
            return
        fired = True
        try:
            on_dead()
        except SystemExit:
            raise
        except BaseException:  # noqa: BLE001 - 守护线程不能把异常抛给解释器
            pass

    if sys.platform == "win32":
        try:
            _wait_windows(pid, fire)
        except OSError:
            fire()
    else:
        # 正常情况下 pid 就是自己的父进程：getppid() 一变就说明被过继了
        is_direct_parent = os.getppid() == pid
        while True:
            alive = (os.getppid() == pid) if is_direct_parent else pid_alive(pid)
            if not alive:
                break
            time.sleep(max(0.1, poll_interval))
    fire()


def watch_parent(parent_pid: int, on_dead, poll_interval: float = 0.5):
    """盯着父进程：它一消失就调用 on_dead（daemon 线程，不阻塞主流程）。

    为什么不用 stdin EOF：PyInstaller 单文件启动后是
    「Electron → bootloader → 真进程」，bootloader 自己握着一份 stdin 句柄，
    Electron 死了它也拿不到 EOF（这正是以前留孤儿的原因）。直接盯 pid 才靠谱。

    返回线程对象（pid 非法或等于自身时返回 None）。
    """
    try:
        pid = int(parent_pid or 0)
    except (TypeError, ValueError):
        return None
    if pid <= 0 or pid == os.getpid():
        return None
    thread = threading.Thread(
        target=_watch_parent_loop,
        args=(pid, on_dead, poll_interval),
        name=f"wrk-parent-watch-{pid}",
        daemon=True,
    )
    thread.start()
    return thread
