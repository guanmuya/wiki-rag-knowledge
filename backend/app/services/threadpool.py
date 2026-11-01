"""HTTP 请求线程池：常驻线程，事件循环永不在请求路径上创建线程。

为什么需要这个模块（v1.0.9 现场日志 `data/logs/lock-watchdog.log`）：

    --- thread wrk-worker-1
        File "app\\services\\ingest.py", line 120, in ingest_version
        File "app\\services\\llm.py", line 268, in pack_vector
        File "numpy\\_core\\multiarray.py", line 11, in <module>
        File "<frozen importlib._bootstrap_external>", line 1317, in create_module
    --- thread MainThread
        File "anyio\\_backends\\_asyncio.py", line 2676, in run_sync_in_worker_thread
        File "threading.py", line 982, in start
    ----- 解锁：写锁阻塞持续 155072ms

anyio 的线程池是**懒加载**的：空闲线程不够时，它在事件循环线程里直接
`worker.start()`。而新线程启动要等 Windows 的 loader lock —— 后台 worker
线程首次 import 原生模块（numpy / lxml / pypdf / docx …）时正握着它。
于是形成一条和业务无关的死链：

    后台任务加载 DLL（持 loader lock，杀软首次扫描时能到几十秒）
      → 请求到来、池里没有空闲线程
      → 事件循环同步 `Thread.start()` → 等 loader lock
      → 整个事件循环停摆：所有接口（包括纯读的 GET /api/kbs）都不再响应
      → 用户看到的就是「知识库里有任务在处理，整个软件卡住」

修复思路：把「创建线程」从请求路径上彻底移走。

1. 启动时一次性建好 N 个常驻线程（daemon，不回收、不扩容）；
2. `run_in_threadpool` 只把任务投进队列，事件循环只 `await` 一个 Future，
   永远不调用 `Thread.start()`；
3. 池满时排队等待，而不是临时扩线程 —— 宁可这一个请求慢一点，
   也绝不能让事件循环被 loader lock / 磁盘 / 杀软拖住。

为什么不用 `concurrent.futures.ThreadPoolExecutor`：
- 它同样在 `submit()` 的调用线程里 `t.start()`（懒扩容），问题照旧；
- 解释器退出时会 `join` 所有工作线程，而桌面版强关时线程可能正卡在
  等 SQLite 写锁上 —— 那会把「退出」变成「卡死」。
这里用 queue + daemon 线程自己实现，退出时不需要任何 join。

安装时机很关键：FastAPI 在**注册路由**时就把 `run_in_threadpool` 抓成了
`functools.partial`（fastapi/routing.py 的 request_response），所以补丁
必须在导入 routers 之前打上。`app/main.py` 里 `threadpool.install()` 排在
`from .routers import ...` 之前，原因就在这里。
"""
from __future__ import annotations

import asyncio
import contextvars
import functools
import logging
import os
import queue
import threading
from typing import Any, Callable, TypeVar

import anyio.to_thread

log = logging.getLogger("wrk.threadpool")

# 兜底用的原版实现（池还没起来时保持旧行为）
_original_run_sync = anyio.to_thread.run_sync

T = TypeVar("T")

# 常驻线程数。桌面版真实并发就十几个（页面切换时几个请求同时打出去），
# 再多只会跟 SQLite 的连接池抢资源。
SIZE = max(2, int(os.getenv("WRK_HTTP_THREADS", "12")))
_PREFIX = "wrk-http"

# 需要把 run_in_threadpool 换成常驻池版本的模块。
# 都是「模块级 from ... import run_in_threadpool」的引用点：只改
# starlette.concurrency 里的定义没用，这些模块早把函数对象抓走了。
_PATCH_TARGETS = (
    "starlette.concurrency",
    "starlette.routing",
    "starlette.endpoints",
    "starlette.datastructures",
    "starlette.background",
    "starlette._exception_handler",
    "fastapi.concurrency",
    "fastapi.routing",
    "fastapi.dependencies.utils",
)

# 直接 `from anyio.to_thread import run_sync` 的引用点（属性名不是 run_in_threadpool）
_RUN_SYNC_TARGETS = (
    ("fastapi.telemetry._runtime", "run_sync"),
)


class _Pool:
    """固定大小、daemon、不回收的线程池。

    任务以 (函数, 参数, 上下文, 事件循环, Future) 的形式入队；工作线程执行完
    通过 `loop.call_soon_threadsafe` 把结果交回事件循环。异常原样转交，
    请求方看到的堆栈与直接在请求线程里执行一致。
    """

    def __init__(self, size: int, prefix: str = _PREFIX) -> None:
        self._queue: queue.SimpleQueue[Any] = queue.SimpleQueue()
        self._threads: list[threading.Thread] = []
        for i in range(max(1, size)):
            thread = threading.Thread(
                target=self._worker, name=f"{prefix}-{i + 1}", daemon=True)
            thread.start()
            self._threads.append(thread)

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:            # 预留的停止信号（当前不主动停）
                return
            func, args, ctx, loop, future = item
            try:
                result = ctx.run(func, *args)
            except BaseException as exc:            # noqa: BLE001 - 原样转交
                _deliver(loop, future, exc=exc)
            else:
                _deliver(loop, future, result=result)

    async def run(self, func: Callable[..., T], *args: Any) -> T:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[T] = loop.create_future()
        # 复制上下文：FastAPI 的依赖、日志 trace id 等都放在 contextvars 里，
        # 不复制就会在「别的线程」里丢上下文（anyio 的实现也做了这一步）。
        self._queue.put((func, args, contextvars.copy_context(), loop, future))
        return await future

    def stats(self) -> dict[str, Any]:
        return {
            "size": len(self._threads),
            "alive": sum(1 for t in self._threads if t.is_alive()),
            "threads": [t.name for t in self._threads],
        }


