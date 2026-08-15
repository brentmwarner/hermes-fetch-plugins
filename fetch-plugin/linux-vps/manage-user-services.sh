#!/usr/bin/env bash
set -euo pipefail

action="${1:-install}"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
user_config_dir="${XDG_CONFIG_HOME:-$HOME/.config}"
service_dir="${user_config_dir}/systemd/user"
runtime_dir="${HOME}/.local/share/fetch-computer"
vnc_service="${service_dir}/fetch-computer-vnc.service"

install_dependencies() {
  if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update
    sudo apt-get install -y xfce4 dbus-x11 tigervnc-standalone-server xauth
    return
  fi
  if command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y @xfce-desktop-environment tigervnc-server-minimal dbus-x11 xorg-x11-xauth
    return
  fi
  printf 'Fetch can install its virtual desktop automatically on Ubuntu/Debian or Fedora.\n' >&2
  printf 'Install XFCE, dbus-run-session, and TigerVNC (Xvnc or Xtigervnc), then rerun install.\n' >&2
  exit 1
}

require_runtime_dependencies() {
  local missing=()
  command -v systemctl >/dev/null 2>&1 || missing+=(systemctl)
  command -v loginctl >/dev/null 2>&1 || missing+=(loginctl)
  command -v startxfce4 >/dev/null 2>&1 || missing+=(startxfce4)
  command -v dbus-run-session >/dev/null 2>&1 || missing+=(dbus-run-session)
  command -v python3 >/dev/null 2>&1 || missing+=(python3)
  command -v xauth >/dev/null 2>&1 || missing+=(xauth)
  if ! command -v Xvnc >/dev/null 2>&1 && ! command -v Xtigervnc >/dev/null 2>&1; then
    missing+=(Xvnc)
  fi
  if (( ${#missing[@]} > 0 )); then
    printf 'Missing required commands: %s\n' "${missing[*]}" >&2
    printf 'Run %s bootstrap to install the supported virtual desktop first.\n' "${0##*/}" >&2
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
  bootstrap)
    install_dependencies
    sudo loginctl enable-linger "$(id -un)"
    exec "$0" install
    ;;
  install)
    require_runtime_dependencies
    require_linger
    install -d -m 0755 "$service_dir" "$runtime_dir"
    install -m 0755 "${script_dir}/run-virtual-desktop.sh" "${runtime_dir}/run-virtual-desktop.sh"
    install -m 0644 "${script_dir}/fetch-computer-vnc.service" "$vnc_service"
    systemctl --user daemon-reload
    systemctl --user enable --now fetch-computer-vnc.service
    python3 "${script_dir}/../computer_setup.py" \
      --target tcp://127.0.0.1:5901 \
      --kind "Virtual Linux desktop" \
      --display :1 \
      --xauthority "${runtime_dir}/Xauthority" \
      --headed-browser \
      --wait-seconds 60
    ;;
  uninstall)
    systemctl --user disable --now fetch-computer-vnc.service || true
    rm -f "$vnc_service" "${runtime_dir}/run-virtual-desktop.sh"
    systemctl --user daemon-reload || true
    python3 "${script_dir}/../computer_setup.py" --disable
    printf 'Fetch computer services were removed. Installed OS packages were left in place.\n'
    ;;
  status)
    require_command loginctl
    require_linger
    systemctl --user status fetch-computer-vnc.service
    python3 "${script_dir}/../computer_setup.py" \
      --target tcp://127.0.0.1:5901 \
      --kind "Virtual Linux desktop" \
      --display :1 \
      --xauthority "${runtime_dir}/Xauthority" \
      --headed-browser \
      --wait-seconds 5 \
      --check-only
    ;;
  *)
    printf 'Usage: %s [bootstrap|install|uninstall|status]\n' "${0##*/}" >&2
    exit 2
    ;;
esac
