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
WORKER_CONCURRENCY = int(os.getenv("WRK_WORKER_CONCURRENCY", "2"))

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
