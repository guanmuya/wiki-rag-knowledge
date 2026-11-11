"""schema 迁移账本（P1-8）回归用例。

清单口径：没有 alembic、schema 靠 `db.py` 手写「只加列」DDL 演进——既不可追溯，
也没有 downgrade。这里验证账本三件事：
1. 新库/老库都有基线版本号，升级补列逐步记版本（可追溯）；
2. `downgrade()` 真能把补的列退回去，并删掉对应记录；
3. 没有可逆 DDL 的步骤拒绝回退，而不是猜着删数据。
"""
from __future__ import annotations

import sqlite3
import shutil
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from app import migrations, models
from app.db import ensure_sqlite_columns


def _make_old_db(tmp_path: Path, drop_columns: list[tuple[str, str]]) -> tuple[Path, object]:
    """建一个当前 schema 的库，再把指定列删掉，模拟老版本用户的库。"""
    db_path = tmp_path / "old.sqlite3"
    url = f"sqlite:///{db_path.as_posix()}"
    engine = create_engine(url, future=True)
    models.Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        for table, column in drop_columns:
            conn.execute(text(f'ALTER TABLE "{table}" DROP COLUMN "{column}"'))
    engine.dispose()
    return db_path, create_engine(url, future=True)


def _columns(db_path: Path, table: str) -> set[str]:
    with sqlite3.connect(db_path) as raw:
        return {row[1] for row in raw.execute(f"PRAGMA table_info({table})")}


def _fresh_db(tmp_path: Path) -> tuple[Path, object]:
    db_path = tmp_path / "fresh.sqlite3"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)
    models.Base.metadata.create_all(bind=engine)
    return db_path, engine


def test_fresh_database_starts_at_baseline_version(tmp_path):
    db_path, engine = _fresh_db(tmp_path)

    assert ensure_sqlite_columns(engine, models.Base.metadata) == []
    assert migrations.current_version(engine) == migrations.BASELINE_VERSION

    history = migrations.history(engine)
    assert len(history) == 1
    assert history[0]["kind"] == "baseline"
    assert history[0]["applied_at"], "记录必须带时间，否则谈不上追溯"
    engine.dispose()


def test_upgrade_records_versioned_steps(tmp_path):
    """老库补两列 → 账本里应有两条带确切 DDL 与逆 DDL 的记录。"""
    db_path, engine = _make_old_db(tmp_path, [("chunks", "kind"), ("chunks", "meta")])
    assert migrations.current_version(engine) == 0, "升级前不应有账本"

    added = ensure_sqlite_columns(engine, models.Base.metadata)
    assert {"chunks.kind", "chunks.meta"} <= set(added)

    history = migrations.history(engine)
    kinds = [h["kind"] for h in history]
    assert kinds[0] == "baseline", "老库第一次升级先补记基线版本"
    steps = history[1:]
    assert len(steps) == 2, f"补两列应记两条，实际 {steps}"
    assert migrations.current_version(engine) == 3
    assert {s["name"] for s in steps} == {"add_column:chunks.kind", "add_column:chunks.meta"}
    for step in steps:
        assert "ADD COLUMN" in step["detail"], "必须记录执行的确切 DDL"
        assert "DROP COLUMN" in step["revert_sql"], "必须记录可逆 DDL"
    engine.dispose()


def test_repeated_ensure_does_not_grow_ledger(tmp_path):
    db_path, engine = _make_old_db(tmp_path, [("chunks", "kind")])
    ensure_sqlite_columns(engine, models.Base.metadata)
    before = migrations.history(engine)

    assert ensure_sqlite_columns(engine, models.Base.metadata) == []
    assert migrations.history(engine) == before, "重复升级不能重复记账"
    engine.dispose()


def test_downgrade_undoes_added_columns(tmp_path):
    db_path, engine = _make_old_db(tmp_path, [("chunks", "kind"), ("chunks", "meta")])
    ensure_sqlite_columns(engine, models.Base.metadata)
    assert {"kind", "meta"} <= _columns(db_path, "chunks")

    undone = migrations.downgrade(engine, target=migrations.BASELINE_VERSION)

    assert undone == ["add_column:chunks.meta", "add_column:chunks.kind"], "必须按版本逆序回退"
    remaining = _columns(db_path, "chunks")
    assert "kind" not in remaining and "meta" not in remaining
    assert migrations.current_version(engine) == migrations.BASELINE_VERSION
    assert [h["kind"] for h in migrations.history(engine)] == ["baseline"]

    # 退回旧版 schema 之后，再升级一次仍然能补上并重新记账（路径可重复）。
    assert {"chunks.kind", "chunks.meta"} <= set(ensure_sqlite_columns(engine, models.Base.metadata))
    assert migrations.current_version(engine) == 3
    engine.dispose()


def test_downgrade_is_noop_when_already_at_target(tmp_path):
    db_path, engine = _fresh_db(tmp_path)
    ensure_sqlite_columns(engine, models.Base.metadata)
    assert migrations.downgrade(engine, target=migrations.BASELINE_VERSION) == []
    engine.dispose()


def test_downgrade_refuses_step_without_revert_sql(tmp_path):
    """没有可逆 DDL 的步骤必须拒绝回退并说明是谁——不能猜着删数据。"""
    db_path, engine = _fresh_db(tmp_path)
    ensure_sqlite_columns(engine, models.Base.metadata)
    migrations.record(engine, kind="manual", name="needs_human",
                      detail="改类型这类破坏性变更只记录、不自动回退")

    with pytest.raises(migrations.MigrationError) as excinfo:
        migrations.downgrade(engine, target=migrations.BASELINE_VERSION)
    assert "needs_human" in str(excinfo.value)
    engine.dispose()


def test_repo_database_can_be_traced_after_upgrade(tmp_path):
    """真实场景：仓库里已有的 data/db/app.sqlite3（若存在）升级后应带完整账本。"""
    repo_db = Path(__file__).resolve().parent.parent.parent / "data" / "db" / "app.sqlite3"
    if not repo_db.exists():
        return
    copy = tmp_path / "copy.sqlite3"
    shutil.copy2(repo_db, copy)

    engine = create_engine(f"sqlite:///{copy.as_posix()}", future=True)
    ensure_sqlite_columns(engine, models.Base.metadata)
    history = migrations.history(engine)
    assert history and history[0]["kind"] == "baseline"
    with engine.begin() as conn:
        conn.execute(text("SELECT id, kind, meta FROM chunks LIMIT 1"))
    engine.dispose()