def _deliver(loop: asyncio.AbstractEventLoop, future: asyncio.Future,
             *, result: Any = None, exc: BaseException | None = None) -> None:
    """把工作线程的结果交回事件循环（线程安全）。"""
    def _set() -> None:
        if future.done():           # 请求已取消/超时：结果丢掉即可
            return
        if exc is not None:
            future.set_exception(exc)
        else:
            future.set_result(result)

    try:
        loop.call_soon_threadsafe(_set)
    except RuntimeError:            # 事件循环已关闭（应用退出中）
        pass


_pool: _Pool | None = None


async def run_in_threadpool(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """`starlette.concurrency.run_in_threadpool` 的常驻线程池实现。

    签名、异常语义与官方版一致；唯一区别是执行线程来自固定池，
    事件循环不再创建线程。
    """
    if kwargs:
        func = functools.partial(func, **kwargs)      # type: ignore[assignment]
    return await _dispatch(func, args)


async def _pooled_run_sync(func: Callable[..., T], *args: Any,
                           abandon_on_cancel: bool = False,
                           limiter: Any = None) -> T:
    """`anyio.to_thread.run_sync` 的常驻池版本。

    为什么连 anyio 一起换：FastAPI 的 `contextmanager_in_threadpool`
    （同步依赖收尾，例如每个请求的 `db.close()`）和 Starlette 的
    FileResponse / gzip 中间件都是**直接调** `anyio.to_thread.run_sync`，
    不走 `run_in_threadpool`。只换后者，请求收尾那一步仍会在事件循环里
    建线程 —— 卡死照样复发（回归测试里抓到的就是这条路径）。
    """
    return await _dispatch(func, args)


async def _dispatch(func: Callable[..., T], args: tuple[Any, ...]) -> T:
    pool = _pool
    if pool is None:
        # 兜底：lifespan 还没跑（例如没走启动流程的单元测试）。
        # 保持旧行为可用，不影响正确性。
        return await _original_run_sync(func, *args)
    return await pool.run(func, *args)


def install() -> list[str]:
    """把框架里的线程派发入口换成常驻池版本。

    返回实际替换成功的模块名，便于启动日志与测试断言。
    必须在路由注册之前调用（原因见模块文档）。
    """
    import importlib

    patched: list[str] = []

    # 1) 单点：anyio 的公共入口。覆盖所有「直接调 anyio」的地方。
    if anyio.to_thread.run_sync is not _pooled_run_sync:
        anyio.to_thread.run_sync = _pooled_run_sync      # type: ignore[assignment]
        patched.append("anyio.to_thread.run_sync")

    # 2) 各模块里「已经抓走函数对象」的引用点。
    for module_name in _PATCH_TARGETS:
        try:
            module = importlib.import_module(module_name)
        except Exception:          # pragma: no cover - 依赖缺失时保持原样
            log.debug("跳过线程池补丁：%s 不可导入", module_name, exc_info=True)
            continue
        current = getattr(module, "run_in_threadpool", None)
        if current is None or current is run_in_threadpool:
            continue
        setattr(module, "run_in_threadpool", run_in_threadpool)
        patched.append(module_name)

    # 3) `from anyio.to_thread import run_sync` 的引用点（FastAPI telemetry
    #    在关机 flush 时会用；不影响请求路径，但顺手对齐）。
    for module_name, attr in _RUN_SYNC_TARGETS:
        try:
            module = importlib.import_module(module_name)
        except Exception:          # pragma: no cover
            continue
        if getattr(module, attr, None) is _pooled_run_sync:
            continue
        setattr(module, attr, _pooled_run_sync)
        patched.append(f"{module_name}.{attr}")

    if patched:
        log.info("HTTP 线程池补丁已安装：%s", ", ".join(patched))
    return patched


def start(size: int | None = None) -> dict[str, Any]:
    """建好常驻线程（在 lifespan 里、开始服务之前调用）。"""
    global _pool
    if _pool is not None:
        return _pool.stats()
    _pool = _Pool(size or SIZE)
    info = _pool.stats()
    log.info("HTTP 线程池已启动：%d 个常驻线程（不再在请求路径上创建线程）",
             info["alive"])
    return info


def stop() -> None:
    """停掉线程池（仅测试/嵌入式场景需要；桌面版随进程退出）。"""
    global _pool
    pool, _pool = _pool, None
    if pool is None:
        return
    for _ in pool._threads:        # noqa: SLF001 - 本模块自己的池
        pool._queue.put(None)      # noqa: SLF001
    for thread in pool._threads:   # noqa: SLF001
        thread.join(timeout=1)


def stats() -> dict[str, Any]:
    """给诊断接口用：池是否就绪、有多少条常驻线程。"""
    if _pool is None:
        return {"started": False, "size": SIZE, "alive": 0, "threads": []}
    return {"started": True, **(_pool.stats())}
