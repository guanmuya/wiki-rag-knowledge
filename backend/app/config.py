"""全局配置。单企业部署，所有可调项通过环境变量覆盖。"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent          # backend/
PROJECT_ROOT = BASE_DIR.parent                             # repo root
DATA_DIR = Path(os.getenv("WRK_DATA_DIR", PROJECT_ROOT / "data"))
FILES_DIR = DATA_DIR / "files"
DB_PATH = Path(os.getenv("WRK_DB_PATH", DATA_DIR / "db" / "app.sqlite3"))
SECRET_DIR = Path(os.getenv("WRK_SECRET_DIR", DATA_DIR / "secrets"))

for _d in (DATA_DIR, FILES_DIR, DB_PATH.parent, SECRET_DIR):
    _d.mkdir(parents=True, exist_ok=True)

DATABASE_URL = os.getenv("WRK_DATABASE_URL", f"sqlite:///{DB_PATH.as_posix()}")

# 单机首版：进程内 Worker 线程 + 数据库任务表。
WORKER_ENABLED = os.getenv("WRK_WORKER_ENABLED", "1") == "1"
WORKER_POLL_SECONDS = float(os.getenv("WRK_WORKER_POLL_SECONDS", "1.0"))
# 默认 1：SQLite 同时只允许一个写者，多开 worker 线程并不会更快——它们只会在
# 写锁上互相排队，把「一次阻塞」放大成多次超时（日志里那种每 17.5s 一条
# database is locked 的循环，就是两个 worker 在抢同一把锁）。真要并行请先换
# 成 Postgres 再调大这个值。
WORKER_CONCURRENCY = int(os.getenv("WRK_WORKER_CONCURRENCY", "1"))

# ---- SQLite 写锁等待（「整个软件卡住」的直接旋钮）
# 为什么分两档：交互请求（列知识库、看任务、登录审计）宁可快速失败也不能让
# 界面卡住——3 秒还拿不到锁就返回「数据库正忙」，前端提示用户重试即可；
# worker 的写是后台任务，多等一会儿无所谓，等到了就不必把任务判失败。
# 旧版一刀切 15000ms：任何一次外部持锁都会把 UI 卡满 15 秒，前端 4 秒超时后
# 报「后端不可用」，用户只能强关应用。
BUSY_TIMEOUT_INTERACTIVE = int(os.getenv("WRK_BUSY_TIMEOUT_INTERACTIVE", "3000"))
BUSY_TIMEOUT_WORKER = int(os.getenv("WRK_BUSY_TIMEOUT_WORKER", "30000"))
# 任务进度写（tasks.stage/progress）单独一档，刻意很短：它是 best-effort，
# 丢了只是界面少刷新一次。若沿用 worker 档，最坏情况是 30s × 重试次数 =
# 150s 全耗在一条进度上，worker 线程等于被按停，而它本可以继续干活。
# 配合「进度写最多重试 2 次」，单次进度写最坏约 2 秒（见 db.WorkerSession.commit）。
BUSY_TIMEOUT_PROGRESS = int(os.getenv("WRK_BUSY_TIMEOUT_PROGRESS", "1000"))
# 拿不到锁时的重试次数（指数退避 + 抖动）。这是 busy_timeout 之外的兜底，
# 总耗时远小于一次 busy_timeout，别调成几十次。
LOCK_RETRY_ATTEMPTS = int(os.getenv(
    "WRK_LOCK_RETRY_ATTEMPTS", os.getenv("WRK_LOCK_RETRY", "5")))

# 上传限制
MAX_UPLOAD_MB = int(os.getenv("WRK_MAX_UPLOAD_MB", "50"))
# 支持：PDF、Word(docx)、Markdown、纯文本、Excel(xlsx)、PowerPoint(pptx)
ALLOWED_SUFFIXES = {
    ".txt", ".md", ".markdown", ".log",
    ".pdf", ".docx", ".xlsx", ".pptx",
}

# 检索参数
DEFAULT_CHUNK_SIZE = int(os.getenv("WRK_CHUNK_SIZE", "700"))
DEFAULT_CHUNK_OVERLAP = int(os.getenv("WRK_CHUNK_OVERLAP", "120"))
# 表格块单独预算：表格信息密度高，同样长度能承载更多有效内容；
# 但仍需远小于 embedding 上下文窗口，避免被模型静默截断。
TABLE_CHUNK_BUDGET = int(os.getenv("WRK_TABLE_CHUNK_BUDGET", "1600"))
RRF_K = int(os.getenv("WRK_RRF_K", "60"))
PER_KB_CANDIDATES = int(os.getenv("WRK_PER_KB_CANDIDATES", "8"))
MAX_EVIDENCE = int(os.getenv("WRK_MAX_EVIDENCE", "10"))
CONTEXT_CHAR_BUDGET = int(os.getenv("WRK_CONTEXT_CHAR_BUDGET", "9000"))

# ---- Wiki 主题分批归纳（Map-Reduce）
# 为什么需要：单库要支撑 1000 份文档（约 3000-5000 块），一次请求塞不下，
# 只取前 N 块又会让库尾内容永远进不了 Wiki。按字符预算切片，每片独立归纳。
#
# 单片字符预算：一次 Map 调用携带的资料片段总长，与 CONTEXT_CHAR_BUDGET 同量级。
WIKI_SHARD_CHAR_BUDGET = int(
    os.getenv("WRK_WIKI_SHARD_CHAR_BUDGET", str(CONTEXT_CHAR_BUDGET)))
# 单片内单个分块的摘录长度。默认 900 与旧实现一致，保证小库（单片即可装下）
# 的主题归纳结果与改动前逐字相同。
WIKI_TOPIC_EXCERPT_CHARS = int(os.getenv("WRK_WIKI_TOPIC_EXCERPT_CHARS", "900"))
# 分片数过多时的自适应下限：摘录最多压到这个长度，再压就没有归纳价值了。
WIKI_TOPIC_EXCERPT_MIN = int(os.getenv("WRK_WIKI_TOPIC_EXCERPT_MIN", "120"))
# Map 调用数上限（0 = 不限制）。超过上限先自动压缩单块摘录以覆盖全库；
# 压到下限仍然超，就如实多调用——宁可多花几次请求，也不把库尾内容丢掉。
WIKI_MAX_MAP_CALLS = int(os.getenv("WRK_WIKI_MAX_MAP_CALLS", "40"))
# 合并后的页面数上限。Map 阶段每片最多 12 个主题，大库合并后仍可能有上百个，
# 页面数不设上限会让「生成 Wiki」变成几百次调用。超限时按证据数量保留，
# 并在 notes 里说明被略过的主题——不静默截断。
WIKI_MAX_PAGES = int(os.getenv("WRK_WIKI_MAX_PAGES", "60"))

ACCESS_TOKEN_TTL_MINUTES = int(os.getenv("WRK_TOKEN_TTL_MINUTES", "720"))
BOOTSTRAP_ADMIN_USERNAME = os.getenv("WRK_ADMIN_USERNAME", "admin")
BOOTSTRAP_ADMIN_PASSWORD = os.getenv("WRK_ADMIN_PASSWORD", "")

CORS_ORIGINS = [o for o in os.getenv(
    "WRK_CORS_ORIGINS",
    "http://localhost:5173,http://127.0.0.1:5173,http://localhost:4173,http://127.0.0.1:4173",
).split(",") if o]

# 调用供应商接口时是否继承系统代理环境变量。
# 默认关闭：企业内网部署不应把 API Key 意外送进系统代理，也避免畸形 NO_PROXY 干扰。
HTTP_TRUST_ENV = os.getenv("WRK_HTTP_TRUST_ENV", "0") == "1"
HTTP_PROXY = os.getenv("WRK_HTTP_PROXY", "")     # 需要代理时显式配置，如 http://proxy.corp:8080
HTTP_TIMEOUT_READ = float(os.getenv("WRK_HTTP_TIMEOUT_READ", "120"))
