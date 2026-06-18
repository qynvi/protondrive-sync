#!/bin/bash
# Launch the ProtonDrive Sync TUI.
#
# When started from a terminal this resizes the window and runs the app.
# When started from a GUI launcher (.desktop entry / desktop icon) there is no
# controlling terminal, so we re-exec ourselves inside a detected terminal
# emulator. A guard env var prevents any re-exec loop.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_BIN="${SCRIPT_DIR}/.venv/bin/protondrive-sync"
SELF="${SCRIPT_DIR}/$(basename "${BASH_SOURCE[0]}")"

run_app() {
    # Resize terminal to 174 columns x 45 rows, then launch the TUI app.
    printf '\e[8;45;174t'
    sleep 0.1
    exec "$APP_BIN"
}

# Already inside a terminal, or re-exec'd by the block below.
if [ -t 1 ] || [ "${PROTONDRIVE_SYNC_REEXEC:-}" = "1" ]; then
    run_app
fi

# Launched without a terminal (e.g. from the app drawer / desktop icon).
# Find a terminal emulator and re-exec this script inside it.
export PROTONDRIVE_SYNC_REEXEC=1

if command -v ptyxis >/dev/null 2>&1; then
    exec ptyxis -- "$SELF"
elif command -v kgx >/dev/null 2>&1; then
    exec kgx -- "$SELF"
elif command -v gnome-console >/dev/null 2>&1; then
    exec gnome-console -- "$SELF"
elif command -v gnome-terminal >/dev/null 2>&1; then
    exec gnome-terminal -- "$SELF"
elif command -v konsole >/dev/null 2>&1; then
    exec konsole -e "$SELF"
elif command -v xfce4-terminal >/dev/null 2>&1; then
    exec xfce4-terminal -x "$SELF"
elif command -v alacritty >/dev/null 2>&1; then
    exec alacritty -e "$SELF"
elif command -v kitty >/dev/null 2>&1; then
    exec kitty "$SELF"
elif command -v xterm >/dev/null 2>&1; then
    exec xterm -e "$SELF"
elif command -v x-terminal-emulator >/dev/null 2>&1; then
    exec x-terminal-emulator -e "$SELF"
fi

# No terminal emulator found: run directly as a last resort.
run_app
