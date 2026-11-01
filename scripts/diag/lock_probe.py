#!/usr/bin/env python
"""写锁诊断探针：一次跑清楚「database is locked 到底卡在哪」。

把原来三个临时脚本合并成一个（scripts/_diag_lock.py / _diag_lock2.py /
_diag_lock3.py，已删除）：

- `_diag_lock.py`  ：复制真实库 → 排一个 embedding 重建任务 → 盯写锁
- `_diag_lock2.py` ：不复制文件目录 → 排两个真实入库任务 → 盯写锁
- `_diag_lock3.py` ：两个进程共享一份库 → 验证跨进程写锁

现在这三件事分别对应 `--tasks 1`、`--tasks 2`、`--tag p1 / p2` 两个终端。

核心手法不变：**另开一个 sqlite3 连接每 0.5s 抢一次写锁**，抢到就记耗时，
抢不到就记一次「被挡住」；加 `--dump-stacks` 时顺手 dump 本进程全部线程栈，
直接看出是哪个线程、哪一行代码握着写锁。

用法（Windows，venv 里的 python）：

    # 1) 默认：只读复制桌面版真实库到 .tmp-diag，跑 1 个任务并盯写锁
    python scripts/diag/lock_probe.py --tasks 1 --dump-stacks

    # 2) 只监控某个库（不排任务），持续 60 秒
    python scripts/diag/lock_probe.py --db .tmp-diag/db/app.sqlite3 --watch --duration 60

    # 3) 跨进程写锁：开两个终端，各自跑一份（共享 .tmp-diag 的同一份库）
    python scripts/diag/lock_probe.py --db .tmp-diag/db/app.sqlite3 --tasks 1 --tag p1
    python scripts/diag/lock_probe.py --db .tmp-diag/db/app.sqlite3 --tasks 1 --tag p2

    # 4) 指定源数据目录（仍然只读复制，绝不写原库）
    python scripts/diag/lock_probe.py --copy-from "%APPDATA%\\wiki-rag-desktop\\data" --tasks 2

安全约束（重要）：
- **绝不对桌面版真实数据目录做写操作**。不给 `--db` 也不给 `--copy-from` 时，
  默认就是把 `%APPDATA%\\wiki-rag-desktop\\data` 复制到 `.tmp-diag/` 再跑。
- `--db` 指向真实数据目录时直接拒绝（退出码 2），要跑请用 `--copy-from`。
- 复制时只带 `db/app.sqlite3` 和 `secrets/`，**不复制 files/**：`document_versions.
  storage_path` 是绝对路径，入库任务会去读原文件（只读）。原文件被删了就自动
  退化成 embedding 重建任务（不需要文件）。

退出码：0 = 没抓到写锁阻塞；1 = 抓到了（说明还存在长写事务/孤儿持锁）；
2 = 参数或环境错误。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import threading
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]          # scripts/diag/lock_probe.py → 仓库根
BACKEND = ROOT / "backend"
TMP = ROOT / ".tmp-diag"

# 桌面版真实数据目录（Electron 用 app.getPath('userData') 下的 data/）
REAL_DATA = Path(os.path.expandvars(r"%APPDATA%\wiki-rag-desktop\data"))


# ------------------------------------------------------------------ 参数
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="lock_probe.py",
        description="SQLite 写锁诊断探针（合并版 _diag_lock*.py）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("用法（Windows", 1)[-1],
    )
    parser.add_argument("--db", metavar="PATH",
                        help="直接用这个库（必须不是桌面版真实库）；缺省时自动复制真实库")
    parser.add_argument("--copy-from", metavar="DIR",
                        help="源数据目录（含 db/app.sqlite3 与 secrets/）；只读复制到 .tmp-diag")
    parser.add_argument("--tasks", type=int, default=1, metavar="N",
                        help="排 N 个任务（默认 1）；0 = 只监控不排任务")
    parser.add_argument("--kind", choices=("auto", "ingest", "rebuild"), default="auto",
                        help="任务类型：ingest=文档入库，rebuild=embedding 重建，auto=有文件就入库")
    parser.add_argument("--watch", action="store_true",
                        help="只监控写锁，不排任务（等价 --tasks 0）")
    parser.add_argument("--duration", type=float, default=180.0, metavar="SEC",
                        help="最长运行时间，默认 180 秒")
    parser.add_argument("--dump-stacks", action="store_true",
                        help="抓到阻塞时 dump 本进程全部线程栈（定位持锁线程）")
    parser.add_argument("--tag", default="probe", metavar="NAME",
                        help="日志前缀，跨进程对比时用来区分（如 p1 / p2）")
    parser.add_argument("--concurrency", type=int, default=1, metavar="N",
                        help="worker 线程数，默认 1（SQLite 上多 worker 收益为负）")
    parser.add_argument("--threshold", type=float, default=0.25, metavar="SEC",
                        help="写锁等待超过多少秒算「被挡住」，默认 0.25")
    parser.add_argument("--interval", type=float, default=0.5, metavar="SEC",
                        help="探针间隔，默认 0.5 秒")
    parser.add_argument("--keep", action="store_true",
                        help="保留 .tmp-diag（默认每次复制前清空）")
    return parser.parse_args(argv)


def _log(tag: str, message: str) -> None:
    print(f"[{tag}] {message}", flush=True)


def _die(message: str, code: int = 2) -> None:
    """参数/环境错误统一退出码 2（1 留给「抓到写锁阻塞」）。"""
    print(message, file=sys.stderr, flush=True)
    raise SystemExit(code)


# ------------------------------------------------------------------ 数据准备
def _is_inside_real_data(path: Path) -> bool:
    """判断路径是否落在桌面版真实数据目录里（非 Windows 上 %APPDATA% 不展开，自然为 False）。"""
    try:
        if not REAL_DATA.exists():
            return False
        path.resolve().relative_to(REAL_DATA.resolve())
        return True
    except (ValueError, OSError):
        return False


def _copy_data(src: Path, *, keep: bool) -> Path:
    """把源数据目录的 db + secrets 复制到 .tmp-diag，返回复制后的库路径。"""
    if not src.exists():
        _die(f"源数据目录不存在：{src}")
    src_db = src / "db" / "app.sqlite3"
    if not src_db.exists():
        _die(f"源库里没有 db/app.sqlite3：{src}")

    if TMP.exists() and not keep:
        shutil.rmtree(TMP, ignore_errors=True)
    for sub in ("db", "files", "secrets"):
        (TMP / sub).mkdir(parents=True, exist_ok=True)

    dst_db = TMP / "db" / "app.sqlite3"
    # 用 copy2 保留时间戳；WAL 模式下顺带把 -wal/-shm 一起带上，避免读到半截事务
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(src_db) + suffix)
        if candidate.exists():
            shutil.copy2(candidate, Path(str(dst_db) + suffix))
    if (src / "secrets").exists():
        for item in (src / "secrets").iterdir():
            if item.is_file():
                shutil.copy2(item, TMP / "secrets" / item.name)
    _log("copy", f"{src} → {TMP}（只读复制，原库未改动）")
    _log("copy", "注意：files/ 未复制，入库任务会读原路径的文件（只读）")
    return dst_db


def prepare_data_dir(args: argparse.Namespace) -> tuple[Path, Path]:
    """确定 (data_dir, db_path)，并把 WRK_* 环境变量设好（必须在 import app 之前）。"""
    if args.copy_from:
        db_path = _copy_data(Path(os.path.expandvars(args.copy_from)), keep=args.keep)
    elif args.db:
        db_path = Path(os.path.expandvars(args.db)).expanduser().resolve()
        if _is_inside_real_data(db_path):
            _die(
                "拒绝：--db 指向桌面版真实数据目录，探针会写库。\n"
                f"      请改用 --copy-from {REAL_DATA}"
            )
        if not db_path.exists():
            _die(f"库不存在：{db_path}")
        if not args.keep:
            _log("warn", f"直接使用 {db_path}（会写库；不是真实数据目录就行）")
    else:
        _log("copy", "未指定 --db/--copy-from，默认只读复制桌面版真实库")
        db_path = _copy_data(REAL_DATA, keep=args.keep)

    data_dir = db_path.parent.parent if db_path.parent.name == "db" else db_path.parent
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "secrets").mkdir(parents=True, exist_ok=True)

    tasks = 0 if args.watch else max(0, args.tasks)
    os.environ["WRK_DATA_DIR"] = str(data_dir)
    os.environ["WRK_DB_PATH"] = str(db_path)
    os.environ["WRK_SECRET_DIR"] = str(data_dir / "secrets")
    os.environ["WRK_HTTP_TRUST_ENV"] = "0"
    os.environ["WRK_WORKER_ENABLED"] = "1" if tasks else "0"
    os.environ["WRK_WORKER_CONCURRENCY"] = str(max(1, args.concurrency))
    # 探针自己不用 HTTP，避免它顺手起服务/写日志
    os.environ.setdefault("WRK_LOG_LEVEL", "INFO")
    return data_dir, db_path


# ------------------------------------------------------------------ 探针
class LockMonitor:
    """独立连接反复抢写锁：抢到记耗时，抢不到记事件（可选 dump 线程栈）。"""

    def __init__(self, db_path: Path, *, tag: str, interval: float, threshold: float,
                 dump_stacks: bool):
        self.db_path = db_path
        self.tag = tag
        self.interval = max(0.05, interval)
        self.threshold = max(0.01, threshold)
        self.dump_stacks = dump_stacks
        self.events: list[str] = []
        self.dumps = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"lock-monitor-{tag}",
                                        daemon=True)

    # -- 生命周期 --------------------------------------------------
    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    # -- 内部 ------------------------------------------------------
    def _record(self, message: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {message}"
        self.events.append(line)
        _log(self.tag, "阻塞事件：" + line)

    def _dump(self, reason: str) -> None:
        """dump 本进程所有线程栈：持锁线程就在里面。"""
        self.dumps += 1
        names = {t.ident: t.name for t in threading.enumerate()}
        frames = sys._current_frames()
        print(f"\n########## [{self.tag}] 写锁阻塞（{reason}），线程栈如下 ##########")
        for ident, frame in frames.items():
            print(f"\n===== thread {names.get(ident, ident)} =====")
            print("".join(traceback.format_stack(frame)))
        print(f"########## [{self.tag}] dump 结束 ##########\n", flush=True)

    def _run(self) -> None:
        conn = sqlite3.connect(str(self.db_path), timeout=30, isolation_level=None)
        try:
            conn.execute("PRAGMA busy_timeout=400")     # 探针自己只等 0.4s
            while not self._stop.is_set():
                started = time.monotonic()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    # 写一行拿真锁（app_settings 一定存在；0 行也没关系，锁照样到手）
                    conn.execute("UPDATE app_settings SET value = value WHERE key = 'org_name'")
                    conn.execute("COMMIT")
                    waited = time.monotonic() - started
                    if waited > self.threshold:
                        self._record(f"写入等待 {waited:.2f}s（超过阈值 {self.threshold}s）")
                except sqlite3.OperationalError as exc:
                    waited = time.monotonic() - started
                    self._record(f"写入失败 {waited:.2f}s：{exc}")
                    if self.dump_stacks and self.dumps < 3:
                        self._dump(f"{exc} 等了 {waited:.2f}s")
                finally:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                self._stop.wait(self.interval)
        finally:
            conn.close()


# ------------------------------------------------------------------ 排任务
def enqueue_tasks(db, models, text, args, count: int) -> list[int]:
    """排 count 个任务，返回任务 id。没有可用文件就退化成 embedding 重建。"""
    ids: list[int] = []
    stamp = int(time.time() * 1000)

    usable: list[tuple] = []
    if args.kind in ("auto", "ingest"):
        rows = db.execute(text(
            "select v.id, v.doc_id, d.kb_id, d.filename, v.storage_path "
            "from document_versions v join documents d on d.id = v.doc_id "
            "order by v.id desc limit 40"
        )).fetchall()
        usable = [r for r in rows if r[4] and Path(r[4]).exists()]
        if args.kind == "ingest" and not usable:
            _die("没有可用的原始文件（document_versions.storage_path 都不存在）")

    kb_id = None
    if len(usable) < count:
        kb_id = db.execute(text(
            "select kb_id from chunks group by kb_id order by count(*) desc limit 1"
        )).scalar()
        if kb_id is None:
            kb_id = db.execute(text("select id from knowledge_bases order by id limit 1")).scalar()

    for index in range(count):
        if index < len(usable):
            version_id, doc_id, row_kb, filename, path = usable[index]
            task = models.Task(
                kind=models.TaskKind.ingest_document.value,
                status=models.TaskStatus.queued.value, stage="排队中", progress=0,
                kb_id=row_kb, doc_id=doc_id, doc_version_id=version_id,
                dedupe_key=f"probe:{args.tag}:{stamp}:{index}",
                payload=json.dumps({"doc": filename}, ensure_ascii=False), created_by=1,
            )
            db.add(task)
            db.flush()
            ids.append(task.id)
            _log(args.tag, f"排队入库任务 #{task.id} → kb {row_kb} 文件 {filename}")
        else:
            if kb_id is None:
                _die("库里没有知识库，排不出 embedding 重建任务")
            task = models.Task(
                kind=models.TaskKind.embedding_rebuild.value,
                status=models.TaskStatus.queued.value, stage="排队中", progress=0,
                kb_id=kb_id, dedupe_key=f"probe:{args.tag}:{stamp}:{index}",
                payload=json.dumps({}, ensure_ascii=False), created_by=1,
            )
            db.add(task)
            db.flush()
            ids.append(task.id)
            _log(args.tag, f"排队 embedding 重建任务 #{task.id} → kb {kb_id}")
    db.commit()
    return ids


def _task_states(db, models, text, ids: list[int]) -> list[tuple]:
    if not ids:
        return []
    return db.execute(text(
        "select id, status, stage, progress, substr(coalesce(error,''), 1, 80) "
        f"from tasks where id in ({','.join(map(str, ids))}) order by id"
    )).fetchall()


# ------------------------------------------------------------------ 主流程
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.watch and args.tasks:
        _log(args.tag, "--watch 覆盖 --tasks：只监控，不排任务")
    data_dir, db_path = prepare_data_dir(args)

    sys.path.insert(0, str(BACKEND))
    from sqlalchemy import text as sql_text            # noqa: E402

    from app import models                             # noqa: E402
    from app.db import SessionLocal, init_db           # noqa: E402
    from app.services import llm, worker               # noqa: E402

    init_db()                                          # 老库自动补列（只动副本）
    _log(args.tag, f"库：{db_path}")
    _log(args.tag, f"数据目录：{data_dir}")
    _log(args.tag, f"任务数：{0 if args.watch else args.tasks}  worker 并发：{args.concurrency}")

    with SessionLocal() as db:
        cfg, provider, sig, dim = llm.active_embedding(db)
        _log(args.tag, f"embedding：{getattr(cfg, 'model', None)} @ "
                       f"{getattr(provider, 'base_url', None)} sig={sig} dim={dim}")
        ids = [] if args.watch else enqueue_tasks(db, models, sql_text, args, args.tasks)

    monitor = LockMonitor(db_path, tag=args.tag, interval=args.interval,
                          threshold=args.threshold, dump_stacks=args.dump_stacks)
    monitor.start()

    if ids:
        worker.start()
    deadline = time.monotonic() + max(1.0, args.duration)
    try:
        while time.monotonic() < deadline:
            if ids:
                with SessionLocal() as db:
                    rows = _task_states(db, models, sql_text, ids)
                _log(args.tag, f"任务状态：{rows}")
                if all(r[1] in ("success", "failed", "partial") for r in rows):
                    break
            time.sleep(2 if ids else 1)
    except KeyboardInterrupt:
        _log(args.tag, "收到 Ctrl+C，提前结束")
    finally:
        if ids:
            worker.stop()
        monitor.stop()

    print(f"\n===== [{args.tag}] 结论 =====")
    print(f"阻塞事件 {len(monitor.events)} 条，线程栈 dump {monitor.dumps} 次")
    for line in monitor.events[-20:]:
        print("  " + line)
    if not monitor.events:
        print("  没抓到写锁阻塞：这条路径上的写事务是短的。")
    else:
        print("  抓到了写锁阻塞：用 --dump-stacks 看持锁线程，或配合 scripts/verify/no-orphan.ps1"
              " 排查是否有孤儿 sidecar 在抢锁。")
    return 1 if monitor.events else 0


if __name__ == "__main__":
    raise SystemExit(main())
