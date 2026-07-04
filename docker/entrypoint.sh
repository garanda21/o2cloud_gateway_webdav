#!/bin/sh
set -eu

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
