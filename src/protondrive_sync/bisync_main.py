"""Standalone entry point for the bisync daemon.

Runs the adaptive activity-window coalescing loop:
- Scans for filesystem changes every check_interval seconds
- Tracks burst windows per folder
- Runs safety checks + bisync when a burst window closes

Used by systemd service / Windows scheduled task.
"""

from __future__ import annotations

import signal
import sys
import time
from typing import Callable, Optional

from pathlib import Path

from .core.config import AppConfig, load_config, save_config
from .core.bisync import (
    BurstState,
    should_sync,
    scan_for_modifications,
    run_safety_checks,
    has_pending_review,
    write_pending_review,
)
from .core.rclone import run_bisync, RcloneError
from .core.git_meta import refresh_git_metadata, git_available


LogCallback = Callable[[str], None]


def _log_default(msg: str) -> None:
    print(f"[bisync] {msg}", flush=True)


def run_daemon(config: Optional[AppConfig] = None, log: Optional[LogCallback] = None) -> None:
    """Main daemon loop. Runs until SIGTERM/SIGINT."""
    if config is None:
        config = load_config()
    log = log or _log_default

    stop = False

    def shutdown(signum: int, frame: object) -> None:
        nonlocal stop
        log("Shutting down ...")
        stop = True

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # Per-folder burst tracking
    burst_states: dict[str, BurstState] = {}

    # Git metadata refresh tracking (every ~10 minutes)
    GIT_META_REFRESH_INTERVAL = 600  # seconds
    last_git_meta_refresh: float = 0.0
    git_ok = git_available()

    log("Bisync daemon started")
    log(f"  Check interval:   {config.bisync_check_interval}s")
    log(f"  Quiet threshold:  {config.bisync_quiet_threshold}s")
    log(f"  Max burst:        {config.bisync_max_burst}s")
    if git_ok:
        log(f"  Git meta refresh: every {GIT_META_REFRESH_INTERVAL}s")

    while not stop:
        # Sleep in 1s increments for responsive shutdown
        for _ in range(config.bisync_check_interval):
            if stop:
                return
            time.sleep(1)

        now = time.time()

        # Periodic git metadata refresh
        if git_ok and (now - last_git_meta_refresh) >= GIT_META_REFRESH_INTERVAL:
            last_git_meta_refresh = now
            for folder in config.folders:
                if not folder.enabled:
                    continue
                try:
                    local_path = Path(folder.local_path)
                    if local_path.is_dir() and not local_path.is_symlink():
                        result = refresh_git_metadata(local_path, config.filters)
                        if result is not None:
                            log(f"Git metadata updated: {folder.local_path}")
                except Exception:
                    pass  # git metadata refresh is best-effort

        for folder in config.folders:
            if folder.sync_mode != "bisync" or not folder.enabled:
                continue

            folder_key = folder.local_path

            # Initialize burst state — last_check_time=0 so the first scan
            # after startup covers all time and catches pre-existing changes
            if folder_key not in burst_states:
                burst_states[folder_key] = BurstState(last_check_time=0)

            state = burst_states[folder_key]

            # Skip if blocked by pending review
            if has_pending_review(config, folder_key):
                continue

            # Scan for changes since last check
            changed = scan_for_modifications(
                folder, since=state.last_check_time, filters=config.filters,
            )
            state.last_check_time = now

            if changed:
                state.record_change(now)

            # Check if it's time to sync
            if should_sync(state, config.bisync_quiet_threshold, config.bisync_max_burst):
                _do_sync(folder, config, state, log)

    log("Bisync daemon stopped")


def _do_sync(
    folder: object,  # FolderMapping, but avoid circular import annotation issues
    config: AppConfig,
    state: BurstState,
    log: LogCallback,
) -> None:
    """Execute a single sync cycle for a folder."""
    from .core.config import FolderMapping, save_config
    assert isinstance(folder, FolderMapping)

    log(f"Syncing: {folder.local_path}")

    # Run safety checks
    report = run_safety_checks(folder, config)

    # If suspicious changes found, block sync and await user review
    if not report.safe_to_sync:
        log(f"  Review required: {len(report.suspicious_changes)} suspicious change(s)")
        for fc in report.suspicious_changes:
            log(f"    {fc.path}: {fc.remote_size} → {fc.local_size} ({fc.change_pct:+.0f}%)")
        write_pending_review(config, folder.local_path, report.suspicious_changes)
        state.reset()
        return

    # Log protected files
    if report.protected_paths:
        log(f"  Protected {len(report.protected_paths)} deleted work file(s):")
        for p in report.protected_paths:
            log(f"    {p}")

    # Run bisync
    resync = not folder.bisync_initialized
    try:
        run_bisync(
            folder.local_path,
            folder.remote_subpath,
            config,
            resync=resync,
            progress=log,
        )
        if resync:
            folder.bisync_initialized = True
            save_config(config)
            log(f"  Initial sync complete (--resync)")
        else:
            log(f"  Sync complete")
    except RcloneError as exc:
        err_msg = str(exc)
        # Auto-recover from missing bisync listing files — this happens
        # when the bisync cache is cleared or when rclone flags change
        # (e.g. adding --copy-links changes the listing filename hash).
        # Re-run with --resync to rebuild the baseline listings.
        if "cannot find prior" in err_msg and not resync:
            log("  Listing cache missing — running --resync to rebuild baseline")
            try:
                run_bisync(
                    folder.local_path,
                    folder.remote_subpath,
                    config,
                    resync=True,
                    progress=log,
                )
                log(f"  Resync complete — baseline rebuilt")
            except RcloneError as exc2:
                log(f"  Resync also failed: {exc2}")
        else:
            log(f"  Sync failed: {exc}")

    state.reset()


def main() -> None:
    """CLI entry point for bisync daemon."""
    run_daemon()


if __name__ == "__main__":
    main()
