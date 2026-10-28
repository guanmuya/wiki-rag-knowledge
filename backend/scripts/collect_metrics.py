"""论文实验数据采集：对一组知识库跑一组问题，把「成本 + 效果」逐条落成 CSV。

用途：做「分块 vs Wiki」对照实验时，需要同时拿到两列数字——
- 效果：证据数、首位覆盖率、回答长度、引用数、是否证据不足
- 代价：模型调用次数、token、耗时

只跑效果不比代价的对比是不成立的：Wiki 整理前期投入明显更高，
论文必须能报出「多花了多少调用、多少 token、多少时间」。

用法（仓库根目录，后端需已启动）：

    # 1) 准备问题文件 questions.json
    #    {"kb_ids": [1, 2], "questions": ["一线城市住宿上限是多少？", "..."]}
    #    也支持纯数组：["问题1", "问题2"]（此时 kb_ids 用 --kb-ids 传）

    # 2) 采集
    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe backend/scripts/collect_metrics.py \
        --base-url http://127.0.0.1:8000 \
        --username admin --password '你的密码' \
        --input questions.json \
        --out results.csv

    # 只跑检索（不生成回答，更快、只测检索质量与检索成本）
    ... --mode search

    # 用已有 token（避免脚本里出现明文密码）
    ... --token "$WRK_TOKEN"

    # 给每条结果打上实验标签，便于多轮对比合并
    ... --label wiki-chunk700

    # 每个问题重复 3 次，用于观察波动（CSV 里 repeat 列区分）
    ... --repeat 3

输出列（一行 = 一次「问题 × 知识库组 × 重复」）：

    run_label, repeat, kb_ids, kb_modes, question, mode,
    evidence_count, top1_coverage, mean_coverage, source_types,
    answer_chars, citation_count, invalid_citations, insufficient,
    latency_ms, calls, chat_calls, embed_calls, live_calls, offline_calls,
    prompt_tokens, completion_tokens, embed_tokens, total_tokens,
    stats_mode, embed_mode, error

设计说明（为什么这么写）：
1. **不复用 app 的数据库/服务**，只走 HTTP。实验要能对「用户实际看到的数字」负责，
   直接调内部函数会绕过路由层的 usage 组装——而缺陷恰恰可能藏在那一层。
2. **失败不中断**：单个问题 4xx/5xx/超时都写一行 error，继续跑下一个。
   跑 200 个问题中途炸掉一个就全废，实验成本太高。
3. **CSV 用 utf-8-sig**：Excel 直接双击打开不乱码，省掉一次手工转码。
4. **token 与调用次数分开列**：论文表格里通常只放 total_tokens 和 calls，
   但审稿人会问「其中多少是向量调用」，所以明细必须留在 CSV 里。
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import httpx

# ---------------------------------------------------------------- CSV 列定义
FIELDS = [
    "run_label", "repeat", "kb_ids", "kb_modes", "question", "mode",
    # ---- 效果
    "evidence_count", "top1_coverage", "mean_coverage", "source_types",
    "answer_chars", "citation_count", "invalid_citations", "insufficient",
    # ---- 代价
    "latency_ms", "model_latency_ms",
    "calls", "chat_calls", "embed_calls", "live_calls", "offline_calls",
    "prompt_tokens", "completion_tokens", "embed_tokens", "total_tokens",
    "stats_mode", "embed_mode",
    "error",
]


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- 输入解析
def load_input(path: Path, cli_kb_ids: list[int] | None) -> tuple[list[int], list[str]]:
    """读问题文件，返回 (kb_ids, questions)。

    两种形态都支持，因为实验过程中经常先写纯问题列表，后来才定库：
    - {"kb_ids": [...], "questions": [...]}
    - ["问题1", "问题2"]
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"问题文件不存在：{path}")
    except ValueError as exc:
        raise SystemExit(f"问题文件不是合法 JSON：{exc}")

    if isinstance(raw, list):
        kb_ids, questions = [], raw
    elif isinstance(raw, dict):
        kb_ids = [int(x) for x in (raw.get("kb_ids") or [])]
        questions = raw.get("questions") or []
        # 兼容 {"items":[{"question": "...", "kb_ids":[...]}]}：分库实验时常用
        if not questions and isinstance(raw.get("items"), list):
            questions = [it.get("question") if isinstance(it, dict) else it
                         for it in raw["items"]]
    else:
        raise SystemExit("问题文件格式不支持：应为对象或数组")

    questions = [str(q).strip() for q in questions if str(q).strip()]
    if not questions:
        raise SystemExit("问题文件里没有问题")
    if cli_kb_ids:
        kb_ids = cli_kb_ids
    if not kb_ids:
        raise SystemExit("没有指定知识库：文件里没有 kb_ids，也没传 --kb-ids")
    return kb_ids, questions


