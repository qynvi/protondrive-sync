#!/usr/bin/env bash
set -euo pipefail

# ProtonDrive Sync installer
# Creates venv, installs deps, installs icon, places .desktop on desktop.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_NAME="protondrive-sync"
VENV_DIR="${SCRIPT_DIR}/.venv"
ICON_NAME="protondrive-sync"
ICON_SOURCE="${SCRIPT_DIR}/resources/protondrive-sync.svg"

# ---------- helpers ----------

info()  { echo -e "\033[1;34m[info]\033[0m  $*"; }
ok()    { echo -e "\033[1;32m[ok]\033[0m    $*"; }
warn()  { echo -e "\033[1;33m[warn]\033[0m  $*"; }
err()   { echo -e "\033[1;31m[error]\033[0m $*" >&2; }

require() {
    if ! command -v "$1" &>/dev/null; then
        err "$1 is required but not found."
        echo "    $2"
        exit 1
    fi
}

# ---------- preflight ----------

info "ProtonDrive Sync installer"
echo ""

# Python 3.11+
require python3 "Install Python 3.11+: sudo apt install python3 python3-venv"

PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PYTHON_MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)

if [ "$PYTHON_MAJOR" -lt 3 ] || { [ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt 11 ]; }; then
    err "Python 3.11+ required, found $PYTHON_VERSION"
    exit 1
fi
ok "Python $PYTHON_VERSION"

# rclone
if command -v rclone &>/dev/null; then
    RCLONE_VER=$(rclone version 2>/dev/null | head -1)
    ok "rclone: $RCLONE_VER"
else
    warn "rclone not installed."
    echo "    Install it before using the app:"
    ARCH=$(uname -m)
    if [ "$ARCH" = "aarch64" ] || [ "$ARCH" = "arm64" ]; then
        echo "      curl -L -O https://downloads.rclone.org/current/rclone-current-linux-arm64.deb"
        echo "      sudo dpkg -i rclone-current-linux-arm64.deb"
    elif [ "$ARCH" = "x86_64" ] || [ "$ARCH" = "amd64" ]; then
        echo "      curl -L -O https://downloads.rclone.org/current/rclone-current-linux-amd64.deb"
        echo "      sudo dpkg -i rclone-current-linux-amd64.deb"
    else
        echo "      curl https://rclone.org/install.sh | sudo bash"
    fi
    echo ""
fi

# FUSE
if command -v fusermount &>/dev/null || command -v fusermount3 &>/dev/null; then
    ok "FUSE available"
else
    warn "FUSE not found. Install it: sudo apt install fuse3"
fi

# ---------- venv + install ----------

echo ""
info "Setting up Python virtual environment ..."

if [ -d "$VENV_DIR" ]; then
    info "Existing venv found, reusing."
else
    python3 -m venv "$VENV_DIR"
fi

"${VENV_DIR}/bin/pip" install --upgrade pip --quiet
"${VENV_DIR}/bin/pip" install -e "${SCRIPT_DIR}" --quiet

ok "Python package installed"

# Verify the entry point works
if "${VENV_DIR}/bin/python3" -c "from protondrive_sync.app import main" 2>/dev/null; then
    ok "Import check passed"
else
    err "Import check failed — something went wrong during install."
    exit 1
fi

# ---------- icon ----------

echo ""
info "Installing icon ..."

if [ -f "$ICON_SOURCE" ]; then
    ICON_DIR="${HOME}/.local/share/icons/hicolor/scalable/apps"
    mkdir -p "$ICON_DIR"
    cp "$ICON_SOURCE" "${ICON_DIR}/${ICON_NAME}.svg"

    # Update icon cache (best-effort)
    gtk-update-icon-cache "${HOME}/.local/share/icons/hicolor/" 2>/dev/null || true

    ok "Icon installed to ${ICON_DIR}/${ICON_NAME}.svg"
else
    warn "Icon source not found at ${ICON_SOURCE}, skipping."
fi

# ---------- .desktop ----------

echo ""
info "Creating desktop shortcut ..."

