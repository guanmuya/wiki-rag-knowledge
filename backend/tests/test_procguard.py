"""procguard（后端单实例文件锁 + 父进程守护）单元测试。

为什么要这两件事（桌面版真实故障）：Electron 的 `stopBackend()` 只杀掉了
PyInstaller 的 bootloader，真正跑 FastAPI + worker 的 python 进程活了下来。
孤儿进程继续抢任务、长时间占用 SQLite 写锁，主程序这边所有写请求就卡在
`database is locked` 上——用户看到的是「embedding 解析文档时整个软件卡住」。

procguard 负责：
1. 数据目录文件锁 `<data_dir>/backend.lock`：同一数据目录只允许一个后端实例。
   第二个实例抢不到锁时**立即**退出（旧实例健康就 attach 复用，不健康就清掉
   残留锁重试，重试用尽打印 `WRK_LOCKED` 并 exit(1)）——绝不能出现两个后端
   同时跑 worker。
2. 父进程守护：父进程（Electron）一消失就回调，由调用方决定怎么退出，
   不再依赖 stdin EOF（bootloader 握着句柄，EOF 永远不来）。

procguard.py 由 sidecar-lifecycle 维护，公开 API 已对齐：
`lock_path / acquire_data_lock / release_data_lock / read_lock_info /
pid_alive / probe_health / watch_parent`。
文件还没落地时整文件跳过，避免并行开发期间把测试套件变红。
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time

import pytest

procguard = pytest.importorskip(
    "app.services.procguard",
    reason="procguard.py 尚未落地（sidecar-lifecycle 正在实现），跳过整组用例",
)

# 锁文件第 0 字节是给 _locking/flock 占位用的，JSON 从 offset 1 开始。
# procguard 已经不再 unlink 锁文件（unlink-while-locked 在 POSIX 上会破坏单实例），
# 所以这两条理论上跨平台都成立；只是本仓库的桌面版只发 Windows、没有 Linux CI，
# POSIX 分支没有实测过，保守起见先只在这边跑。有 Linux runner 时可以直接放开。
WIN_ONLY = pytest.mark.skipif(
    sys.platform != "win32",
    reason="POSIX 分支未在本仓库实测（桌面版只发 Windows）；放开前先在 Linux 上跑一遍",
)


@pytest.fixture()
def data_dir(tmp_path):
    """每个用例一个独立数据目录；结束时无论如何都把锁放掉。"""
    yield tmp_path
    try:
        procguard.release_data_lock()
    except Exception:            # pragma: no cover - 清理不该影响结论
        pass


def _free_port() -> int:
    """拿一个当前没人监听的端口：让 probe_health 立刻失败（=旧实例已死）。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _dead_pid() -> int:
    """起一个立刻退出的子进程，拿到一个确定已经死掉的 pid。"""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=30)
    return int(proc.pid)


# ------------------------------------------------------------------ 文件锁
@WIN_ONLY
def test_second_acquire_in_same_process_exits(data_dir, capsys):
    """同一数据目录第二次抢锁必须立刻失败退出，而不是悄悄再起一个 worker。"""
    port = _free_port()
    payload = procguard.acquire_data_lock(data_dir, port)

    assert payload["pid"] == os.getpid()
    assert payload["port"] == port
    assert payload["host"] == "127.0.0.1"
    assert payload["started_at"]
    # 锁文件就落在数据目录里，名字固定（Electron/运维脚本都按这个名字找）
    assert procguard.lock_path(data_dir) == data_dir / procguard.LOCK_FILENAME
    assert procguard.read_lock_info(data_dir)["pid"] == os.getpid()

    started = time.monotonic()
    with pytest.raises(SystemExit) as excinfo:
        procguard.acquire_data_lock(data_dir, port + 1, attempts=2)
    elapsed = time.monotonic() - started

    assert excinfo.value.code == 1, "抢不到锁应该 exit(1)，exit(0) 只用于 attach 已有实例"
    assert elapsed < 5, f"第二次抢锁花了 {elapsed:.2f}s，应该非阻塞立刻失败"
    captured = capsys.readouterr()
    assert "WRK_LOCKED" in captured.err, f"没打印可读原因：{captured.err!r}"


@WIN_ONLY
def test_acquire_is_blocked_even_before_holder_writes_info(data_dir):
    """锁文件内容坏了/为空也不能让第二个实例挤进来（锁靠的是字节锁，不是 JSON）。"""
    port = _free_port()
    procguard.acquire_data_lock(data_dir, port)
    path = procguard.lock_path(data_dir)
    # 模拟「进程刚拿到锁、payload 还没写完就被杀了」：只留占位字节
    with open(path, "r+b") as fh:
        fh.seek(1)
        fh.truncate()

    with pytest.raises(SystemExit):
        procguard.acquire_data_lock(data_dir, port + 1, attempts=1)


