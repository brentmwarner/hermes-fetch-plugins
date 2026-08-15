#!/usr/bin/env bash
set -euo pipefail

display_number="${FETCH_DISPLAY_NUMBER:-1}"
display_name=":${display_number}"
rfb_port="${FETCH_RFB_PORT:-5901}"
geometry="${FETCH_GEOMETRY:-1280x800}"
localhost_flag="${FETCH_VNC_LOCALHOST:-1}"
xauthority="${XAUTHORITY:-${HOME:-/home/fetch}/.Xauthority}"
wallpaper="${FETCH_WALLPAPER:-/usr/share/fetch/wallpaper.png}"

export HOME="${HOME:-/home/fetch}"
export USER="${USER:-fetch}"
export LOGNAME="${LOGNAME:-$USER}"
export DISPLAY="$display_name"
export XAUTHORITY="$xauthority"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp/runtime-fetch}"
export LANG="${LANG:-C.UTF-8}"
export LC_ALL="${LC_ALL:-C.UTF-8}"

mkdir -p "$HOME" "$XDG_RUNTIME_DIR" "$(dirname -- "$xauthority")"
chmod 0700 "$XDG_RUNTIME_DIR" || true
if [[ -d /usr/share/fetch/skel/config ]]; then
  mkdir -p "$HOME/.config"
  cp -an /usr/share/fetch/skel/config/. "$HOME/.config/" 2>/dev/null || true
fi

if command -v Xtigervnc >/dev/null 2>&1; then
  vnc_server="$(command -v Xtigervnc)"
elif command -v Xvnc >/dev/null 2>&1; then
  vnc_server="$(command -v Xvnc)"
else
  printf 'Fetch computer image is missing Xvnc or Xtigervnc.\n' >&2
  exit 1
fi

# shellcheck disable=SC2329  # Invoked by the signal/exit trap below.
cleanup() {
  trap - EXIT INT TERM
  if [[ -n "${wallpaper_pid:-}" ]]; then
    kill "$wallpaper_pid" 2>/dev/null || true
  fi
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
if [[ -e "$socket_path" ]]; then
  printf 'Fetch virtual display %s already has a socket at %s.\n' \
    "$display_name" "$socket_path" >&2
  exit 1
fi
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
rm -f "$lock_path"
mkdir -p /tmp/.X11-unix
chmod 1777 /tmp/.X11-unix || true

touch "$xauthority"
chmod 0600 "$xauthority" || true
if ! xauth -f "$xauthority" nlist "$display_name" 2>/dev/null | grep -q .; then
  xauth -f "$xauthority" remove "$display_name" 2>/dev/null || true
  cookie="$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"
  xauth -f "$xauthority" add "$display_name" MIT-MAGIC-COOKIE-1 "$cookie" || true
fi

vnc_args=(
  "$display_name"
  -geometry "$geometry"
  -depth 24
  -rfbport "$rfb_port"
  -SecurityTypes None
  -AlwaysShared
  -auth "$xauthority"
  -nolisten tcp
)
if [[ "$localhost_flag" == "1" ]]; then
  vnc_args+=(-localhost)
fi

XAUTHORITY="$xauthority" "$vnc_server" "${vnc_args[@]}" &
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

paint_wallpaper() {
  [[ -f "$wallpaper" ]] || return 0
  if command -v hsetroot >/dev/null 2>&1; then
    DISPLAY="$display_name" XAUTHORITY="$xauthority" hsetroot -cover "$wallpaper" || true
  fi
  if command -v xfconf-query >/dev/null 2>&1; then
    while read -r prop; do
      [[ -n "$prop" ]] || continue
      DISPLAY="$display_name" XAUTHORITY="$xauthority" \
        xfconf-query -c xfce4-desktop -p "$prop" -n -t string -s "$wallpaper" || true
    done < <(DISPLAY="$display_name" XAUTHORITY="$xauthority" \
      xfconf-query -c xfce4-desktop -l 2>/dev/null | grep last-image || true)
  fi
}

(
  for _attempt in {1..40}; do
    sleep 0.5
    paint_wallpaper
  done
) &
wallpaper_pid=$!

while kill -0 "$vnc_pid" 2>/dev/null && kill -0 "$desktop_pid" 2>/dev/null; do
  sleep 2
done

exit 1
