"""Partitioned remote audit and manual deep verify orchestration."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import AppConfig, FolderMapping
from .inventory import get_audit_state, list_inventory_entries, update_audit_state
from .verify import VerifyReport, verify_subtree_targeted
from .state import mark_folder_status, utc_now


@dataclass
class AuditPartition:
    key: str
    rel_path: str
    file_count: int


@dataclass
class AuditRunResult:
    completed: bool
    audited: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    incomplete: bool = False
    message: str | None = None


VerifySubtreeFunc = Callable[[str, str, AppConfig], VerifyReport]


def build_audit_partitions(
    config: AppConfig, folder: FolderMapping
) -> list[AuditPartition]:
    """Build stable audit partitions from inventory paths."""
    groups: dict[str, int] = {}
    max_files = config.remote_audit_partition_max_files
    for entry in list_inventory_entries(config, folder):
        if entry.deleted_at:
            continue
        parts = Path(entry.path).parts
        if not parts:
            continue
        key = parts[0]
        if groups.get(key, 0) >= max_files and len(parts) > 1:
            key = f"{parts[0]}/{parts[1]}"
        groups[key] = groups.get(key, 0) + 1
    return [
        AuditPartition(key=key, rel_path=key, file_count=count)
        for key, count in sorted(groups.items())
    ]


def _partition_after_cursor(
    partitions: list[AuditPartition], cursor: str | None
) -> list[AuditPartition]:
    if not cursor:
        return partitions
    for index, partition in enumerate(partitions):
        if partition.key == cursor:
            return partitions[index + 1 :]
    return partitions


def run_partitioned_audit(
    config: AppConfig,
    folder: FolderMapping,
    *,
    verify_func: Callable[..., VerifyReport] = verify_subtree_targeted,
    time_budget_minutes: int | None = None,
) -> AuditRunResult:
    """Run a resumable partitioned audit until complete or time budget expires."""
    partitions = build_audit_partitions(config, folder)
    if not partitions:
        update_audit_state(
            config,
            folder,
            cursor=None,
            last_started_at=utc_now(),
            last_completed_at=utc_now(),
            incomplete=0,
            last_error=None,
        )
        return AuditRunResult(completed=True)

    state = get_audit_state(config, folder) or {}
    remaining = _partition_after_cursor(
        partitions,
        state.get("cursor") if isinstance(state.get("cursor"), str) else None,
    )
    budget_seconds = (
        time_budget_minutes or config.remote_audit_time_budget_minutes
    ) * 60
    started = time.monotonic()
    update_audit_state(
        config, folder, last_started_at=utc_now(), incomplete=1, last_error=None
    )
    result = AuditRunResult(completed=False)

    for partition in remaining:
        if time.monotonic() - started >= budget_seconds:
            update_audit_state(config, folder, cursor=partition.key, incomplete=1)
            mark_folder_status(config, folder, "audit_due")
            result.incomplete = True
            result.message = "audit time budget reached"
            return result
        local_subtree = str(Path(folder.local_path) / partition.rel_path)
        remote_subtree = (
            f"{folder.remote_subpath.strip('/')}/{partition.rel_path}".strip("/")
        )
        report = verify_func(
            local_subtree,
            remote_subtree,
            config,
            folder=folder,
            symlink_mode=folder.symlink_mode,
            timeout=budget_seconds,
        )
        if not report.ok:
            result.failed.append(partition.key)
            result.message = report.message or "partition verify failed"
            update_audit_state(
                config,
                folder,
                cursor=partition.key,
                incomplete=1,
                last_error=result.message,
            )
            mark_folder_status(config, folder, "degraded", error=result.message)
            return result
        result.audited.append(partition.key)
        update_audit_state(config, folder, cursor=partition.key, incomplete=1)

    update_audit_state(
        config,
        folder,
        cursor=None,
        last_completed_at=utc_now(),
        incomplete=0,
        last_error=None,
    )
    result.completed = True
    return result


def run_deep_verify(
    config: AppConfig,
    folder: FolderMapping,
    *,
    rel_path: str | None = None,
    verify_func: Callable[..., VerifyReport] = verify_subtree_targeted,
) -> VerifyReport:
    """Run a manual full-folder or subtree verify."""
    rel = (rel_path or "").strip("/")
    local = str(Path(folder.local_path) / rel) if rel else folder.local_path
    remote = (
        f"{folder.remote_subpath.strip('/')}/{rel}".strip("/")
        if rel
        else folder.remote_subpath
    )
    return verify_func(
        local,
        remote,
        config,
        folder=folder,
        symlink_mode=folder.symlink_mode,
        timeout=config.remote_audit_time_budget_minutes * 60,
    )
