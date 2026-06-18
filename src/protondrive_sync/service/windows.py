"""Windows Task Scheduler integration for the background bisync daemon."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from ..core.config import AppConfig
from ..core.platform import is_windows


BISYNC_TASK_NAME = "ProtonDriveBisync"

ALL_TASK_NAMES = (BISYNC_TASK_NAME,)


class WindowsServiceError(Exception):
    """Raised on Windows task scheduler failures."""


def _schtasks(*args: str) -> subprocess.CompletedProcess[str]:
    """Run schtasks.exe with given arguments."""
    cmd = ["schtasks.exe"] + list(args)
    return subprocess.run(cmd, capture_output=True, text=True)


def _bat_dir() -> Path:
    bat_dir = Path.home() / ".protondrive-sync"
    bat_dir.mkdir(parents=True, exist_ok=True)
    return bat_dir


def install_bisync_task(config: AppConfig) -> Path:
    """Create a Windows scheduled task for the bisync daemon."""
    if not is_windows():
        raise WindowsServiceError("Windows task scheduler only available on Windows")

    python = sys.executable
    bat_path = _bat_dir() / "bisync.bat"
    bat_path.write_text(
        f'@echo off\n"{python}" -m protondrive_sync.bisync_main\n',
        encoding="utf-8",
    )

    result = _schtasks(
        "/Create",
        "/TN",
        BISYNC_TASK_NAME,
        "/TR",
        str(bat_path),
        "/SC",
        "ONLOGON",
        "/RL",
        "LIMITED",
        "/F",
    )
    if result.returncode != 0:
        raise WindowsServiceError(f"Failed to create task: {result.stderr}")
    return bat_path


def install_tasks(config: AppConfig) -> list[Path]:
    """Install relevant tasks based on config. Returns paths written."""
    paths: list[Path] = []
    if config.has_enabled_folders():
        paths.append(install_bisync_task(config))
    return paths


def remove_tasks() -> bool:
    """Remove all scheduled tasks."""
    for name in ALL_TASK_NAMES:
        _schtasks("/Delete", "/TN", name, "/F")
    return True


def is_task_running(task_name: str) -> bool:
    """Check if a scheduled task is currently running."""
    result = _schtasks("/Query", "/TN", task_name, "/FO", "CSV", "/NH")
    if result.returncode != 0:
        return False
    return "Running" in result.stdout


def start_task(task_name: str) -> bool:
    """Run a scheduled task immediately."""
    result = _schtasks("/Run", "/TN", task_name)
    return result.returncode == 0


def stop_task(task_name: str) -> bool:
    """End a running scheduled task."""
    result = _schtasks("/End", "/TN", task_name)
    return result.returncode == 0