def test_second_acquire_never_deletes_lock_file(data_dir):
    """抢锁失败时绝不能删锁文件（unlink-while-locked 会让「单实例」失效）。

    POSIX 上 unlink 一个正被 flock 持有的文件是合法的：下一个进程 open 拿到的是
    **新 inode**，flock 直接成功 → 同一数据目录能跑起两个后端，各跑一个 worker
    抢同一份任务、各持一把写锁。这正是「孤儿持锁」的另一种造法，所以锁文件必须
    原地不动（残留文件也不用删：进程崩了锁随句柄释放，下一轮直接就能锁上）。

    断言：第二次抢锁失败后，锁文件还在、inode 没变、内容还是第一位持有者的。
    """
    port = _free_port()
    first = procguard.acquire_data_lock(data_dir, port)
    path = procguard.lock_path(data_dir)
    ino_before = os.stat(path).st_ino

    with pytest.raises(SystemExit):
        procguard.acquire_data_lock(data_dir, port + 1, attempts=1)

    ino_after = os.stat(path).st_ino
    assert ino_after == ino_before, (
        f"锁文件被删掉重建了（inode {ino_before} → {ino_after}）："
        "unlink-while-locked 会让 POSIX 上的单实例保护失效"
    )
    info = procguard.read_lock_info(data_dir)
    assert info.get("pid") == first["pid"] == os.getpid()
    assert info.get("port") == port, "抢锁失败的那次把持有者信息覆盖掉了"


def test_release_allows_reacquire(data_dir):
    """放锁后必须能重新拿到（「重启后端」按钮依赖这条）。"""
    port = _free_port()
    first = procguard.acquire_data_lock(data_dir, port)
    assert first["port"] == port

    procguard.release_data_lock()
    # 放锁后不应再 SystemExit
    second = procguard.acquire_data_lock(data_dir, port)
    assert second["pid"] == os.getpid()
    assert second["port"] == port


def test_read_lock_info_tolerates_missing_and_corrupt(data_dir):
    """锁信息只是给人看的诊断数据，缺文件/坏内容都必须返回 {}，不能抛。"""
    assert procguard.read_lock_info(data_dir) == {}

    path = procguard.lock_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\n" + "这不是 JSON".encode("utf-8"))
    assert procguard.read_lock_info(data_dir) == {}


def test_pid_alive(data_dir):
    assert procguard.pid_alive(os.getpid()) is True
    assert procguard.pid_alive(_dead_pid()) is False


# ------------------------------------------------------------------ 父进程守护
def test_watch_parent_fires_once_when_parent_already_gone(data_dir):
    """父进程已经不在了：立刻回调，且只回调一次（不能重复触发退出流程）。"""
    calls: list[float] = []
    fired = threading.Event()

    def on_dead() -> None:
        calls.append(time.monotonic())
        fired.set()

    thread = procguard.watch_parent(_dead_pid(), on_dead, poll_interval=0.1)
    assert thread is not None, "合法 pid 应该返回守护线程"
    assert fired.wait(timeout=3.0), "父进程已死却没触发回调"

    time.sleep(1.0)
    assert len(calls) == 1, f"回调被触发了 {len(calls)} 次"


def test_watch_parent_waits_for_live_parent_then_fires(data_dir):
    """父进程活着时不触发；被强杀后 3s 内必须触发。"""
    parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    fired = threading.Event()
    try:
        thread = procguard.watch_parent(parent.pid, fired.set, poll_interval=0.1)
        assert thread is not None
        assert not fired.wait(timeout=1.2), "父进程还活着就触发了回调（会误杀自己）"
    finally:
        parent.kill()
        parent.wait(timeout=30)

    assert fired.wait(timeout=3.0), "父进程被强杀后守护回调没触发——这就是留孤儿的路径"


def test_watch_parent_rejects_invalid_pid(data_dir):
    """非法/自身 pid 不该起线程（否则进程会盯着自己，一启动就自杀）。"""
    fired = threading.Event()
    assert procguard.watch_parent(0, fired.set) is None
    assert procguard.watch_parent(-1, fired.set) is None
    assert procguard.watch_parent(os.getpid(), fired.set) is None
    assert procguard.watch_parent("abc", fired.set) is None
    assert not fired.wait(timeout=0.3)
