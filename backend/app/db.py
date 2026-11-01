"""数据库会话管理。SQLite 单文件，WAL 模式，配合多线程 Worker。

这里有三件事是为了同一类真实故障（「用 embedding 模型解析文档时整个软件卡住」）：

1. **写锁等待分档**。`PRAGMA busy_timeout` 是连接级状态，而交互请求和后台
   worker 对「等锁」的容忍度差一个数量级：界面宁可 3 秒内给一句「数据库正忙」，
   后台任务则值得等 30 秒把活干完。旧版一刀切 15000ms，等于让每一次外部持锁
   都变成「UI 卡 15 秒」。
2. **写锁错误可重试**。`OperationalError: database is locked` 是环境问题，
   不是业务错误，用 `retry_on_locked` / `run_with_lock_retry` 做指数退避重试。
3. **进度写不致命**。worker 的 stage/progress 更新失败只该丢一次进度，
   不该把整条任务判失败（见 `WorkerSession.commit`）。
"""
from __future__ import annotations

import functools
import logging
import random
import time
from contextlib import contextmanager
from typing import Callable, Iterator, TypeVar

from fastapi import HTTPException
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from . import config

log = logging.getLogger("wrk.db")

T = TypeVar("T")

IS_SQLITE = config.DATABASE_URL.startswith("sqlite")

# SQLite 拿不到写锁时的报错文本：库级是 "database is locked"，
# 表级（共享缓存模式）是 "database table is locked"。别的 OperationalError
# （缺列、语法错、磁盘满）重试一万次也没用，必须排除掉。
LOCK_ERROR_MARKERS = ("database is locked", "database table is locked")

# 交互请求撞上写锁时给用户看的话：说明「现在忙」，而不是「出错了」。
DB_BUSY_DETAIL = "数据库正忙（有后台任务正在写入），请稍后重试"


def is_lock_error(exc: BaseException | None) -> bool:
    """判断异常是不是「写锁被别人占着」——只有这类错误重试才有意义。"""
    if not isinstance(exc, OperationalError):
        return False
    msg = str(getattr(exc, "orig", None) or exc).lower()
    return any(marker in msg for marker in LOCK_ERROR_MARKERS)


connect_args = {"check_same_thread": False} if IS_SQLITE else {}


def _make_engine(role: str):
    """按角色建 engine：交互（3s 等锁）与 worker（30s 等锁）。

    为什么必须是两个 engine，而不是「一个 engine + 会话里改 PRAGMA」：
    `Session.rollback()` 一结束事务就把连接还给了池子，之后再执行 PRAGMA 拿到
    的是**另一条**连接——真正带着 30s 的那条还留在池里（实测：worker 跑完，
    池里必有一条连接是 30000，下一个交互请求又卡 30 秒）。PRAGMA 是连接级状态，
    想让它稳定生效，就得让「这条连接归谁用」在池的层面分开。

    连接池的 checkin 事件再把等待时间复位成本 engine 的默认值：
    会话中途可能临时改过（进度写 1.5s、审计 0.5s），不复位就会漏给下一个使用者。
    """
    def _timeout() -> int:
        return int(config.BUSY_TIMEOUT_WORKER if role == "worker"
                   else config.BUSY_TIMEOUT_INTERACTIVE)

    eng = create_engine(
        config.DATABASE_URL,
        connect_args=connect_args,
        future=True,
        pool_pre_ping=True,
    )
    if not IS_SQLITE:
        return eng

    @event.listens_for(eng, "connect")
    def _sqlite_pragmas(dbapi_conn, _rec):  # pragma: no cover - 驱动回调
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute(f"PRAGMA busy_timeout={_timeout()}")
        # 显式写死自动 checkpoint 阈值（WAL 默认就是 1000 页）。写出来是为了
        # 让「WAL 会自己收缩」这件事在代码里可查：有人把这里调大，就必须同时
        # 知道 -wal 文件会随之变大。
        cur.execute("PRAGMA wal_autocheckpoint=1000")
        cur.close()

    @event.listens_for(eng, "checkin")
    def _sqlite_reset_busy_timeout(dbapi_conn, _rec):  # pragma: no cover - 驱动回调
        try:
            cur = dbapi_conn.cursor()
            cur.execute(f"PRAGMA busy_timeout={_timeout()}")
            cur.close()
        except Exception:  # 连接已坏，交给 pool_pre_ping 处理
            pass

    return eng


engine = _make_engine("interactive")
# worker 专用池：同一份 SQLite 文件，只是等锁时间更长。WAL 下多连接是安全的。
worker_engine = _make_engine("worker")


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


class Base(DeclarativeBase):
    pass


