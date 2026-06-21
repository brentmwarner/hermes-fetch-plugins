#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${HERMES_ENV_FILE:-"$HOME/.hermes/.env"}"

load_relay_env() {
  local line key value
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    [[ -z "$line" || "$line" == \#* ]] && continue
    [[ "$line" == export\ * ]] && line="${line#export }"
    [[ "$line" == *=* ]] || continue
    key="${line%%=*}"
    value="${line#*=}"
    key="${key%"${key##*[![:space:]]}"}"
    case "$key" in
      HERMES_APNS_*|HERMES_RELAY_*) ;;
      *) continue ;;
    esac
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    if [[ "$value" == \"*\" && "$value" == *\" ]]; then
      value="${value:1:${#value}-2}"
    elif [[ "$value" == \'*\' && "$value" == *\' ]]; then
      value="${value:1:${#value}-2}"
    fi
    export "$key=$value"
  done < "$ENV_FILE"
}

[[ -f "$ENV_FILE" ]] && load_relay_env

export HERMES_RELAY_DATABASE_PATH="${HERMES_RELAY_DATABASE_PATH:-"$ROOT/data/push-relay.db"}"
export HERMES_RELAY_ALLOW_CUSTOM_BODY="${HERMES_RELAY_ALLOW_CUSTOM_BODY:-true}"

cd "$ROOT"
exec python -m uvicorn push_relay.app:app \
  --host "${HERMES_RELAY_HOST:-127.0.0.1}" \
  --port "${HERMES_RELAY_PORT:-8787}"
