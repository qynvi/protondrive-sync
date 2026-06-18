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
from dataclasses import dataclass
from typing import Callable, Optional

from pathlib import Path

from .core.config import AppConfig, FolderMapping, effective_filters, load_config
from .core.bisync import (
    BurstState,
    detect_unstable_writes,
    should_sync,
    scan_for_modifications,
    has_pending_review,
)
from .core.locks import (
    LockError,
    acquire_remote_lease,
    local_folder_lock,
    release_remote_lease,
)
from .core.state import (
    folder_blocks_automatic_sync,
    mark_folder_status,
    update_folder_state,
    utc_now,
)
from .core.git_meta import refresh_git_metadata, git_available


LogCallback = Callable[[str], None]


@dataclass
class ScheduledSync:
    folder: FolderMapping
    state: BurstState
    reason: str
    config_index: int
    changed_count: int = 0
    upload_bytes: int = 0
    delete_count: int = 0
    remote_due: bool = False

    @property
    def has_local_changes(self) -> bool:
        return self.reason == "local" and self.changed_count > 0


def _log_default(msg: str) -> None:
    print(f"[bisync] {msg}", flush=True)


def _local_change_summary(
    folder: FolderMapping, config: AppConfig, *, since_ns: int | None = None
) -> tuple[int, int, int]:
    """Return changed path count, upload bytes, and delete count for scheduler ordering."""
    from .core.inventory import scan_local_inventory
    from .core.sync_engine import changed_local_paths

    paths = changed_local_paths(folder, config, since_ns=since_ns)
    local_entries = scan_local_inventory(folder, config, hash_paths=set(paths))
    delete_count = sum(1 for path in paths if path not in local_entries)
    upload_bytes = sum(
        (local_entries[path].local_size or 0) for path in paths if path in local_entries
    )
    return len(paths), upload_bytes, delete_count


def _scheduled_sync_sort_key(
    candidate: ScheduledSync,
) -> tuple[int, int, int, int, int]:
    """Prioritize recent local changes, then fewest changed paths, then bytes."""
    priority = 0 if candidate.has_local_changes else 2
    return (
        priority,
        candidate.changed_count,
        candidate.upload_bytes,
        candidate.delete_count,
        candidate.config_index,
    )


def run_daemon(
    config: Optional[AppConfig] = None, log: Optional[LogCallback] = None
) -> None:
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
    startup_time = time.time()
    last_remote_poll: dict[str, float] = {
        folder.local_path: startup_time for folder in config.folders
    }

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

        candidates: list[ScheduledSync] = []

        for index, folder in enumerate(config.folders):
            if not folder.enabled:
                continue

            folder_key = folder.local_path

            # Initialize burst state — last_check_time=0 so the first scan
            # after startup covers all time and catches pre-existing changes
            if folder_key not in burst_states:
                burst_states[folder_key] = BurstState(last_check_time=0)

            state = burst_states[folder_key]

            # Skip if blocked by pending review
            if has_pending_review(config, folder_key):
                mark_folder_status(
                    config, folder, "pending_review", error="pending review"
                )
                continue

            if folder_blocks_automatic_sync(config, folder):
                continue

            # Scan for changes since last check
            scan_started = time.time()
            changed = scan_for_modifications(
                folder,
                since=state.last_check_time,
                filters=effective_filters(config, folder),
            )
            state.last_check_time = max(0.0, scan_started - config.scan_overlap_seconds)
            update_folder_state(
                config, folder, last_scan_started_ns=int(scan_started * 1_000_000_000)
            )

            if changed:
                state.record_change(now)

            remote_due = (
                now - last_remote_poll.get(folder_key, 0.0)
                >= config.remote_poll_interval_seconds
            )

            local_due = should_sync(
                state, config.bisync_quiet_threshold, config.bisync_max_burst
            )
            if local_due:
                since = (
                    max(0.0, state.start_time - config.scan_overlap_seconds)
                    if state.active
                    else 0.0
                )
                since_ns = int(since * 1_000_000_000) if since else None
                try:
                    changed_count, upload_bytes, delete_count = _local_change_summary(
                        folder,
                        config,
                        since_ns=since_ns,
                    )
                except Exception as exc:
                    log(
                        f"  Scheduler skipped {folder.local_path}: local change summary failed: {exc}"
                    )
                    state.record_change(now)
                    continue
                if changed_count > 0:
                    candidates.append(
                        ScheduledSync(
                            folder=folder,
                            state=state,
                            reason="local",
                            config_index=index,
                            changed_count=changed_count,
                            upload_bytes=upload_bytes,
                            delete_count=delete_count,
                            remote_due=remote_due,
                        )
                    )
                    continue
                state.reset()

            if remote_due:
                candidates.append(
                    ScheduledSync(
                        folder=folder,
                        state=state,
                        reason="remote",
                        config_index=index,
                        remote_due=True,
                    )
                )

        for candidate in sorted(candidates, key=_scheduled_sync_sort_key):
            if stop:
                return
            folder_key = candidate.folder.local_path
            log(
                "Scheduler selected: "
                f"{candidate.folder.local_path} reason={candidate.reason} "
                f"changed={candidate.changed_count} bytes={candidate.upload_bytes} "
                f"deletes={candidate.delete_count} remote_due={candidate.remote_due}"
            )
            poll_remote_journal = candidate.reason == "remote"
            if candidate.remote_due and poll_remote_journal:
                last_remote_poll[folder_key] = time.time()
                update_folder_state(
                    config, candidate.folder, last_remote_poll=utc_now()
                )
            _do_sync(
                candidate.folder,
                config,
                candidate.state,
                log,
                poll_remote_journal=poll_remote_journal,
            )
            if stop:
                return

    log("Bisync daemon stopped")


