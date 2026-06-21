#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${HERMES_RELAY_PORT:-8787}"
PID_PATH="$ROOT/data/relay.pid"

if [[ -f "$PID_PATH" ]]; then
  pid="$(cat "$PID_PATH")"
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid"
    rm -f "$PID_PATH"
    echo "Hermes push relay stopped"
    exit 0
  fi
  rm -f "$PID_PATH"
fi

if command -v lsof >/dev/null 2>&1; then
  pids="$(lsof -tiTCP:"$PORT" -sTCP:LISTEN || true)"
  if [[ -n "$pids" ]]; then
    kill $pids
    echo "Hermes push relay stopped"
    exit 0
  fi
fi

echo "Hermes push relay was not running"
