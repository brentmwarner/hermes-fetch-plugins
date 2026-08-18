#!/usr/bin/env bash
# Extra X screens inside the one fetch-computer container.
# :1 is started by the entrypoint. Additional bots are DISPLAY=:N, not a
# second container. Recycled from Cloud's start-desktop.sh.
set -euo pipefail

HOME_DIR="${HOME:-/home/fetch}"
geometry="${FETCH_GEOMETRY:-1280x800}"
localhost_flag="${FETCH_VNC_LOCALHOST:-0}"
xauthority="${XAUTHORITY:-${HOME_DIR}/.Xauthority}"
wallpaper="${FETCH_WALLPAPER:-/usr/share/fetch/wallpaper.png}"

usage() {
  echo "usage: fetch-start-display :N | fetch-start-display --stop :N" >&2
  exit 2
}

display_num_from() {
  local raw="${1:-}"
  raw="${raw#:}"
  if [[ ! "$raw" =~ ^[0-9]+$ ]] || [[ "$raw" -lt 1 ]] || [[ "$raw" -gt 16 ]]; then
    echo "fetch-start-display: display must be :N with 1<=N<=16 (got ${1:-})" >&2
    exit 2
  fi
  echo "$raw"
}

vnc_port_for() {
  if [[ "$1" -eq 1 ]]; then
    echo 5901
  else
    echo $((5900 + $1))
  fi
}

slot_dir() {
  echo "$HOME_DIR/desktops/$1"
}

wanted_file() {
  echo "$HOME_DIR/desktops/wanted"
}

remember_display() {
  local n="$1"
  local file tmp
  file="$(wanted_file)"
  mkdir -p "$(dirname -- "$file")"
  touch "$file"
  tmp="$(mktemp)"
  grep -v "^${n}\$" "$file" >"$tmp" || true
  echo "$n" >>"$tmp"
  mv "$tmp" "$file"
}

forget_display() {
  local n="$1"
  local file tmp
  file="$(wanted_file)"
  [[ -f "$file" ]] || return 0
  tmp="$(mktemp)"
  grep -v "^${n}\$" "$file" >"$tmp" || true
  mv "$tmp" "$file"
}

x11_socket_path() {
  echo "/tmp/.X11-unix/X$1"
}

x11_socket_listening() {
  local path="$1"
  [[ -S "$path" ]] || return 1
  awk -v p="$path" '
    $4 == "00010000" && $NF == p { found = 1; exit }
    END { exit !found }
  ' /proc/net/unix 2>/dev/null
}

pick_vnc() {
  if command -v Xtigervnc >/dev/null 2>&1; then
    command -v Xtigervnc
  elif command -v Xvnc >/dev/null 2>&1; then
    command -v Xvnc
  else
    echo "fetch-start-display: missing Xtigervnc / Xvnc" >&2
    exit 1
  fi
}

ensure_xauth() {
  local display="$1"
  mkdir -p "$(dirname -- "$xauthority")"
  touch "$xauthority"
  chmod 0600 "$xauthority" || true
  if ! xauth -f "$xauthority" nlist "$display" 2>/dev/null | grep -q .; then
    xauth -f "$xauthority" remove "$display" 2>/dev/null || true
    local cookie
    cookie="$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"
    xauth -f "$xauthority" add "$display" MIT-MAGIC-COOKIE-1 "$cookie" || true
  fi
}

install_xfce_skel() {
  local dest="$1"
  mkdir -p "$dest"
  if [[ -d /usr/share/fetch/skel/config ]]; then
    cp -an /usr/share/fetch/skel/config/. "$dest/" 2>/dev/null || true
  fi
}

paint_wallpaper() {
  local display="$1"
  [[ -f "$wallpaper" ]] || return 0
  if command -v hsetroot >/dev/null 2>&1; then
    DISPLAY="$display" XAUTHORITY="$xauthority" hsetroot -cover "$wallpaper" || true
  fi
}

wait_for_socket() {
  local path="$1"
  local pid="$2"
  local i
  for i in $(seq 1 100); do
    if [[ -S "$path" ]]; then
      return 0
    fi
    if ! kill -0 "$pid" 2>/dev/null; then
      return 1
    fi
    sleep 0.1
  done
  return 1
}

