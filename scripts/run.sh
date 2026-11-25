#!/usr/bin/env bash
# Start People Lens locally: Postgres (Docker), then the FastAPI backend and the Streamlit frontend.
#
# Usage:  scripts/run.sh            start (seeds + ingests only if the database is empty)
#         scripts/run.sh --reseed   rebuild the synthetic data and policy index first
# Env:    API_PORT (8000), UI_PORT (8501), PG_PORT (55432), PG_CONTAINER (pl-postgres)
# Stop:   Ctrl+C stops the backend and frontend (the Postgres container keeps running).

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

API_PORT="${API_PORT:-8000}"
UI_PORT="${UI_PORT:-8501}"
PG_PORT="${PG_PORT:-55432}"
PG_CONTAINER="${PG_CONTAINER:-pl-postgres}"
LOG_DIR="$ROOT/.logs"
PY="$ROOT/.venv/bin/python"
RESEED=0
[[ "${1:-}" == "--reseed" ]] && RESEED=1

info() { printf '\033[1;34m▸\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

port_busy() { lsof -iTCP:"$1" -sTCP:LISTEN -t >/dev/null 2>&1; }

wait_for() {  # wait_for <name> <cmd...>
  local name="$1"; shift
  for _ in $(seq 1 60); do
    if "$@" >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  fail "$name did not become ready (see $LOG_DIR)"
}

# ------------------------------------------------------------------ prerequisites
[[ -f .env ]] || { cp .env.example .env; info "Created .env from .env.example (set XAI_API_KEY in it)"; }
PROVIDER="$(grep -E '^LLM_PROVIDER=' .env | cut -d= -f2 | tr -d '[:space:]')"
PROVIDER="${PROVIDER:-grok}"
KEY_VAR=$([[ "$PROVIDER" == "openai" ]] && echo OPENAI_API_KEY || echo XAI_API_KEY)
info "LLM provider: $PROVIDER"
grep -qE "^${KEY_VAR}=.+" .env || info "$KEY_VAR is empty in .env: the assistant will run in degraded mode (policy excerpts only)"

if [[ ! -x "$PY" ]]; then
  info "Creating virtualenv and installing dependencies"
  PYTHON_BIN="$(command -v python3.13 || command -v python3.12 || true)"
  [[ -n "$PYTHON_BIN" ]] || fail "Python 3.12+ is required"
  "$PYTHON_BIN" -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt
fi

for p in "$API_PORT" "$UI_PORT"; do
  port_busy "$p" && fail "Port $p is already in use (stop the other process or set API_PORT/UI_PORT)"
done

# ------------------------------------------------------------------ database
command -v docker >/dev/null || fail "Docker is required"
if ! docker info >/dev/null 2>&1; then
  if [[ "$(uname)" == "Darwin" ]]; then
    info "Starting Docker Desktop"
    open -a Docker
    wait_for "Docker" docker info
  else
    fail "Docker daemon is not running"
  fi
fi

if docker ps --format '{{.Names}}' | grep -qx "$PG_CONTAINER"; then
  info "Postgres container '$PG_CONTAINER' already running"
elif docker ps -a --format '{{.Names}}' | grep -qx "$PG_CONTAINER"; then
  info "Starting existing Postgres container '$PG_CONTAINER'"
  docker start "$PG_CONTAINER" >/dev/null
else
  info "Creating Postgres container '$PG_CONTAINER' on port $PG_PORT"
  docker run -d --name "$PG_CONTAINER" -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=peoplelens \
    -p "$PG_PORT":5432 pgvector/pgvector:pg16 >/dev/null
fi
wait_for "Postgres" docker exec "$PG_CONTAINER" pg_isready -U postgres -d peoplelens
sleep 1

SEEDED="$(docker exec "$PG_CONTAINER" psql -U postgres -d peoplelens -tAc \
  "SELECT (to_regclass('hr.employees') IS NOT NULL AND to_regclass('kb.chunks') IS NOT NULL)" 2>/dev/null || echo f)"
if [[ "$SEEDED" == "t" ]]; then
  CHUNKS="$(docker exec "$PG_CONTAINER" psql -U postgres -d peoplelens -tAc "SELECT count(*) FROM kb.chunks")"
  [[ "$CHUNKS" -gt 0 ]] || SEEDED=f
fi
if [[ "$RESEED" == 1 || "$SEEDED" != "t" ]]; then
  info "Seeding synthetic data"
  "$PY" -m scripts.seed_data
  info "Ingesting policy documents"
  "$PY" -m app.rag.ingest | grep -v '^{"ts' || true
else
  info "Database already seeded (use --reseed to rebuild)"
fi

# ------------------------------------------------------------------ backend + frontend
mkdir -p "$LOG_DIR"
PIDS=()
cleanup() {
  trap - INT TERM EXIT
  info "Stopping backend and frontend"
  for pid in "${PIDS[@]:-}"; do [[ -n "$pid" ]] && kill "$pid" 2>/dev/null || true; done
  wait 2>/dev/null || true
}
on_signal() { cleanup; exit 0; }
trap on_signal INT TERM
trap cleanup EXIT

info "Starting backend on :$API_PORT (log: .logs/api.log)"
"$ROOT/.venv/bin/uvicorn" app.api.main:app --host 127.0.0.1 --port "$API_PORT" > "$LOG_DIR/api.log" 2>&1 &
PIDS+=($!)
wait_for "Backend" curl -sf "http://127.0.0.1:$API_PORT/health"

info "Starting frontend on :$UI_PORT (log: .logs/ui.log)"
API_URL="http://127.0.0.1:$API_PORT" "$ROOT/.venv/bin/streamlit" run ui/streamlit_app.py \
  --server.port "$UI_PORT" --server.headless true --browser.gatherUsageStats false > "$LOG_DIR/ui.log" 2>&1 &
PIDS+=($!)
wait_for "Frontend" curl -sf "http://127.0.0.1:$UI_PORT/_stcore/health"

cat <<EOF

  People Lens is running
    UI        http://localhost:$UI_PORT
    API docs  http://localhost:$API_PORT/docs
    Health    $(curl -s "http://127.0.0.1:$API_PORT/health")

  Press Ctrl+C to stop.
EOF

# exit if either process dies
while true; do
  for pid in "${PIDS[@]}"; do
    if ! kill -0 "$pid" 2>/dev/null; then
      fail "A process exited unexpectedly (pid $pid); see $LOG_DIR"
    fi
  done
  sleep 2
done
