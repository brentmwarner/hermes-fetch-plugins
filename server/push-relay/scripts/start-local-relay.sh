#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${HERMES_RELAY_PORT:-8787}"
VENV_DIR="${HERMES_AGENT_VENV:-"$HOME/.hermes/hermes-agent/venv"}"
LOG_PATH="$ROOT/data/relay.log"
PID_PATH="$ROOT/data/relay.pid"

mkdir -p "$ROOT/data"

if command -v lsof >/dev/null 2>&1 && lsof -tiTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Hermes push relay is already listening on port $PORT"
  exit 0
fi

PATH_PREFIX="$PATH"
if [[ -d "$VENV_DIR/bin" ]]; then
  PATH_PREFIX="$VENV_DIR/bin:$PATH_PREFIX"
fi

nohup env PATH="$PATH_PREFIX" "$ROOT/scripts/run-local-relay.sh" > "$LOG_PATH" 2>&1 &
echo "$!" > "$PID_PATH"
echo "Hermes push relay started on http://127.0.0.1:$PORT"
echo "pid: $(cat "$PID_PATH")"
echo "log: $LOG_PATH"