# ---------------------------------------------------------------- HTTP 客户端
class Client:
    def __init__(self, base_url: str, token: str, timeout: float) -> None:
        self.base = base_url.rstrip("/")
        self.http = httpx.Client(
            base_url=self.base,
            timeout=httpx.Timeout(connect=10.0, read=timeout, write=30.0, pool=10.0),
            headers={"Authorization": f"Bearer {token}"},
            trust_env=False,       # 与后端一致：不继承系统代理，避免请求被劫持到代理
        )

    def close(self) -> None:
        self.http.close()

    def get(self, path: str, **kw) -> httpx.Response:
        return self.http.get(path, **kw)

    def post(self, path: str, **kw) -> httpx.Response:
        return self.http.post(path, **kw)

    def login(self, username: str, password: str) -> str:
        r = self.http.post("/api/auth/login",
                           json={"username": username, "password": password})
        if r.status_code != 200:
            raise SystemExit(f"登录失败（{r.status_code}）：{r.text[:300]}")
        return r.json()["token"]


# ---------------------------------------------------------------- 单条采集
def _usage_of(data: dict) -> dict:
    """从响应里取成本。

    ask 与 search/preview 都把成本放在顶层 usage；
    老版本后端没有这个字段时退回 route.usage，再退回空 dict ——
    采集脚本要能跑在「后端刚升级、前端还没跟上」的中间状态上。
    """
    usage = data.get("usage")
    if isinstance(usage, dict) and usage:
        return usage
    route = data.get("route") or {}
    if isinstance(route, dict) and isinstance(route.get("usage"), dict):
        return route["usage"]
    return {}


def _evidence_rows(evidence: list[dict]) -> tuple[int, float, float, str]:
    """(证据数, 首位覆盖率, 平均覆盖率, 来源类型串)。"""
    if not evidence:
        return 0, 0.0, 0.0, ""
    coverages = [float(e.get("coverage") or 0.0) for e in evidence]
    types = sorted({str(e.get("source_type") or "") for e in evidence})
    return len(evidence), coverages[0], statistics.fmean(coverages), "+".join(t for t in types if t)


