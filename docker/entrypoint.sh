#!/bin/sh
set -eu

ENTRYPOINT_DIR=$(CDPATH= cd "$(dirname "$0")" && pwd)
. "$ENTRYPOINT_DIR/xdisplay.sh"

export DISPLAY="${DISPLAY:-:99}"
X_DISPLAY_NUMBER=$(x_display_number "$DISPLAY")
X_SOCKET_FILE="/tmp/.X11-unix/X${X_DISPLAY_NUMBER}"

# --- Privilege / permissions bootstrap -------------------------------------
# When started as root, align the runtime user with the host-provided PUID/PGID
# (so it can write the bind-mounted volumes), fix ownership, prepare the X11
# socket dir, then drop privileges to that user via gosu and re-exec this script.
PUID="${PUID:-10001}"
PGID="${PGID:-10001}"

if [ "$(id -u)" = "0" ]; then
  mkdir -p /tmp/.X11-unix
  chmod 1777 /tmp/.X11-unix
  x_prepare_display "$DISPLAY" /tmp

  groupmod -o -g "$PGID" o2gateway 2>/dev/null || true
  usermod  -o -u "$PUID" -g "$PGID" o2gateway 2>/dev/null || true

  mkdir -p /config /cache /data
  chown -R "$PUID:$PGID" /config /cache /data /home/o2gateway 2>/dev/null || true

  export HOME=/home/o2gateway
  exec gosu "$PUID:$PGID" "$0" "$@"
fi

export HOME="${HOME:-/home/o2gateway}"
XVFB_SCREEN="${XVFB_SCREEN:-1280x900x24}"
VNC_PORT="${VNC_PORT:-5900}"
NOVNC_HOST="${NOVNC_HOST:-0.0.0.0}"
NOVNC_PORT="${NOVNC_PORT:-6080}"

mkdir -p /tmp/.X11-unix
chmod 1777 /tmp/.X11-unix 2>/dev/null || true
x_prepare_display "$DISPLAY" /tmp

PIDS=""
XVFB_PID=""
CLEANUP_DONE=0

start_bg() {
  "$@" &
  pid="$!"
  PIDS="$PIDS $pid"
}

cleanup() {
  [ "$CLEANUP_DONE" = "0" ] || return
  CLEANUP_DONE=1
  trap - INT TERM EXIT
  for pid in $PIDS; do
    kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
  x_prepare_display "$DISPLAY" /tmp || true
}

trap cleanup INT TERM EXIT

Xvfb "$DISPLAY" -screen 0 "$XVFB_SCREEN" -ac +extension RANDR >/tmp/xvfb.log 2>&1 &
XVFB_PID="$!"
PIDS="$PIDS $XVFB_PID"

# Give the X server a short moment to bind before VNC and Chromium use it.
sleep 1
if ! kill -0 "$XVFB_PID" 2>/dev/null || [ ! -S "$X_SOCKET_FILE" ]; then
  wait "$XVFB_PID" 2>/dev/null || true
  echo "Xvfb failed to start on $DISPLAY." >&2
  if [ -s /tmp/xvfb.log ]; then
    cat /tmp/xvfb.log >&2
  fi
  exit 1
fi

if command -v fluxbox >/dev/null 2>&1; then
  start_bg fluxbox >/tmp/fluxbox.log 2>&1
fi

start_bg x11vnc -display "$DISPLAY" -forever -shared -rfbport "$VNC_PORT" -nopw -quiet >/tmp/x11vnc.log 2>&1
start_bg websockify --web=/usr/share/novnc "$NOVNC_HOST:$NOVNC_PORT" "127.0.0.1:$VNC_PORT" >/tmp/novnc.log 2>&1

"$@" &
APP_PID="$!"
PIDS="$PIDS $APP_PID"

wait "$APP_PID"
