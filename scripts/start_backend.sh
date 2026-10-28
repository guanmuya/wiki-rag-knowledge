#!/usr/bin/env bash
# 启动后端（含进程内 Worker）。首次运行会自动建库并打印初始管理员密码。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="${PYTHON:-$ROOT/.venv/Scripts/python.exe}"
if [ ! -x "$PY" ]; then
  PY="$ROOT/.venv/bin/python"
fi
if [ ! -x "$PY" ]; then
  echo "未找到虚拟环境，先创建：python -m venv .venv && .venv/Scripts/pip install -r backend/requirements.txt"
  exit 1
fi

export PYTHONPATH="$ROOT/backend${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONIOENCODING=utf-8

# 管理员初始化策略（与 app/main.py 的 bootstrap_admin 一致）：
# - 设置了 WRK_ADMIN_PASSWORD → 首次启动自动建管理员（开发/测试用）；
# - 留空 → 不建号，空库启动后前端会跳 #/setup 引导页，由用户自己设账号密码。
: "${WRK_ADMIN_PASSWORD:=}"

HOST="${WRK_HOST:-127.0.0.1}"
PORT="${WRK_PORT:-8000}"

echo "后端启动：http://${HOST}:${PORT}  (API 文档 /docs)"
exec "$PY" -m uvicorn app.main:app --app-dir "$ROOT/backend" --host "$HOST" --port "$PORT" "$@"
