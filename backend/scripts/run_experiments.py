"""五个对照实验的离线采集器（P0-1 / 配置 A 离线）。

用途
----
按 ``docs/EXPERIMENT.md`` §3 的 E1–E5 与 ``docs/THESIS.md`` §4 的指标定义，
在**无真实模型配置**的情况下，用真实代码产出可复现的原始结果数据：

* E1 原文分块 vs Wiki 增强、E2 单库 vs 多库、E3 加权 RRF 消融、E5 overlap 消融
  均通过真实接口 ``POST /api/search/preview`` 采集（in-process TestClient，
  与独立起 uvicorn 走同一套路由/依赖/usage 组装）。
* E4 表格三策略按实验方案要求用探针脚本复现（调用与 ``search_chunks`` 相同的
  ``keyword_rank`` / ``vector_rank`` / ``rrf_fuse`` / ``split_table`` 等函数）。

诚实性约束
----------
本机无在线模型配置与凭据，因此本轮只能跑 **配置 A 离线**（无 llm、无 embedding，
向量为 ``local-hash-embedding:256``、生成侧为抽取式）。按 ``docs/EXPERIMENT.md``
§7 与 ``docs/THESIS.md`` §6.7，离线结果**不能作为论文主结果**，只作为「链路可运行、
数字可复现」的证据。凡依赖真实模型或人工标注的指标（正确性三档、证据支持率、
拒答正确率、Wilcoxon/McNemar 的 p 值、配置 B 的全部指标）一律标注「待实验」，
不填推测值（``docs/EXPERIMENT.md:3``）。

用法（仓库根目录）::

    PYTHONIOENCODING=utf-8 python backend/scripts/run_experiments.py

产出全部写入 ``docs/results/``，不写 ``data/``；临时库放在系统临时目录，
脚本结束即弃。CSV 前 27 列与 ``backend/scripts/collect_metrics.py`` 的 ``FIELDS``
同名同序，便于用该脚本在真实起服务时复核。
"""
from __future__ import annotations

import csv
import json
import logging
import math
import os
import platform
import re
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
RESULTS = ROOT / "docs" / "results"

# 只用于本次临时实验库的本地口令；不是任何真实凭据。
ADMIN_PASSWORD = "wrk-exp-offline-local-only"

# 实验五的 overlap 取值：EXPERIMENT.md §3 实验五给 {0,60,120} 并允许探上界 240，
# THESIS.md §4.1 写 {0,120,240}。两组取并集，四个值全部实测。
OVERLAPS = [0, 60, 120, 240]
TABLE_FILES = [
    "员工薪酬明细_120行.docx",
    "员工薪酬明细_200行.xlsx",
    "员工薪酬明细_700行.docx",
]
DEMO_FILES = [
    "员工手册.md", "培训与职业发展制度.md", "薪酬与福利说明.md", "考勤与假期补充规定.md",
    "客户支持与运维手册.md", "故障案例库.md", "客户沟通规范.md",
    "产品与销售知识.md", "产品功能详解.md", "招投标与合同要点.md",
]

_FULL2HALF = str.maketrans("０１２３４５６７８９％：／", "0123456789%:/")


def norm(s: str) -> str:
    return re.sub(r"\s+", "", str(s).translate(_FULL2HALF)).lower()


def keyword_parts(kw: Any) -> list[str]:
    return [norm(p) for p in str(kw).split("|") if norm(p)]


def derive_expected(chunks: list[tuple[int, str, str]], questions: list[dict]) -> dict:
    """按 answer_keywords 自动标注「预期命中单元」，规则逐题可追溯。

    规则：把每条关键词按 ``|`` 拆成片段、去空白与全角差异；某分块的
    ``text + heading`` 若包含**某一条关键词的全部片段**，即为该题的预期单元。
    无答案题（answer_keywords 为空）没有预期单元，不计入 Hit@k 分母。
    """
    out: dict[str, dict] = {}
    for q in questions:
        parts = [p for p in (keyword_parts(k) for k in q.get("answer_keywords") or []) if p]
        ids: list[int] = []
        for cid, text, heading in chunks:
            hay = norm(text) + " " + norm(heading)
            if any(all(p in hay for p in one) for one in parts):
                ids.append(cid)
        out[q["id"]] = {
            "question": q["question"],
            "type": q["type"],
            "expected_kb": q.get("expected_kb") or [],
            "answer_keywords": q.get("answer_keywords") or [],
            "expected_chunk_ids": ids,
            "scored": bool(parts) and bool(ids),
            "match_rule": "answer_keywords 按 | 拆片段，全部片段命中同一分块",
        }
    return out


def hardware() -> dict:
    """实测本机硬件（补 EXPERIMENT.md:19 的「待实测」）。"""
    info: dict[str, Any] = {
        "os": platform.platform(),
        "python": platform.python_version(),
        "machine": platform.machine(),
        "cpu_logical_count": os.cpu_count(),
    }
    ps = (
        "$obj = Get-CimInstance Win32_Processor | Select-Object -First 1; "
        "$cpu = $obj.Name; "
        "if (-not $cpu -or $cpu -eq 'Not Available') { "
        "$cpu = (Get-ItemProperty 'HKLM:\\HARDWARE\\DESCRIPTION\\System\\CentralProcessor\\0' "
        "-ErrorAction SilentlyContinue).ProcessorNameString }; "
        "$ram = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB, 2); "
        "$disk = (Get-CimInstance Win32_DiskDrive | Select-Object -First 1).Model; "
        "[pscustomobject]@{cpu_model=$cpu; cpu_manufacturer=$obj.Manufacturer; "
        "ram_gb=$ram; disk_model=$disk} | ConvertTo-Json -Compress"
    )
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=90)
        info.update(json.loads(r.stdout.strip()))
        if not info.get("cpu_model") or info["cpu_model"] == "Not Available":
            info["cpu_model_note"] = (
                "本机为虚拟机：Win32_Processor.Name 与注册表 ProcessorNameString 均报告 "
                "Not Available，无法取得具体型号；仅记录 Manufacturer 与逻辑核数")
    except Exception as exc:                                  # 取不到就如实留白，不填推测值
        info["hardware_probe_error"] = f"{type(exc).__name__}: {exc}"
    return info


