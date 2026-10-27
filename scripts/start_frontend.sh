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
  # 回退到 DSH 运行时自带的 pnpm
  PNPM_JS="C:/Users/Administrator/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/pnpm/bin/pnpm.mjs"
  NODE_BIN="C:/Users/Administrator/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/node/bin/node.exe"
fi

exec "$NODE_BIN" "$PNPM_JS" dev "$@"
