#!/usr/bin/env bash
set -euo pipefail

display_number=1
display_name=":${display_number}"
rfb_port=5901
xauthority="${XAUTHORITY:-${HOME}/.local/share/fetch-computer/Xauthority}"

if command -v Xtigervnc >/dev/null 2>&1; then
  vnc_server="$(command -v Xtigervnc)"
elif command -v Xvnc >/dev/null 2>&1; then
  vnc_server="$(command -v Xvnc)"
else
  printf 'Fetch virtual desktop requires Xvnc or Xtigervnc.\n' >&2
  exit 1
fi

# shellcheck disable=SC2329  # Invoked by the signal/exit trap below.
cleanup() {
  trap - EXIT INT TERM
  if [[ -n "${desktop_pid:-}" ]]; then
    kill "$desktop_pid" 2>/dev/null || true
  fi
  if [[ -n "${vnc_pid:-}" ]]; then
    kill "$vnc_pid" 2>/dev/null || true
  fi
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

lock_path="/tmp/.X${display_number}-lock"
socket_path="/tmp/.X11-unix/X${display_number}"
if [[ -f "$lock_path" ]]; then
  read -r existing_pid < "$lock_path" || existing_pid=""
  if [[ "$existing_pid" =~ ^[0-9]+$ ]] && kill -0 "$existing_pid" 2>/dev/null; then
    printf 'Fetch virtual display %s is already owned by pid %s.\n' \
      "$display_name" "$existing_pid" >&2
    exit 1
  fi
fi
if (exec 3<>"/dev/tcp/127.0.0.1/${rfb_port}") 2>/dev/null; then
  printf 'Fetch virtual desktop port %s is already in use.\n' "$rfb_port" >&2
  exit 1
fi
rm -f "$lock_path" "$socket_path"

install -d -m 0700 "$(dirname -- "$xauthority")"
touch "$xauthority"
chmod 0600 "$xauthority"
xauth -f "$xauthority" remove "$display_name" 2>/dev/null || true
cookie="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
xauth -f "$xauthority" add "$display_name" MIT-MAGIC-COOKIE-1 "$cookie"

XAUTHORITY="$xauthority" "$vnc_server" "$display_name" \
  -geometry 1920x1080 \
  -depth 24 \
  -rfbport "$rfb_port" \
  -localhost \
  -SecurityTypes None \
  -AlwaysShared \
  -auth "$xauthority" \
  -nolisten tcp &
vnc_pid=$!

for _attempt in {1..100}; do
  if [[ -S "$socket_path" ]]; then
    break
  fi
  if ! kill -0 "$vnc_pid" 2>/dev/null; then
    wait "$vnc_pid"
    exit 1
  fi
  sleep 0.1
done

if [[ ! -S "$socket_path" ]]; then
  printf 'Fetch virtual display %s did not become ready.\n' "$display_name" >&2
  exit 1
fi

unset DBUS_SESSION_BUS_ADDRESS DESKTOP_SESSION GDK_BACKEND QT_QPA_PLATFORM \
  SESSION_MANAGER WAYLAND_DISPLAY XDG_CURRENT_DESKTOP XDG_SESSION_DESKTOP XDG_SESSION_TYPE
DISPLAY="$display_name" XAUTHORITY="$xauthority" dbus-run-session -- startxfce4 &
desktop_pid=$!

while kill -0 "$vnc_pid" 2>/dev/null && kill -0 "$desktop_pid" 2>/dev/null; do
  sleep 2
done

exit 1
