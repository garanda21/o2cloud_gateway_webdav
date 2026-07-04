#!/bin/sh
set -eu

# --- Privilege / permissions bootstrap -------------------------------------
# When started as root, align the runtime user with the host-provided PUID/PGID
# (so it can write the bind-mounted volumes), fix ownership, prepare the X11
# socket dir, then drop privileges to that user via gosu and re-exec this script.
PUID="${PUID:-10001}"
PGID="${PGID:-10001}"

if [ "$(id -u)" = "0" ]; then
  groupmod -o -g "$PGID" o2gateway 2>/dev/null || true
  usermod  -o -u "$PUID" -g "$PGID" o2gateway 2>/dev/null || true

  mkdir -p /config /cache /data /tmp/.X11-unix
  chmod 1777 /tmp/.X11-unix
  chown -R "$PUID:$PGID" /config /cache /data /home/o2gateway 2>/dev/null || true

  export HOME=/home/o2gateway
  exec gosu "$PUID:$PGID" "$0" "$@"
fi

export HOME="${HOME:-/home/o2gateway}"
export DISPLAY="${DISPLAY:-:99}"
XVFB_SCREEN="${XVFB_SCREEN:-1280x900x24}"
VNC_PORT="${VNC_PORT:-5900}"
NOVNC_HOST="${NOVNC_HOST:-0.0.0.0}"
NOVNC_PORT="${NOVNC_PORT:-6080}"

PIDS=""

start_bg() {
  "$@" &
  pid="$!"
  PIDS="$PIDS $pid"
}

cleanup() {
  for pid in $PIDS; do
    kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}

trap cleanup INT TERM EXIT

start_bg Xvfb "$DISPLAY" -screen 0 "$XVFB_SCREEN" -ac +extension RANDR

# Give the X server a short moment to bind before VNC and Chromium use it.
sleep 1

if command -v fluxbox >/dev/null 2>&1; then
  start_bg fluxbox >/tmp/fluxbox.log 2>&1
fi

start_bg x11vnc -display "$DISPLAY" -forever -shared -rfbport "$VNC_PORT" -nopw -quiet >/tmp/x11vnc.log 2>&1
start_bg websockify --web=/usr/share/novnc "$NOVNC_HOST:$NOVNC_PORT" "127.0.0.1:$VNC_PORT" >/tmp/novnc.log 2>&1

"$@" &
APP_PID="$!"
PIDS="$PIDS $APP_PID"

wait "$APP_PID"
