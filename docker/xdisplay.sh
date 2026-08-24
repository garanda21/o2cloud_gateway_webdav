#!/bin/sh

# Helpers for safely reusing the fixed X display across container restarts.
# Xvfb stores its PID and Unix socket under /tmp; those artifacts can survive a
# Docker restart when the previous process was killed before it could clean up.

x_display_number() {
  x_display_value="$1"
  x_display_value="${x_display_value##*:}"
  x_display_value="${x_display_value%%.*}"

  case "$x_display_value" in
    ""|*[!0-9]*)
      echo "Invalid X display: $1" >&2
      return 2
      ;;
  esac

  printf '%s\n' "$x_display_value"
}

x_lock_owner_pid() {
  x_lock_file="$1"
  [ -r "$x_lock_file" ] || return 1

  x_owner_pid=$(tr -d '[:space:]' < "$x_lock_file" 2>/dev/null) || return 1
  case "$x_owner_pid" in
    ""|*[!0-9]*) return 1 ;;
  esac
  [ "$x_owner_pid" -gt 0 ] 2>/dev/null || return 1

  printf '%s\n' "$x_owner_pid"
}

x_prepare_display() {
  x_display="$1"
  x_runtime_dir="${2:-/tmp}"
  x_number=$(x_display_number "$x_display") || return $?
  x_lock_file="$x_runtime_dir/.X${x_number}-lock"
  x_socket_file="$x_runtime_dir/.X11-unix/X${x_number}"

  if [ -e "$x_lock_file" ] || [ -L "$x_lock_file" ]; then
    if x_owner_pid=$(x_lock_owner_pid "$x_lock_file") && kill -0 "$x_owner_pid" 2>/dev/null; then
      echo "X display $x_display is already active (PID $x_owner_pid); refusing to remove its lock." >&2
      return 1
    fi
  fi

  if [ -e "$x_lock_file" ] || [ -L "$x_lock_file" ] || [ -e "$x_socket_file" ] || [ -L "$x_socket_file" ]; then
    echo "Removing stale X display artifacts for $x_display." >&2
  fi
  rm -f "$x_lock_file" "$x_socket_file"
}
