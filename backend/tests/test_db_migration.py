"""老库升级：缺少新增列时必须自动补齐，而不是启动后查询就 500。

桌面版用户升级后，AppData 里的库是旧 schema。`create_all` 只建缺失的表、
不会给已存在的表加列，所以这里模拟「旧库」——建表后手动删掉新列，
再跑迁移，验证能自愈。
"""
from __future__ import annotations

import sqlite3

from sqlalchemy import create_engine, inspect, text

from app import models
from app.db import ensure_sqlite_columns


def _make_old_db(tmp_path, drop_columns: list[tuple[str, str]]) -> tuple:
    """建一个当前 schema 的库，再把指定列删掉，模拟旧版本。"""
    db_path = tmp_path / "old.sqlite3"
    url = f"sqlite:///{db_path.as_posix()}"
    engine = create_engine(url, future=True)
    models.Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        for table, column in drop_columns:
            conn.execute(text(f'ALTER TABLE "{table}" DROP COLUMN "{column}"'))
    engine.dispose()
    return db_path, create_engine(url, future=True)


def _columns(db_path, table: str) -> set[str]:
    with sqlite3.connect(db_path) as raw:
        return {row[1] for row in raw.execute(f"PRAGMA table_info({table})")}


def test_missing_chunk_columns_are_added(tmp_path):
    """chunks 缺 kind / meta —— 这是桌面版升级必踩的路径。"""
    db_path, engine = _make_old_db(tmp_path, [("chunks", "kind"), ("chunks", "meta")])
    assert "kind" not in _columns(db_path, "chunks"), "构造旧库失败"

    added = ensure_sqlite_columns(engine, models.Base.metadata)

    cols = _columns(db_path, "chunks")
    assert "kind" in cols, "kind 列未被补齐，旧库升级后查询会 500"
    assert "meta" in cols, "meta 列未被补齐"
    assert "chunks.kind" in added and "chunks.meta" in added

    # 旧数据必须仍然可查，且新列有合理默认值
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO knowledge_bases (name, description, mode, status, status_detail, "
            "embed_signature, embed_dim, created_at) "
            "VALUES ('迁移测试库', '', 'basic', 'ready', '', '', 0, '2026-01-01 00:00:00')"))
        conn.execute(text(
            "INSERT INTO documents (kb_id, filename, suffix, size_bytes, mime, status, "
            "status_detail, deleted, created_at) "
            "VALUES (1, 'a.md', '.md', 10, 'text/markdown', 'ready', '', 0, '2026-01-01 00:00:00')"))
        conn.execute(text(
            "INSERT INTO document_versions (doc_id, version_no, storage_path, checksum, "
            "status, status_detail, text_chars, chunk_count, created_at) "
            "VALUES (1, 1, '/tmp/a', '', 'success', '', 10, 1, '2026-01-01 00:00:00')"))
        conn.execute(text(
            "INSERT INTO chunks (kb_id, doc_id, doc_version_id, ordinal, text, heading, "
            "locator, keywords, dim, embed_signature, chunk_hash, created_at) "
            "VALUES (1, 1, 1, 0, '旧数据内容', '', '', '', 0, '', 'h1', '2026-01-01 00:00:00')"))
        row = conn.execute(text("SELECT text, kind, meta FROM chunks")).one()
    assert row[0] == "旧数据内容"
    assert row[1] == "text", "kind 应带默认值 text"
    assert row[2] == "{}", "meta 应带默认值 {}"
    engine.dispose()


def test_migration_is_idempotent(tmp_path):
    """重复执行不应报错，也不应重复加列。"""
    db_path, engine = _make_old_db(tmp_path, [("chunks", "kind")])

    first = ensure_sqlite_columns(engine, models.Base.metadata)
    second = ensure_sqlite_columns(engine, models.Base.metadata)
    third = ensure_sqlite_columns(engine, models.Base.metadata)

    assert "chunks.kind" in first
    assert second == [] and third == [], "重复执行不应再次加列"
    assert "kind" in _columns(db_path, "chunks")
    engine.dispose()


def test_multiple_missing_columns_across_tables(tmp_path):
    """多个表同时缺列时应全部补齐，不能只补第一个。"""
    db_path, engine = _make_old_db(tmp_path, [
        ("chunks", "kind"), ("chunks", "meta"),
        ("messages", "latency_ms"),
    ])
    added = ensure_sqlite_columns(engine, models.Base.metadata)
    assert "chunks.kind" in added and "chunks.meta" in added
    assert "messages.latency_ms" in added
    assert "latency_ms" in _columns(db_path, "messages")
    engine.dispose()


def test_fresh_database_needs_no_migration(tmp_path):
    """新库走 create_all 已经完整，迁移应是空操作。"""
    db_path = tmp_path / "fresh.sqlite3"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)
    models.Base.metadata.create_all(bind=engine)

    added = ensure_sqlite_columns(engine, models.Base.metadata)
    assert added == [], f"新库不应补列，实际补了 {added}"

    insp = inspect(engine)
    assert "chunks" in insp.get_table_names()
    engine.dispose()


def test_repo_database_upgrades_cleanly(tmp_path):
    """真实场景：仓库里已有的 data/db/app.sqlite3（若存在）应能被安全升级。"""
    from pathlib import Path

    repo_db = Path(__file__).resolve().parent.parent.parent / "data" / "db" / "app.sqlite3"
    if not repo_db.exists():
        return
    import shutil
    copy = tmp_path / "copy.sqlite3"
    shutil.copy2(repo_db, copy)

    engine = create_engine(f"sqlite:///{copy.as_posix()}", future=True)
    ensure_sqlite_columns(engine, models.Base.metadata)
    # 升级后能真正查 Chunk，不报 no such column
    with engine.begin() as conn:
        conn.execute(text("SELECT id, kind, meta FROM chunks LIMIT 1"))
    engine.dispose()
