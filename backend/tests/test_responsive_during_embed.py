"""慢速向量化期间，API 读接口必须保持响应（用户看到「后端超时」的回归）。

场景：本地 Ollama 一批要好几秒，worker 在向量化；此时用户切到「检索调试」/「关于」，
前端 4 秒超时就会报「后端不可用」。本测试断言：
- 向量化进行中，`GET /api/meta`、`GET /api/kbs` 仍然 < 1.5s 返回；
- 同时验证写接口（建会话）也不会被卡住。
"""
from __future__ import annotations

import threading
import time
import uuid

from sqlalchemy import text

from app import models
from app.db import SessionLocal
from app.services import llm, worker

DOC = """# 慢速向量化

## 章节一

这里是一段用来触发向量化的正文，重复几遍让分块多一点。

## 章节二

再来一段正文，确保至少有一个批次需要调用 embedding。
"""


def _upload(app_client, admin_headers) -> int:
    kb = app_client.post("/api/kbs", headers=admin_headers,
                         json={"name": f"响应-{uuid.uuid4().hex[:6]}", "description": "",
                               "mode": "basic"}).json()
    up = app_client.post(f"/api/kbs/{kb['id']}/documents", headers=admin_headers,
                         files=[("files", ("slow.md", (DOC * 6).encode(), "text/markdown"))])
    assert up.status_code == 200, up.text
    return kb["id"]


def test_read_endpoints_stay_fast_during_slow_embedding(app_client, admin_headers, monkeypatch):
    _upload(app_client, admin_headers)

    in_embed = threading.Event()
    release = threading.Event()

    def slow_embed(db, texts, **kwargs):
        in_embed.set()
        release.wait(timeout=15)          # 卡住向量化，模拟 Ollama 慢
        return [llm.hash_embed(t) for t in texts]

    monkeypatch.setattr(llm, "embed", slow_embed)

    errors: list[BaseException] = []

    def run_worker():
        try:
            while worker.run_once("slow-test"):
                pass
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    thread = threading.Thread(target=run_worker, name="slow-worker")
    thread.start()
    try:
        assert in_embed.wait(timeout=20), "worker 没有进入向量化"

        t0 = time.monotonic()
        r = app_client.get("/api/meta")
        meta_ms = (time.monotonic() - t0) * 1000
        assert r.status_code == 200, r.text

        t0 = time.monotonic()
        r = app_client.get("/api/kbs", headers=admin_headers)
        kbs_ms = (time.monotonic() - t0) * 1000
        assert r.status_code == 200, r.text

        t0 = time.monotonic()
        r = app_client.post("/api/conversations", headers=admin_headers,
                            json={"title": "向量化期间建会话", "kb_ids": []})
        write_ms = (time.monotonic() - t0) * 1000
        assert r.status_code == 201, r.text

        assert meta_ms < 1500, f"/api/meta 在向量化期间耗时 {meta_ms:.0f}ms"
        assert kbs_ms < 1500, f"/api/kbs 在向量化期间耗时 {kbs_ms:.0f}ms"
        assert write_ms < 1500, f"写接口在向量化期间耗时 {write_ms:.0f}ms"
    finally:
        release.set()
        thread.join(timeout=60)

    assert not errors, errors


def test_wiki_section_embedding_does_not_hold_lock(app_client, admin_headers, monkeypatch):
    """Wiki 生成里的章节向量化同样不能占着写锁（这是第二个长锁点）。

    `build_wiki` 每页的顺序曾经是：写证据（delete 旧证据 → 开启写事务）→
    `_index_sections()`（逐节 embed）→ commit。写事务于是横跨整段向量化，
    另一个 worker 的批量提交就会 `database is locked`。
    这里卡住第 2 次 embed（第 1 次是分块向量化，第 2 次是章节向量化），
    断言此时写库仍然 < 1.2s。
    """
    kb = app_client.post("/api/kbs", headers=admin_headers,
                         json={"name": f"wiki-响应-{uuid.uuid4().hex[:6]}", "description": "",
                               "mode": "wiki"}).json()
    up = app_client.post(f"/api/kbs/{kb['id']}/documents", headers=admin_headers,
                         files=[("files", ("wiki.md", DOC.encode(), "text/markdown"))])
    assert up.status_code == 200, up.text

    calls = {"n": 0}
    in_section_embed = threading.Event()
    release = threading.Event()

    def slow_embed(db, texts, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:          # 第 2 次调用 = build_wiki 里的章节向量化
            in_section_embed.set()
            release.wait(timeout=20)
        return [llm.hash_embed(t) for t in texts]

    monkeypatch.setattr(llm, "embed", slow_embed)

    errors: list[BaseException] = []

    def run_worker():
        try:
            while worker.run_once("wiki-lock-test"):
                pass
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    thread = threading.Thread(target=run_worker, name="wiki-lock-worker")
    thread.start()
    elapsed = None
    try:
        assert in_section_embed.wait(timeout=40), "没有走到章节向量化（第 2 次 embed）"
        other = SessionLocal()
        try:
            other.execute(text("PRAGMA busy_timeout=800"))
            started = time.monotonic()
            other.add(models.AppSetting(key=f"wiki-lock-{uuid.uuid4().hex[:8]}", value="1"))
            other.commit()
            elapsed = time.monotonic() - started
        finally:
            other.close()
    finally:
        release.set()
        thread.join(timeout=90)

    assert not errors, errors
    assert elapsed is not None and elapsed < 1.2, f"章节向量化期间写入被写锁挡住 {elapsed:.2f}s"
