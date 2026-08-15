#!/usr/bin/env bash
set -euo pipefail

action="${1:-install}"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
user_config_dir="$(systemd-path user-configuration)"
service_dir="${user_config_dir}/systemd/user"
runtime_dir="${HOME}/.local/share/fetch-computer"
service_path="${service_dir}/fetch-computer-x11.service"

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    printf 'Missing required command: %s\n' "$1" >&2
    printf 'On Ubuntu: sudo apt install tigervnc-scraping-server\n' >&2
    exit 1
  fi
}

require_x11_session() {
  if [ "${XDG_SESSION_TYPE:-}" != "x11" ]; then
    printf 'Fetch physical-desktop sharing currently requires an X11 session.\n' >&2
    printf 'Choose an Xorg session at the Linux sign-in screen, sign in, and run this again.\n' >&2
    exit 1
  fi
  if [ -z "${DISPLAY:-}" ]; then
    printf 'DISPLAY is missing. Run setup from a terminal inside the desktop session.\n' >&2
    exit 1
  fi
}

case "$action" in
  install)
    require_command systemctl
    require_command x0vncserver
    require_x11_session
    install -d -m 0755 "$service_dir" "$runtime_dir"
    install -m 0755 "${script_dir}/run-x0vncserver.sh" "${runtime_dir}/run-x0vncserver.sh"
    install -m 0644 "${script_dir}/fetch-computer-x11.service" "$service_path"
    systemctl --user import-environment DISPLAY XAUTHORITY XDG_SESSION_TYPE
    systemctl --user daemon-reload
    systemctl --user enable --now fetch-computer-x11.service
    python3 "${script_dir}/../computer_setup.py" \
      --target tcp://127.0.0.1:5900 \
      --kind "Linux desktop" \
      --display "${DISPLAY}" \
      --wait-seconds 60
    ;;
  uninstall)
    systemctl --user disable --now fetch-computer-x11.service || true
    rm -f "$service_path" "${runtime_dir}/run-x0vncserver.sh"
    systemctl --user daemon-reload
    printf 'Fetch desktop sharing was removed. TigerVNC was left installed.\n'
    ;;
  status)
    systemctl --user status fetch-computer-x11.service
    python3 "${script_dir}/../computer_setup.py" \
      --target tcp://127.0.0.1:5900 \
      --kind "Linux desktop" \
      --display "${DISPLAY:-:0}" \
      --wait-seconds 5 \
      --check-only
    ;;
  *)
    printf 'Usage: %s [install|uninstall|status]\n' "${0##*/}" >&2
    exit 2
    ;;
esac