def git_head() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(ROOT),
                              capture_output=True, text=True, timeout=30).stdout.strip()
    except Exception:
        return ""


def wiki_covariates(db, models, wiki) -> dict:
    """从任务结果的 notes + 只读探针解析 §2.3 的四个协变量。

    notes 为空时按 EXPERIMENT.md §2.3 可推断「单片归纳、未截断、未跳过」；
    但确切片数与摘录长度由 ``wiki._plan_shards()`` 只读探针实测，不推测。
    """
    import re as _re
    from sqlalchemy import select as sa_select
    notes: list[str] = []
    for t in db.execute(sa_select(models.Task)).scalars():
        res = t.result
        if isinstance(res, str):
            try:
                res = json.loads(res)
            except (ValueError, TypeError):
                continue
        if not isinstance(res, dict):
            continue
        w = res.get("wiki")
        if isinstance(w, dict) and isinstance(w.get("notes"), list):
            notes += [str(x) for x in w["notes"]]
        if isinstance(res.get("notes"), list):
            notes += [str(x) for x in res["notes"]]
    joined = " ".join(notes)

    def grab(pattern: str) -> int | None:
        m = _re.search(pattern, joined)
        return int(m.group(1)) if m else None

    # 只读探针：不写库、不调模型，直接得到每库分片数与摘录长度。
    shards_by_kb: list[dict] = []
    for kb in db.execute(sa_select(models.KnowledgeBase)).scalars():
        if kb.mode != models.KbMode.wiki.value:
            continue
        try:
            chunks = wiki._chunks_for_kb(db, kb.id)
            shards, excerpt = wiki._plan_shards(chunks)
            shards_by_kb.append({"kb_id": kb.id, "kb_name": kb.name,
                                 "chunks": len(chunks), "shards": len(shards),
                                 "excerpt": excerpt})
        except Exception as exc:
            shards_by_kb.append({"kb_id": kb.id, "kb_name": kb.name,
                                 "error": f"{type(exc).__name__}: {exc}"})
    measured_shards = max((x["shards"] for x in shards_by_kb if "shards" in x), default=None)
    measured_excerpt = max((x["excerpt"] for x in shards_by_kb if "excerpt" in x), default=None)

    return {
        "wiki_total_chunks": grab(r"全库共 (\d+) 个分块"),
        "wiki_shards": grab(r"按字符预算分 (\d+) 片归纳后合并") or measured_shards,
        "wiki_excerpt": grab(r"单片摘录 (\d+) 字") or measured_excerpt,
        "wiki_topics_merged": grab(r"合并后共 (\d+) 个主题"),
        "wiki_pages_capped": grab(r"按证据数量保留前 (\d+) 个"),
        "wiki_notes_present": bool(notes),
        "wiki_notes_raw": notes,
        "wiki_shards_by_kb": shards_by_kb,
    }


def run_seam_probe(work: Path, chunking, parser) -> tuple[list[dict], list[dict]]:
    """实验五的接缝探针（EXPERIMENT.md §3 实验五 `seam_question_hit`）。

    `demo_data` 10 份 markdown 的段落都短于 700 字符，不触发
    ``split_blocks`` 的「同一标题内长度溢出」分支，因此 44 题题集上
    overlap 0/60/120/240 的分块完全相同（这是真实结论，见 e5_overlap.csv）。
    为单独测出 overlap 机制，这里造一份**合成受控语料**：32 个约 170 字符的
    段落，每段以一个唯一锚点结尾；相邻锚点是否还能落在同一个分块里，
    就是「答案原句被切在块边界」的结构化代理指标。
    合成语料只用于本探针，不进 44 题题集、不写入 docs/results 之外的库。
    """
    filler = "本节说明实验数据采集的口径、字段含义与复现步骤，用于填充段落长度以触发分块边界。"
    paragraphs = []
    markers = []
    for i in range(1, 33):
        markers.append(f"【锚点{i:02d}】")
        paragraphs.append(filler * 4 + markers[-1])
    md = "# 接缝测试文档\n\n" + "\n\n".join(paragraphs) + "\n"
    path = work / "seam_probe.md"
    path.write_text(md, encoding="utf-8")
    parsed = parser.parse_document(path)

    summary_rows: list[dict] = []
    detail_rows: list[dict] = []
    for ov in OVERLAPS:
        units = chunking.split_blocks(parsed.blocks, overlap=ov)
        texts = [u.text for u in units]
        presence = {m: [j for j, t in enumerate(texts) if m in t] for m in markers}
        duplicated = sum(1 for m in markers if len(presence[m]) >= 2)
        co_occur = 0
        for i in range(len(markers) - 1):
            a, b = markers[i], markers[i + 1]
            hit = int(bool(set(presence[a]) & set(presence[b])))
            co_occur += hit
            detail_rows.append({"overlap": ov, "pair_index": i + 1,
                                "anchor_a": a, "anchor_b": b,
                                "co_occur_in_one_chunk": hit,
                                "chunks_of_a": "|".join(str(x) for x in presence[a]),
                                "chunks_of_b": "|".join(str(x) for x in presence[b])})
        total_pairs = len(markers) - 1
        summary_rows.append({
            "overlap": ov, "chunk_count": len(units),
            "total_chars": sum(len(t) for t in texts),
            "avg_len": round(statistics.fmean([len(t) for t in texts]), 2),
            "vector_batches": math.ceil(len(units) / 16),
            "anchors_duplicated_by_overlap": duplicated,
            "seam_pairs_in_one_chunk": co_occur,
            "seam_total_pairs": total_pairs,
            "seam_pair_hit_rate": round(co_occur / total_pairs, 4),
        })
    return summary_rows, detail_rows


