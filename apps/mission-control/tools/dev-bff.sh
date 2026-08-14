#!/usr/bin/env bash
# Start a throwaway BFF on 51799 against the real Hermes upstreams.
# Reads the production env for upstream credentials but never touches the
# production store — STORE_PATH points at /tmp.
set -euo pipefail

ROOT=/home/pi/agent-mission-control
DEV_PORT=${DEV_PORT:-51799}

DEV_STORE=${DEV_STORE:-/tmp/amc-e2e.db}

set -a
# shellcheck disable=SC1091
. /etc/agent-mission-control/env
set +a

# Everything below is assigned AFTER the source on purpose: the production env
# defines PORT and STORE_PATH, so a `${VAR:-default}` written above would
# silently inherit them and point this throwaway instance at the real store.
export PORT=$DEV_PORT
export STORE_PATH=$DEV_STORE
export FRONTEND_DIR=$ROOT/frontend/dist
export ALLOWED_ORIGIN=http://127.0.0.1:$PORT
export ALLOWED_HOST=127.0.0.1:$PORT
export BIND_HOST=127.0.0.1
export ALLOWED_CIDRS=127.0.0.1/32

cd "$ROOT"
exec "$ROOT/.venv/bin/python" -m uvicorn agent_mission_control.main:app \
  --host 127.0.0.1 --port "$PORT"
