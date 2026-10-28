"""冒烟验证打包后的 wrk-backend.exe：能起、能自愈建表、/api/health 与 /api/setup/status 正常。

不依赖仓库 data/ 与 %APPDATA%：全部落在临时目录，跑完即删。
用法（仓库根目录）：.venv/Scripts/python.exe .tmp-verify/smoke_backend_exe.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
EXE = ROOT / "frontend" / "build" / "backend" / "wrk-backend.exe"

tmp = Path(tempfile.mkdtemp(prefix="wrk_exe_"))
env = {**os.environ}
env.update({
    "WRK_DATA_DIR": str(tmp),
    "WRK_DB_PATH": str(tmp / "db" / "app.sqlite3"),
    "WRK_SECRET_DIR": str(tmp / "secrets"),
    "WRK_PORT": "0",
    "WRK_WORKER_ENABLED": "0",
    # 与 electron/main.cjs 的 spawn 环境保持一致（否则冻结后的 stdout 走系统代码页）
    "PYTHONIOENCODING": "utf-8",
    "PYTHONUTF8": "1",
    "PYTHONUNBUFFERED": "1",
})
# 关键：这台机器全局有 ELECTRON_RUN_AS_NODE，对 exe 无影响，但保持一致
env.pop("ELECTRON_RUN_AS_NODE", None)
env.pop("WRK_ADMIN_PASSWORD", None)      # 空库 + 无环境变量 → 应进入待初始化

print(f"exe : {EXE}")
print(f"data: {tmp}\n")

# stdin 必须保持打开：desktop_entry 用 stdin EOF 判断父进程退出，
# 继承一个已关闭的 stdin 会让它在启动瞬间自杀（实测就是这样拿不到 WRK_READY）。
proc = subprocess.Popen([str(EXE)], stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        env=env, text=True, encoding="utf-8", errors="replace",
                        bufsize=1)
ready = None
deadline = time.time() + 90
lines: list[str] = []
try:
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                break
            continue
        lines.append(line.rstrip())
        if line.startswith("WRK_READY "):
            ready = json.loads(line[len("WRK_READY "):])
            break
    if ready is None:
        print("FAIL: 90s 内没有拿到 WRK_READY")
        print("\n".join(lines[-40:]))
        sys.exit(1)

    base = f"http://127.0.0.1:{ready['port']}"
    print(f"READY: {ready}\n")

    with httpx.Client(trust_env=False, timeout=10) as c:
        health = c.get(f"{base}/api/health").json()
        print("GET /api/health      ->", health)
        status = c.get(f"{base}/api/setup/status").json()
        print("GET /api/setup/status->", status)
        meta = c.get(f"{base}/api/meta").json()
        print("GET /api/meta        ->", {k: meta.get(k) for k in
                                        ("ok", "version", "initialized", "needs_setup", "org_name")})

    assert health.get("ok") is True, health
    assert status.get("initialized") is False, status
    assert meta.get("needs_setup") is True, meta
    print("\nPASS: 空数据目录启动 → 未初始化，前端应跳 #/setup")
finally:
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
    shutil.rmtree(tmp, ignore_errors=True)
