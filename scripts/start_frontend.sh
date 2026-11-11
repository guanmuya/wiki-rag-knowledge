#!/usr/bin/env bash
# 启动前端开发服务器（Vite，5173），/api 代理到后端 8000。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT/frontend"

NODE_BIN="${NODE_BIN:-node}"
PNPM_JS="${PNPM_JS:-}"

if [ -z "${PNPM_JS}" ]; then
  if command -v pnpm >/dev/null 2>&1; then
    exec pnpm dev "$@"
  fi
  if command -v corepack >/dev/null 2>&1; then
    exec corepack pnpm dev "$@"
  fi
  echo "未找到 pnpm：先装 pnpm 9（corepack enable / npm i -g pnpm@9），" >&2
  echo "或用 npx --yes pnpm@9 dev 启动；也可用 PNPM_JS=<pnpm.mjs 路径> 指定。" >&2
  exit 1
fi

exec "$NODE_BIN" "$PNPM_JS" dev "$@"
