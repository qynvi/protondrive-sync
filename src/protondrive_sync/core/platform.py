"""Platform detection and path conventions."""

from __future__ import annotations

import os
import platform
import shutil
import sys
from pathlib import Path
from typing import Optional


def is_linux() -> bool:
    return platform.system() == "Linux"


def is_windows() -> bool:
    return platform.system() == "Windows"


def is_macos() -> bool:
    return platform.system() == "Darwin"


def get_arch() -> str:
    """Return normalised architecture string: aarch64, x86_64, etc."""
    machine = platform.machine().lower()
    # Normalise common aliases
    if machine in ("arm64", "aarch64"):
        return "aarch64"
    if machine in ("x86_64", "amd64"):
        return "x86_64"
    return machine


def get_config_dir() -> Path:
    """Return the application config directory, respecting XDG on Linux."""
    if is_windows():
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    config_dir = base / "protondrive-sync"
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir


def get_cache_dir() -> Path:
    """Return the application cache directory."""
    if is_windows():
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    cache_dir = base / "protondrive-sync"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def get_log_dir() -> Path:
    """Return the log directory."""
    if is_windows():
        log_dir = get_cache_dir() / "logs"
    else:
        log_dir = Path.home() / ".local" / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def repo_root() -> Path | None:
    """Return the application repo root for source/editable installs."""
    candidate = Path(__file__).resolve().parents[3]
    if (candidate / "pyproject.toml").exists():
        return candidate
    return None


def find_proton_cli(explicit_path: str | None = None) -> str | None:
    """Locate the Proton Drive CLI binary.

    Resolution order:
    1. explicit configured path (config.proton_cli_path)
    2. vendored binary installed by scripts/install-proton-cli.sh
    3. PATH lookup
    4. conventional user locations
    """
    binary_name = "proton-drive.exe" if is_windows() else "proton-drive"

    if explicit_path:
        candidate = Path(explicit_path).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)

    root = repo_root()
    if root is not None:
        vendored = root / "vendor" / "proton-drive-cli" / binary_name
        if vendored.is_file() and os.access(vendored, os.X_OK):
            return str(vendored)

    on_path = shutil.which("proton-drive")
    if on_path:
        return on_path

    for candidate in (
        Path.home() / ".local" / "bin" / binary_name,
        Path.home() / "Desktop" / binary_name,
    ):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def supports_symlinks() -> bool:
    """Check whether the platform supports symlinks without elevation."""
    if is_linux() or is_macos():
        return True
    if is_windows():
        # Windows supports symlinks if developer mode is enabled,
        # but junctions always work for directories. We handle this
        # in symlinks.py — here we just report general support.
        return True
    return False


# ---------------------------------------------------------------------------
# Instance lock — prevents multiple TUI windows from running concurrently.
# Two instances writing config.json concurrently causes data loss.
# ---------------------------------------------------------------------------

_lock_fd: Optional[int] = None


class InstanceAlreadyRunning(Exception):
    """Raised when another TUI instance already holds the lock."""


def _lock_path() -> Path:
    return get_config_dir() / ".tui.lock"


def acquire_instance_lock() -> None:
    """Acquire an exclusive lock file. Raises InstanceAlreadyRunning if held.

    The lock is held for the lifetime of the process via an open file
    descriptor. It is automatically released when the process exits
    (even on crash/SIGKILL) because the OS closes all file descriptors.
    """
    global _lock_fd
    if _lock_fd is not None:
        return  # Already holding the lock

    lock_file = _lock_path()
    lock_file.parent.mkdir(parents=True, exist_ok=True)

    fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        if is_windows():
            import msvcrt

            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except (IOError, OSError):
                os.close(fd)
                raise InstanceAlreadyRunning(
                    "Another ProtonDrive Sync window is already running."
                )
        else:
            import fcntl

            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (IOError, OSError):
                os.close(fd)
                raise InstanceAlreadyRunning(
                    "Another ProtonDrive Sync window is already running."
                )
    except InstanceAlreadyRunning:
        raise
    except Exception:
        os.close(fd)
        raise

    # Write our PID into the lock file so other instances can identify us
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode())
    os.fsync(fd)

    _lock_fd = fd


def get_lock_holder_pid() -> int | None:
    """Read the PID of the process holding the instance lock.

    Returns None if the lock file doesn't exist, is empty, or contains
    non-numeric content.
    """
    try:
        text = _lock_path().read_text().strip()
        return int(text) if text else None
    except (OSError, ValueError):
        return None


def kill_instance(pid: int) -> bool:
    """Send SIGTERM to a process and wait up to 5 seconds for it to exit.

    Returns True if the process is no longer running, False if it
    didn't terminate in time.  On Windows, os.kill with SIGTERM calls
    TerminateProcess (hard kill).
    """
    import signal
    import time

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return True  # already dead

    for _ in range(50):  # 5s in 100ms steps
        time.sleep(0.1)
        try:
            os.kill(pid, 0)  # probe — raises OSError if dead
        except OSError:
            return True

    return False  # didn't die in time


def release_instance_lock() -> None:
    """Release the instance lock if held."""
    global _lock_fd
    if _lock_fd is None:
        return

    try:
        if is_windows():
            import msvcrt

            try:
                msvcrt.locking(_lock_fd, msvcrt.LK_UNLCK, 1)
            except (IOError, OSError):
                pass
        else:
            import fcntl

            try:
                fcntl.flock(_lock_fd, fcntl.LOCK_UN)
            except (IOError, OSError):
                pass
        os.close(_lock_fd)
    except OSError:
        pass

    _lock_fd = None

    # Clean up the lock file (best-effort)
    try:
        _lock_path().unlink(missing_ok=True)
    except OSError:
        pass
