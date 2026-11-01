"""「知识库里有任务在处理时整个软件卡住」的回归测试。

现场（v1.0.9 用户机器 `data/logs/lock-watchdog.log`）：

    --- thread wrk-worker-1
        File "app\\services\\ingest.py", line 120, in ingest_version
        File "app\\services\\llm.py", line 268, in pack_vector
        File "numpy\\_core\\multiarray.py", line 11, in <module>
        File "<frozen importlib._bootstrap_external>", line 1317, in create_module
    --- thread MainThread
        File "anyio\\_backends\\_asyncio.py", line 2676, in run_sync_in_worker_thread
        File "threading.py", line 982, in start
    ----- 解锁：写锁阻塞持续 155072ms

两个独立缺陷，各有各的断言：

1. **事件循环同步建线程**：anyio 懒加载线程池在空闲线程不够时直接
   `worker.start()`；那一刻后台线程正 import 原生模块（持 Windows loader
   lock），新线程启动被挡住 → 事件循环停摆 → 所有接口无响应。
   这里把 `Thread.start` 人为拖慢 3 秒来复现该机制。
2. **写事务横跨慢操作**：`delete(Chunk)` 先开写事务，再在循环里
   `pack_vector()`（首次 import numpy）。写锁被占几十秒，交互写请求全部
   3 秒超时。这里把 `pack_vector` 拖慢，断言另一个连接仍能立刻写入。
"""
from __future__ import annotations

import base64
import os
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from sqlalchemy import text

from app import models
from app.db import SessionLocal
from app.services import llm, threadpool, warmup, worker

BACKEND_DIR = Path(__file__).resolve().parent.parent

DOC = """# 卡死回归

## 第一节

这里是一段用来触发入库流程的正文，重复几遍让分块多一点。

## 第二节

再来一段正文，确保至少有一个批次需要向量化。
"""


# ---------------------------------------------------------------- 1. 线程池
def test_threadpool_patch_installed(app_client):
    """补丁必须打到 FastAPI/Starlette 的每个引用点，而不只是定义处。

    FastAPI 在**注册路由**时就把 `run_in_threadpool` 抓成了
    functools.partial；只改 `starlette.concurrency` 里的定义，
    同步端点仍然会走 anyio 的懒加载池，卡死会原样复发。
    """
    import fastapi.concurrency
    import fastapi.dependencies.utils
    import fastapi.routing
    import starlette.concurrency

    for module in (starlette.concurrency, fastapi.concurrency,
                   fastapi.routing, fastapi.dependencies.utils):
        assert module.run_in_threadpool is threadpool.run_in_threadpool, module.__name__

    # FastAPI 的同步依赖收尾（每个请求的 db.close()）直接调 anyio，
    # 不走 run_in_threadpool —— 这条路径也必须换掉，否则请求收尾仍在建线程。
    import anyio.to_thread

    assert anyio.to_thread.run_sync.__module__ == "app.services.threadpool", \
        "anyio.to_thread.run_sync 没有被换成常驻池版本"

    info = threadpool.stats()
    assert info["started"] is True, "lifespan 没有启动常驻线程池"
    assert info["alive"] == info["size"] >= 2


def test_request_is_served_by_resident_thread(app_client, admin_headers):
    """请求必须由常驻池的 wrk-http-N 线程执行（事件循环自己不干活）。"""
    r = app_client.get("/api/diagnostics/process", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["served_by"].startswith("wrk-http-"), r.json()["served_by"]
    assert r.json()["threadpool"]["started"] is True


def test_requests_never_create_threads(app_client, admin_headers, monkeypatch):
    """慢速 `Thread.start` 不能再拖住请求 —— 这就是 v1.0.9 卡死的机制。

    把 `threading.Thread.start` 拖慢 3 秒，等价于「此刻有线程正握着
    Windows loader lock」。修复前：anyio 在事件循环里调 Thread.start，
    4 个并发请求整体卡 3 秒；修复后：常驻池早已建好线程，一次都不会调用。
    """
    # 先把测试自己的 4 条线程建好（ThreadPoolExecutor 是懒建线程的，
    # 不先占满的话它会和被测代码抢着建线程，把断言搞乱）
    executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="probe")
    gate = threading.Event()
    try:
        waiters = [executor.submit(gate.wait, 5) for _ in range(4)]
        gate.set()
        for fut in waiters:
            fut.result(timeout=5)

        original_start = threading.Thread.start
        started_threads: list[str] = []

        def slow_start(self):                      # noqa: ANN001 - 模拟 loader lock
            started_threads.append(self.name)
            time.sleep(3)
            return original_start(self)

        before = {t.ident for t in threading.enumerate()}
        monkeypatch.setattr(threading.Thread, "start", slow_start)

        def one() -> tuple[int, float]:
            t0 = time.monotonic()
            resp = app_client.get("/api/kbs", headers=admin_headers)
            return resp.status_code, (time.monotonic() - t0) * 1000

        results = [f.result(timeout=30) for f in [executor.submit(one) for _ in range(4)]]
        after = {t.ident for t in threading.enumerate()}

        assert started_threads == [], f"请求路径上又创建了线程：{started_threads}"
        for status, ms in results:
            assert status == 200
            assert ms < 1500, f"请求被 Thread.start 拖住 {ms:.0f}ms（修复失效）"
        assert after - before == set(), "请求期间出现了新线程"
    finally:
        executor.shutdown(wait=True)