def collect_one(client: Client, *, kb_ids: list[int], kb_modes: str, question: str,
                mode: str, conv_id: int | None, repeat: int, label: str,
                top_k: int | None) -> dict:
    """跑一个问题，返回一行 CSV 数据。任何异常都转成 error 字段，不抛出。"""
    row: dict[str, Any] = {f: "" for f in FIELDS}
    row.update({
        "run_label": label, "repeat": repeat,
        "kb_ids": "|".join(str(k) for k in kb_ids), "kb_modes": kb_modes,
        "question": question, "mode": mode,
        "evidence_count": 0, "top1_coverage": 0.0, "mean_coverage": 0.0,
        "source_types": "", "answer_chars": 0, "citation_count": 0,
        "invalid_citations": 0, "insufficient": "",
        "latency_ms": 0, "model_latency_ms": 0,
        "calls": 0, "chat_calls": 0, "embed_calls": 0,
        "live_calls": 0, "offline_calls": 0,
        "prompt_tokens": 0, "completion_tokens": 0, "embed_tokens": 0, "total_tokens": 0,
        "stats_mode": "", "embed_mode": "",
    })

    started = time.time()
    try:
        if mode == "search":
            r = client.post("/api/search/preview",
                            json={"kb_ids": kb_ids, "query": question})
        else:
            if conv_id is None:
                raise RuntimeError("ask 模式需要会话 id")
            body: dict[str, Any] = {"question": question, "kb_ids": kb_ids}
            if top_k:
                body["top_k"] = top_k
            r = client.post(f"/api/conversations/{conv_id}/ask", json=body)
    except (httpx.HTTPError, RuntimeError) as exc:
        # 任何单条失败都只写一行 error，绝不让整轮实验中断——
        # 跑 200 个问题中途崩掉一个就全废，重跑的时间成本远高于一行空数据。
        row["error"] = f"{exc.__class__.__name__}: {exc}"
        row["latency_ms"] = int((time.time() - started) * 1000)
        return row

    if r.status_code != 200:
        row["error"] = f"HTTP {r.status_code}: {r.text[:300]}"
        row["latency_ms"] = int((time.time() - started) * 1000)
        return row

    try:
        data = r.json()
    except ValueError:
        row["error"] = f"响应不是 JSON：{r.text[:200]}"
        row["latency_ms"] = int((time.time() - started) * 1000)
        return row

    evidence = data.get("evidence") or []
    count, top1, mean_cov, types = _evidence_rows(evidence)
    row["evidence_count"] = count
    row["top1_coverage"] = round(top1, 4)
    row["mean_coverage"] = round(mean_cov, 4)
    row["source_types"] = types

    usage = _usage_of(data)
    row["calls"] = int(usage.get("calls") or 0)
    row["chat_calls"] = int(usage.get("chat_calls") or 0)
    row["embed_calls"] = int(usage.get("embed_calls") or 0)
    row["live_calls"] = int(usage.get("live_calls") or 0)
    row["offline_calls"] = int(usage.get("offline_calls") or 0)
    row["prompt_tokens"] = int(usage.get("prompt_tokens") or 0)
    row["completion_tokens"] = int(usage.get("completion_tokens") or 0)
    row["embed_tokens"] = int(usage.get("embed_tokens") or 0)
    row["total_tokens"] = int(usage.get("total_tokens")
                              or (row["prompt_tokens"] + row["completion_tokens"]
                                  + row["embed_tokens"]))
    # stats_mode 描述的是**生成通道**（后端契约如此，前端按它显示「未调用生成模型」）。
    # 但只检索的行根本没有生成调用，后端返回的默认 "live" 会造成
    # 「这一行用了真实模型」的误读，所以这里改写为更准确的标签：
    # - search 模式：no-chat（本来就没有生成）
    # - ask 模式但生成调用为 0：offline-extractive（离线抽取式，走了降级但没花生成的钱）
    # - 其余：沿用后端 mode（live / offline）
    backend_mode = str(usage.get("mode") or "")
    if mode == "search":
        row["stats_mode"] = "no-chat"
    elif row["chat_calls"] == 0:
        row["stats_mode"] = "offline-extractive"
    else:
        row["stats_mode"] = backend_mode
    row["embed_mode"] = str(usage.get("embed_mode") or "")

    if mode == "ask":
        content = str(data.get("content") or "")
        row["answer_chars"] = len(content)
        citations = data.get("citations") or {}
        row["citation_count"] = len(citations.get("cited") or [])
        row["invalid_citations"] = len(citations.get("invalid") or [])
        row["insufficient"] = bool(data.get("insufficient"))

    # 端到端耗时用实测墙钟时间，而不是 usage.latency_ms：
    # 后者只含模型调用时间，不含检索打分、数据库查询、序列化。
    # 论文报「单次问答耗时」时用的是前者，两者都要留着对比。
    row["latency_ms"] = int((time.time() - started) * 1000)
    row["model_latency_ms"] = int(usage.get("latency_ms") or 0)
    return row