# Find the user's Desktop directory (respects XDG)
if command -v xdg-user-dir &>/dev/null; then
    DESKTOP_DIR=$(xdg-user-dir DESKTOP 2>/dev/null)
else
    DESKTOP_DIR="${HOME}/Desktop"
fi

# Fallback if xdg-user-dir returns empty or nonexistent
if [ -z "$DESKTOP_DIR" ] || [ ! -d "$DESKTOP_DIR" ]; then
    DESKTOP_DIR="${HOME}/Desktop"
fi

if [ ! -d "$DESKTOP_DIR" ]; then
    warn "Desktop directory not found at ${DESKTOP_DIR}, skipping shortcut."
else
    # Detect terminal emulator
    TERMINAL=""
    TERMINAL_ARGS=""

    if command -v gnome-terminal &>/dev/null; then
        TERMINAL="gnome-terminal"
        TERMINAL_ARGS="--window --"
    elif command -v xfce4-terminal &>/dev/null; then
        TERMINAL="xfce4-terminal"
        TERMINAL_ARGS="--execute"
    elif command -v konsole &>/dev/null; then
        TERMINAL="konsole"
        TERMINAL_ARGS="-e"
    elif command -v xterm &>/dev/null; then
        TERMINAL="xterm"
        TERMINAL_ARGS="-e"
    fi

    LAUNCHER="${SCRIPT_DIR}/launch.sh"

    if [ -z "$TERMINAL" ]; then
        warn "No supported terminal emulator found. Writing .desktop with Terminal=true fallback."
        EXEC_LINE="${LAUNCHER}"
        USE_TERMINAL="true"
    else
        EXEC_LINE="${TERMINAL} ${TERMINAL_ARGS} ${LAUNCHER}"
        USE_TERMINAL="false"
    fi

    DESKTOP_FILE="${DESKTOP_DIR}/${APP_NAME}.desktop"

    cat > "$DESKTOP_FILE" << DESKTOP_EOF
[Desktop Entry]
Type=Application
Name=ProtonDrive Sync
Comment=TUI management tool for Proton Drive sync via rclone
Exec=${EXEC_LINE}
Terminal=${USE_TERMINAL}
Icon=${ICON_NAME}
Categories=Utility;FileTools;
StartupNotify=false
DESKTOP_EOF

    chmod +x "$DESKTOP_FILE"

    # Mark as trusted on GNOME (best-effort)
    gio set "$DESKTOP_FILE" metadata::trusted true 2>/dev/null || true

    ok "Desktop shortcut created at ${DESKTOP_FILE}"
fi

# ---------- also install to applications menu ----------

APPLICATIONS_DIR="${HOME}/.local/share/applications"
mkdir -p "$APPLICATIONS_DIR"
cp "${DESKTOP_DIR}/${APP_NAME}.desktop" "${APPLICATIONS_DIR}/${APP_NAME}.desktop" 2>/dev/null || true
ok "Also added to applications menu"

# ---------- done ----------

echo ""
echo "============================================"
echo "  ProtonDrive Sync installed successfully"
echo "============================================"
echo ""
echo "  Next steps:"
echo ""

if ! command -v rclone &>/dev/null; then
    echo "  1. Install rclone (see above)"
    echo "  2. Configure rclone:  rclone config"
    echo "     - Create a remote of type 'protondrive'"
    echo "     - Name it 'proton' (or update the name in app settings)"
    echo "  3. Launch the app:    double-click 'ProtonDrive Sync' on desktop"
    echo "                    or: ${VENV_DIR}/bin/protondrive-sync"
else
    if rclone listremotes 2>/dev/null | grep -q ":"; then
        REMOTE_NAME=$(rclone listremotes 2>/dev/null | head -1 | tr -d ':')
        echo "  rclone remote detected: ${REMOTE_NAME}"
        echo "  (Make sure the app settings match this name)"
        echo ""
    else
        echo "  1. Configure rclone:  rclone config"
        echo "     - Create a remote of type 'protondrive'"
        echo ""
    fi
    echo "  Launch the app:  double-click 'ProtonDrive Sync' on desktop"
    echo "               or: ${VENV_DIR}/bin/protondrive-sync"
fi

echo ""
