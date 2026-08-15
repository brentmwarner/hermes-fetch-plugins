#!/usr/bin/env bash
set -euo pipefail

action="${1:-install}"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
user_config_dir="${XDG_CONFIG_HOME:-$HOME/.config}"
service_dir="${user_config_dir}/systemd/user"
vnc_service="${service_dir}/fetch-computer-vnc.service"

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    printf 'Missing required command: %s\n' "$1" >&2
    printf 'On Ubuntu 24.04: sudo apt install xfce4 dbus-x11 tigervnc-standalone-server\n' >&2
    exit 1
  fi
}

require_linger() {
  local linger_state
  linger_state="$(loginctl show-user "$(id -un)" --property=Linger --value 2>/dev/null || true)"
  if [[ "$linger_state" != "yes" ]]; then
    printf 'Fetch computer access must remain running after SSH logout. Enable it first:\n' >&2
    printf '  sudo loginctl enable-linger %q\n' "$(id -un)" >&2
    exit 1
  fi
}

case "$action" in
  install)
    require_command systemctl
    require_command loginctl
    require_command tigervncserver
    require_command startxfce4
    require_linger
    install -d -m 0755 "$service_dir"
    install -m 0644 "${script_dir}/fetch-computer-vnc.service" "$vnc_service"
    systemctl --user daemon-reload
    systemctl --user enable --now fetch-computer-vnc.service
    python3 "${script_dir}/../computer_setup.py" \
      --target tcp://127.0.0.1:5901 \
      --kind "Virtual Linux desktop" \
      --display :1 \
      --wait-seconds 60
    ;;
  uninstall)
    systemctl --user disable --now fetch-computer-vnc.service || true
    rm -f "$vnc_service"
    systemctl --user daemon-reload || true
    python3 "${script_dir}/../computer_setup.py" --disable
    printf 'Fetch computer services were removed. Installed Ubuntu packages were left in place.\n'
    ;;
  status)
    require_command loginctl
    require_linger
    systemctl --user status fetch-computer-vnc.service
    python3 "${script_dir}/../computer_setup.py" \
      --target tcp://127.0.0.1:5901 \
      --kind "Virtual Linux desktop" \
      --display :1 \
      --wait-seconds 5 \
      --check-only
    ;;
  *)
    printf 'Usage: %s [install|uninstall|status]\n' "${0##*/}" >&2
    exit 2
    ;;
esac
