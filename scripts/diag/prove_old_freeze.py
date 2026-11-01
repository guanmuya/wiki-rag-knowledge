"""验证回归测试确实能抓到老行为：把补丁还原成 anyio 原版，再跑同一组断言。

这不是产品代码，是「证明测试有效」的一次性实验：
修复前事件循环会自己 `Thread.start()`，被慢启动拖住 → 请求整体卡 3 秒。
"""
from __future__ import annotations

import functools
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

TMP = Path(os.environ.get("TMP", r"C:\Users\Administrator\AppData\Local\Temp")) / "wrk-old-behavior"
os.environ["WRK_DATA_DIR"] = str(TMP)
os.environ["WRK_DB_PATH"] = str(TMP / "old.sqlite3")
os.environ["WRK_SECRET_DIR"] = str(TMP / "secrets")
os.environ["WRK_WORKER_ENABLED"] = "0"
os.environ["WRK_WARMUP"] = "0"
os.environ["WRK_ADMIN_PASSWORD"] = "repro-pass-123456"
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

import anyio.to_thread  # noqa: E402

from app.services import threadpool  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

with TestClient(app) as client:
    token = client.post("/api/auth/login",
                        json={"username": "admin", "password": "repro-pass-123456"}).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    client.get("/api/kbs", headers=headers)

    # ---- 还原成「修复前」：常驻池下线，_dispatch 退回 anyio 懒加载池
    threadpool._pool = None       # noqa: SLF001

    executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="probe")
    gate = threading.Event()
    waiters = [executor.submit(gate.wait, 5) for _ in range(4)]
    gate.set()
    [f.result(timeout=5) for f in waiters]

    original_start = threading.Thread.start
    created: list[str] = []

    def slow_start(self):                     # 模拟 loader lock 被占
        created.append(self.name)
        time.sleep(3)
        return original_start(self)

    def one():
        t0 = time.monotonic()
        r = client.get("/api/kbs", headers=headers)
        return r.status_code, (time.monotonic() - t0) * 1000

    threading.Thread.start = slow_start
    try:
        results = [f.result(timeout=60) for f in [executor.submit(one) for _ in range(4)]]
    finally:
        threading.Thread.start = original_start
        executor.shutdown(wait=True)

    print("修复前行为：新建线程 =", created)
    for status, ms in results:
        print(f"  HTTP {status}  耗时 {ms:.0f}ms")
