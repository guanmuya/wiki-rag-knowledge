"""验证扩充语料的检索命中：对 questions.json 抽查若干题，调 /api/search/preview。

判定标准：返回的证据文本中，是否出现该题全部 answer_keywords。
no_answer 题只记录检索是否返回结果，不做命中判定。

用法（仓库根目录，后端需已启动）：
    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/check_questions.py
    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/check_questions.py --limit 5
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:8000"
QUESTIONS = ROOT / "demo_data" / "questions.json"


def login(client: httpx.Client) -> str:
    pwd = "Admin@2026"
    r = client.post(f"{BASE}/api/auth/login", json={"username": "admin", "password": pwd})
    r.raise_for_status()
    return r.json()["token"]


def kb_ids(client: httpx.Client, headers: dict) -> dict[str, int]:
    r = client.get(f"{BASE}/api/kbs", headers=headers)
    r.raise_for_status()
    data = r.json()
    items = data if isinstance(data, list) else data.get("items", [])
    return {kb["name"]: kb["id"] for kb in items}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只抽查前 N 题，0 表示全量")
    ap.add_argument("--only", default="", help="只跑指定 id，逗号分隔")
    args = ap.parse_args()

    questions = json.loads(QUESTIONS.read_text(encoding="utf-8"))
    if args.only:
        want = {s.strip() for s in args.only.split(",") if s.strip()}
        questions = [q for q in questions if q["id"] in want]
    elif args.limit:
        questions = questions[: args.limit]

    # 后端走本机，显式关闭代理：系统代理会拦截 127.0.0.1 并返回 502
    with httpx.Client(trust_env=False, timeout=30.0) as client:
        token = login(client)
        headers = {"Authorization": f"Bearer {token}"}
        names = kb_ids(client, headers)
        print(f"知识库：{names}\n")

        hit = miss = skipped = 0
        for q in questions:
            ids = [names[n] for n in q["expected_kb"] if n in names]
            r = client.post(f"{BASE}/api/search/preview",
                            headers=headers, json={"kb_ids": ids, "query": q["question"]})
            if r.status_code != 200:
                print(f"[{q['id']}] HTTP {r.status_code}: {r.text[:200]}")
                miss += 1
                continue
            data = r.json()
            # 只比对证据正文，不比对标题/章节名：Wiki 证据带 page_title，
            # 若把标题算进命中，页面标题恰好含关键词时会误判为命中
            blob = "\n".join(e.get("text", "") for e in data.get("evidence", []))
            titles = " ".join(e.get("page_title") or "" for e in data.get("evidence", []))
            routes = ", ".join(f"{rt['kb_name']}:{rt['used']}" for rt in data.get("routes", []))
            kws = q["answer_keywords"]

            if q["type"] == "no_answer":
                skipped += 1
                print(f"[{q['id']}] {q['type']:9s} 证据 {len(data.get('evidence', []))} 条 "
                      f"({routes}) — 无答案题，不判命中")
                continue

            found = [k for k in kws if k in blob]
            ok = len(found) == len(kws)
            if ok:
                hit += 1
            else:
                miss += 1
            flag = "✓" if ok else "✗"
            print(f"[{q['id']}] {q['type']:9s} {flag} 命中 {len(found)}/{len(kws)} "
                  f"证据 {len(data.get('evidence', []))} 条 ({routes})")
            if not ok:
                missing = [k for k in kws if k not in blob]
                only_title = [k for k in missing if k in titles]
                print(f"      缺：{missing}")
                if only_title:
                    print(f"      注意：{only_title} 只出现在页面标题里，正文没有 → 判为未命中")
                print(f"      证据：{blob[:400]}")

        total = hit + miss
        print(f"\n可判题 {total} 题，命中 {hit} 题，未命中 {miss} 题"
              f"（另有 {skipped} 道 no_answer 题不参与判定）")
        return 0 if miss == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