def _do_sync(
    folder: object,  # FolderMapping, but avoid circular import annotation issues
    config: AppConfig,
    state: BurstState,
    log: LogCallback,
    *,
    poll_remote_journal: bool = True,
) -> None:
    """Execute a single sync cycle for a folder."""
    assert isinstance(folder, FolderMapping)

    log(f"Syncing: {folder.local_path}")

    if not folder.bisync_initialized:
        message = "folder is not initialized; refusing daemon --resync"
        log(f"  Sync blocked: {message}")
        mark_folder_status(config, folder, "degraded", error=message)
        state.reset()
        return

    since = (
        max(0.0, state.start_time - config.scan_overlap_seconds)
        if state.active
        else 0.0
    )
    if state.active:
        unstable = detect_unstable_writes(
            folder,
            since,
            effective_filters(config, folder),
            delay_seconds=config.stable_check_delay_seconds,
        )
        if unstable:
            log(
                f"  Local writes still active; postponing sync ({len(unstable)} path(s))"
            )
            for rel in unstable[:10]:
                log(f"    {rel}")
            state.record_change(time.time())
            return

    try:
        lock = local_folder_lock(folder).acquire()
    except LockError as exc:
        log(f"  Sync skipped: {exc}")
        state.record_change(time.time())
        return

    lease = None
    try:
        mark_folder_status(config, folder, "syncing")
        lease = acquire_remote_lease(folder, config, operation="daemon-sync")

        from .core.sync_engine import run_targeted_sync_cycle

        since_ns = int(since * 1_000_000_000) if since else None
        targeted = run_targeted_sync_cycle(
            folder,
            config,
            since_ns=since_ns,
            poll_remote_journal=poll_remote_journal,
            progress=log,
        )
        if targeted.status == "healthy":
            log(
                "  Targeted sync complete: "
                f"uploads={len(targeted.synced_paths)}, "
                f"downloads={len(targeted.downloaded_paths)}, "
                f"deletes={len(targeted.deleted_paths)}"
            )
        elif targeted.status == "journal_stale":
            log(
                f"  Journal stale; exact local changes handled where safe: {targeted.message}"
            )
        elif targeted.status == "journal_pending":
            log("  Journal upload pending; new data sync blocked until retry succeeds")
        elif targeted.status == "pending_review":
            log(f"  Review required: {targeted.message}")
        else:
            log(f"  Targeted sync degraded: {targeted.message}")
        state.reset()
    except LockError as exc:
        log(f"  Sync skipped: {exc}")
        state.record_change(time.time())
    except Exception as exc:  # noqa: BLE001 - daemon must not crash on one folder
        log(f"  Sync failed: {exc}")
        mark_folder_status(config, folder, "degraded", error=str(exc))
        state.reset()
    finally:
        lock.release()
        release_remote_lease(lease, config)


def main() -> None:
    """CLI entry point for bisync daemon."""
    run_daemon()


if __name__ == "__main__":
    main()
