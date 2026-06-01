"""Windows Task Scheduler integration for background mount, pinner, and bisync."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from ..core.config import AppConfig
from ..core.platform import find_rclone, is_windows
from ..core.rclone import build_mount_args


MOUNT_TASK_NAME = "ProtonDriveMount"
PINNER_TASK_NAME = "ProtonDrivePinner"
BISYNC_TASK_NAME = "ProtonDriveBisync"

ALL_TASK_NAMES = (MOUNT_TASK_NAME, PINNER_TASK_NAME, BISYNC_TASK_NAME)


class WindowsServiceError(Exception):
    """Raised on Windows task scheduler failures."""


def _schtasks(*args: str) -> subprocess.CompletedProcess[str]:
    """Run schtasks.exe with given arguments."""
    cmd = ["schtasks.exe"] + list(args)
    return subprocess.run(cmd, capture_output=True, text=True)


def generate_mount_bat(config: AppConfig) -> str:
    """Generate a .bat script that runs rclone mount."""
    rclone_bin = find_rclone() or "rclone"
    mount_args = build_mount_args(config)
    args_str = " ".join(mount_args)
    return f'@echo off\n"{rclone_bin}" {args_str}\n'


def _bat_dir() -> Path:
    bat_dir = Path.home() / ".protondrive-sync"
    bat_dir.mkdir(parents=True, exist_ok=True)
    return bat_dir


def install_mount_task(config: AppConfig) -> Path:
    """Create a Windows scheduled task for rclone mount on logon."""
    if not is_windows():
        raise WindowsServiceError("Windows task scheduler only available on Windows")

    bat_path = _bat_dir() / "mount.bat"
    bat_path.write_text(generate_mount_bat(config), encoding="utf-8")

    result = _schtasks(
        "/Create", "/TN", MOUNT_TASK_NAME,
        "/TR", str(bat_path), "/SC", "ONLOGON",
        "/RL", "LIMITED", "/F",
    )
    if result.returncode != 0:
        raise WindowsServiceError(f"Failed to create task: {result.stderr}")
    return bat_path


def install_pinner_task(config: AppConfig) -> Path:
    """Create a Windows scheduled task for the cache pinner."""
    if not is_windows():
        raise WindowsServiceError("Windows task scheduler only available on Windows")

    python = sys.executable
    bat_path = _bat_dir() / "pinner.bat"
    bat_path.write_text(
        f'@echo off\n"{python}" -m protondrive_sync.pinner_main\n',
        encoding="utf-8",
    )

    result = _schtasks(
        "/Create", "/TN", PINNER_TASK_NAME,
        "/TR", str(bat_path), "/SC", "ONLOGON",
        "/RL", "LIMITED", "/F",
    )
    if result.returncode != 0:
        raise WindowsServiceError(f"Failed to create task: {result.stderr}")
    return bat_path


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
        "/Create", "/TN", BISYNC_TASK_NAME,
        "/TR", str(bat_path), "/SC", "ONLOGON",
        "/RL", "LIMITED", "/F",
    )
    if result.returncode != 0:
        raise WindowsServiceError(f"Failed to create task: {result.stderr}")
    return bat_path


def install_tasks(config: AppConfig) -> list[Path]:
    """Install relevant tasks based on config. Returns paths written."""
    paths: list[Path] = []
    if config.has_mount_folders():
        paths.append(install_mount_task(config))
        paths.append(install_pinner_task(config))
    if config.has_bisync_folders():
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
