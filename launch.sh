#!/bin/bash
# Resize terminal to 174 columns x 45 rows, then launch the TUI app.
printf '\e[8;45;174t'
sleep 0.1
exec "$(dirname "$0")/.venv/bin/protondrive-sync"