# ---------------------------------------------------------------- 主流程
def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="采集「分块 vs Wiki」对比实验的效果与成本数据",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--base-url", default="http://127.0.0.1:8000", help="后端地址")
    p.add_argument("--username", default="", help="登录用户名（与 --password 一起用）")
    p.add_argument("--password", default="", help="登录密码")
    p.add_argument("--token", default="", help="已有 Bearer token（优先于用户名密码）")
    p.add_argument("--input", required=True, help="问题 JSON 文件")
    p.add_argument("--out", default="results.csv", help="输出 CSV 路径")
    p.add_argument("--kb-ids", default="", help="知识库 id，逗号分隔（覆盖文件里的）")
    p.add_argument("--mode", choices=["ask", "search", "both"], default="both",
                   help="ask=完整问答；search=只检索；both=各跑一次（默认）")
    p.add_argument("--top-k", type=int, default=None, help="覆盖检索 top_k")
    p.add_argument("--repeat", type=int, default=1, help="每个问题重复次数")
    p.add_argument("--label", default="", help="本轮实验标签，写进 run_label 列")
    p.add_argument("--conv-title", default="", help="自动创建会话时的标题")
    p.add_argument("--timeout", type=float, default=180.0, help="单请求读超时（秒）")
    p.add_argument("--sleep", type=float, default=0.0, help="每次请求之间的间隔（秒）")
    p.add_argument("--append", action="store_true", help="追加到已有 CSV（不写表头）")
    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)

    kb_ids_cli = [int(x) for x in args.kb_ids.replace(" ", "").split(",") if x]
    kb_ids, questions = load_input(Path(args.input), kb_ids_cli)

    token = args.token.strip()
    client = Client(args.base_url, token or "placeholder", args.timeout)
    if not token:
        if not args.username:
            client.close()
            raise SystemExit("需要 --token 或 --username/--password")
        token = client.login(args.username, args.password)
        client.http.headers["Authorization"] = f"Bearer {token}"

    # 记录库的模式，CSV 里直接能看出这一行是分块库还是 Wiki 库
    modes: list[str] = []
    for kid in kb_ids:
        r = client.get(f"/api/kbs/{kid}")
        if r.status_code != 200:
            client.close()
            raise SystemExit(f"读取知识库 {kid} 失败（{r.status_code}）：{r.text[:200]}")
        kb = r.json()
        modes.append(f"{kid}:{kb.get('mode', '?')}")
    kb_modes = "|".join(modes)
    log(f"知识库：{kb_modes}")
    log(f"问题数：{len(questions)}，模式：{args.mode}，重复：{args.repeat}")

    # ask 模式需要一个会话；search 模式不需要，别多建垃圾数据
    conv_id: int | None = None
    if args.mode in ("ask", "both"):
        r = client.post("/api/conversations", json={
            "title": args.conv_title or f"实验采集 {time.strftime('%Y-%m-%d %H:%M')}",
            "kb_ids": kb_ids,
        })
        if r.status_code not in (200, 201):
            client.close()
            raise SystemExit(f"创建会话失败（{r.status_code}）：{r.text[:300]}")
        conv_id = r.json()["id"]
        log(f"会话 id：{conv_id}")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not (args.append and out_path.exists())

    modes_to_run = ["search", "ask"] if args.mode == "both" else [args.mode]
    # 追加模式下若已有 model_latency_ms 列，保持列一致（旧文件少这一列也不报错）
    fields = list(FIELDS)
    if out_path.exists() and args.append:
        with out_path.open("r", encoding="utf-8-sig", newline="") as fh:
            header = next(csv.reader(fh), [])
        if header:
            fields = header

    rows_written = 0
    errors = 0
    with out_path.open("a" if args.append else "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        for rep in range(1, max(args.repeat, 1) + 1):
            for idx, question in enumerate(questions, start=1):
                for run_mode in modes_to_run:
                    row = collect_one(
                        client, kb_ids=kb_ids, kb_modes=kb_modes, question=question,
                        mode=run_mode, conv_id=conv_id, repeat=rep,
                        label=args.label, top_k=args.top_k,
                    )
                    writer.writerow(row)
                    fh.flush()          # 边跑边落盘：中途 Ctrl-C 也不丢已采的数据
                    rows_written += 1
                    if row["error"]:
                        errors += 1
                        log(f"  [×] {run_mode} #{idx}: {row['error'][:120]}")
                    else:
                        log(f"  [✓] {run_mode} #{idx} rep{rep} "
                            f"证据{row['evidence_count']} 覆盖{row['top1_coverage']:.2f} "
                            f"调用{row['calls']} token{row['total_tokens']} "
                            f"{row['latency_ms']}ms")
                    if args.sleep > 0:
                        time.sleep(args.sleep)

    client.close()
    log(f"\n完成：{rows_written} 行 → {out_path}"
        f"（失败 {errors} 条，已写入 error 列）")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
