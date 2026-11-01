"""复现「任务处理中整个软件卡住」：直接驱动已安装的打包后端 exe。

用法：python scripts/diag/repro_freeze_exe.py [exe路径]
会在临时数据目录里建库、上传一份大 Markdown，然后以 100ms 间隔探测
GET /api/kbs（纯读接口），记录每次的耗时；同时看后端的 lock-watchdog 日志。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx

EXE = sys.argv[1] if len(sys.argv) > 1 else r"E:\Programs\wiki-rag-desktop\resources\backend\wrk-backend.exe"
PASSWORD = "repro-pass-123456"
PROBE_TIMEOUT = 3.0


def make_doc() -> bytes:
    parts = []
    for i in range(120):
        parts.append(
            f"## 第 {i} 节 客户支持流程\n\n"
            f"客户问题编号 {i} 的处理流程如下：首先由一线坐席记录问题，"
            f"其次在 30 分钟内给出初步答复，最后由二线工程师在 4 小时内给出解决方案。"
            f"如果问题涉及退款，需要走财务审批流程，审批人为区域经理。"
            f"本节的检索关键词：客户支持、退款、审批、响应时长、工单。\n"
        )
    return "\n".join(parts).encode("utf-8")


def main() -> int:
    data_dir = Path(tempfile.mkdtemp(prefix="wrk-repro-"))
    env = dict(os.environ)
    env.update({
        "WRK_ADMIN_PASSWORD": PASSWORD,
        "WRK_LOG_LEVEL": "info",
        "WRK_WORKER_POLL_SECONDS": "1",
        "WRK_WATCH_STDIN": "0",
        "PYTHONIOENCODING": "utf-8",
    })
    print(f"数据目录: {data_dir}")
    t0 = time.time()
    proc = subprocess.Popen(
        [EXE, "--data-dir", str(data_dir)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    port = None
    lines: list[str] = []

    def reader() -> None:
        nonlocal port
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.append(line.rstrip())
            if port is None and "WRK_READY" in line:
                try:
                    port = int(json.loads(line.split("WRK_READY", 1)[1])["port"])
                except Exception:
                    pass

    threading.Thread(target=reader, daemon=True).start()
    while port is None and time.time() - t0 < 90:
        if proc.poll() is not None:
            print("后端提前退出：")
            print("\n".join(lines[-40:]))
            return 1
        time.sleep(0.1)
    if port is None:
        print("等不到 WRK_READY")
        print("\n".join(lines[-40:]))
        proc.kill()
        return 1
    print(f"就绪: port={port} 启动耗时={time.time()-t0:.1f}s")

    base = f"http://127.0.0.1:{port}"
    probes: list[tuple[float, float, str]] = []   # (t, elapsed, status)
    stop = threading.Event()

    def probe_loop(client: httpx.Client) -> None:
        while not stop.is_set():
            started = time.time()
            status = ""
            try:
                r = client.get("/api/kbs", timeout=PROBE_TIMEOUT)
                status = f"http {r.status_code}"
            except Exception as exc:               # noqa: BLE001
                status = f"{type(exc).__name__}"
            probes.append((started - t0, (time.time() - started) * 1000, status))
            time.sleep(0.1)

    try:
        with httpx.Client(base_url=base, trust_env=False) as client:
            login = client.post("/api/auth/login", json={"username": "admin", "password": PASSWORD})
            login.raise_for_status()
            token = login.json()["token"]
            client.headers["Authorization"] = f"Bearer {token}"
            kb = client.post("/api/kbs", json={"name": "复现库", "description": "", "mode": "basic"})
            kb.raise_for_status()
            kb_id = kb.json()["id"]

            # 先做一次正常请求，确认基线
            t = time.time(); client.get("/api/kbs", timeout=PROBE_TIMEOUT); base_ms = (time.time() - t) * 1000
            print(f"基线 GET /api/kbs = {base_ms:.0f}ms")

            threading.Thread(target=probe_loop, args=(client,), daemon=True).start()

            doc = make_doc()
            up = client.post(
                f"/api/kbs/{kb_id}/documents",
                files=[("files", ("客户支持手册.md", doc, "text/markdown"))],
                timeout=60,
            )
            print("上传:", up.status_code, up.text[:200])
            up.raise_for_status()

            deadline = time.time() + 180
            last = None
            while time.time() < deadline:
                tasks = client.get("/api/tasks", timeout=PROBE_TIMEOUT).json()["items"]
                if tasks:
                    t0_ = tasks[0]
                    cur = (t0_["status"], t0_["stage"], t0_["progress"])
                    if cur != last:
                        print(f"  任务 {t0_['id']}: {cur}")
                        last = cur
                    if t0_["status"] in ("success", "failed"):
                        break
                time.sleep(0.5)
            stop.set()
            time.sleep(0.2)

            slow = [p for p in probes if p[1] > 1000]
            print(f"\n探测次数={len(probes)} 超过1s={len(slow)} 最大={max((p[1] for p in probes), default=0):.0f}ms")
            for p in slow[:20]:
                print(f"  t={p[0]:.1f}s 耗时={p[1]:.0f}ms {p[2]}")
            print("\n后端日志尾:")
            print("\n".join(lines[-25:]))
    finally:
        stop.set()
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, text=True)
        wd = data_dir / "logs" / "lock-watchdog.log"
        if wd.exists():
            print("\n===== lock-watchdog =====")
            print(wd.read_text(encoding="utf-8", errors="replace")[:4000])
        shutil.rmtree(data_dir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
