#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if command -v python3 >/dev/null 2>&1; then
  exec python3 "${script_dir}/manage.py" "$@"
fi
if command -v python >/dev/null 2>&1; then
  exec python "${script_dir}/manage.py" "$@"
fi
printf 'python3 is required to manage the Fetch computer.\n' >&2
exit 1
