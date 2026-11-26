#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOST="${KIKI_HOST:-127.0.0.1}"
PORT="${KIKI_PORT:-8000}"
MODE="${1:-all}"

if [[ -x "${ROOT_DIR}/.venv/bin/uvicorn" ]]; then
  UVICORN_BIN="${ROOT_DIR}/.venv/bin/uvicorn"
else
  UVICORN_BIN="uvicorn"
fi

start_backend() {
  echo "[kiki] Starting backend on http://${HOST}:${PORT} ..."
  exec "${UVICORN_BIN}" app.main:app \
    --app-dir "${ROOT_DIR}/backend" \
    --host "${HOST}" \
    --port "${PORT}" \
    --reload
}

start_frontend() {
  echo "[kiki] Starting frontend on http://localhost:5173 ..."
  cd "${ROOT_DIR}/frontend"
  exec npm run dev
}

case "${MODE}" in
  backend|be|api)
    start_backend
    ;;
  frontend|fe|web)
    start_frontend
    ;;
  all)
    echo "[kiki] Starting backend (:${PORT}) and frontend (:5173)..."
    "${UVICORN_BIN}" app.main:app \
      --app-dir "${ROOT_DIR}/backend" \
      --host "${HOST}" \
      --port "${PORT}" \
      --reload &
    BE_PID=$!

    (cd "${ROOT_DIR}/frontend" && npm run dev) &
    FE_PID=$!

    cleanup() {
      echo ""
      echo "[kiki] Stopping services..."
      kill "${BE_PID}" "${FE_PID}" 2>/dev/null || true
      wait "${BE_PID}" "${FE_PID}" 2>/dev/null || true
    }
    trap cleanup INT TERM EXIT

    wait
    ;;
  -h|--help|help)
    echo "Usage: ./start.sh [all|backend|frontend]"
    echo "Env:   KIKI_HOST=127.0.0.1 KIKI_PORT=8000"
    ;;
  *)
    echo "Unknown mode: ${MODE}" >&2
    echo "Usage: ./start.sh [all|backend|frontend]" >&2
    exit 1
    ;;
esac
