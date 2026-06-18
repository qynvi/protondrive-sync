"""Daemon-start safety preflight.

This gate is intentionally practical: it proves the local app state is clean
before daemon startup and flags large folders whose full remote audit is not a
startup-time operation on Proton Drive.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import AppConfig, FolderMapping
from .inventory import folder_inventory_count, list_journal_outbox, scan_local_inventory
from .state import get_folder_state
from .sync_engine import changed_local_paths


BLOCK_START_STATUSES = {
    "syncing",
    "verifying",
    "degraded",
    "pending_review",
    "journal_pending",
}


@dataclass
class FolderPreflight:
    local_path: str
    remote_subpath: str
    status: str
    inventory_count: int
    changed_count: int
    upload_or_update_count: int
    delete_count: int
    upload_bytes: int
    is_large: bool
    blockers: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.blockers


@dataclass
class DaemonPreflightReport:
    ok: bool
    outbox_pending: int
    folders: list[FolderPreflight]
    blockers: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)


def evaluate_folder_preflight(
    config: AppConfig, folder: FolderMapping
) -> FolderPreflight:
    state = get_folder_state(config, folder)
    inventory_count = folder_inventory_count(config, folder)
    changed = changed_local_paths(folder, config)
    local_entries = scan_local_inventory(folder, config, hash_paths=set(changed))
    delete_count = sum(1 for path in changed if path not in local_entries)
    upload_bytes = sum(
        (local_entries[path].local_size or 0)
        for path in changed
        if path in local_entries
    )
    is_large = inventory_count >= config.remote_audit_large_folder_file_count
    result = FolderPreflight(
        local_path=folder.local_path,
        remote_subpath=folder.remote_subpath,
        status=state.status,
        inventory_count=inventory_count,
        changed_count=len(changed),
        upload_or_update_count=len(changed) - delete_count,
        delete_count=delete_count,
        upload_bytes=upload_bytes,
        is_large=is_large,
    )
    if not folder.enabled:
        return result
    if inventory_count <= 0:
        result.blockers.append("inventory is empty")
    if state.status in BLOCK_START_STATUSES:
        result.blockers.append(f"blocking state: {state.status}")
    if changed:
        result.blockers.append(
            f"local drift before daemon start: {len(changed)} path(s)"
        )
    if is_large:
        result.limitations.append(
            "full remote check is too expensive for daemon startup; rely on inventory, journals, and partitioned audit"
        )
    return result


def evaluate_daemon_preflight(config: AppConfig) -> DaemonPreflightReport:
    outbox = list_journal_outbox(config)
    folders = [evaluate_folder_preflight(config, folder) for folder in config.folders]
    blockers: list[str] = []
    limitations: list[str] = []
    if outbox:
        blockers.append(
            f"journal outbox pending: {len(outbox)} entr{'y' if len(outbox) == 1 else 'ies'}"
        )
    for folder in folders:
        for blocker in folder.blockers:
            blockers.append(f"{folder.local_path}: {blocker}")
        for limitation in folder.limitations:
            limitations.append(f"{folder.local_path}: {limitation}")
    return DaemonPreflightReport(
        ok=not blockers,
        outbox_pending=len(outbox),
        folders=folders,
        blockers=blockers,
        limitations=limitations,
    )