def switch_mode(client, headers, kb_id: int, mode: str) -> None:
    r = client.patch(f"/api/kbs/{kb_id}", headers=headers, json={"mode": mode})
    if r.status_code != 200:
        raise RuntimeError(f"切换知识库 {kb_id} 到 {mode} 失败：{r.status_code} {r.text[:200]}")


def drain_worker(worker, limit: int = 200) -> int:
    done = 0
    for _ in range(limit):
        if not worker.run_once("exp"):
            break
        done += 1
    return done


def make_chunk_kb(db, models, chunking, llm, *, name: str, filename: str,
                  units: list, sig: str, work_dir: Path) -> int:
    """用真实入库同款字段，把一组 TextUnit 写成一个临时 basic 知识库。"""
    from sqlalchemy import select as sa_select
    admin = db.execute(sa_select(models.User).where(
        models.User.role == models.Role.admin.value)).scalars().first()
    kb = models.KnowledgeBase(
        name=name, description="离线实验临时库", mode=models.KbMode.basic.value,
        status=models.KbStatus.ready.value, status_detail="实验用",
        embed_signature=sig, embed_dim=256, created_by=admin.id if admin else None,
    )
    db.add(kb)
    db.flush()
    doc = models.Document(kb_id=kb.id, filename=filename, suffix=".md",
                          size_bytes=0, mime="text/markdown",
                          status=models.KbStatus.ready.value, created_by=admin.id if admin else None)
    db.add(doc)
    db.flush()
    ver = models.DocumentVersion(doc_id=doc.id, version_no=1,
                                 storage_path=str(work_dir / "dummy"), checksum="",
                                 status=models.TaskStatus.success.value,
                                 text_chars=sum(len(u.text) for u in units),
                                 chunk_count=len(units))
    db.add(ver)
    db.flush()
    doc.current_version_id = ver.id
    rows = []
    for i, u in enumerate(units):
        vec = llm.hash_embed(u.text, 256)
        rows.append(models.Chunk(
            kb_id=kb.id, doc_id=doc.id, doc_version_id=ver.id, ordinal=i,
            text=u.text, heading=(u.heading or "")[:500], locator=u.locator,
            keywords=chunking.keyword_field(u.text),
            vector_blob=llm.pack_vector(vec), dim=256, embed_signature=sig,
            chunk_hash=chunking.chunk_hash(u.text, u.heading or ""),
            kind=getattr(u, "kind", "text"),
            meta=json.dumps(getattr(u, "meta", None) or {}, ensure_ascii=False),
        ))
    db.add_all(rows)
    db.commit()
    return kb.id


