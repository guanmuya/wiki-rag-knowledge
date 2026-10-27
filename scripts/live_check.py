"""对运行中的服务做一次真实端到端验收（不依赖 TestClient）。

用法：python scripts/live_check.py [--base http://127.0.0.1:8000]
密码取环境变量 WRK_ADMIN_PASSWORD，默认从 data/secrets 无、需显式传入。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import httpx

BASE = "http://127.0.0.1:8000"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default=os.getenv("WRK_ADMIN_PASSWORD", ""))
    args = ap.parse_args()
    if not args.password:
        print("需要 --password 或 WRK_ADMIN_PASSWORD")
        return 2

    c = httpx.Client(base_url=args.base, timeout=120.0, trust_env=False)
    r = c.post("/api/auth/login", json={"username": args.user, "password": args.password})
    r.raise_for_status()
    c.headers["Authorization"] = f"Bearer {r.json()['token']}"

    kbs = c.get("/api/kbs").json()["items"]
    print(f"知识库 {len(kbs)} 个：")
    for kb in kbs:
        print(f"  #{kb['id']} {kb['name']} [{kb['mode_label']}] {kb['status_label']} "
              f"文档 {kb['stats']['doc_count']} / 分块 {kb['stats']['chunk_count']} / "
              f"Wiki {kb['stats']['wiki_page_count']}")

    wiki_kbs = [k for k in kbs if k["mode"] == "wiki" and k["wiki_ready"]]
    basic_kbs = [k for k in kbs if k["mode"] == "basic" and k["stats"]["chunk_count"]]
    if not (wiki_kbs and basic_kbs):
        print("缺少可对比的库（需要一个 Wiki 就绪库 + 一个分块库）")
        return 1

    chosen = [wiki_kbs[0]["id"], basic_kbs[0]["id"]]
    print(f"\n混合检索库：{[k['name'] for k in kbs if k['id'] in chosen]}")

    questions = [
        "一线城市出差住宿上限是多少？",
        "P0 工单首次响应时限是多久？",
        "年假可以顺延到什么时候？",
        "标准版服务可用性承诺是多少？",
    ]
    conv = c.post("/api/conversations", json={"title": "验收会话", "kb_ids": chosen}).json()
    cid = conv["id"]
    for q in questions:
        r = c.post(f"/api/conversations/{cid}/ask", json={"question": q})
        if r.status_code != 200:
            print(f"  提问失败：{q} → {r.status_code} {r.text[:200]}")
            continue
        d = r.json()
        routes = " | ".join(f"{x['kb_name']}:{x['used']}{'(回退)' if x['fallback'] else ''}"
                            for x in d["route"]["kb_routes"])
        top = d["evidence"][0] if d["evidence"] else None
        src = ""
        if top:
            src = (f"{top['source_type']} / {top.get('doc_name') or top.get('page_title')}"
                   f" / v{top.get('version_no')} / {top.get('locator') or top.get('heading')}")
        print(f"\nQ: {q}")
        print(f"   路由: {routes}")
        print(f"   证据 {len(d['evidence'])} 条，首条: {src}")
        print(f"   引用: {d['citations']}  模式: {d['usage']['mode']}  "
              f"调用 {d['usage']['calls']} 次 / {d['usage']['latency_ms']}ms")
        print(f"   A: {d['message']['content'][:160].replace(chr(10), ' ')}…")

    print("\n=== 检索预览（不生成回答，用于实验调参）===")
    prev = c.post("/api/search/preview", json={"kb_ids": chosen, "query": "报销审批流程"}).json()
    for ev in prev["evidence"][:5]:
        print(f"  [{ev['rank']}] {ev['kb_name']}/{ev['source_type']} "
              f"score={ev['score']} {ev.get('doc_name') or ev.get('page_title')}")
    print("  stats:", prev["stats"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
