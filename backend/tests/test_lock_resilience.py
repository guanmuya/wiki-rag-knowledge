"""写锁韧性回归：外部持锁 / 进度写失败 / 大文件上传都不该把软件卡死。

桌面版真实故障：SQLite 写锁被长时间占用，而连接上写着 `busy_timeout=15000`，
于是**所有**写请求排队等 15 秒，用户看到的就是「embedding 解析文档时整个软件卡住」。
光缩短超时不够，还要保证三件事，这里各写一条回归：

1. 别的进程拿着写锁时，读接口照常（WAL 读不该被写锁挡住），
   写接口必须在预算内给出结果（成功或可读错误），而不是卡到 15s。
2. 进度写（任务 stage/progress 的 commit）遇到 `database is locked` 必须是
   best-effort：只丢一次进度刷新，任务本身要继续跑完，不能因此判 failed。
   旧实现里 `_set_progress()` 直接 `db.commit()`，一次瞬时锁冲突就能让一份
   已经解析完的文档变成「失败」。
3. 上传大文件时，写事务不能横跨「文件落盘 + 校验」。旧实现先 `INSERT` 文档行
   （拿写锁）再 `write_bytes()`，8MB 落盘期间所有写请求都排队。

设计原则：只用公开接口（conftest 的 `app_client` / `admin_headers` + TestClient），
不依赖 backend/app 里正在被并行重构的内部新 API，所以这些用例在队友改代码期间
依然有意义。
"""
from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from pathlib import Path

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app import config, models, security
from app.db import SessionLocal
from app.services import ingest, llm, worker

DOC = """# 写锁韧性

## 第一节

这段正文用来触发一次完整的入库：解析 → 分块 → 向量化 → 建索引。

## 第二节

再多一段，保证至少有一个批次需要调用 embedding。
"""

# 需求是「写请求不要卡 15s」。交互连接的 busy_timeout=3000ms，SQLite 的 busy
# handler 还会多溢出 ~0.4s，实测约 3.37s 才返回；再留 1s 给 CI 抖动。
# 这条断言要挡的是「又退回 15s 卡死」，不是死抠 3.0 —— 预算贴着实现只会随机红。
WRITE_BUDGET_S = 4.5
READ_BUDGET_S = 1.5
UPLOAD_WRITE_BUDGET_S = 1.0


# ------------------------------------------------------------------ 小工具
def _new_kb(app_client, admin_headers, mode: str = "basic") -> int:
    r = app_client.post("/api/kbs", headers=admin_headers,
                        json={"name": f"锁韧性-{uuid.uuid4().hex[:6]}",
                              "description": "", "mode": mode})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _upload_doc(app_client, admin_headers, kb_id: int, name: str = "lock.md",
                data: bytes | None = None) -> dict:
    r = app_client.post(f"/api/kbs/{kb_id}/documents", headers=admin_headers,
                        files=[("files", (name, data if data is not None else DOC.encode(),
                                          "text/markdown"))])
    assert r.status_code == 200, r.text
    body = r.json()
    assert not body["errors"], body["errors"]
    return body["items"][0]


def _task_row(task_id: int) -> models.Task | None:
    db = SessionLocal()
    try:
        return db.get(models.Task, task_id)
    finally:
        db.close()


def _finish_task(task_id: int, reason: str = "测试清理：不留给后续用例") -> None:
    """把测试里排出来的任务标失败，避免后面的用例真的去解析它。"""
    db = SessionLocal()
    try:
        task = db.get(models.Task, task_id)
        if task and task.status in (models.TaskStatus.queued.value,
                                    models.TaskStatus.running.value):
            task.status = models.TaskStatus.failed.value
            task.stage = "失败"
            task.error = reason
            db.add(task)
            db.commit()
    finally:
        db.close()


def _assert_db_write_locked(db_path: Path) -> None:
    """确认此刻确实有别人握着写锁——否则后面的计时断言毫无意义。"""
    probe = sqlite3.connect(str(db_path), timeout=0, isolation_level=None)
    try:
        with pytest.raises(sqlite3.OperationalError):
            probe.execute("BEGIN IMMEDIATE")
    finally:
        probe.close()


class _HoldWriteLock:
    """另开一个连接 `BEGIN IMMEDIATE` 把写锁占住（WAL 下就是 RESERVED 锁）。

    用真正的第二个 sqlite3 连接，而不是在 SQLAlchemy 里加锁：要复现的是
    「另一个进程（孤儿 sidecar）拿着写锁」这个现场。
    """

    def __init__(self, db_path: Path, seconds: float = 5.0):
        self.db_path = db_path
        self.seconds = seconds
        self.ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="external-write-lock",
                                        daemon=True)

    def _run(self) -> None:
        conn = sqlite3.connect(str(self.db_path), timeout=5.0, isolation_level=None)
        try:
            conn.execute("PRAGMA busy_timeout=2000")
            conn.execute("BEGIN IMMEDIATE")      # 立刻拿到写锁
            self.ready.set()
            time.sleep(self.seconds)
            conn.execute("ROLLBACK")
        finally:
            conn.close()

    def __enter__(self):
        self._thread.start()
        assert self.ready.wait(timeout=10), "外部连接没拿到写锁"
        _assert_db_write_locked(self.db_path)
        return self

    def __exit__(self, *exc_info):
        self._thread.join(timeout=self.seconds + 10)
        return False


