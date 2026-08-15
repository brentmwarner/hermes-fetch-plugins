#!/usr/bin/env bash
set -euo pipefail

display_name="${DISPLAY:-:0}"
args=(
  -fg
  -display "$display_name"
  -localhost yes
  -SecurityTypes None
  -rfbport 5900
)

exec x0vncserver "${args[@]}"
