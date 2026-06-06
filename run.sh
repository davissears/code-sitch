#!/usr/bin/env bash
# Launch the Claude Code Situation Monitor and open it in the browser.
set -euo pipefail
cd "$(dirname "$0")"

PORT="${CSM_PORT:-8787}"
URL="http://127.0.0.1:${PORT}/"

# already running? just open it.
if curl -fsS "${URL}api/health" >/dev/null 2>&1; then
  echo "Situation Monitor already running at ${URL}"
  open "${URL}" 2>/dev/null || true
  exit 0
fi

echo "Starting Situation Monitor on ${URL}"
# open the browser shortly after the server binds
( for _ in $(seq 1 40); do
    curl -fsS "${URL}api/health" >/dev/null 2>&1 && { open "${URL}" 2>/dev/null || true; break; }
    sleep 0.25
  done ) &

exec python3 server.py
