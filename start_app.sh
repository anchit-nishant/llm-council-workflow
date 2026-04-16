#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-3000}"
API_BASE_URL="${NEXT_PUBLIC_API_BASE_URL:-http://127.0.0.1:${BACKEND_PORT}}"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Missing local Python at $PYTHON_BIN"
  echo "Create the virtualenv first, then rerun this script."
  exit 1
fi

if [[ ! -d "$ROOT_DIR/frontend/node_modules" ]]; then
  echo "Missing frontend dependencies in $ROOT_DIR/frontend/node_modules"
  echo "Run 'cd frontend && npm install' first."
  exit 1
fi

if [[ "${CLEAN_NEXT_CACHE:-1}" == "1" ]] && [[ -d "$ROOT_DIR/frontend/.next" ]]; then
  echo "Clearing stale Next.js cache in $ROOT_DIR/frontend/.next"
  rm -rf "$ROOT_DIR/frontend/.next"
fi

cleanup() {
  local exit_code=$?
  trap - EXIT INT TERM
  if [[ -n "${BACKEND_PID:-}" ]] && kill -0 "$BACKEND_PID" 2>/dev/null; then
    kill "$BACKEND_PID" 2>/dev/null || true
  fi
  if [[ -n "${FRONTEND_PID:-}" ]] && kill -0 "$FRONTEND_PID" 2>/dev/null; then
    kill "$FRONTEND_PID" 2>/dev/null || true
  fi
  wait "${BACKEND_PID:-}" "${FRONTEND_PID:-}" 2>/dev/null || true
  exit "$exit_code"
}

trap cleanup EXIT INT TERM

echo "Starting backend on http://127.0.0.1:${BACKEND_PORT}"
(
  cd "$ROOT_DIR/backend"
  exec "$PYTHON_BIN" -m uvicorn app.main:app --host 127.0.0.1 --port "$BACKEND_PORT" --reload
) &
BACKEND_PID=$!

echo "Starting frontend on http://127.0.0.1:${FRONTEND_PORT}"
(
  cd "$ROOT_DIR/frontend"
  export NEXT_PUBLIC_API_BASE_URL="$API_BASE_URL"
  exec npm run dev -- --hostname 127.0.0.1 --port "$FRONTEND_PORT"
) &
FRONTEND_PID=$!

echo
echo "llm-council-workflow UI: http://127.0.0.1:${FRONTEND_PORT}"
echo "API:        http://127.0.0.1:${BACKEND_PORT}"
echo "Press Ctrl+C to stop both processes."
echo

while true; do
  if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
    wait "$BACKEND_PID" 2>/dev/null || true
    break
  fi
  if ! kill -0 "$FRONTEND_PID" 2>/dev/null; then
    wait "$FRONTEND_PID" 2>/dev/null || true
    break
  fi
  sleep 1
done