def main() -> int:
    # TestClient 每次请求都会打一条 httpx INFO，几百条会淹没进度输出。
    for name in ("httpx", "httpx2", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)

    # ---------------------------------------------------------------- 环境隔离
    work = Path(tempfile.mkdtemp(prefix="wrk_p01_"))
    os.environ.update({
        "WRK_DATA_DIR": str(work / "data"),
        "WRK_DB_PATH": str(work / "data" / "db" / "app.sqlite3"),
        "WRK_SECRET_DIR": str(work / "data" / "secrets"),
        "WRK_ADMIN_PASSWORD": ADMIN_PASSWORD,
        "WRK_WORKER_ENABLED": "0",
        "WRK_AGENT_ENABLED": "0",
        "WRK_HTTP_TRUST_ENV": "0",
        "PYTHONIOENCODING": "utf-8",
    })

    # 所有 app.* 导入必须在环境变量设好之后：config.py 在导入时读取 WRK_*。
    from sqlalchemy import select
    from fastapi.testclient import TestClient
    from app import config, models
    from app.db import SessionLocal
    from app.main import app, VERSION
    from app.services import chunking, llm, parser, retrieval, wiki, worker
    import scripts.seed_expanded as seed_mod
    import scripts.collect_metrics as cm

    print("[1/7] 灌数据：seed_expanded --reset（配置 A 离线）", flush=True)
    saved_argv = sys.argv
    try:
        sys.argv = ["seed_expanded", "--reset"]
        rc = seed_mod.main()
    finally:
        sys.argv = saved_argv
    if rc != 0:
        raise RuntimeError(f"seed_expanded 退出码 {rc}")

    questions = json.loads((ROOT / "demo_data" / "questions.json").read_text(encoding="utf-8"))
    q_by_id = {q["id"]: q for q in questions}
    answered = [q for q in questions if q["type"] != "no_answer"]

    # CSV 列：前 27 列沿用 collect_metrics.FIELDS，后面是本轮实验的扩展列。
    extra_fields = [
        "experiment", "group", "config", "question_id", "question_type", "kb_modes",
        "scored", "expected_unit_count", "hit_1", "hit_3", "hit_5", "hit_10",
        "mrr", "first_hit_rank", "top1_evidence_id", "coverage_top1",
        "candidate_total", "final_total", "embedding_ok", "rrf_k",
        "route_used", "route_fallback", "noise_unrelated_zero_cov",
        "channel_weights", "lexical_gate",
        "overlap", "chunk_count", "total_chars", "avg_len", "vector_batches",
        "locator_granularity", "tail_row_in_context", "answer_in_top10",
        "strategy", "source_file", "probe_questions",
        "wiki_shards", "wiki_excerpt", "wiki_topics_merged", "wiki_pages_capped",
        "wiki_notes_present",
    ]
    fields = list(cm.FIELDS) + [f for f in extra_fields if f not in cm.FIELDS]

    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "questions.search.json").write_text(
        json.dumps([q["question"] for q in questions], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")

    with SessionLocal() as db:
        sig = llm.active_embedding(db)[2]
        kb_rows = db.execute(select(models.KnowledgeBase.id, models.KnowledgeBase.name,
                                    models.KnowledgeBase.mode)).all()
        kb_by_name = {name: (kid, mode) for kid, name, mode in kb_rows}
        seed_chunks = [(r[0], r[1] or "", r[2] or "") for r in db.execute(
            select(models.Chunk.id, models.Chunk.text, models.Chunk.heading)).all()]
    expected_seed = derive_expected(seed_chunks, questions)
    (RESULTS / "expected_units.json").write_text(
        json.dumps({"match_rule": "见 run_experiments.py derive_expected()",
                    "seed_chunk_count": len(seed_chunks),
                    "embed_signature": sig, "questions": expected_seed},
                   ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    all_rows: list[dict] = []

    with TestClient(app) as client:
        login = client.post("/api/auth/login",
                            json={"username": "admin", "password": ADMIN_PASSWORD})
        if login.status_code != 200:
            raise RuntimeError(f"登录失败：{login.status_code} {login.text[:200]}")
        headers = {"Authorization": f"Bearer {login.json()['token']}"}
        meta = client.get("/api/meta").json()
        if not meta.get("offline_models"):
            raise RuntimeError("检测到在线模型配置，本脚本只适用于配置 A 离线")
        print(f"[2/7] 接口就绪：v{VERSION}，offline_models={meta['offline_models']}，"
              f"签名={sig}", flush=True)
        all_kb_ids = [kb_by_name[n][0] for n in ("人事制度库", "客户支持库", "产品与销售库")]

        # ------------------------------------------------------------ 采集器
        def collect(*, experiment: str, group: str, cfg_label: str, q: dict,
                    kb_ids: list[int], expected: dict, kb_modes: str | None = None,
                    overrides: dict | None = None) -> dict:
            row: dict[str, Any] = {f: "" for f in fields}
            started = time.perf_counter()
            err = ""
            data: dict = {}
            try:
                r = client.post("/api/search/preview", headers=headers,
                                json={"kb_ids": list(kb_ids), "query": q["question"]})
                latency_ms = int((time.perf_counter() - started) * 1000)
                if r.status_code == 200:
                    data = r.json()
                else:
                    err = f"HTTP {r.status_code}: {r.text[:200]}"
            except Exception as exc:
                latency_ms = int((time.perf_counter() - started) * 1000)
                err = f"{type(exc).__name__}: {exc}"

            ev = data.get("evidence") or []
            st = data.get("stats") or {}
            usage = data.get("usage") or {}
            routes = data.get("routes") or []
            ids = set(expected.get(q["id"], {}).get("expected_chunk_ids") or [])
            scored = bool(expected.get(q["id"], {}).get("scored")) and bool(ids)

            hit_flags: list[bool] = []
            for e in ev[:10]:
                unit_ids: set[int] = set()
                if e.get("chunk_id") is not None:
                    unit_ids.add(int(e["chunk_id"]))
                for sub in (e.get("evidence") or []):
                    if sub.get("chunk_id") is not None:
                        unit_ids.add(int(sub["chunk_id"]))
                hit_flags.append(bool(unit_ids & ids))
            first = next((i for i, h in enumerate(hit_flags, 1) if h), 0)
            coverages = [float(e.get("coverage") or 0.0) for e in ev]
            source_types = sorted({str(e.get("source_type") or "") for e in ev if e.get("source_type")})
            want = set(q.get("expected_kb") or [])
            noise = sum(1 for e in ev if float(e.get("coverage") or 0.0) == 0.0
                        and e.get("kb_name") not in want)
            modes_label = kb_modes if kb_modes is not None else "|".join(
                f"{k}:basic" for k in kb_ids)

            # collect_metrics.FIELDS 同名列：search 模式，无生成侧字段。
            row.update({
                "run_label": f"{experiment}/{group}", "repeat": 1,
                "kb_ids": "|".join(str(k) for k in kb_ids),
                "kb_modes": modes_label,
                "question": q["question"], "mode": "search",
                "evidence_count": len(ev),
                "top1_coverage": round(coverages[0], 4) if coverages else 0.0,
                "mean_coverage": round(statistics.fmean(coverages), 4) if coverages else 0.0,
                "source_types": "+".join(source_types),
                "answer_chars": 0, "citation_count": 0, "invalid_citations": 0,
                "insufficient": "", "latency_ms": latency_ms,
                "model_latency_ms": int(usage.get("latency_ms") or 0),
                "calls": int(usage.get("calls") or 0),
                "chat_calls": int(usage.get("chat_calls") or 0),
                "embed_calls": int(usage.get("embed_calls") or 0),
                "live_calls": int(usage.get("live_calls") or 0),
                "offline_calls": int(usage.get("offline_calls") or 0),
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
                "embed_tokens": int(usage.get("embed_tokens") or 0),
                "total_tokens": int(usage.get("total_tokens") or 0),
                "stats_mode": "no-chat", "embed_mode": str(usage.get("embed_mode") or ""),
                "error": err,
            })
            row.update({
                "experiment": experiment, "group": group, "config": cfg_label,
                "question_id": q["id"], "question_type": q["type"],
                "kb_modes": modes_label,
                "scored": int(scored), "expected_unit_count": len(ids),
                "hit_1": int(any(hit_flags[:1])), "hit_3": int(any(hit_flags[:3])),
                "hit_5": int(any(hit_flags[:5])), "hit_10": int(any(hit_flags[:10])),
                "mrr": round(1.0 / first, 4) if first else 0.0,
                "first_hit_rank": first,
                "top1_evidence_id": ev[0].get("evidence_id", "") if ev else "",
                "coverage_top1": round(coverages[0], 4) if coverages else 0.0,
                "candidate_total": int(st.get("candidate_total") or 0),
                "final_total": int(st.get("final_total") or 0),
                "embedding_ok": bool(st.get("embedding_ok")),
                "rrf_k": int(st.get("rrf_k") or 0),
                "route_used": "|".join(str(x.get("used", "")) for x in routes),
                "route_fallback": "|".join(str(bool(x.get("fallback"))) for x in routes),
                "noise_unrelated_zero_cov": noise,
                "channel_weights": "", "lexical_gate": "",
                "overlap": "", "chunk_count": "", "total_chars": "", "avg_len": "",
                "vector_batches": "", "locator_granularity": "",
                "tail_row_in_context": "", "answer_in_top10": "",
                "strategy": "", "source_file": "", "probe_questions": "",
                "wiki_shards": "", "wiki_excerpt": "", "wiki_topics_merged": "",
                "wiki_pages_capped": "", "wiki_notes_present": "",
            })
            if overrides:
                row.update(overrides)
            return row

        def write_csv(name: str, rows: list[dict]) -> None:
            path = RESULTS / name
            with path.open("w", encoding="utf-8-sig", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
                w.writeheader()
                for r in rows:
                    w.writerow(r)

        # ------------------------------------------------------------ E1
        print("[3/7] E1 原文分块 vs Wiki 增强", flush=True)
        for kid, name, mode in kb_rows:
            if mode != "wiki":
                switch_mode(client, headers, kid, "wiki")
        drain_worker(worker)
        with SessionLocal() as db:
            cov = wiki_covariates(db, models, wiki)
        wiki_rows = []
        for q in questions:
            wiki_rows.append(collect(experiment="E1", group="wiki", cfg_label="mode=wiki/all3",
                                     q=q, kb_ids=all_kb_ids, expected=expected_seed,
                                     kb_modes="|".join(f"{k}:wiki" for k in all_kb_ids),
                                     overrides={k: ("" if v is None else v)
                                                for k, v in cov.items() if k != "wiki_notes_raw"}))
        for kid, _, _ in kb_rows:
            switch_mode(client, headers, kid, "basic")
        basic_rows = []
        for q in questions:
            basic_rows.append(collect(experiment="E1", group="basic", cfg_label="mode=basic/all3",
                                      q=q, kb_ids=all_kb_ids, expected=expected_seed))
        e1_rows = wiki_rows + basic_rows
        write_csv("e1_wiki_vs_basic.csv", e1_rows)
        all_rows += e1_rows

        # ------------------------------------------------------------ E2
        print("[4/7] E2 单库 vs 多库混合路由", flush=True)
        e2_rows = []
        for group, pick in (("single", "one"), ("multi", "all")):
            for q in questions:
                if pick == "one":
                    target = (q.get("expected_kb") or ["人事制度库"])[0]
                    kb_ids = [kb_by_name[target][0]]
                else:
                    kb_ids = [kb_by_name[n][0] for n in ("人事制度库", "客户支持库", "产品与销售库")]
                e2_rows.append(collect(experiment="E2", group=group, cfg_label=f"kb_ids={pick}",
                                       q=q, kb_ids=kb_ids, expected=expected_seed))
        # 多库噪声已在 collect() 里按「coverage==0 且来自非预期库」逐行写入
        # noise_unrelated_zero_cov 列，可在 CSV 中直接追溯。
        write_csv("e2_single_vs_multi.csv", e2_rows)
        all_rows += e2_rows

        # ------------------------------------------------------------ E3
        print("[5/7] E3 加权 / 等权 / 纯关键词 / 纯向量 RRF 消融", flush=True)
        orig_weights = retrieval.channel_weights
        orig_vector_rank = retrieval.vector_rank
        orig_embed = llm.embed
        e3_groups = [
            ("weighted_rrf", None, True, True, "[1.0,0.45]"),
            ("equal_rrf", [1.0, 1.0], True, True, "[1.0,1.0]"),
            ("keyword_only", None, False, True, "[1.0,0.0]"),
            ("vector_only", [0.0, 1.0], True, False, "[0.0,1.0]"),
        ]
        e3_rows = []
        try:
            for gname, weights, qvec_ok, lexical_gate, wlabel in e3_groups:
                def patched_weights(db, ok, _w=weights):
                    if not ok:
                        return [1.0, 0.0]
                    return _w if _w is not None else orig_weights(db, ok)

                def patched_vector_rank(*args, **kwargs):
                    kwargs["lexical_ids"] = None
                    return orig_vector_rank(*args, **kwargs)

                retrieval.channel_weights = patched_weights
                retrieval.vector_rank = patched_vector_rank if not lexical_gate else orig_vector_rank
                if not qvec_ok:
                    llm.embed = lambda db, texts, **kw: [None] * len(texts)
                else:
                    llm.embed = orig_embed
                for q in questions:
                    e3_rows.append(collect(
                        experiment="E3", group=gname,
                        cfg_label=f"weights={wlabel};qvec={qvec_ok};lexical_gate={lexical_gate}",
                        q=q, kb_ids=all_kb_ids, expected=expected_seed,
                        overrides={"channel_weights": wlabel,
                                   "lexical_gate": int(lexical_gate)}))
        finally:
            retrieval.channel_weights = orig_weights
            retrieval.vector_rank = orig_vector_rank
            llm.embed = orig_embed
        write_csv("e3_rrf_ablation.csv", e3_rows)
        all_rows += e3_rows

        # ------------------------------------------------------------ E5
        print("[6/7] E5 overlap 消融", flush=True)
        e5_rows = []
        for ov in OVERLAPS:
            units_all = []
            for fname in DEMO_FILES:
                parsed = parser.parse_document(ROOT / "demo_data" / fname)
                units_all += chunking.split_blocks(parsed.blocks, overlap=ov)
            with SessionLocal() as db:
                kid = make_chunk_kb(db, models, chunking, llm, name=f"E5-overlap-{ov}",
                                    filename="demo_data_all.md", units=units_all,
                                    sig=sig, work_dir=work)
                kb_chunks = [(r[0], r[1] or "", r[2] or "") for r in db.execute(
                    select(models.Chunk.id, models.Chunk.text, models.Chunk.heading)
                    .where(models.Chunk.kb_id == kid)).all()]
            expected_ov = derive_expected(kb_chunks, questions)
            total_chars = sum(len(u.text) for u in units_all)
            for q in questions:
                e5_rows.append(collect(
                    experiment="E5", group=f"overlap_{ov}", cfg_label=f"WRK_CHUNK_OVERLAP={ov}",
                    q=q, kb_ids=[kid], expected=expected_ov,
                    overrides={"overlap": ov, "chunk_count": len(units_all),
                               "total_chars": total_chars,
                               "avg_len": round(total_chars / len(units_all), 2) if units_all else 0,
                               "vector_batches": math.ceil(len(units_all) / 16)}))
        write_csv("e5_overlap.csv", e5_rows)
        all_rows += e5_rows

        # 接缝探针：单独验证 overlap 机制（44 题题集上四组完全相同）。
        seam_summary, seam_detail = run_seam_probe(work, chunking, parser)
        with (RESULTS / "e5_seam_probe.csv").open("w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(seam_summary[0].keys()))
            w.writeheader()
            w.writerows(seam_summary)
        with (RESULTS / "e5_seam_probe_detail.csv").open("w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(seam_detail[0].keys()))
            w.writeheader()
            w.writerows(seam_detail)

        # ------------------------------------------------------------ 检索耗时
        print("[7/7] 离线检索耗时（9 条查询 × 10 次）", flush=True)
        latency_rows = []
        for rep in range(1, 11):
            for q in questions[:9]:
                started = time.perf_counter()
                r = client.post("/api/search/preview", headers=headers,
                                json={"kb_ids": all_kb_ids, "query": q["question"]})
                ms = (time.perf_counter() - started) * 1000
                latency_rows.append({"repeat": rep, "question_id": q["id"],
                                     "question": q["question"], "latency_ms": round(ms, 3),
                                     "status": r.status_code})
        with (RESULTS / "latency.csv").open("w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["repeat", "question_id", "question",
                                               "latency_ms", "status"])
            w.writeheader()
            w.writerows(latency_rows)

    # ---------------------------------------------------------------- E4（探针）
    print("[+] E4 表格分块三策略探针", flush=True)
    e4_rows = []
    for fname in TABLE_FILES:
        parsed = parser.parse_document(ROOT / "demo_data" / "fixtures" / fname)
        tables = [b.table for b in parsed.blocks if b.kind == "table"]
        for t in tables:
            header = t.header or []
            if t.total_rows == 0:
                continue
            strategies = {}
            strategies["row"] = [
                chunking.TextUnit(text=chunking._row_to_line(header, r), heading=t.caption,
                                  locator=f"{t.locator_prefix} 第 {i} 行", ordinal=i, kind="table",
                                  meta={"row_start": i, "row_end": i})
                for i, r in enumerate(t.rows, 1)]
            whole_lines = "\n".join(chunking._row_to_line(header, r) for r in t.rows)
            strategies["whole"] = [chunking.TextUnit(
                text=f"表：{t.caption}（共 {t.total_rows} 行）\n{whole_lines}",
                heading=t.caption, locator=f"{t.locator_prefix} 全表", ordinal=1, kind="table",
                meta={"row_start": 1, "row_end": t.total_rows})]
            strategies["group"] = chunking.split_table(
                t, size=config.TABLE_CHUNK_BUDGET, ordinal_start=1, heading=t.caption)

            for sname, units in strategies.items():
                lens = [len(u.text) for u in units]
                last_row = t.rows[-1]
                last_id = last_row[0]
                # 截断效应：取「包含末尾行的那一块」作为唯一证据，走真实
                # build_context() 拼提示词。整表策略下该块 5.9 万字符会被
                # CONTEXT_CHAR_BUDGET 截断，末尾行必然丢失；行组/逐行策略下
                # 单块远小于预算，末尾行完整保留。
                tail_unit = next((u for u in units if last_id in u.text), None)
                evs = [retrieval.Evidence(kb_id=0, kb_name="E4", source_type="chunk",
                                          text=tail_unit.text if tail_unit else "",
                                          rank=1, doc_name=fname, version_no=1,
                                          locator=tail_unit.locator if tail_unit else "",
                                          heading=tail_unit.heading if tail_unit else "")]
                ctx = retrieval.build_context(evs)
                tail_in_context = int(last_id in ctx)
                # 可回答性代理：真实检索原语，检查答案所在单元是否进 top-10。
                probe_qs = []
                head_idx = [1, 3, 5]
                tail_idx = [max(1, t.total_rows - 4), max(1, t.total_rows - 2), t.total_rows]
                for ri in head_idx + tail_idx:
                    row = t.rows[ri - 1]
                    query = f"工号 {row[0]} 的月薪是多少？"
                    answer_idx = next((i for i, u in enumerate(units)
                                       if row[0] in u.text), None)
                    rows_for_rank = [(i, chunking.keyword_field(u.text), u.text.lower())
                                     for i, u in enumerate(units)]
                    kw = retrieval.keyword_rank(rows_for_rank, query, limit=30)
                    kw_ids = [rid for rid, _ in kw]
                    qvec = llm.hash_embed(query, 256)
                    pairs = [(i, llm.hash_embed(u.text, 256)) for i, u in enumerate(units)]
                    vec = retrieval.vector_rank(pairs, qvec, limit=30,
                                                lexical_ids=set(kw_ids))
                    fused = retrieval.rrf_fuse([kw_ids, vec], weights=[1.0, 0.45])[:10]
                    fused_ids = [rid for rid, _, _ in fused]
                    # 可回答性的另一半：检索引擎命中不算数，还要看答案行有没有
                    # 真正进到 build_context() 拼出的提示词里（整表策略会在这里失效）。
                    top_units = [units[i] for i in fused_ids if i < len(units)]
                    ctx_evs = [retrieval.Evidence(
                        kb_id=0, kb_name="E4", source_type="chunk", text=u.text,
                        rank=j + 1, doc_name=fname, version_no=1,
                        locator=u.locator, heading=u.heading)
                        for j, u in enumerate(top_units)]
                    probe_ctx = retrieval.build_context(ctx_evs)
                    probe_qs.append({"row": ri, "employee_id": row[0],
                                     "position": "head" if ri in head_idx else "tail",
                                     "answer_unit": answer_idx,
                                     "answer_in_top10": int(answer_idx is not None
                                                            and answer_idx in fused_ids),
                                     "answer_in_context": int(row[0] in probe_ctx)})
                e4_rows.append({
                    "source_file": fname, "strategy": sname,
                    "table_caption": t.caption, "total_rows": t.total_rows,
                    "cols": t.col_count, "chunk_count": len(units),
                    "total_chars": sum(lens),
                    "min_len": min(lens), "avg_len": round(sum(lens) / len(lens), 2),
                    "max_len": max(lens),
                    "vector_batches": math.ceil(len(units) / 16),
                    "locator_granularity": {"row": "单行", "whole": "全表",
                                            "group": "行区间"}[sname],
                    "ranges_contiguous": int(all(
                        (u.meta or {}).get("row_start", 0) <= (u.meta or {}).get("row_end", 0)
                        for u in units)),
                    "tail_row_in_context": tail_in_context,
                    "answer_in_top10_head": f"{sum(p['answer_in_top10'] for p in probe_qs if p['position']=='head')}/3",
                    "answer_in_top10_tail": f"{sum(p['answer_in_top10'] for p in probe_qs if p['position']=='tail')}/3",
                    "answer_in_context_head": f"{sum(p['answer_in_context'] for p in probe_qs if p['position']=='head')}/3",
                    "answer_in_context_tail": f"{sum(p['answer_in_context'] for p in probe_qs if p['position']=='tail')}/3",
                    "probe_questions": json.dumps(probe_qs, ensure_ascii=False),
                })
    with (RESULTS / "e4_table_chunking.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(e4_rows[0].keys()))
        w.writeheader()
        w.writerows(e4_rows)
    (RESULTS / "e4_table_chunking.json").write_text(
        json.dumps(e4_rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # ---------------------------------------------------------------- 汇总
    def summarize(rows: list[dict]) -> dict:
        scored = [r for r in rows if int(r.get("scored") or 0) == 1]
        n = len(scored) or 1
        def mean(col: str) -> float:
            return round(sum(float(r.get(col) or 0) for r in scored) / n, 4)
        lat = sorted(float(r.get("latency_ms") or 0) for r in scored)
        def pct(p: float) -> float:
            if not lat:
                return 0.0
            k = max(0, min(len(lat) - 1, int(round((len(lat) - 1) * p))))
            return lat[k]
        return {
            "n_questions": len(rows), "n_scored": len(scored),
            "hit_1": mean("hit_1"), "hit_3": mean("hit_3"),
            "hit_5": mean("hit_5"), "hit_10": mean("hit_10"),
            "mrr": mean("mrr"),
            "coverage": round(sum(1 for r in scored if int(r.get("first_hit_rank") or 0) > 0) / n, 4),
            "top1_coverage": mean("coverage_top1"),
            "mean_coverage": mean("mean_coverage"),
            "evidence_count": mean("evidence_count"),
            "candidate_total": mean("candidate_total"),
            "final_total": mean("final_total"),
            "latency_ms_p50": pct(0.5), "latency_ms_p95": pct(0.95),
            "latency_ms_mean": round(statistics.fmean(lat), 2) if lat else 0.0,
            "calls": mean("calls"), "embed_calls": mean("embed_calls"),
            "live_calls": mean("live_calls"), "offline_calls": mean("offline_calls"),
            "embed_texts": mean("embed_tokens"),
        }

    groups: dict[str, dict[str, list[dict]]] = {}
    for r in all_rows:
        groups.setdefault(r["experiment"], {}).setdefault(r["group"], []).append(r)
    exp_summary = {exp: {g: summarize(rs) for g, rs in gs.items()}
                   for exp, gs in groups.items()}

    lat_vals = [x["latency_ms"] for x in latency_rows if x["status"] == 200]
    lat_sorted = sorted(lat_vals)
    latency_summary = {
        "samples": len(lat_sorted),
        "min": round(lat_sorted[0], 3) if lat_sorted else None,
        "p50": round(lat_sorted[int((len(lat_sorted) - 1) * 0.5)], 3) if lat_sorted else None,
        "p95": round(lat_sorted[int((len(lat_sorted) - 1) * 0.95)], 3) if lat_sorted else None,
        "max": round(lat_sorted[-1], 3) if lat_sorted else None,
        "mean": round(statistics.fmean(lat_sorted), 3) if lat_sorted else None,
        "stdev": round(statistics.pstdev(lat_sorted), 3) if len(lat_sorted) > 1 else None,
        "note": "in-process TestClient 端到端（含路由/序列化），不含网络往返；配置 A 离线哈希向量",
    }

    env = {
        "schema_version": 1,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "git_head": git_head(),
        "app_version": VERSION,
        "experiment_config": "A 离线（无 llm、无 embedding，向量 local-hash-embedding:256）",
        "channel_weights": "[1.0, 0.45]（离线哈希向量降权）",
        "embed_signature": sig,
        "seed": "seed_expanded --reset（三库 55 个检索单元；Wiki 由离线抽取式生成）",
        "question_set": "demo_data/questions.json（44 题：fact/cross/multi_hop 各 11，no_answer 11）",
        "config": {
            "DEFAULT_CHUNK_SIZE": config.DEFAULT_CHUNK_SIZE,
            "DEFAULT_CHUNK_OVERLAP": config.DEFAULT_CHUNK_OVERLAP,
            "TABLE_CHUNK_BUDGET": config.TABLE_CHUNK_BUDGET,
            "RRF_K": config.RRF_K,
            "PER_KB_CANDIDATES": config.PER_KB_CANDIDATES,
            "MAX_EVIDENCE": config.MAX_EVIDENCE,
            "CONTEXT_CHAR_BUDGET": config.CONTEXT_CHAR_BUDGET,
            "WORKER_CONCURRENCY": config.WORKER_CONCURRENCY,
            "WORKER_ENABLED": config.WORKER_ENABLED,
            "WIKI_MAX_MAP_CALLS": config.WIKI_MAX_MAP_CALLS,
            "WIKI_MAX_PAGES": config.WIKI_MAX_PAGES,
        },
        "hardware": hardware(),
        "python": platform.python_version(),
        "randomness": "离线哈希向量确定性；无随机种子，重复运行结果一致（仅耗时波动）",
    }
    (RESULTS / "environment.json").write_text(
        json.dumps(env, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    summary = {
        "config": "A 离线",
        "status_note": (
            "本轮为配置 A 离线实测；按 docs/EXPERIMENT.md §7 与 docs/THESIS.md §6.7，"
            "离线哈希向量与抽取式回答只证明链路可运行，不能作为论文主结果。"
            "正确性三档、证据支持率、拒答正确率、p 值、配置 B 全部指标仍为待实验。"
        ),
        "experiments": exp_summary,
        "e4_table_chunking": e4_rows,
        "e5_seam_probe": seam_summary,
        "latency": latency_summary,
        "wiki_covariates": cov,
        "environment": env,
    }
    (RESULTS / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # 人类可读汇总
    lines = [
        "# 五个对照实验结果（配置 A 离线，实测）",
        "",
        "> 数据来源：`docs/results/*.csv`；采集器 `backend/scripts/run_experiments.py`；",
        "> 环境见 `environment.json`，复现命令见 `README.md`。",
        "",
        "**口径**：配置 A 离线（无真实 llm/embedding，向量为 `local-hash-embedding:256`）。",
        "按 `docs/EXPERIMENT.md` §7 / `docs/THESIS.md` §6.7，本轮数字**不能作为论文主结果**，",
        "只证明链路可运行与结果可复现；依赖真实模型或人工标注的指标一律「待实验」。",
        "",
        "- Hit@k / MRR 分母为 33 道可答题（11 道 `no_answer` 题不计入），判据为",
        "  「前 k 条证据的 `chunk_id`（Wiki 证据用回挂的 `evidence[].chunk_id`）落在自动标注的",
        "  预期命中单元里」，逐题单元见 `expected_units.json`。",
        "- `latency_ms` 为 in-process 接口端到端墙钟时间，不含网络往返。",
        "",
        "## 汇总表",
        "",
        "| 实验 | 组 | 计分题 | Hit@1 | Hit@3 | Hit@5 | Hit@10 | MRR | 覆盖率 | 首位覆盖 | 证据数 | 耗时 P50(ms) |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for exp in ("E1", "E2", "E3", "E5"):
        for g, s in exp_summary.get(exp, {}).items():
            lines.append(
                f"| {exp} | {g} | {s['n_scored']} | {s['hit_1']} | {s['hit_3']} | "
                f"{s['hit_5']} | {s['hit_10']} | {s['mrr']} | {s['coverage']} | "
                f"{s['top1_coverage']} | {s['evidence_count']} | {s['latency_ms_p50']} |")
    lines += [
        "",
        "## 实验四：表格分块三策略（探针实测）",
        "",
        "| 文件 | 表 | 策略 | 块数 | 总字符 | min/avg/max | 向量批次 | 末尾行在上下文 | 头部题命中 | 末尾题命中 | 头部答案进上下文 | 末尾答案进上下文 |",
        "| --- | --- | --- | ---: | ---: | --- | ---: | ---: | --- | --- | --- | --- |",
    ]
    for r in e4_rows:
        lines.append(
            f"| {r['source_file']} | {r['table_caption']} | {r['strategy']} | "
            f"{r['chunk_count']} | "
            f"{r['total_chars']} | {r['min_len']}/{r['avg_len']}/{r['max_len']} | "
            f"{r['vector_batches']} | {r['tail_row_in_context']} | "
            f"{r['answer_in_top10_head']} | {r['answer_in_top10_tail']} | "
            f"{r['answer_in_context_head']} | {r['answer_in_context_tail']} |")
    lines += [
        "",
        "整表策略在「检索命中」上不差（单块就含全部行），但 `tail_row_in_context=0`：",
        "`build_context()` 按 `CONTEXT_CHAR_BUDGET=9000` 把 1 万～5.9 万字符的整表块截断，",
        "末尾行根本进不了提示词——这就是 EXPERIMENT.md §3 实验四要的截断失效证据。",
        "",
        "## 实验五：overlap 接缝探针（合成受控语料）",
        "",
        "44 题题集上四组分块数同为 55（`demo_data` 段落均短于 700 字符，不触发溢出分支），",
        "因此 Hit@k 完全相同；下面用 32 段合成语料单独测 overlap 机制，",
        "逐对明细见 `e5_seam_probe_detail.csv`。",
        "",
        "| overlap | 块数 | 总字符 | 平均长度 | 向量批次 | 被重叠复制的锚点数 | 同块相邻锚点对 | 接缝对命中率 |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: |",
    ]
    for r in seam_summary:
        lines.append(
            f"| {r['overlap']} | {r['chunk_count']} | {r['total_chars']} | "
            f"{r['avg_len']} | {r['vector_batches']} | "
            f"{r['anchors_duplicated_by_overlap']} | "
            f"{r['seam_pairs_in_one_chunk']}/{r['seam_total_pairs']} | "
            f"{r['seam_pair_hit_rate']} |")
    lines += [
        "",
        "## 检索耗时（9 题 × 10 次）",
        "",
        f"min {latency_summary['min']} / P50 {latency_summary['p50']} / "
        f"P95 {latency_summary['p95']} / max {latency_summary['max']} / "
        f"均值 {latency_summary['mean']} ms（样本 {latency_summary['samples']}）；"
        "接入真实 embedding 后必须重测。",
        "",
        "## 待实验（不填推测值）",
        "",
        "- 配置 B（真实 embedding + 在线生成）下 E1–E5 的全部指标；",
        "- 正确性三档、证据支持率、引用精确率、拒答正确率（需两人独立人工标注）；",
        "- Wilcoxon 符号秩 / McNemar 检验的实际 p 值；",
        "- 真实 embedding 下的检索耗时 P50/P95；",
        "- CPU 具体型号：本机为虚拟机，系统只报告 `Not Available`，已在",
        "  `environment.json` 如实记录厂商、逻辑核数、内存与磁盘型号，不填推测值。",
        "",
    ]
    (RESULTS / "summary.md").write_text("\n".join(lines), encoding="utf-8")

    print(f"\n完成：{len(all_rows)} 行原始记录 → {RESULTS}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