# ------------------------------------------------------------------ 写锁重试
def _retry_delay(attempt: int, base_delay: float) -> float:
    """第 attempt 次重试前的等待：指数退避 + 抖动。

    抖动是必须的：两个 worker 同时被锁挡下、又同时醒来重试，会一直撞在一起，
    重试次数再多也只是把同一次碰撞重复几遍。
    """
    return base_delay * (2 ** attempt) * (0.5 + random.random() * 0.5)


def _rollback_first_session(args: tuple, kwargs: dict) -> None:
    """重试前把用到的会话回滚掉。

    会话一旦因锁失败就进入「必须先 rollback」的状态，不先回滚，下一次尝试
    只会拿到 PendingRollbackError，把一次可重试的锁冲突变成真失败。
    """
    for value in list(args) + list(kwargs.values()):
        if isinstance(value, Session):
            try:
                value.rollback()
            except Exception:  # pragma: no cover - 连接已坏
                pass
            return


def run_with_lock_retry(fn: Callable[[], T], *, attempts: int | None = None,
                        base_delay: float | None = None,
                        on_retry: Callable[[], None] | None = None) -> T:
    """执行 fn；只在写锁冲突时重试，其它异常立刻抛出。

    为什么还要这一层：busy_timeout 已经在驱动层等过了，但它是「一次性」的——
    等满就报错。这里的重试是给「等待时间被刻意设短」的场景兜底（交互请求 3s、
    测试里 800ms），也给 worker 一次重新参与跨进程争抢的机会。
    """
    total = max(1, int(attempts if attempts is not None else config.LOCK_RETRY_ATTEMPTS))
    base = float(base_delay if base_delay is not None else 0.15)
    for attempt in range(total):
        try:
            return fn()
        except OperationalError as exc:
            if not is_lock_error(exc) or attempt == total - 1:
                raise
            if on_retry is not None:
                on_retry()
            time.sleep(_retry_delay(attempt, base))
    raise AssertionError("unreachable")  # pragma: no cover


