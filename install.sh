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

# Proton Drive CLI
if [ -x "${SCRIPT_DIR}/vendor/proton-drive-cli/proton-drive" ]; then
    CLI_VER=$("${SCRIPT_DIR}/vendor/proton-drive-cli/proton-drive" --version 2>/dev/null | head -1)
    ok "Proton Drive CLI: ${CLI_VER}"
else
    info "Installing Proton Drive CLI ..."
    "${SCRIPT_DIR}/scripts/install-proton-cli.sh"
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
info "Creating desktop entry ..."

LAUNCHER="${SCRIPT_DIR}/launch.sh"
chmod +x "$LAUNCHER" 2>/dev/null || true

# launch.sh detects a terminal and spawns one itself when started from a GUI,
# so the desktop entry just runs it directly (Terminal=false). This avoids
# relying on the desktop environment to resolve a terminal for Terminal=true.
make_desktop_file() {
    cat > "$1" << DESKTOP_EOF
[Desktop Entry]
Type=Application
Name=ProtonDrive Sync
Comment=TUI management tool for Proton Drive CLI sync
Exec=${LAUNCHER}
Terminal=false
Icon=${ICON_NAME}
Categories=Utility;FileTools;
StartupNotify=false
DESKTOP_EOF
    chmod +x "$1"
}

# Applications menu (app drawer)
APPLICATIONS_DIR="${HOME}/.local/share/applications"
mkdir -p "$APPLICATIONS_DIR"
make_desktop_file "${APPLICATIONS_DIR}/${APP_NAME}.desktop"
update-desktop-database "$APPLICATIONS_DIR" 2>/dev/null || true
ok "Added to applications menu (${APPLICATIONS_DIR}/${APP_NAME}.desktop)"

# Desktop shortcut (respects XDG)
if command -v xdg-user-dir &>/dev/null; then
    DESKTOP_DIR=$(xdg-user-dir DESKTOP 2>/dev/null)
else
    DESKTOP_DIR="${HOME}/Desktop"
fi
if [ -z "$DESKTOP_DIR" ] || [ ! -d "$DESKTOP_DIR" ]; then
    DESKTOP_DIR="${HOME}/Desktop"
fi

if [ -d "$DESKTOP_DIR" ]; then
    DESKTOP_FILE="${DESKTOP_DIR}/${APP_NAME}.desktop"
    make_desktop_file "$DESKTOP_FILE"
    # Mark as trusted so GNOME/Nautilus will launch it (best-effort)
    gio set "$DESKTOP_FILE" metadata::trusted true 2>/dev/null || true
    ok "Desktop shortcut created at ${DESKTOP_FILE}"
else
    warn "Desktop directory not found at ${DESKTOP_DIR}, skipping desktop shortcut."
fi

# ---------- done ----------

echo ""
echo "============================================"
echo "  ProtonDrive Sync installed successfully"
echo "============================================"
echo ""
echo "  Next steps:"
echo ""
echo "  1. Authenticate the Proton Drive CLI if needed:"
echo "       ${SCRIPT_DIR}/vendor/proton-drive-cli/proton-drive auth login"
echo "  2. Launch the app: double-click 'ProtonDrive Sync' on desktop"
echo "                or: ${VENV_DIR}/bin/protondrive-sync"

echo ""