def _timed_call(fn, timeout: float) -> dict:
    """在线程里跑一次请求并计时，超时也不会把整个测试挂死。"""
    result: dict = {}

    def run() -> None:
        started = time.monotonic()
        try:
            resp = fn()
            result["status"] = resp.status_code
            result["body"] = resp.text[:400]
        except BaseException as exc:            # noqa: BLE001 - 原样带回主线程判断
            result["exc"] = exc
        result["elapsed"] = time.monotonic() - started

    thread = threading.Thread(target=run, name="timed-call", daemon=True)
    thread.start()
    thread.join(timeout=timeout)
    result["alive"] = thread.is_alive()
    return result


# ------------------------------------------------------------------ 用例 1
def test_read_and_write_bounded_while_external_lock_held(app_client, admin_headers):
    """别人持写锁 5s：读要照常，写要在预算内给结果（不是卡到 15s）。"""
    db_path = Path(config.DB_PATH)
    assert db_path.exists(), f"测试库不存在：{db_path}"

    with _HoldWriteLock(db_path, seconds=5.0):
        # 读接口：WAL 下读不被写锁挡，必须在 1.5s 内返回
        started = time.monotonic()
        r = app_client.get("/api/kbs", headers=admin_headers)
        read_s = time.monotonic() - started
        assert r.status_code == 200, r.text
        assert read_s < READ_BUDGET_S, (
            f"持锁期间 GET /api/kbs 耗时 {read_s:.2f}s（预算 {READ_BUDGET_S}s）"
        )

        # 写接口：要么成功，要么给出可读错误；两者都必须在预算内
        res = _timed_call(
            lambda: app_client.post("/api/conversations", headers=admin_headers,
                                    json={"title": "持锁期间建会话", "kb_ids": []}),
            timeout=WRITE_BUDGET_S + 6.0,
        )

    assert not res.get("alive"), (
        f"持锁期间写请求 {WRITE_BUDGET_S + 6.0:.1f}s 后仍未返回——又卡在写锁上了"
    )
    assert "exc" not in res, (
        f"持锁期间写请求抛出未处理异常（前端只能看到 500，读不出原因）：{res.get('exc')!r}"
    )
    elapsed = res["elapsed"]
    assert elapsed < WRITE_BUDGET_S, (
        f"持锁期间 POST /api/conversations 耗时 {elapsed:.2f}s"
        f"（预算 {WRITE_BUDGET_S}s，旧实现要等 busy_timeout=15s）"
    )
    if res["status"] >= 400:
        # 「可读错误」= 有 detail 的 HTTP 响应，而不是裸 500 或超时
        assert "detail" in res["body"], (
            f"写请求失败但没给可读原因：HTTP {res['status']} {res['body']}"
        )