# ---------------------------------------------------------------- 2. 写事务
def _upload(app_client, admin_headers) -> int:
    kb = app_client.post("/api/kbs", headers=admin_headers,
                         json={"name": f"卡死回归-{uuid.uuid4().hex[:6]}", "description": "",
                               "mode": "basic"}).json()
    up = app_client.post(f"/api/kbs/{kb['id']}/documents", headers=admin_headers,
                         files=[("files", ("freeze.md", (DOC * 8).encode(), "text/markdown"))])
    assert up.status_code == 200, up.text
    return kb["id"]


def test_chunk_building_does_not_hold_write_lock(app_client, admin_headers, monkeypatch):
    """构造分块（含 pack_vector）期间，别的连接必须能立刻写入。

    修复前 `delete(Chunk)` + flush 先开了写事务，随后循环里 `pack_vector`
    首次 import numpy（打包版要几十秒）—— 写锁被占，交互写请求 3 秒超时。
    """
    _upload(app_client, admin_headers)

    in_pack = threading.Event()
    release = threading.Event()
    real_pack = llm.pack_vector

    def slow_pack(vec):                 # 模拟「首次 import numpy / 慢操作」
        in_pack.set()
        release.wait(timeout=15)
        return real_pack(vec)

    monkeypatch.setattr(llm, "pack_vector", slow_pack)

    errors: list[BaseException] = []

    def run_worker() -> None:
        try:
            while worker.run_once("freeze-test"):
                pass
        except BaseException as exc:      # pragma: no cover - 断言里会报出来
            errors.append(exc)

    thread = threading.Thread(target=run_worker, name="freeze-worker")
    thread.start()
    elapsed = None
    read_ms = None
    try:
        assert in_pack.wait(timeout=30), "worker 没有走到 pack_vector"

        # 读接口：同一时刻必须照常返回
        t0 = time.monotonic()
        resp = app_client.get("/api/kbs", headers=admin_headers)
        read_ms = (time.monotonic() - t0) * 1000
        assert resp.status_code == 200, resp.text

        # 写接口：修复前这里会等满 busy_timeout 后抛 database is locked
        other = SessionLocal()
        try:
            other.execute(text("PRAGMA busy_timeout=800"))
            t0 = time.monotonic()
            other.add(models.AppSetting(key=f"freeze-{uuid.uuid4().hex[:8]}", value="1"))
            other.commit()
            elapsed = (time.monotonic() - t0) * 1000
        finally:
            other.close()
    finally:
        release.set()
        thread.join(timeout=60)

    assert not errors, errors
    assert read_ms is not None and read_ms < 1000, f"pack_vector 期间读接口耗时 {read_ms:.0f}ms"
    assert elapsed is not None and elapsed < 900, \
        f"pack_vector 期间写入被写锁挡住 {elapsed:.0f}ms（写事务又横跨慢操作了）"


# ---------------------------------------------------------------- 3. 向量打包
def test_pack_vector_bytes_match_numpy():
    """去掉 numpy 后必须与历史数据逐字节兼容（老库里的向量还要能读）。"""
    np = pytest.importorskip("numpy")
    vec = [0.1, -0.2, 3.5, 1e-3]
    expected = base64.b64encode(np.asarray(vec, dtype="float32").tobytes()).decode("ascii")
    assert llm.pack_vector(vec) == expected
    assert llm.unpack_vector(llm.pack_vector(vec)) == np.asarray(vec, dtype="float32").tolist()
    assert llm.unpack_vector("") is None
    assert llm.unpack_vector(None) is None


def test_pack_vector_does_not_import_numpy(isolated_env):
    """入库路径不能再懒加载 numpy（这是 155 秒写锁的直接触发点）。"""
    code = (
        "import sys\n"
        "from app.services.llm import pack_vector, unpack_vector\n"
        "blob = pack_vector([0.25, -0.5, 1.0])\n"
        "assert unpack_vector(blob) == [0.25, -0.5, 1.0], unpack_vector(blob)\n"
        "assert 'numpy' not in sys.modules, 'pack_vector 又把 numpy 拉进来了'\n"
        "print('ok')\n"
    )
    env = dict(os.environ)
    env["WRK_DATA_DIR"] = isolated_env
    env["PYTHONPATH"] = str(BACKEND_DIR)
    env["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run([sys.executable, "-c", code], cwd=str(BACKEND_DIR), env=env,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------- 4. 预热
def test_warmup_runs_in_background(monkeypatch):
    """预热机制：后台线程 import 指定模块，失败不影响启动。"""
    monkeypatch.setattr(warmup, "ENABLED", True)
    monkeypatch.setattr(warmup, "_state", {"started": False, "done": False, "modules": {}})
    monkeypatch.setattr(warmup, "_thread", None)

    assert warmup.start(("json", "decimal")) is True
    assert warmup.wait(timeout=10) is True
    state = warmup.status()
    assert state["done"] is True
    assert state["modules"]["json"]["ok"] is True
    assert state["modules"]["decimal"]["ok"] is True


def test_warmup_disabled_by_env(isolated_env):
    """测试环境关掉预热（conftest 设了 WRK_WARMUP=0），避免后台线程干扰断言。"""
    assert warmup.ENABLED is False
