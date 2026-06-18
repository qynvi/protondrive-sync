"""Systemd user service generation and management for Linux."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from textwrap import dedent

from ..core.config import AppConfig
from ..core.platform import is_linux


BISYNC_SERVICE_NAME = "protondrive-bisync"

ALL_SERVICE_NAMES = (BISYNC_SERVICE_NAME,)


class SystemdError(Exception):
    """Raised on systemd operation failures."""


def _user_unit_dir() -> Path:
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    return unit_dir


def _systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    """Run systemctl --user with the given arguments."""
    cmd = ["systemctl", "--user"] + list(args)
    return subprocess.run(cmd, capture_output=True, text=True)


# --- Unit file generation ---


def generate_bisync_service(config: AppConfig) -> str:
    """Generate the systemd unit file for the bisync daemon.

    This is a long-running service (Type=simple) because the adaptive
    timing loop runs internally — NOT a timer+oneshot.
    """
    python = sys.executable

    if config.low_footprint:
        priority_lines = "Nice=19\nIOSchedulingClass=idle"
    else:
        priority_lines = ""

    return dedent(f"""\
        [Unit]
        Description=ProtonDrive bisync daemon
        After=network-online.target
        Wants=network-online.target

        [Service]
        Type=simple
        ExecStart={python} -m protondrive_sync.bisync_main
        Restart=on-failure
        RestartSec=10
        Environment=HOME={Path.home()}
        {priority_lines}

        [Install]
        WantedBy=default.target
    """)


# --- Install / uninstall ---


def install_services(config: AppConfig) -> list[Path]:
    """Write systemd unit files to disk. Only installs services relevant
    to the configured folder modes. Returns paths written."""
    if not is_linux():
        raise SystemdError("systemd services are only supported on Linux")

    unit_dir = _user_unit_dir()
    written: list[Path] = []

    if config.has_enabled_folders():
        bisync_path = unit_dir / f"{BISYNC_SERVICE_NAME}.service"
        bisync_path.write_text(generate_bisync_service(config), encoding="utf-8")
        written.append(bisync_path)

    # Reload systemd to pick up new/changed units
    _systemctl("daemon-reload")

    return written


# --- Start / stop / enable / disable ---


def _get_active_service_names(config: AppConfig) -> list[str]:
    """Return which service names should be managed based on config."""
    names: list[str] = []
    if config.has_enabled_folders():
        names.append(BISYNC_SERVICE_NAME)
    return names


def enable_services(config: AppConfig | None = None) -> bool:
    """Enable relevant services to start on login."""
    names = _get_active_service_names(config) if config else list(ALL_SERVICE_NAMES)
    ok = True
    for name in names:
        r = _systemctl("enable", f"{name}.service")
        ok = ok and r.returncode == 0
    return ok


def disable_services(config: AppConfig | None = None) -> bool:
    """Disable services from starting on login."""
    # Disable all — safe even if unit doesn't exist
    ok = True
    for name in ALL_SERVICE_NAMES:
        r = _systemctl("disable", f"{name}.service")
        # Ignore errors for services that don't exist
    return ok


def start_services(config: AppConfig | None = None) -> bool:
    """Start relevant services now."""
    names = _get_active_service_names(config) if config else list(ALL_SERVICE_NAMES)
    ok = True
    for name in names:
        r = _systemctl("start", f"{name}.service")
        ok = ok and r.returncode == 0
    return ok


def stop_services(config: AppConfig | None = None) -> bool:
    """Stop all services."""
    ok = True
    # Stop in reverse dependency order
    for name in reversed(ALL_SERVICE_NAMES):
        r = _systemctl("stop", f"{name}.service")
        # Don't fail if a service wasn't running
    return ok


# --- Status ---


def service_status(service_name: str) -> dict[str, str]:
    """Get the status of a systemd service. Returns key properties."""
    result = _systemctl(
        "show",
        f"{service_name}.service",
        "--property=ActiveState,SubState,LoadState",
    )
    if result.returncode != 0:
        return {"ActiveState": "unknown", "SubState": "unknown", "LoadState": "unknown"}

    props: dict[str, str] = {}
    for line in result.stdout.strip().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            props[k] = v
    return props


def is_service_active(service_name: str) -> bool:
    """Check if a service is currently running."""
    status = service_status(service_name)
    return status.get("ActiveState") == "active"


def is_service_enabled(service_name: str) -> bool:
    """Check if a service is enabled (starts on login)."""
    result = _systemctl("is-enabled", f"{service_name}.service")
    return result.stdout.strip() == "enabled"
