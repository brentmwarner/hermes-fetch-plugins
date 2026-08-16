#!/usr/bin/env bash
set -euo pipefail

# Run a GUI command on the Fetch computer desktop. Hermes stays on the host;
# the process itself runs inside fetch-computer against that container's X
# server and cookie. Do not point host Chrome at the host X11 socket directory.
if [[ $# -eq 0 ]]; then
  printf 'Usage: %s <command> [args...]\n' "${0##*/}" >&2
  exit 2
fi

exec docker exec -i \
  -e DISPLAY="${DISPLAY:-:1}" \
  -e XAUTHORITY="${XAUTHORITY:-/home/fetch/.Xauthority}" \
  fetch-computer \
  "$@"
