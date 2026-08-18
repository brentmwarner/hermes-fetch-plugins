#!/usr/bin/env bash
set -euo pipefail

# Run a GUI command on this Hermes profile's Fetch computer desktop.
# Hermes stays on the host; the process runs inside fetch-computer against
# DISPLAY=:N for this bot. Do not point host Chrome at the host X11 socket.
if [[ $# -eq 0 ]]; then
  printf 'Usage: %s <command> [args...]\n' "${0##*/}" >&2
  exit 2
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
plugin_dir="$(cd -- "${script_dir}/.." && pwd)"

display="${FETCH_DISPLAY:-}"
if [[ -z "$display" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    py=python3
  elif command -v python >/dev/null 2>&1; then
    py=python
  else
    py=""
  fi
  if [[ -n "$py" ]]; then
    display="$("$py" -c "
import importlib.util, sys
sys.path.insert(0, r'''${plugin_dir}''')
path = r'''${script_dir}/manage.py'''
spec = importlib.util.spec_from_file_location('fetch_plugin_linux_computer', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
print(module.display_name(module.pin_and_start_display()))
" 2>/dev/null || true)"
  fi
fi
if [[ -z "$display" ]]; then
  display=":1"
fi

exec docker exec -i \
  -e DISPLAY="${display}" \
  -e XAUTHORITY="/home/fetch/.Xauthority" \
  fetch-computer \
  "$@"
