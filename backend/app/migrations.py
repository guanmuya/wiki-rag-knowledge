"""schema 版本账本：给「只加列」的自动迁移配版本号与回退能力（P1-8）。

背景：本项目**没有 alembic**（桌面版不想多一个外部工具依赖），老库升级一直靠
`db.ensure_sqlite_columns()` 现场 `ALTER TABLE ADD COLUMN`。这套做法能跑，但有两个
缺口，正是体检清单 P1-8 记的那條：

1. **不可追溯**：升级完只知道「补过列」，不知道用户那个库现在到底是哪一版 schema，
   出了问题无法回答「这台机器补过什么」。
2. **不可回退**：补错了列只能人工处理，也没有任何记录能说明怎么退。

这里补一张 `schema_migrations` 账本，每步一行：版本号、类型、目标、**执行的确切
DDL**、**可逆 DDL**、时间。`downgrade()` 按版本逆序执行可逆 DDL 并删掉对应行。

刻意的边界（与 DESIGN.md §4.4 的保守取舍一致）：
- 账本只做「记录 + 按记录回退」，不改变 `create_all()` 建表 / 补列自愈的分工；
- **没有可逆 DDL 的步骤拒绝回退**，抛错让人工处理，而不是猜着删数据；
- 账本写不进去不影响升级本身（`db.ensure_sqlite_columns` 会吞掉并告警）——
  记录是「有最好」，不能因为它让用户升级失败。
"""
from __future__ import annotations

import datetime as _dt
import logging

from sqlalchemy import text

log = logging.getLogger("wrk.migrations")

TABLE = "schema_migrations"

# 基线版本：`create_all()` 建出来的第一版 schema。老库（升级前没有账本）第一次
# 走迁移时补记这一行，于是「这个库从哪一版开始」有据可查。
BASELINE_VERSION = 1
BASELINE_NAME = "baseline"

_CREATE_TABLE = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    version    INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL DEFAULT '',
    name       TEXT NOT NULL DEFAULT '',
    detail     TEXT NOT NULL DEFAULT '',
    revert_sql TEXT NOT NULL DEFAULT '',
    applied_at TEXT NOT NULL DEFAULT ''
)
"""


class MigrationError(RuntimeError):
    """无法安全回退（例如没有可逆 DDL）。"""


def _now() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ensure_table(bind) -> None:
    """建账本表（幂等）。用原始 SQL 而不是 ORM 模型：账本是基础设施，
    不该混进业务 `Base.metadata`，否则 `create_all` 的语义会被它牵连。"""
    with bind.begin() as conn:
        conn.execute(text(_CREATE_TABLE))


def history(bind) -> list[dict]:
    """全部迁移记录，按版本升序。"""
    ensure_table(bind)
    with bind.begin() as conn:
        rows = conn.execute(text(
            f"SELECT version, kind, name, detail, revert_sql, applied_at "
            f"FROM {TABLE} ORDER BY version"
        )).all()
    return [
        {"version": r[0], "kind": r[1], "name": r[2], "detail": r[3],
         "revert_sql": r[4], "applied_at": r[5]}
        for r in rows
    ]


def current_version(bind) -> int:
    """当前库的 schema 版本；0 表示还没有账本记录。"""
    rows = history(bind)
    return max((r["version"] for r in rows), default=0)


def record(bind, *, kind: str, name: str, detail: str = "",
           revert_sql: str = "", version: int | None = None) -> int:
    """记一步迁移，返回写入的版本号（默认取当前版本 + 1）。"""
    ensure_table(bind)
    with bind.begin() as conn:
        if version is None:
            latest = conn.execute(text(f"SELECT MAX(version) FROM {TABLE}")).scalar()
            version = int(latest or 0) + 1
        conn.execute(text(
            f"INSERT INTO {TABLE} (version, kind, name, detail, revert_sql, applied_at) "
            f"VALUES (:v, :k, :n, :d, :r, :t)"
        ), {"v": version, "k": kind, "n": name, "d": detail,
            "r": revert_sql, "t": _now()})
    return version


def record_baseline(bind) -> bool:
    """账本为空时补记基线版本；返回是否写入。

    第一次升级老库时账本是空的——补记基线，后面的补列步骤才谈得上「版本 2、3」。
    """
    if history(bind):
        return False
    record(bind, kind="baseline", name=BASELINE_NAME, version=BASELINE_VERSION,
           detail="create_all() 建表后的基线版本；本库此前没有迁移账本")
    log.info("已建立 schema 迁移账本基线：v%d", BASELINE_VERSION)
    return True


def downgrade(bind, target: int = BASELINE_VERSION) -> list[str]:
    """回退到 `target` 版本，返回被回退的步骤名（按回退顺序）。

    只回退 `version > target` 的记录，且每条都必须带可逆 DDL：
    没有就抛 `MigrationError`，绝不做「删表猜数据」这种操作。
    """
    steps = [r for r in history(bind) if r["version"] > target]
    if not steps:
        return []
    undone: list[str] = []
    with bind.begin() as conn:
        for step in sorted(steps, key=lambda r: -r["version"]):
            revert = (step["revert_sql"] or "").strip()
            if not revert:
                raise MigrationError(
                    f"迁移 v{step['version']}（{step['name']}）没有可逆 DDL，"
                    f"无法自动回退，请人工处理")
            conn.execute(text(revert))
            conn.execute(text(f"DELETE FROM {TABLE} WHERE version = :v"),
                         {"v": step["version"]})
            undone.append(step["name"])
    log.warning("已回退 %d 步 schema 迁移到 v%d：%s", len(undone), target,
                ", ".join(undone))
    return undone
