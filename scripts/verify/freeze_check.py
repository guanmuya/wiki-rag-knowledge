"""验收脚本：任务处理期间，整个应用必须保持响应（v1.0.10 卡死缺陷的回归）。

用法：
    python scripts/verify/freeze_check.py [exe路径]

做的事：
1. 用临时数据目录拉起后端（打包 exe 或任意可执行 sidecar）；
2. 建库 → 上传多份文档，让 worker 连续跑任务；
3. 任务期间并发打**读**接口（GET /api/kbs、/api/tasks）和**写**接口
   （POST /api/conversations），记录每次耗时；
4. 读 /api/diagnostics/process 断言：常驻线程池已启动、预热完成、
   请求由 wrk-http-N 线程执行；
5. 任何一次 > 1s 的请求都算失败（v1.0.9 会卡 155s）。

退出码：0 = 通过；1 = 有请求被卡住或诊断字段不对。
"""
from __future__ import annotations

import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx

EXE = sys.argv[1] if len(sys.argv) > 1 else str(
    Path(__file__).resolve().parents[2] / "frontend" / "build" / "backend" / "wrk-backend.exe")
PASSWORD = "repro-pass-123456"
SLOW_MS = 1000.0
PROBE_TIMEOUT = 5.0


def make_doc(idx: int) -> bytes:
    parts = []
    for i in range(160):
        parts.append(
            f"## 第 {i} 节 客户支持流程 {idx}\n\n"
            f"客户问题编号 {idx}-{i} 的处理流程：一线坐席记录问题，30 分钟内初步答复，"
            f"二线工程师 4 小时内给出解决方案；涉及退款需财务审批，审批人为区域经理。"
            f"关键词：客户支持、退款、审批、响应时长、工单、升级、回访。\n"
        )
    return "\n".join(parts).encode("utf-8")


def main() -> int:
    data_dir = Path(tempfile.mkdtemp(prefix="wrk-freeze-check-"))
    env = dict(os.environ)
    env.update({
        "WRK_ADMIN_PASSWORD": PASSWORD,
        "WRK_LOG_LEVEL": "warning",
        "WRK_WORKER_POLL_SECONDS": "1",
        "WRK_WATCH_STDIN": "0",
        "PYTHONIOENCODING": "utf-8",
    })
    print(f"[check] exe={EXE}")
    print(f"[check] 数据目录={data_dir}")
    t0 = time.time()
    proc = subprocess.Popen(
        [EXE, "--data-dir", str(data_dir)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
        text=True, encoding="utf-8", errors="replace", bufsize=1)
    port: int | None = None
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
            print("[check] 后端提前退出：\n" + "\n".join(lines[-30:]))
            return 1
        time.sleep(0.1)
    if port is None:
        print("[check] 等不到 WRK_READY：\n" + "\n".join(lines[-30:]))
        proc.kill()
        return 1
    print(f"[check] 就绪 port={port} 启动 {time.time() - t0:.1f}s")

    samples: list[tuple[float, float, str]] = []     # (相对秒, 耗时ms, 结果)
    stop = threading.Event()

    def probe(client: httpx.Client, path: str, method: str = "GET", body=None) -> None:
        while not stop.is_set():
            started = time.time()
            try:
                if method == "POST":
                    r = client.post(path, json=body, timeout=PROBE_TIMEOUT)
                else:
                    r = client.get(path, timeout=PROBE_TIMEOUT)
                outcome = f"http {r.status_code}"
            except Exception as exc:                  # noqa: BLE001
                outcome = type(exc).__name__
            samples.append((started - t0, (time.time() - started) * 1000, f"{method} {path} → {outcome}"))
            time.sleep(0.12)

    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
            login = client.post("/api/auth/login", json={"username": "admin", "password": PASSWORD})
            login.raise_for_status()
            client.headers["Authorization"] = f"Bearer {login.json()['token']}"
            kb = client.post("/api/kbs", json={"name": "验收库", "description": "", "mode": "basic"})
            kb.raise_for_status()
            kb_id = kb.json()["id"]

            probes = [
                threading.Thread(target=probe, args=(client, "/api/kbs"), daemon=True),
                threading.Thread(target=probe, args=(client, "/api/kbs"), daemon=True),
                threading.Thread(target=probe, args=(client, "/api/tasks"), daemon=True),
                threading.Thread(target=probe, args=(client, "/api/meta"), daemon=True),
                threading.Thread(target=probe, args=(client, "/api/conversations", "POST",
                                                    {"title": "验收会话", "kb_ids": []}), daemon=True),
            ]
            for t in probes:
                t.start()

            for idx in range(3):
                up = client.post(f"/api/kbs/{kb_id}/documents",
                                 files=[("files", (f"验收{idx}.md", make_doc(idx), "text/markdown"))],
                                 timeout=120)
                up.raise_for_status()
            print("[check] 已上传 3 份文档，等任务跑完…")

            deadline = time.time() + 240
            last = None
            while time.time() < deadline:
                tasks = client.get("/api/tasks", timeout=PROBE_TIMEOUT).json()["items"]
                if tasks:
                    cur = (tasks[0]["status"], tasks[0]["stage"], tasks[0]["progress"])
                    if cur != last:
                        print(f"[check]   任务 {tasks[0]['id']}: {cur}")
                        last = cur
                if tasks and all(t["status"] in ("success", "failed") for t in tasks):
                    break
                time.sleep(0.5)

            stop.set()
            time.sleep(0.3)

            diag = client.get("/api/diagnostics/process", timeout=PROBE_TIMEOUT).json()
            print(f"[check] served_by={diag.get('served_by')} "
                  f"threadpool={diag.get('threadpool', {}).get('alive')} 条常驻线程 "
                  f"warmup_done={diag.get('warmup', {}).get('done')} "
                  f"warmup_ms={diag.get('warmup', {}).get('total_ms')}")
            print(f"[check] 预热明细={json.dumps(diag.get('warmup', {}).get('modules', {}), ensure_ascii=False)}")

            problems: list[str] = []
            if not str(diag.get("served_by", "")).startswith("wrk-http-"):
                problems.append(f"请求不是常驻线程池执行的：{diag.get('served_by')}")
            if not diag.get("threadpool", {}).get("started"):
                problems.append("常驻线程池未启动")
            if not diag.get("warmup", {}).get("done"):
                problems.append("重模块预热未完成")

            slow = [s for s in samples if s[1] >= SLOW_MS]
            errors = [s for s in samples if "http 2" not in s[2]]
            durations = [s[1] for s in samples]
            print(f"[check] 探测 {len(samples)} 次，最大 {max(durations, default=0):.0f}ms，"
                  f"中位 {statistics.median(durations) if durations else 0:.0f}ms，"
                  f">= {SLOW_MS:.0f}ms 的 {len(slow)} 次，非 2xx {len(errors)} 次")
            for s in slow[:10]:
                print(f"[check]   慢请求 t={s[0]:.1f}s {s[1]:.0f}ms {s[2]}")
            for s in errors[:10]:
                print(f"[check]   失败请求 t={s[0]:.1f}s {s[1]:.0f}ms {s[2]}")
            if slow:
                problems.append(f"{len(slow)} 次请求超过 {SLOW_MS:.0f}ms（任务期间被卡住）")
            if errors:
                problems.append(f"{len(errors)} 次请求失败")

            print("\n[check] 结论：" + ("通过 ✅" if not problems else "失败 ❌"))
            for p in problems:
                print(f"[check]   - {p}")
            return 0 if not problems else 1
    finally:
        stop.set()
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, text=True)
        shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
