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
  --wait-seconds 15
printf 'Fetch will ask for the VNC password on the iPhone when you connect.\n'