def retry_on_locked(func=None, *, attempts: int | None = None, base_delay: float | None = None):
    """`run_with_lock_retry` 的装饰器版，支持带括号和不带括号两种写法。

    只适合「整个函数重跑是安全的」操作（抢任务、重算状态、写进度）。
    带副作用的业务写入请用 run_with_lock_retry，把重试范围收窄到那一小段。
    """
    def decorate(target: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(target)
        def wrapper(*args, **kwargs):
            def _call():
                try:
                    return target(*args, **kwargs)
                except OperationalError:
                    _rollback_first_session(args, kwargs)
                    raise
            return run_with_lock_retry(_call, attempts=attempts, base_delay=base_delay)
        return wrapper

    return decorate(func) if func is not None else decorate


# ------------------------------------------------------------------ 会话
def apply_busy_timeout(db: Session, timeout_ms: int) -> None:
    """把 busy_timeout 设到当前这条**物理连接**上（必须在 checkout 之后调用）。

    连接池会把同一条连接借给下一个请求，所以这里是「改这条连接」而不是
    「改全局配置」；归还时由 engine 的 checkin 事件复位成交互档，
    见 `_sqlite_reset_busy_timeout`。
    """
    if not IS_SQLITE:
        return
    db.execute(text(f"PRAGMA busy_timeout={max(0, int(timeout_ms))}"))


class WorkerSession(Session):
    """Worker 专用会话：区分「进度写」和「业务写」，只宽容前者。

    为什么要在 Session 层拦：进度写散落在 ingest / wiki 里（`_set_progress`、
    每批向量化之后的 commit），它们只更新任务行的 stage / progress。这些写被
    外部进程的写锁挡下时，正确行为是「这次进度没记上，接着干活」，而不是把
    整个任务判失败——原来的行为正是后者：一条 `UPDATE tasks SET stage=?` 撞锁，
    异常冒到 run_once，任务被标 failed，用户看到「解析失败」，其实文档本身
    一点问题都没有。

    一旦事务里还有分块、版本、知识库状态这些业务数据，锁错误照常抛出——
    那些数据不能丢，也不该假装成功。
    """

    _PROGRESS_ONLY_ATTRS = frozenset({"stage", "progress"})

    def commit(self) -> None:  # noqa: D102 - 见类文档
        if not self._only_task_progress():
            return super().commit()

        snapshot = self._progress_snapshot()
        # 进度写最多试 2 次（不是 LOCK_RETRY_ATTEMPTS 次）：它是 best-effort，
        # 真遇到「别人长时间持锁」时，正确的做法是赶紧放过去继续干活，
        # 而不是在这里把重试预算耗光。配合 1s 的短等待，单次最坏约 2 秒。
        total = max(1, min(int(config.LOCK_RETRY_ATTEMPTS), 2))
        # 进度写用短等待（BUSY_TIMEOUT_PROGRESS），提交后恢复 worker 档：
        # 否则最坏情况是 30s × 5 次 = 150s 全耗在一条进度上，worker 线程等于
        # 被按停，而它本可以继续解析。业务写仍然享受 worker 档的长等待。
        #
        # 每次尝试都要重设一次：`rollback()` 会把连接还回池子，下一次尝试可能
        # 拿到另一条连接（带着 worker 档默认值），只在循环外设一次是不够的。
        previous = self._read_busy_timeout()
        short = min(previous, config.BUSY_TIMEOUT_PROGRESS)
        try:
            for attempt in range(total):
                self._set_busy_timeout(short)
                try:
                    return super().commit()
                except OperationalError as exc:
                    if not is_lock_error(exc):
                        raise
                    # 锁失败后事务已废：回滚（丢掉的只有这次进度更新），
                    # 再按主键把进度重写一遍，重试才有意义。
                    self.rollback()
                    if attempt == total - 1:
                        log.warning(
                            "进度写入连续 %d 次被写锁挡住，跳过本次进度 %s：%s",
                            total, snapshot, getattr(exc, "orig", exc),
                        )
                        return
                    self._replay_progress(snapshot)
                    time.sleep(_retry_delay(attempt, 0.15))
        finally:
            self._set_busy_timeout(previous)

    def _read_busy_timeout(self) -> int:
        """读当前连接的 busy_timeout（拿不到就按 worker 档算）。"""
        try:
            value = self.execute(text("PRAGMA busy_timeout")).scalar()
            return int(value or config.BUSY_TIMEOUT_WORKER)
        except Exception:  # pragma: no cover - 连接不可用
            return config.BUSY_TIMEOUT_WORKER

    def _set_busy_timeout(self, timeout_ms: int) -> None:
        try:
            self.execute(text(f"PRAGMA busy_timeout={max(0, int(timeout_ms))}"))
        except Exception:  # pragma: no cover - 连接不可用
            log.debug("设置 busy_timeout 失败", exc_info=True)

    def _only_task_progress(self) -> bool:
        """本次事务是否「只改了任务行的 stage/progress」。"""
        if self.new or self.deleted or not self.dirty:
            return False
        from sqlalchemy import inspect as sa_inspect

        from . import models

        for obj in self.dirty:
            if not isinstance(obj, models.Task):
                return False
            changed = set(sa_inspect(obj).committed_state.keys())
            # 空集合 = 只是 add() 了一下，没实际改动，按普通提交处理更安全
            if not changed or not changed <= self._PROGRESS_ONLY_ATTRS:
                return False
        return True

    def _progress_snapshot(self) -> list[tuple[int, str, int]]:
        from . import models

        return [(obj.id, obj.stage, obj.progress) for obj in self.dirty
                if isinstance(obj, models.Task)]

    def _replay_progress(self, snapshot: list[tuple[int, str, int]]) -> None:
        from . import models

        for task_id, stage, progress in snapshot:
            task = self.get(models.Task, task_id)
            if task is None:
                continue
            task.stage = stage
            task.progress = progress
            self.add(task)


WorkerSessionLocal = sessionmaker(
    bind=worker_engine, autoflush=False, expire_on_commit=False, future=True,
    class_=WorkerSession)


def get_db() -> Iterator[Session]:
    """请求级会话（FastAPI 依赖）。

    顺手把「写锁等不到」翻译成 503 + 可读提示：`OperationalError: database is
    locked` 本来会冒成 500 Internal Server Error，前端只能显示「后端不可用」，
    用户根本不知道发生了什么。这里是所有 HTTP 请求的必经之路，一处收口，
    kbs / chat / wiki / settings 的写接口就都能给出「数据库正忙，请稍后重试」。
    """
    db = SessionLocal()
    try:
        yield db
    except OperationalError as exc:
        db.rollback()
        if not is_lock_error(exc):
            raise
        log.warning("请求被写锁挡住：%s", getattr(exc, "orig", exc))
        raise HTTPException(status_code=503, detail=DB_BUSY_DETAIL) from exc
    finally:
        db.close()


@contextmanager
def session_scope(role: str = "interactive") -> Iterator[Session]:
    """给 Worker / 脚本使用的独立事务作用域。

    `role="worker"`：走 worker_engine（连接默认 30s 等锁）+ WorkerSession
    （进度写容忍写锁冲突）。其它角色（含默认）保持原有语义——`get_db` 与
    `session_scope()` 被大量代码依赖，默认行为不能变。
    """
    worker_role = role == "worker"
    db = (WorkerSessionLocal if worker_role else SessionLocal)()
    try:
        if worker_role:
            apply_busy_timeout(db, config.BUSY_TIMEOUT_WORKER)
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@contextmanager
def worker_session() -> Iterator[Session]:
    """worker 会话的简写：`session_scope(role="worker")`。"""
    with session_scope(role="worker") as db:
        yield db


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