stop_display() {
  local n
  n="$(display_num_from "${1:-}")"
  local dir
  dir="$(slot_dir "$n")"
  if [[ -d "$dir" ]]; then
    local pidfile pid
    for pidfile in "$dir"/*.pid; do
      [[ -f "$pidfile" ]] || continue
      pid="$(cat "$pidfile" 2>/dev/null || true)"
      if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
        kill "$pid" 2>/dev/null || true
      fi
      rm -f "$pidfile"
    done
  fi
  pkill -f "Xtigervnc :$n " 2>/dev/null || true
  pkill -f "Xvnc :$n " 2>/dev/null || true
  forget_display "$n"
  echo "fetch-start-display: stopped :$n"
}

start_display() {
  local n display dir vnc_port socket_path lock_path
  n="$(display_num_from "${1:-}")"
  display=":$n"
  dir="$(slot_dir "$n")"
  vnc_port="${FETCH_RFB_PORT:-$(vnc_port_for "$n")}"
  socket_path="$(x11_socket_path "$n")"
  lock_path="/tmp/.X${n}-lock"

  mkdir -p "$HOME_DIR" /tmp/.X11-unix "$dir"
  chmod 1777 /tmp/.X11-unix || true

  if DISPLAY="$display" XAUTHORITY="$xauthority" xdpyinfo >/dev/null 2>&1; then
    remember_display "$n"
    echo "fetch-start-display: :$n already up (vnc $vnc_port)"
    return 0
  fi
  if [[ -e "$socket_path" ]] && x11_socket_listening "$socket_path"; then
    remember_display "$n"
    echo "fetch-start-display: :$n already up (vnc $vnc_port)"
    return 0
  fi
  if [[ -e "$socket_path" ]]; then
    rm -f "$socket_path"
  fi
  if [[ -f "$lock_path" ]]; then
    local existing_pid
    read -r existing_pid < "$lock_path" || existing_pid=""
    if [[ "$existing_pid" =~ ^[0-9]+$ ]] && kill -0 "$existing_pid" 2>/dev/null; then
      echo "fetch-start-display: :$n is already owned by pid $existing_pid" >&2
      exit 1
    fi
    rm -f "$lock_path"
  fi

  local runtime="/tmp/xdg-runtime-fetch-$n"
  mkdir -p "$runtime"
  chmod 700 "$runtime"
  export XDG_RUNTIME_DIR="$runtime"

  local xfce_home="$dir/xfce"
  install_xfce_skel "$xfce_home"
  export XDG_CONFIG_HOME="$xfce_home"
  export XDG_CACHE_HOME="$dir/cache"
  export XDG_DATA_HOME="$dir/data"
  mkdir -p "$XDG_CACHE_HOME" "$XDG_DATA_HOME"

  ensure_xauth "$display"
  export DISPLAY="$display"
  export XAUTHORITY="$xauthority"

  local vnc_server
  vnc_server="$(pick_vnc)"
  local vnc_args=(
    "$display"
    -geometry "$geometry"
    -depth 24
    -rfbport "$vnc_port"
    -SecurityTypes None
    -AlwaysShared
    -auth "$xauthority"
    -nolisten tcp
  )
  if [[ "$localhost_flag" == "1" ]]; then
    vnc_args+=(-localhost)
  fi
  XAUTHORITY="$xauthority" "$vnc_server" "${vnc_args[@]}" >/tmp/tigervnc-"$n".log 2>&1 &
  echo $! >"$dir/vnc.pid"
  if ! wait_for_socket "$socket_path" "$!"; then
    echo "fetch-start-display: X display $display did not come up" >&2
    exit 1
  fi

  unset DBUS_SESSION_BUS_ADDRESS DESKTOP_SESSION GDK_BACKEND QT_QPA_PLATFORM \
    SESSION_MANAGER WAYLAND_DISPLAY XDG_CURRENT_DESKTOP XDG_SESSION_DESKTOP XDG_SESSION_TYPE
  DISPLAY="$display" XAUTHORITY="$xauthority" \
    XDG_CONFIG_HOME="$XDG_CONFIG_HOME" XDG_RUNTIME_DIR="$XDG_RUNTIME_DIR" \
    dbus-run-session -- startxfce4 >/tmp/xfce-"$n".log 2>&1 &
  echo $! >"$dir/desktop.pid"

  (
    local i
    for i in $(seq 1 40); do
      sleep 0.5
      paint_wallpaper "$display"
    done
  ) >/tmp/wallpaper-"$n".log 2>&1 &
  echo $! >"$dir/wallpaper.pid"

  remember_display "$n"
  echo "fetch-start-display: :$n ready (vnc $vnc_port)"
}

cmd="${1:-}"
if [[ "$cmd" == "--stop" ]]; then
  stop_display "${2:-}"
  exit 0
fi
if [[ "$cmd" == "-h" || "$cmd" == "--help" || -z "$cmd" ]]; then
  usage
fi
start_display "$cmd"
