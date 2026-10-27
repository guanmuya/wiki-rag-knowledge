"""数据库会话管理。SQLite 单文件，WAL 模式，配合多线程 Worker。"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from . import config

log = logging.getLogger("wrk.db")

connect_args = {"check_same_thread": False} if config.DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(
    config.DATABASE_URL,
    connect_args=connect_args,
    future=True,
    pool_pre_ping=True,
)

if config.DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _rec):  # pragma: no cover - 驱动回调
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=15000")
        cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


class Base(DeclarativeBase):
    pass


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """给 Worker / 脚本使用的独立事务作用域。"""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db() -> None:
    from . import models  # noqa: F401  确保模型注册
    Base.metadata.create_all(bind=engine)
    ensure_sqlite_columns(engine, Base.metadata)


def ensure_sqlite_columns(bind, metadata) -> list[str]:
    """给已存在的表补齐新增列，返回实际补上的 "表.列" 列表。

    为什么必须有这一步：`create_all` 只建缺失的**表**，不会给已存在的表
    加列。桌面版升级后，用户 AppData 里的老库缺少后来新增的列
    （如 `chunks.kind` / `chunks.meta`），任何查询都会
    `OperationalError: no such column`，直接 500。

    这里只处理「加列」这一种情况——改类型、加约束等破坏性变更不做自动处理，
    宁可启动时报错让人工迁移，也不要静默改坏数据。

    bind / metadata 作为参数传入，是为了能在测试里对独立的临时库验证，
    不依赖全局单例。
    """
    if bind.dialect.name != "sqlite":
        return []
    from sqlalchemy import inspect, text

    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names())
    added: list[str] = []

    with bind.begin() as conn:
        for table in metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            current = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in current:
                    continue
                ddl = _sqlite_add_column_ddl(bind, table.name, column)
                if not ddl:
                    continue
                try:
                    conn.execute(text(ddl))
                    added.append(f"{table.name}.{column.name}")
                except Exception as exc:  # pragma: no cover - 依赖具体数据
                    log.warning("补列失败 %s.%s：%s", table.name, column.name, exc)

    if added:
        log.warning("数据库已补齐缺失列：%s", ", ".join(added))
    return added


def _sqlite_add_column_ddl(bind, table_name: str, column) -> str | None:
    """生成 SQLite 的 ADD COLUMN 语句。

    SQLite 限制：新增列不能是 UNIQUE、不能有非空无默认值、
    默认值不能是表达式。这里按此规则保守处理。
    """
    from sqlalchemy import String, Text, Integer, Boolean, Float, DateTime

    try:
        col_type = column.type.compile(dialect=bind.dialect)
    except Exception:
        return None

    default = column.default.arg if column.default is not None else None
    if callable(default):
        # 例如 default=utcnow，SQLite 无法写进 DDL，改用运行时默认
        default = None

    if isinstance(column.type, Boolean):
        default_sql = "0" if not default else "1"
    elif isinstance(column.type, (Integer, Float)):
        default_sql = str(default) if default is not None else "0"
    elif isinstance(column.type, DateTime):
        default_sql = None
    elif isinstance(column.type, (String, Text)):
        default_sql = "'{}'".format(str(default).replace("'", "''")) if default is not None else None
    else:
        default_sql = None

    parts = [f'ALTER TABLE "{table_name}" ADD COLUMN "{column.name}" {col_type}']
    if default_sql is not None:
        parts.append(f"DEFAULT {default_sql}")
    return " ".join(parts)
