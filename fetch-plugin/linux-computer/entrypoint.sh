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

# First boot copies the full skel without clobbering a persisted home.
# Branding chrome is then re-applied every start so a rebuild + restart
# picks up the glass panel / Fetch mark without wiping unrelated user files.
apply_fetch_branding() {
  local skel="/usr/share/fetch/skel/config"
  [[ -d "$skel" ]] || return 0
  mkdir -p "$HOME/.config"
  cp -an "$skel/." "$HOME/.config/" 2>/dev/null || true

  local rel dest
  for rel in \
    gtk-3.0/gtk.css \
    gtk-3.0/settings.ini \
    gtk-4.0/settings.ini \
    xfce4/xfconf/xfce-perchannel-xml/xfce4-panel.xml \
    xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml \
    xfce4/xfconf/xfce-perchannel-xml/xfwm4.xml \
    xfce4/xfconf/xfce-perchannel-xml/xsettings.xml
  do
    if [[ -f "$skel/$rel" ]]; then
      dest="$HOME/.config/$rel"
      mkdir -p "$(dirname -- "$dest")"
      cp -a "$skel/$rel" "$dest" || true
    fi
  done
}
apply_fetch_branding

# xfwm4 compositor is required for real XFCE panel alpha. Shadows stay off so
# TigerVNC painting stays stable. If a rebuild ever hits a compositor/VNC
# conflict, set FETCH_COMPOSITOR=0: gtk.css then reads as a solid dark glass
# bar (no wallpaper bleed) instead of a broken framebuffer.
if [[ "${FETCH_COMPOSITOR:-1}" != "1" ]]; then
  sed -i 's/name="use_compositing" type="bool" value="true"/name="use_compositing" type="bool" value="false"/' \
    "$HOME/.config/xfce4/xfconf/xfce-perchannel-xml/xfwm4.xml" 2>/dev/null || true
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
x11_socket_listening() {
  local path="$1"
  [[ -S "$path" ]] || return 1
  awk -v p="$path" '
    $4 == "00010000" && $NF == p { found = 1; exit }
    END { exit !found }
  ' /proc/net/unix 2>/dev/null
}
if [[ -e "$socket_path" ]]; then
  if x11_socket_listening "$socket_path"; then
    printf 'Fetch virtual display %s already has a live socket at %s.\n' \
      "$display_name" "$socket_path" >&2
    exit 1
  fi
  rm -f "$socket_path"
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

# The container owns the cookie. Never reuse a host-written Xauthority that
# may not match TigerVNC; regenerate on every start so Chrome/VA.gov can open.
touch "$xauthority"
chmod 0600 "$xauthority" || true
xauth -f "$xauthority" remove "$display_name" 2>/dev/null || true
cookie="$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"
xauth -f "$xauthority" add "$display_name" MIT-MAGIC-COOKIE-1 "$cookie"

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

xfconf() {
  DISPLAY="$display_name" XAUTHORITY="$xauthority" xfconf-query "$@"
}

paint_wallpaper() {
  [[ -f "$wallpaper" ]] || return 0
  if command -v hsetroot >/dev/null 2>&1; then
    DISPLAY="$display_name" XAUTHORITY="$xauthority" hsetroot -cover "$wallpaper" || true
  fi
  if command -v xfconf-query >/dev/null 2>&1; then
    while read -r prop; do
      [[ -n "$prop" ]] || continue
      xfconf -c xfce4-desktop -p "$prop" -n -t string -s "$wallpaper" || true
    done < <(xfconf -c xfce4-desktop -l 2>/dev/null | grep last-image || true)
    xfconf -c xfce4-desktop -p /desktop-icons/style -n -t int -s 0 || true
    xfconf -c xsettings -p /Net/ThemeName -n -t string -s Adwaita-dark || true
    xfconf -c xsettings -p /Net/IconThemeName -n -t string -s Adwaita || true
    if [[ "${FETCH_COMPOSITOR:-1}" == "1" ]]; then
      xfconf -c xfwm4 -p /general/use_compositing -n -t bool -s true || true
    else
      xfconf -c xfwm4 -p /general/use_compositing -n -t bool -s false || true
    fi
  fi
}

(
  for _attempt in {1..40}; do
    sleep 0.5
    paint_wallpaper
  done
) &
wallpaper_pid=$!

# Extra bot desktops (DISPLAY=:N) persist in ~/desktops/wanted so a container
# restart brings them back. :1 is this entrypoint's own VNC.
wanted_file="${HOME}/desktops/wanted"
mkdir -p "$(dirname -- "$wanted_file")"
if [[ ! -f "$wanted_file" ]] || ! grep -qx "$display_number" "$wanted_file" 2>/dev/null; then
  printf '%s\n' "$display_number" >>"$wanted_file"
fi
if [[ -x /usr/local/bin/fetch-start-display && -f "$wanted_file" ]]; then
  while read -r extra_num; do
    [[ "$extra_num" =~ ^[0-9]+$ ]] || continue
    [[ "$extra_num" -eq "$display_number" ]] && continue
    FETCH_VNC_LOCALHOST="${FETCH_VNC_LOCALHOST:-0}" \
      /usr/local/bin/fetch-start-display ":${extra_num}" &
  done <"$wanted_file"
fi

while kill -0 "$vnc_pid" 2>/dev/null && kill -0 "$desktop_pid" 2>/dev/null; do
  sleep 2
done

exit 1
