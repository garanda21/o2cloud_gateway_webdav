#!/bin/sh
# Helper sourced by entrypoint.sh (and by the tests) to guarantee a writable
# HOME before Chromium is launched.
#
# WHY THIS EXISTS
# Chromium, started through Playwright for the assisted login, needs a
# writable HOME for its crashpad database. Without one it exits immediately:
#     chrome_crashpad_handler: --database is required
#     <process did exit: exitCode=null, signal=SIGTRAP>
# and the only user-visible symptom is a black noVNC screen with no error,
# which is very hard to diagnose.
#
# The root bootstrap in entrypoint.sh chowns /home/o2gateway and exports HOME,
# but it only runs when the container starts as root. Under rootless podman
# with --userns=keep-id the container starts directly as the host UID, so that
# block is skipped -- and podman injects a passwd entry for the host user
# whose home is the image WORKDIR (/app), which is root-owned. HOME is thus
# already set to an unwritable path and "${HOME:-...}" keeps it.

# ensure_writable_home [candidate...]
#   Leaves HOME untouched if it is already writable. Otherwise sets it to the
#   first candidate that can be created and written to. Prints the chosen
#   value. Returns 1 if nothing worked, so the caller can warn.
ensure_writable_home() {
  if [ -w "${HOME:-/nonexistent}" ]; then
    printf '%s\n' "$HOME"
    return 0
  fi

  _ewh_previous="${HOME:-unset}"
  for _ewh_candidate in "$@"; do
    [ -n "$_ewh_candidate" ] || continue
    if mkdir -p "$_ewh_candidate" 2>/dev/null && [ -w "$_ewh_candidate" ]; then
      HOME="$_ewh_candidate"
      export HOME
      echo "entrypoint: HOME=$_ewh_previous is not writable, using $HOME" >&2
      unset _ewh_candidate _ewh_previous
      printf '%s\n' "$HOME"
      return 0
    fi
  done

  # Nothing worked. Still leave HOME with a value so the rest of the
  # entrypoint (and anything it spawns) does not run with HOME unset, which
  # breaks tools in a different and even more confusing way.
  HOME="${HOME:-${1:-/home/o2gateway}}"
  export HOME
  echo "entrypoint: no writable HOME found; the assisted login will fail." >&2
  echo "entrypoint: pass -e HOME=/config/home and make sure /config is writable." >&2
  unset _ewh_candidate _ewh_previous
  printf '%s\n' "$HOME"
  return 1
}
