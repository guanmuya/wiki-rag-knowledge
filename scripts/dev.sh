#!/usr/bin/env bash
# 一键准备并启动：建虚拟环境 → 装依赖 → 灌演示数据 → 同时起前后端。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="$ROOT/.venv/Scripts/python.exe"
[ -x "$PY" ] || PY="$ROOT/.venv/bin/python"

if [ ! -x "$PY" ]; then
  echo "==> 创建虚拟环境"
  python -m venv .venv
  PY="$ROOT/.venv/Scripts/python.exe"
  [ -x "$PY" ] || PY="$ROOT/.venv/bin/python"
fi

echo "==> 安装后端依赖"
"$PY" -m pip install -q -r backend/requirements.txt

echo "==> 安装前端依赖"
(cd frontend && \
  if command -v pnpm >/dev/null 2>&1; then pnpm install
  elif command -v corepack >/dev/null 2>&1; then corepack pnpm install
  else npx --yes pnpm@9 install
  fi)

if [ "${SKIP_SEED:-0}" != "1" ]; then
  echo "==> 导入演示数据（幂等）"
  (cd backend && PYTHONPATH="$ROOT/backend" PYTHONIOENCODING=utf-8 "$PY" -m scripts.seed_demo)
fi

echo "==> 启动后端 (8000) 与前端 (5173)"
trap 'kill 0' EXIT
"$ROOT/scripts/start_backend.sh" &
sleep 3
"$ROOT/scripts/start_frontend.sh" &
wait
