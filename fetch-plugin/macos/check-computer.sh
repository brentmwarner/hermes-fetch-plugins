#!/usr/bin/env bash
set -euo pipefail

if [ "$(uname -s)" != "Darwin" ]; then
  printf 'This check is for macOS.\n' >&2
  exit 1
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
python3 "${script_dir}/../computer_setup.py" \
  --target tcp://127.0.0.1:5900 \
  --kind "Mac desktop" \
  --ask-vnc-password \
  --headed-browser \
  --wait-seconds 15
printf 'Fetch saved the dedicated VNC password on this Mac. The iPhone will connect without a transport credential prompt.\n'
