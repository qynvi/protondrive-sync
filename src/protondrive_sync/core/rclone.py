"""rclone subprocess management — mount, bisync, status, health checks."""

from __future__ import annotations

import json
import subprocess
import signal
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .config import AppConfig, write_filter_file
from .platform import find_rclone

ProgressCallback = Callable[[str], None]


class RcloneError(Exception):
    """Raised when an rclone operation fails."""


class RcloneCancelled(RcloneError):
    """Raised when an rclone operation is cancelled by the user."""


@dataclass
class RemoteInfo:
    """Information about a configured rclone remote."""

    name: str
    type: str


@dataclass
class SpaceInfo:
    """Disk usage reported by rclone about."""

    total: Optional[int] = None
    used: Optional[int] = None
    free: Optional[int] = None


@dataclass
class RemoteFileInfo:
    """File metadata from rclone lsjson."""

    path: str
    size: int
    is_dir: bool = False


def _rclone_bin() -> str:
    path = find_rclone()
    if path is None:
        raise RcloneError(
            "rclone not found. Install it: https://rclone.org/install/"
        )
    return path


def _run(
    args: list[str],
    *,
    timeout: int = 30,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run an rclone command and return the result."""
    cmd = [_rclone_bin()] + args
    try:
        return subprocess.run(
            cmd,
            capture_output=capture,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise RcloneError(f"rclone binary not found: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RcloneError(f"rclone command timed out: {' '.join(cmd)}") from exc


def _run_streaming(
    args: list[str],
    progress: ProgressCallback,
    *,
    timeout: int = 3600,
    cancel_event: Optional[threading.Event] = None,
) -> int:
    """Run an rclone command with real-time output streaming.

    rclone emits progress stats to stderr. Per-file transfer info also
    goes to stderr with -v. We read both stdout and stderr line-by-line
    and forward to the progress callback.

    If cancel_event is provided and becomes set, the process is terminated
    and RcloneCancelled is raised.

    Returns the process return code.
    """
    cmd = [_rclone_bin()] + args
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError as exc:
        raise RcloneError(f"rclone binary not found: {exc}") from exc

    stderr_lines: list[str] = []

    def _read_stream(stream: object, label: str) -> None:
        for line in stream:  # type: ignore[union-attr]
            stripped = line.rstrip()
            if stripped:
                stderr_lines.append(stripped)
                progress(f"  {stripped}")

    # Read stdout and stderr in parallel threads so neither blocks
    t_out = threading.Thread(target=_read_stream, args=(proc.stdout, "out"), daemon=True)
    t_err = threading.Thread(target=_read_stream, args=(proc.stderr, "err"), daemon=True)
    t_out.start()
    t_err.start()

    # Poll loop: check for cancellation every 0.5s
    deadline = time.monotonic() + timeout
    cancelled = False
    while True:
        try:
            proc.wait(timeout=0.5)
            break  # Process exited
        except subprocess.TimeoutExpired:
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                break
            if time.monotonic() > deadline:
                proc.kill()
                proc.wait()
                t_out.join(timeout=5)
                t_err.join(timeout=5)
                raise RcloneError(
                    f"rclone command timed out after {timeout}s: {' '.join(cmd)}"
                )

    t_out.join(timeout=5)
    t_err.join(timeout=5)

    if cancelled:
        raise RcloneCancelled("Operation cancelled by user")

    if proc.returncode != 0:
        # Collect last few lines for error context
        tail = "\n".join(stderr_lines[-10:])
        raise RcloneError(
            f"rclone failed (exit code {proc.returncode}):\n{tail}"
        )

    return proc.returncode


def _retry_args(config: AppConfig) -> list[str]:
    """Build retry and rate-limit flags for Proton Drive API resilience."""
    if config.low_footprint:
        tpslimit = "2"
        tpslimit_burst = "1"
        retries_sleep = "15s"
    else:
        tpslimit = "8"
        tpslimit_burst = "4"
        retries_sleep = "5s"

    return [
        "--retries", "5",
        "--retries-sleep", retries_sleep,
        "--low-level-retries", "10",
        "--tpslimit", tpslimit,
        "--tpslimit-burst", tpslimit_burst,
        # Auto-replace stale drafts from interrupted uploads — eliminates
        # 2501 "draft already exists" errors without manual intervention.
        "--protondrive-replace-existing-draft",
    ]


def _stats_args() -> list[str]:
    """Build progress stats flags for real-time output."""
    return [
        "--stats", "2s",
        "--stats-one-line",
        "-v",
    ]


def check_version() -> str:
    """Return the installed rclone version string."""
    result = _run(["version"])
    if result.returncode != 0:
        raise RcloneError(f"rclone version failed: {result.stderr}")
    # First line: "rclone v1.65.0"
    first_line = result.stdout.strip().splitlines()[0]
    return first_line


def list_remotes() -> list[RemoteInfo]:
    """List configured rclone remotes."""
    result = _run(["listremotes", "--long"])
    if result.returncode != 0:
        raise RcloneError(f"Failed to list remotes: {result.stderr}")
    remotes = []
    for line in result.stdout.strip().splitlines():
        if ":" in line:
            parts = line.split(":", 1)
            name = parts[0].strip()
            rtype = parts[1].strip() if len(parts) > 1 else "unknown"
            remotes.append(RemoteInfo(name=name, type=rtype))
    return remotes


def remote_exists(name: str) -> bool:
    """Check if a named remote is configured."""
    return any(r.name == name for r in list_remotes())


def get_space_info(remote: str) -> SpaceInfo:
    """Get storage usage for a remote via `rclone about`."""
    result = _run(["about", f"{remote}:", "--json"], timeout=60)
    if result.returncode != 0:
        raise RcloneError(f"rclone about failed: {result.stderr}")
    try:
        data = json.loads(result.stdout)
        return SpaceInfo(
            total=data.get("total"),
            used=data.get("used"),
            free=data.get("free"),
        )
    except json.JSONDecodeError:
        return SpaceInfo()


# --- Remote directory management ---


def rclone_mkdir(remote_path: str, config: AppConfig) -> None:
    """Create a directory on the remote if it doesn't already exist."""
    result = _run(["mkdir", f"{config.remote_name}:{remote_path}"])
    if result.returncode != 0:
        raise RcloneError(
            f"Failed to create remote directory '{remote_path}': {result.stderr}"
        )


# --- Mount mode ---


def build_mount_args(config: AppConfig) -> list[str]:
    """Build the argument list for `rclone mount`."""
    filter_path = write_filter_file(config)

    if config.low_footprint:
        transfers = 1
        checkers = 1
        poll_interval = "60s"
        bwlimit = "2M:10M"
    else:
        transfers = config.transfers
        checkers = config.checkers
        poll_interval = config.poll_interval
        bwlimit = None

    args = [
        "mount",
        f"{config.remote_name}:",
        config.mount_point,
        "--vfs-cache-mode", "full",
        "--vfs-write-back", config.write_back,
        "--poll-interval", poll_interval,
        "--dir-cache-time", config.dir_cache_time,
        "--vfs-cache-max-age", config.cache_max_age,
        "--vfs-cache-max-size", config.cache_max_size,
        "--transfers", str(transfers),
        "--checkers", str(checkers),
        "--log-level", config.log_level,
        "--log-file", str(config.log_file),
        "--filter-from", str(filter_path),
    ]

    if bwlimit:
        args.extend(["--bwlimit", bwlimit])

    return args


def mount(config: AppConfig) -> subprocess.Popen:
    """Start rclone mount as a background process. Returns the Popen handle."""
    mount_path = Path(config.mount_point)
    mount_path.mkdir(parents=True, exist_ok=True)

    args = build_mount_args(config)
    cmd = [_rclone_bin()] + args

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return proc


def unmount(mount_point: str) -> bool:
    """Unmount a FUSE mount. Returns True on success."""
    from .platform import is_linux, is_macos, is_windows

    if is_linux() or is_macos():
        result = subprocess.run(
            ["fusermount", "-u", mount_point],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            result = subprocess.run(
                ["umount", mount_point],
                capture_output=True,
                text=True,
            )
        return result.returncode == 0
    elif is_windows():
        return True
    return False


def is_mounted(mount_point: str) -> bool:
    """Check if a path is a FUSE mount point."""
    mount_path = Path(mount_point)
    if not mount_path.exists():
        return False
    try:
        result = subprocess.run(
            ["mountpoint", "-q", mount_point],
            capture_output=True,
        )
        return result.returncode == 0
    except FileNotFoundError:
        try:
            mounts = Path("/proc/mounts").read_text()
            return mount_point in mounts
        except OSError:
            return False


# --- Bisync mode ---


def build_bisync_args(
    local_path: str,
    remote_subpath: str,
    config: AppConfig,
    *,
    resync: bool = False,
) -> list[str]:
    """Build the argument list for `rclone bisync`."""
    filter_path = write_filter_file(config)

    if config.low_footprint:
        transfers = 1
        checkers = 1
        bwlimit = "2M:10M"
    else:
        transfers = config.transfers
        checkers = config.checkers
        bwlimit = None

    args = [
        "bisync",
        local_path,
        f"{config.remote_name}:{remote_subpath}",
        "--transfers", str(transfers),
        "--checkers", str(checkers),
        "--filter-from", str(filter_path),
        "--log-level", config.log_level,
        "--log-file", str(config.log_file),
        # Disable rclone's built-in abort-on-mass-delete — we handle
        # safety checks ourselves in core/bisync.py
        "--resilient",
        # Symlink handling: follow symlinks and sync target content as
        # regular files, or silently skip them if disabled
        "--copy-links" if config.copy_links else "--skip-links",
    ]
    args.extend(_retry_args(config))

    if resync:
        args.append("--resync")

    if bwlimit:
        args.extend(["--bwlimit", bwlimit])

    return args


def run_bisync(
    local_path: str,
    remote_subpath: str,
    config: AppConfig,
    *,
    resync: bool = False,
    progress: Optional[ProgressCallback] = None,
    cancel_event: Optional[threading.Event] = None,
) -> str:
    """Execute rclone bisync.

    If progress callback is provided, streams output in real-time.
    Otherwise, runs silently and returns stdout.
    Raises RcloneError on failure, RcloneCancelled if cancelled.
    """
    args = build_bisync_args(
        local_path, remote_subpath, config, resync=resync,
    )

    if progress:
        # Remove --log-level and --log-file — they conflict with -v
        # from _stats_args(). In streaming mode output goes to the
        # progress callback, so the log file isn't needed.
        cleaned: list[str] = []
        skip_next = False
        for arg in args:
            if skip_next:
                skip_next = False
                continue
            if arg in ("--log-level", "--log-file"):
                skip_next = True
                continue
            cleaned.append(arg)
        cleaned.extend(_stats_args())
        _run_streaming(cleaned, progress, timeout=3600, cancel_event=cancel_event)
        return ""
    else:
        result = _run(args, timeout=3600)
        if result.returncode != 0:
            raise RcloneError(
                f"bisync failed ({local_path} <> {config.remote_name}:{remote_subpath}):\n"
                f"{result.stderr}"
            )
        return result.stdout


# --- Shared utilities ---


def sync_upload(
    local_path: str,
    remote_path: str,
    config: AppConfig,
    *,
    progress: Optional[ProgressCallback] = None,
    cancel_event: Optional[threading.Event] = None,
) -> None:
    """One-shot upload: rclone copy local → remote. Used during migration.

    If progress callback is provided, streams output in real-time.
    Raises RcloneCancelled if cancel_event is set during streaming.
    """
    filter_path = write_filter_file(config)

    if config.low_footprint:
        transfers = 1
        checkers = 1
    else:
        transfers = config.transfers
        checkers = config.checkers

    args = [
        "copy",
        local_path,
        f"{config.remote_name}:{remote_path}",
        "--filter-from", str(filter_path),
        "--transfers", str(transfers),
        "--checkers", str(checkers),
        "--copy-links" if config.copy_links else "--skip-links",
    ]
    args.extend(_retry_args(config))

    if progress:
        args.extend(_stats_args())
        _run_streaming(args, progress, timeout=3600, cancel_event=cancel_event)
    else:
        args.append("-v")
        result = _run(args, timeout=3600)
        if result.returncode != 0:
            raise RcloneError(
                f"Upload failed ({local_path} -> {remote_path}):\n{result.stderr}"
            )


def verify_sync(local_path: str, remote_path: str, config: AppConfig) -> bool:
    """Verify local and remote are in sync via `rclone check`."""
    filter_path = write_filter_file(config)
    result = _run(
        [
            "check",
            local_path,
            f"{config.remote_name}:{remote_path}",
            "--filter-from", str(filter_path),
            # Must match the symlink mode used by sync_upload(),
            # otherwise verification fails for symlinked files
            "--copy-links" if config.copy_links else "--skip-links",
        ],
        timeout=600,
    )
    return result.returncode == 0


def rclone_lsjson(
    remote_name: str,
    remote_path: str,
    *,
    recursive: bool = True,
    files_only: bool = True,
    dirs_only: bool = False,
) -> list[RemoteFileInfo]:
    """List files on remote with metadata via `rclone lsjson`.

    Returns file path and size — no file content is downloaded.

    Args:
        files_only: Only list files (default True). Mutually exclusive with dirs_only.
        dirs_only: Only list directories. Mutually exclusive with files_only.
    """
    if files_only and dirs_only:
        raise ValueError("files_only and dirs_only are mutually exclusive")

    remote_spec = f"{remote_name}:{remote_path}"
    args = ["lsjson", remote_spec]
    if recursive:
        args.append("--recursive")
    if dirs_only:
        args.append("--dirs-only")
    elif files_only:
        args.append("--files-only")

    result = _run(args, timeout=300)
    if result.returncode != 0:
        raise RcloneError(f"lsjson failed for {remote_spec}: {result.stderr}")

    try:
        entries = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []

    return [
        RemoteFileInfo(
            path=e.get("Path", ""),
            size=e.get("Size", 0),
            is_dir=e.get("IsDir", False),
        )
        for e in entries
    ]


def rclone_moveto(src: str, dst: str) -> None:
    """Rename/move a file or directory on a remote.

    Both src and dst should be full remote paths like 'remote:path/file'.
    """
    result = _run(["moveto", src, dst], timeout=120)
    if result.returncode != 0:
        raise RcloneError(f"moveto failed ({src} → {dst}): {result.stderr}")


def list_remote_dirs(remote: str, path: str = "") -> list[str]:
    """List subdirectories at a remote path (non-recursive).

    Uses rclone lsjson --dirs-only for reliable JSON output.
    Returns a list of directory names at the given level.
    """
    try:
        entries = rclone_lsjson(
            remote, path, recursive=False, files_only=False, dirs_only=True,
        )
        return [e.path for e in entries if e.is_dir]
    except RcloneError:
        return []