# ------------------------------------------------------------------ 用例 2
def test_progress_write_lock_does_not_fail_task(app_client, admin_headers, monkeypatch):
    """进度写撞上 database is locked 时，任务不能因此判失败（best-effort）。

    模拟方式：在 `ingest.ingest_version()` 执行期间，让**第一次** commit
    抛 `OperationalError: database is locked`——这正是进度写 `_set_progress()`
    在真实锁冲突时遇到的东西。挂在 commit 这一层而不是 `_set_progress` 本身，
    是因为修法可能把 try/except 放进 `_set_progress` 内部（或者换成独立的进度
    session），只有从 commit 打进去才能验证「到底有没有兜住」。
    """
    kb_id = _new_kb(app_client, admin_headers)
    item = _upload_doc(app_client, admin_headers, kb_id, "progress-lock.md")
    task_id = item["task_id"]

    # 向量化走离线哈希，测试不依赖本地 Ollama
    monkeypatch.setattr(llm, "embed", lambda db, texts, **kw: [llm.hash_embed(t) for t in texts])

    state = {"armed": False, "raised": False, "commits": 0}
    real_commit = Session.commit
    real_ingest = ingest.ingest_version

    def flaky_commit(self):
        state["commits"] += 1
        if state["armed"] and not state["raised"]:
            state["raised"] = True
            # 不在这里 rollback：真实锁冲突后事务就是「需要 rollback」的状态，
            # 兜住的那一方必须自己 rollback 才能继续，这条路径也要被验证。
            raise OperationalError("database is locked", None, Exception("database is locked"))
        return real_commit(self)

    def armed_ingest(db, task, **kwargs):
        if task.id == task_id:
            state["armed"] = True
        try:
            return real_ingest(db, task, **kwargs)
        finally:
            state["armed"] = False

    monkeypatch.setattr(Session, "commit", flaky_commit)
    monkeypatch.setattr(ingest, "ingest_version", armed_ingest)

    deadline = time.monotonic() + 90
    try:
        while time.monotonic() < deadline:
            task = _task_row(task_id)
            if task and task.status in (models.TaskStatus.success.value,
                                        models.TaskStatus.failed.value,
                                        models.TaskStatus.partial.value):
                break
            if not worker.run_once("lock-resilience"):
                time.sleep(0.05)
        else:
            raise AssertionError(f"任务 #{task_id} 90s 内没跑完")

        assert state["raised"], (
            "没有抓到任何一次进度写 commit——进度刷新可能被整个删掉了，"
            "本用例失去意义（界面会一直停在 0%）"
        )

        task = _task_row(task_id)
        assert task is not None
        assert task.status == models.TaskStatus.success.value, (
            f"进度写失败把任务判成了 {task.status}：{task.error}\n"
            "进度写必须 best-effort（失败只记日志/降级，不能影响任务结果）"
        )
        assert task.progress == 100, f"任务成功但进度停在 {task.progress}"

        db = SessionLocal()
        try:
            version = db.get(models.DocumentVersion, item["version_id"])
            doc = db.get(models.Document, item["doc_id"])
        finally:
            db.close()
        assert version is not None and version.status == models.TaskStatus.success.value
        assert doc is not None and doc.status != models.KbStatus.failed.value
    finally:
        monkeypatch.undo()


# ------------------------------------------------------------------ 用例 3
def test_upload_does_not_hold_write_lock_during_file_write(app_client, admin_headers,
                                                           monkeypatch):
    """上传 8MB 期间并发写必须 < 1s：写事务不能横跨文件落盘 + 校验。

    为了不让用例靠「8MB 恰好写得慢」碰运气，这里把落盘/校验拖慢 1.5s：
    只要上传路径在写事务里做文件 IO，并发写就一定会等到它结束。
    """
    kb_id = _new_kb(app_client, admin_headers)

    in_io = threading.Event()
    fired = {"once": False}

    def _slow_once():
        if not fired["once"]:
            fired["once"] = True
            in_io.set()
            time.sleep(1.5)          # 模拟大文件落盘 + 校验耗时

    real_write_bytes = Path.write_bytes
    real_checksum = security.checksum_file

    def slow_write_bytes(self, data, *a, **kw):
        if str(config.FILES_DIR) in str(self):
            _slow_once()
        return real_write_bytes(self, data, *a, **kw)

    def slow_checksum(path):
        _slow_once()
        return real_checksum(path)

    monkeypatch.setattr(Path, "write_bytes", slow_write_bytes)
    monkeypatch.setattr("app.security.checksum_file", slow_checksum)

    big = ("# 大文件\n\n" + "锁韧性测试正文。" * 64 + "\n") * 12000
    payload = big.encode()[: 8 * 1024 * 1024]
    upload_result: dict = {}

    def do_upload() -> None:
        try:
            upload_result["resp"] = app_client.post(
                f"/api/kbs/{kb_id}/documents", headers=admin_headers,
                files=[("files", ("big.md", payload, "text/markdown"))])
        except BaseException as exc:            # noqa: BLE001
            upload_result["exc"] = exc

    uploader = threading.Thread(target=do_upload, name="big-upload", daemon=True)
    uploader.start()
    try:
        assert in_io.wait(timeout=20), "上传路径没有走到落盘/校验（实现变了？）"

        res = _timed_call(
            lambda: app_client.post("/api/conversations", headers=admin_headers,
                                    json={"title": "上传期间建会话", "kb_ids": []}),
            timeout=UPLOAD_WRITE_BUDGET_S + 6.0,
        )
    finally:
        uploader.join(timeout=60)

    assert "exc" not in upload_result, f"上传本身失败了：{upload_result.get('exc')!r}"
    resp = upload_result["resp"]
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert items, resp.text
    _finish_task(items[0]["task_id"])

    assert not res.get("alive"), "上传 8MB 期间写请求一直没返回"
    assert "exc" not in res, f"上传期间写请求抛异常：{res.get('exc')!r}"
    assert res["status"] < 400, f"上传期间写请求失败：{res['status']} {res['body']}"
    assert res["elapsed"] < UPLOAD_WRITE_BUDGET_S, (
        f"上传 8MB 期间写请求耗时 {res['elapsed']:.2f}s"
        f"（预算 {UPLOAD_WRITE_BUDGET_S}s：写事务不该横跨文件落盘）"
    )
