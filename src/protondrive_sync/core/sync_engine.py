"""P3 targeted sync planner/executor.

This engine syncs only paths that changed relative to the app inventory. It
never trusts the journal or local scan alone; every remote decision uses an
exact metadata probe (Proton Drive CLI ``info``/``list``) before mutation, and
verifies transfers by comparing the remote's claimed sha1/size against the
local file — never trusting exit codes alone.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import time
import uuid
from collections import defaultdict
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable

from .bisync import is_work_file
from .config import AppConfig, FolderMapping
from .inventory import (
    InventoryError,
    InventoryEntry,
    folder_inventory_count,
    get_inventory_entry,
    init_inventory,
    record_journal_seen,
    list_inventory_entries,
    outbox_has_pending,
    scan_local_inventory,
    sha1_file,
    upsert_inventory_entries,
    upsert_inventory_entry,
)
from .journal import (
    JournalChange,
    JournalEntry,
    make_journal_entry,
    poll_journal,
    retry_journal_outbox,
    write_journal_entry,
)
from .local_backup import move_local_to_backup
from .proton_cli import (
    ProtonDriveCLI,
    ProtonError,
    ProtonNotFound,
    RemoteNode,
)
from .state import folder_id, mark_folder_status, update_folder_state, utc_now


LINK_BLOB_SUFFIX = ".rclonelink"

# Eventual-consistency retries for post-transfer batch verification.
_BATCH_VERIFY_ATTEMPTS = 4
_BATCH_VERIFY_DELAY = 3.0


def make_backend(
    config: AppConfig, backend: ProtonDriveCLI | None = None
) -> ProtonDriveCLI:
    """Return the provided backend or construct one for this config."""
    return backend if backend is not None else ProtonDriveCLI(config)


@dataclass
class TargetedSyncResult:
    status: str = "healthy"
    synced_paths: list[str] = field(default_factory=list)
    downloaded_paths: list[str] = field(default_factory=list)
    deleted_paths: list[str] = field(default_factory=list)
    review_paths: list[str] = field(default_factory=list)
    degraded_paths: list[str] = field(default_factory=list)
    journal_entries: int = 0
    message: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in ("healthy", "audit_due", "journal_stale")


def remote_child_path(folder: FolderMapping, rel_path: str) -> str:
    rel = rel_path.strip("/")
    root = folder.remote_subpath.strip("/")
    return f"{root}/{rel}" if rel else root


def remote_storage_path(
    folder: FolderMapping, rel_path: str, *, kind: str | None = None
) -> str:
    """Return the actual remote object path for targeted mutations."""
    remote = remote_child_path(folder, rel_path)
    if (
        folder.symlink_mode == "preserve"
        and kind == "symlink"
        and not remote.endswith(LINK_BLOB_SUFFIX)
    ):
        return f"{remote}{LINK_BLOB_SUFFIX}"
    return remote


def _normalize_remote_listing_path(folder: FolderMapping, path: str) -> str:
    if folder.symlink_mode == "preserve" and path.endswith(LINK_BLOB_SUFFIX):
        return path[: -len(LINK_BLOB_SUFFIX)]
    return path


def _remote_parent_for_path(folder: FolderMapping, rel_path: str) -> str:
    remote = PurePosixPath(remote_child_path(folder, rel_path))
    parent = str(remote.parent)
    if parent == ".":
        return folder.remote_subpath.strip("/")
    return parent


def _parent_rel_from_remote(folder: FolderMapping, remote_parent: str) -> str:
    root = folder.remote_subpath.strip("/")
    parent = remote_parent.strip("/")
    if parent == root:
        return ""
    if parent.startswith(root + "/"):
        return parent[len(root) :].strip("/")
    return ""


def list_remote_infos_for_paths(
    folder: FolderMapping,
    config: AppConfig,
    paths: list[str],
    *,
    backend: ProtonDriveCLI | None = None,
) -> dict[str, RemoteNode]:
    """List remote metadata for selected paths, one ``list`` call per parent."""
    backend = make_backend(config, backend)
    parents = sorted({_remote_parent_for_path(folder, path) for path in paths})
    infos: dict[str, RemoteNode] = {}
    for parent in parents:
        parent_rel = _parent_rel_from_remote(folder, parent)
        for node in backend.list_dir(parent):
            if node.is_dir or node.name is None:
                continue
            rel = f"{parent_rel}/{node.name}".strip("/")
            rel = _normalize_remote_listing_path(folder, rel)
            node.path = rel
            infos[rel] = node
    return infos


def _entry_state(entry: InventoryEntry | None) -> dict[str, object] | None:
    if entry is None or entry.deleted_at:
        return None
    return {
        "kind": entry.kind,
        "size": entry.remote_size
        if entry.remote_size is not None
        else entry.local_size,
        "sha1": entry.remote_sha1 or entry.local_sha1,
        "link_target": entry.link_target,
    }


def _local_state(entry: InventoryEntry | None) -> dict[str, object] | None:
    if entry is None or entry.deleted_at:
        return None
    return {
        "kind": entry.kind,
        "size": entry.local_size,
        "sha1": entry.local_sha1,
        "link_target": entry.link_target,
    }


def _remote_state(info: RemoteNode | None) -> dict[str, object] | None:
    if info is None:
        return None
    return {
        "kind": "dir" if info.is_dir else "file",
        "size": info.size,
        "sha1": info.sha1,
    }


def _remote_matches_inventory(
    previous: InventoryEntry | None, remote: RemoteNode | None
) -> bool:
    if previous is None or previous.deleted_at:
        return remote is None
    if remote is None:
        return previous.remote_size is None
    if previous.remote_size is not None and remote.size != previous.remote_size:
        return False
    if (
        previous.remote_sha1
        and remote.sha1
        and previous.remote_sha1.lower() != remote.sha1.lower()
    ):
        return False
    return True


def _local_matches_inventory(
    previous: InventoryEntry | None, local: InventoryEntry | None
) -> bool:
    if previous is None or previous.deleted_at:
        return local is None
    if local is None:
        return False
    if previous.kind != local.kind:
        return False
    if previous.local_size is not None and local.local_size != previous.local_size:
        return False
    if (
        previous.local_sha1
        and local.local_sha1
        and previous.local_sha1.lower() != local.local_sha1.lower()
    ):
        return False
    if previous.link_target != local.link_target:
        return False
    if (
        previous.local_mtime_ns is not None
        and local.local_mtime_ns != previous.local_mtime_ns
    ):
        return False
    return True


def classify_delta(
    previous: InventoryEntry | None,
    local: InventoryEntry | None,
    remote: RemoteNode | None,
) -> str:
    """Classify one path using previous inventory plus current exact states."""
    if previous is None or previous.deleted_at:
        if local is not None and remote is None:
            return "local_only_new"
        if local is None and remote is not None:
            return "remote_only_new"
        if local is not None and remote is not None:
            return "same_path_both_modified"
        return "unchanged"

    local_same = _local_matches_inventory(previous, local)
    remote_same = _remote_matches_inventory(previous, remote)
    if local_same and remote_same:
        return "unchanged"
    if local is None and remote_same:
        return "local_deleted"
    if local_same and remote is None:
        return "remote_deleted"
    if local is not None and previous.kind != local.kind:
        return "same_path_type_changed"
    if remote is not None and previous.kind == "file" and remote.is_dir:
        return "same_path_type_changed"
    if not local_same and remote_same:
        return "local_modified"
    if local_same and not remote_same:
        return "remote_modified"
    return "same_path_both_modified"


def changed_local_paths(
    folder: FolderMapping, config: AppConfig, *, since_ns: int | None = None
) -> list[str]:
    """Return paths that differ locally from inventory."""
    hash_all = config.integrity_mode == "deep_hash"
    current = scan_local_inventory(folder, config, hash_all=hash_all)
    previous = {entry.path: entry for entry in list_inventory_entries(config, folder)}
    changed: list[str] = []
    for path, local in current.items():
        if (
            since_ns is not None
            and local.local_mtime_ns is not None
            and local.local_mtime_ns < since_ns
        ):
            prev = previous.get(path)
            if prev is not None and _local_matches_inventory(prev, local):
                continue
        prev = previous.get(path)
        if not _local_matches_inventory(prev, local):
            changed.append(path)
    for path, prev in previous.items():
        if prev.deleted_at:
            continue
        if path not in current:
            changed.append(path)
    return sorted(set(changed))


def _scan_selected_local(
    folder: FolderMapping, config: AppConfig, paths: list[str]
) -> dict[str, InventoryEntry]:
    hash_paths = (
        set(paths) if config.integrity_mode in ("changed_hash", "deep_hash") else None
    )
    selected = set(paths)
    scanned = scan_local_inventory(
        folder,
        config,
        hash_paths=hash_paths,
        hash_all=config.integrity_mode == "deep_hash",
    )
    return {path: entry for path, entry in scanned.items() if path in selected}


def _exact_remote_or_none(
    folder: FolderMapping,
    config: AppConfig,
    path: str,
    *,
    backend: ProtonDriveCLI | None = None,
) -> RemoteNode | None:
    """Stat one path. In preserve mode, fall back to the ``.rclonelink`` node."""
    backend = make_backend(config, backend)
    remote_rel = remote_child_path(folder, path)
    node = backend.stat_or_none(remote_rel)
    if (
        node is None
        and folder.symlink_mode == "preserve"
        and not remote_rel.endswith(LINK_BLOB_SUFFIX)
    ):
        node = backend.stat_or_none(remote_rel + LINK_BLOB_SUFFIX)
    if node is not None:
        node.path = path
    return node


def _symlink_target(folder: FolderMapping, path: str, local: InventoryEntry) -> str:
    if local.link_target is not None:
        return local.link_target
    return os.readlink(Path(folder.local_path) / path)


def _expected_signature(
    folder: FolderMapping, path: str, local: InventoryEntry
) -> tuple[int | None, str | None]:
    """Return (expected_size, expected_sha1) for verifying an uploaded entry."""
    if local.kind == "symlink":
        blob = _symlink_target(folder, path, local).encode("utf-8")
        return len(blob), hashlib.sha1(blob).hexdigest()
    sha1 = local.local_sha1
    if sha1 is None:
        try:
            sha1 = sha1_file(Path(folder.local_path) / path)
        except OSError:
            sha1 = None
    return local.local_size, sha1


def _upload_entry(
    backend: ProtonDriveCLI,
    folder: FolderMapping,
    path: str,
    local: InventoryEntry,
    *,
    replace: bool,
) -> str:
    """Upload one entry (file or preserved symlink). Returns the remote rel path."""
    remote_rel = remote_storage_path(folder, path, kind=local.kind)
    if local.kind == "symlink":
        backend.upload_text(
            _symlink_target(folder, path, local), remote_rel, replace=replace
        )
    else:
        local_abs = str(Path(folder.local_path) / path)
        backend.upload(
            local_abs, remote_rel, replace=replace, size_hint=local.local_size or 0
        )
    return remote_rel


def _verify_uploaded(
    backend: ProtonDriveCLI,
    folder: FolderMapping,
    path: str,
    local: InventoryEntry,
    remote_rel: str,
) -> RemoteNode | None:
    """Confirm an uploaded entry's remote sha1/size matches the local file."""
    node = backend.wait_until_present(remote_rel)
    if node is None:
        return None
    exp_size, exp_sha1 = _expected_signature(folder, path, local)
    if exp_size is not None and node.size != exp_size:
        return None
    if exp_sha1 and node.sha1 and node.sha1.lower() != exp_sha1.lower():
        return None
    node.path = path
    return node


def _updated_entry(
    folder: FolderMapping,
    path: str,
    local: InventoryEntry,
    remote: RemoteNode | None,
    *,
    source: str,
) -> InventoryEntry:
    return InventoryEntry(
        folder_id=folder_id(folder.local_path, folder.remote_subpath),
        path=path,
        kind=local.kind,
        local_size=local.local_size,
        local_mtime_ns=local.local_mtime_ns,
        local_sha1=local.local_sha1,
        remote_size=remote.size if remote is not None else local.local_size,
        remote_sha1=remote.sha1 if remote is not None else local.local_sha1,
        remote_modtime=remote.modtime if remote is not None else None,
        link_target=local.link_target,
        last_verified_at=utc_now(),
        last_changed_at=utc_now(),
        last_source=source,
        deleted_at=None,
    )


def apply_local_upload(
    folder: FolderMapping,
    config: AppConfig,
    path: str,
    local: InventoryEntry,
    *,
    operation_id: str | None = None,
    backend: ProtonDriveCLI | None = None,
) -> TargetedSyncResult:
    """Upload one changed local path after exact remote preflight."""
    result = TargetedSyncResult()
    backend = make_backend(config, backend)
    previous = get_inventory_entry(config, folder, path)
    remote_before = _exact_remote_or_none(folder, config, path, backend=backend)
    classification = classify_delta(previous, local, remote_before)
    if classification not in ("local_only_new", "local_modified"):
        result.status = "pending_review"
        result.review_paths.append(path)
        result.message = classification
        return result

    # New files keep the CLI default (hard-fail on unexpected name conflict) as
    # a safety net; intentional overwrites replace (the old revision is trashed
    # and stays recoverable).
    replace = remote_before is not None
    try:
        remote_rel = _upload_entry(backend, folder, path, local, replace=replace)
        remote_after = _verify_uploaded(backend, folder, path, local, remote_rel)
        if remote_after is None:
            result.status = "degraded"
            result.degraded_paths.append(path)
            result.message = "targeted upload verify failed"
            return result
    except ProtonError as exc:
        result.status = "degraded"
        result.degraded_paths.append(path)
        result.message = str(exc)
        return result

    new_entry = _updated_entry(folder, path, local, remote_after, source="local")
    upsert_inventory_entry(config, new_entry)
    journal = make_journal_entry(
        folder,
        config,
        [
            JournalChange(
                path=path,
                action="upload",
                before=_entry_state(previous),
                after=_local_state(new_entry),
            )
        ],
        operation_id=operation_id,
    )
    if not write_journal_entry(config, journal):
        result.status = "journal_pending"
        result.message = "data uploaded but journal upload is pending"
    result.synced_paths.append(path)
    return result


def apply_local_delete(
    folder: FolderMapping,
    config: AppConfig,
    path: str,
    *,
    operation_id: str | None = None,
    backend: ProtonDriveCLI | None = None,
) -> TargetedSyncResult:
    """Propagate a local delete only when it is low-risk; remote goes to trash."""
    result = TargetedSyncResult()
    backend = make_backend(config, backend)
    previous = get_inventory_entry(config, folder, path)
    remote_before = _exact_remote_or_none(folder, config, path, backend=backend)
    classification = classify_delta(previous, None, remote_before)
    if classification != "local_deleted":
        result.status = "pending_review"
        result.review_paths.append(path)
        result.message = classification
        return result
    if previous and (
        (previous.remote_size or 0) >= config.protect_delete_min_bytes
        or is_work_file(path)
    ):
        result.status = "pending_review"
        result.review_paths.append(path)
        result.message = "protected delete requires review"
        return result
    try:
        remote_abs = remote_storage_path(
            folder, path, kind=previous.kind if previous else None
        )
        if remote_before is not None:
            # Trash is the recoverable backup (replaces remote rename-to-backup).
            if not backend.trash(remote_abs):
                result.status = "degraded"
                result.degraded_paths.append(path)
                result.message = "remote trash reported failure"
                return result
            if not backend.wait_until_absent(remote_abs):
                result.status = "degraded"
                result.degraded_paths.append(path)
                result.message = "remote path still present after trash"
                return result
    except ProtonError as exc:
        result.status = "degraded"
        result.degraded_paths.append(path)
        result.message = str(exc)
        return result
    deleted = InventoryEntry(
        folder_id=folder_id(folder.local_path, folder.remote_subpath),
        path=path,
        kind=previous.kind if previous else "file",
        last_changed_at=utc_now(),
        last_source="local",
        deleted_at=utc_now(),
    )
    upsert_inventory_entry(config, deleted)
    journal = make_journal_entry(
        folder,
        config,
        [
            JournalChange(
                path=path, action="delete", before=_entry_state(previous), after=None
            )
        ],
        operation_id=operation_id,
    )
    if not write_journal_entry(config, journal):
        result.status = "journal_pending"
        result.message = "delete completed but journal upload is pending"
    result.deleted_paths.append(path)
    return result


def apply_journal_change(
    folder: FolderMapping,
    config: AppConfig,
    entry: JournalEntry,
    change: JournalChange,
    *,
    backend: ProtonDriveCLI | None = None,
) -> TargetedSyncResult:
    """Apply one verified remote journal change to local state."""
    result = TargetedSyncResult(journal_entries=1)
    backend = make_backend(config, backend)
    path = change.path
    previous = get_inventory_entry(config, folder, path)
    local_current = _scan_selected_local(folder, config, [path]).get(path)
    remote_current = _exact_remote_or_none(folder, config, path, backend=backend)

    if change.action in ("upload", "download", "move"):
        expected_after = change.after or {}
        if (
            remote_current is None
            or (
                expected_after.get("size") is not None
                and remote_current.size != expected_after.get("size")
            )
            or (
                expected_after.get("sha1")
                and remote_current.sha1
                and expected_after.get("sha1") != remote_current.sha1
            )
        ):
            result.status = "degraded"
            result.degraded_paths.append(path)
            result.message = "journal after-state does not match exact remote"
            return result
        if not _local_matches_inventory(previous, local_current):
            result.status = "pending_review"
            result.review_paths.append(path)
            result.message = "local changed before journal download"
            return result
        is_symlink = (change.after or {}).get("kind") == "symlink"
        local_target = Path(folder.local_path) / path
        local_target.parent.mkdir(parents=True, exist_ok=True)
        remote_rel = remote_storage_path(
            folder, path, kind="symlink" if is_symlink else None
        )
        with tempfile.TemporaryDirectory(
            prefix="protondrive-sync-targeted-download-", dir=str(local_target.parent)
        ) as tmp:
            temp_path = Path(tmp) / local_target.name
            try:
                if is_symlink:
                    target = backend.download_text(remote_rel)
                    if (
                        remote_current.sha1
                        and hashlib.sha1(target.encode("utf-8")).hexdigest()
                        != remote_current.sha1.lower()
                    ):
                        result.status = "degraded"
                        result.degraded_paths.append(path)
                        result.message = "targeted symlink download verify failed"
                        return result
                    os.symlink(target, temp_path)
                else:
                    backend.download(
                        remote_rel,
                        str(temp_path),
                        claimed_modtime=remote_current.modtime,
                        size_hint=remote_current.size,
                    )
                    if remote_current.sha1:
                        actual = sha1_file(temp_path)
                        if actual.lower() != remote_current.sha1.lower():
                            result.status = "degraded"
                            result.degraded_paths.append(path)
                            result.message = "targeted download verify failed"
                            return result
                move_local_to_backup(str(local_target), run_id=entry.operation_id)
                temp_path.replace(local_target)
            except ProtonError as exc:
                result.status = "degraded"
                result.degraded_paths.append(path)
                result.message = str(exc)
                return result
        local_after = _scan_selected_local(folder, config, [path]).get(path)
        if local_after is None:
            result.status = "degraded"
            result.degraded_paths.append(path)
            result.message = "downloaded path missing after publish"
            return result
        upsert_inventory_entry(
            config,
            _updated_entry(folder, path, local_after, remote_current, source="journal"),
        )
        result.downloaded_paths.append(path)
        return result

    if change.action == "delete":
        if not _local_matches_inventory(previous, local_current):
            result.status = "pending_review"
            result.review_paths.append(path)
            result.message = "local changed before journal delete"
            return result
        if remote_current is not None:
            result.status = "degraded"
            result.degraded_paths.append(path)
            result.message = "journal delete but remote path still exists"
            return result
        local_target = Path(folder.local_path) / path
        move_local_to_backup(str(local_target), run_id=entry.operation_id)
        deleted = InventoryEntry(
            folder_id=folder_id(folder.local_path, folder.remote_subpath),
            path=path,
            kind=previous.kind if previous else "file",
            last_changed_at=utc_now(),
            last_source="journal",
            deleted_at=utc_now(),
        )
        upsert_inventory_entry(config, deleted)
        result.deleted_paths.append(path)
        return result

    result.status = "pending_review"
    result.review_paths.append(path)
    result.message = f"unsupported journal action: {change.action}"
    return result


def _merge_result(target: TargetedSyncResult, source: TargetedSyncResult) -> None:
    target.synced_paths.extend(source.synced_paths)
    target.downloaded_paths.extend(source.downloaded_paths)
    target.deleted_paths.extend(source.deleted_paths)
    target.review_paths.extend(source.review_paths)
    target.degraded_paths.extend(source.degraded_paths)
    target.journal_entries += source.journal_entries
    if source.status in ("degraded", "pending_review", "journal_pending"):
        target.status = source.status
        target.message = source.message


def _batch_review(message: str, paths: list[str]) -> TargetedSyncResult:
    return TargetedSyncResult(
        status="pending_review", review_paths=paths[:50], message=message
    )


def _staged_upload_source(
    stack: ExitStack, folder: FolderMapping, path: str, local: InventoryEntry
) -> str:
    """Return a local path whose basename equals the desired remote name.

    Regular files already have the right basename; preserved symlinks are
    materialized as a ``.rclonelink`` blob in a temp dir for the upload.
    """
    remote_rel = remote_storage_path(folder, path, kind=local.kind)
    name = PurePosixPath(remote_rel.strip("/")).name
    if local.kind == "symlink":
        tmp = stack.enter_context(
            tempfile.TemporaryDirectory(prefix="protondrive-batch-link-")
        )
        staged = Path(tmp) / name
        staged.write_text(_symlink_target(folder, path, local), encoding="utf-8")
        return str(staged)
    local_abs = Path(folder.local_path) / path
    if local_abs.name == name:
        return str(local_abs)
    tmp = stack.enter_context(
        tempfile.TemporaryDirectory(prefix="protondrive-batch-stage-")
    )
    staged = Path(tmp) / name
    import shutil

    shutil.copy2(local_abs, staged)
    return str(staged)


def _batch_upload(
    backend: ProtonDriveCLI,
    folder: FolderMapping,
    upload_paths: list[str],
    local_by_path: dict[str, InventoryEntry],
    remote_by_path: dict[str, RemoteNode],
    progress: Callable[[str], None] | None,
) -> None:
    """Upload a batch, one CLI invocation per (parent dir, replace strategy)."""
    groups: dict[tuple[str, bool], list[str]] = defaultdict(list)
    sizes: dict[tuple[str, bool], int] = defaultdict(int)
    with ExitStack() as stack:
        for path in upload_paths:
            local = local_by_path[path]
            parent = _remote_parent_for_path(folder, path)
            replace = remote_by_path.get(path) is not None
            key = (parent, replace)
            groups[key].append(_staged_upload_source(stack, folder, path, local))
            sizes[key] += local.local_size or 0
        for (parent, replace), sources in groups.items():
            if progress:
                progress(f"  batch upload: {len(sources)} file(s) -> {parent}")
            backend.upload_many(
                sources,
                parent,
                replace=replace,
                total_size_hint=sizes[(parent, replace)],
            )


def _batch_verify_uploads(
    backend: ProtonDriveCLI,
    folder: FolderMapping,
    config: AppConfig,
    upload_paths: list[str],
    local_by_path: dict[str, InventoryEntry],
) -> tuple[list[str], dict[str, RemoteNode]]:
    """Verify uploaded files by remote sha1/size, with consistency retries."""
    expected = {
        p: _expected_signature(folder, p, local_by_path[p]) for p in upload_paths
    }
    infos: dict[str, RemoteNode] = {}
    bad: list[str] = list(upload_paths)
    for attempt in range(_BATCH_VERIFY_ATTEMPTS):
        infos = list_remote_infos_for_paths(
            folder, config, upload_paths, backend=backend
        )
        bad = []
        for path in upload_paths:
            node = infos.get(path)
            exp_size, exp_sha1 = expected[path]
            if (
                node is None
                or (exp_size is not None and node.size != exp_size)
                or (exp_sha1 and node.sha1 and node.sha1.lower() != exp_sha1.lower())
            ):
                bad.append(path)
        if not bad:
            break
        if attempt < _BATCH_VERIFY_ATTEMPTS - 1:
            time.sleep(_BATCH_VERIFY_DELAY)
    return bad, infos


def _batch_verify_deletes(
    backend: ProtonDriveCLI,
    folder: FolderMapping,
    config: AppConfig,
    delete_paths: list[str],
) -> list[str]:
    """Confirm trashed paths are gone, with consistency retries."""
    still: list[str] = list(delete_paths)
    for attempt in range(_BATCH_VERIFY_ATTEMPTS):
        remaining = list_remote_infos_for_paths(
            folder, config, delete_paths, backend=backend
        )
        still = [path for path in delete_paths if path in remaining]
        if not still:
            break
        if attempt < _BATCH_VERIFY_ATTEMPTS - 1:
            time.sleep(_BATCH_VERIFY_DELAY)
    return still


def apply_local_batch(
    folder: FolderMapping,
    config: AppConfig,
    paths: list[str],
    local_by_path: dict[str, InventoryEntry],
    *,
    operation_id: str | None = None,
    progress: Callable[[str], None] | None = None,
    backend: ProtonDriveCLI | None = None,
) -> TargetedSyncResult:
    """Apply a batch of local changes with grouped backend operations."""
    result = TargetedSyncResult()
    backend = make_backend(config, backend)
    operation_id = operation_id or str(uuid.uuid4())
    try:
        remote_by_path = list_remote_infos_for_paths(
            folder, config, paths, backend=backend
        )
    except ProtonError as exc:
        return TargetedSyncResult(
            status="degraded", message=f"batch remote preflight failed: {exc}"
        )

    upload_paths: list[str] = []
    delete_paths: list[str] = []
    previous_by_path: dict[str, InventoryEntry | None] = {}
    local_signatures = {
        (entry.local_size, entry.local_sha1)
        for entry in local_by_path.values()
        if entry.local_size is not None and entry.local_sha1
    }
    for path in paths:
        previous = get_inventory_entry(config, folder, path)
        previous_by_path[path] = previous
        local = local_by_path.get(path)
        remote = remote_by_path.get(path)
        classification = classify_delta(previous, local, remote)
        if local is not None and classification in ("local_only_new", "local_modified"):
            upload_paths.append(path)
            continue
        if local is None and classification == "local_deleted":
            previous_size = None
            previous_sha1 = None
            if previous is not None:
                previous_size = (
                    previous.remote_size
                    if previous.remote_size is not None
                    else previous.local_size
                )
                previous_sha1 = previous.remote_sha1 or previous.local_sha1
            previous_signature = (previous_size, previous_sha1)
            recreated_in_batch = (
                previous_signature[0] is not None
                and previous_signature[1]
                and previous_signature in local_signatures
            )
            if (
                previous
                and (
                    (previous.remote_size or 0) >= config.protect_delete_min_bytes
                    or is_work_file(path)
                )
                and not recreated_in_batch
            ):
                return _batch_review("protected delete requires review", [path])
            delete_paths.append(path)
            continue
        if classification == "unchanged":
            continue
        return _batch_review(classification, [path])

    verified_remote: dict[str, RemoteNode] = {}
    if upload_paths:
        try:
            _batch_upload(
                backend, folder, upload_paths, local_by_path, remote_by_path, progress
            )
        except ProtonError as exc:
            return TargetedSyncResult(
                status="degraded", degraded_paths=upload_paths[:50], message=str(exc)
            )
        bad, verified_remote = _batch_verify_uploads(
            backend, folder, config, upload_paths, local_by_path
        )
        if bad:
            return TargetedSyncResult(
                status="degraded",
                degraded_paths=bad[:50],
                message=f"batch upload verify failed: {len(bad)} path(s)",
            )

    if delete_paths:
        if progress:
            progress(f"  batch trash: {len(delete_paths)} path(s)")
        try:
            for path in delete_paths:
                previous = previous_by_path[path]
                remote_rel = remote_storage_path(
                    folder, path, kind=previous.kind if previous else None
                )
                if not backend.trash(remote_rel):
                    return TargetedSyncResult(
                        status="degraded",
                        degraded_paths=[path],
                        message="remote trash reported failure",
                    )
        except ProtonError as exc:
            return TargetedSyncResult(
                status="degraded", degraded_paths=delete_paths[:50], message=str(exc)
            )
        still_present = _batch_verify_deletes(backend, folder, config, delete_paths)
        if still_present:
            return TargetedSyncResult(
                status="degraded",
                degraded_paths=still_present[:50],
                message=f"batch delete verify failed: {len(still_present)} path(s) still present",
            )

    now = utc_now()
    entries: list[InventoryEntry] = []
    journal_changes: list[JournalChange] = []
    for path in upload_paths:
        local = local_by_path[path]
        remote_node = verified_remote.get(path)
        entry = InventoryEntry(
            folder_id=folder_id(folder.local_path, folder.remote_subpath),
            path=path,
            kind=local.kind,
            local_size=local.local_size,
            local_mtime_ns=local.local_mtime_ns,
            local_sha1=local.local_sha1,
            remote_size=remote_node.size
            if remote_node is not None
            else local.local_size,
            remote_sha1=remote_node.sha1
            if remote_node is not None
            else local.local_sha1,
            remote_modtime=remote_node.modtime if remote_node is not None else None,
            link_target=local.link_target,
            last_verified_at=now,
            last_changed_at=now,
            last_source="batch-local",
            deleted_at=None,
        )
        entries.append(entry)
        journal_changes.append(
            JournalChange(
                path=path,
                action="upload",
                before=_entry_state(previous_by_path[path]),
                after=_local_state(entry),
            )
        )
    for path in delete_paths:
        previous = previous_by_path[path]
        entry = InventoryEntry(
            folder_id=folder_id(folder.local_path, folder.remote_subpath),
            path=path,
            kind=previous.kind if previous else "file",
            last_changed_at=now,
            last_source="batch-local",
            deleted_at=now,
        )
        entries.append(entry)
        journal_changes.append(
            JournalChange(
                path=path, action="delete", before=_entry_state(previous), after=None
            )
        )

    upsert_inventory_entries(config, entries)
    if journal_changes:
        journal = make_journal_entry(
            folder, config, journal_changes, operation_id=operation_id
        )
        if not write_journal_entry(config, journal):
            result.status = "journal_pending"
            result.message = "batch data synced but journal upload is pending"

    result.synced_paths.extend(upload_paths)
    result.deleted_paths.extend(delete_paths)
    return result


def ensure_inventory_seeded(config: AppConfig, folder: FolderMapping) -> None:
    init_inventory(config)
    if folder_inventory_count(config, folder) > 0:
        return
    raise InventoryError("inventory is empty; run CLI setup before syncing")


def run_targeted_sync_cycle(
    folder: FolderMapping,
    config: AppConfig,
    *,
    since_ns: int | None = None,
    poll_remote_journal: bool = True,
    progress: Callable[[str], None] | None = None,
) -> TargetedSyncResult:
    """Run one targeted sync cycle for a folder."""
    result = TargetedSyncResult()
    operation_id = str(uuid.uuid4())
    backend = make_backend(config)
    try:
        ensure_inventory_seeded(config, folder)
    except InventoryError as exc:
        mark_folder_status(config, folder, "degraded", error=str(exc))
        return TargetedSyncResult(status="degraded", message=str(exc))

    fid = folder_id(folder.local_path, folder.remote_subpath)
    retry_journal_outbox(config, fid)
    if outbox_has_pending(config, folder):
        mark_folder_status(
            config,
            folder,
            "journal_pending",
            error="journal outbox has pending entries",
        )
        return TargetedSyncResult(
            status="journal_pending", message="journal outbox has pending entries"
        )

    journal_entries: list[JournalEntry] = []
    if poll_remote_journal:
        try:
            journal_entries = poll_journal(config, folder)
        except ProtonError as exc:
            mark_folder_status(config, folder, "journal_stale", error=str(exc))
            journal_entries = []
            result.status = "journal_stale"
            result.message = str(exc)

    for journal_entry in journal_entries:
        for change in journal_entry.changes:
            if progress:
                progress(f"  journal {change.action}: {change.path}")
            _merge_result(
                result,
                apply_journal_change(
                    folder, config, journal_entry, change, backend=backend
                ),
            )
            if result.status in ("degraded", "pending_review", "journal_pending"):
                mark_folder_status(config, folder, result.status, error=result.message)
                return result
        record_journal_seen(config, fid, journal_entry.entry_id)

    paths = changed_local_paths(folder, config, since_ns=since_ns)
    if config.batch_sync_enabled and len(paths) >= config.batch_min_paths_per_cycle:
        if len(paths) > config.batch_max_paths_per_cycle:
            message = f"batch path set too large: {len(paths)} paths"
            mark_folder_status(config, folder, "pending_review", error=message)
            return TargetedSyncResult(
                status="pending_review", review_paths=paths[:50], message=message
            )
    elif len(paths) > config.targeted_max_paths_per_cycle:
        message = f"targeted path batch too large: {len(paths)} paths"
        mark_folder_status(config, folder, "pending_review", error=message)
        return TargetedSyncResult(
            status="pending_review", review_paths=paths[:50], message=message
        )

    local_by_path = _scan_selected_local(folder, config, paths)
    total_bytes = sum((entry.local_size or 0) for entry in local_by_path.values())
    if total_bytes > config.targeted_max_bytes_per_cycle:
        message = f"targeted byte batch too large: {total_bytes} bytes"
        mark_folder_status(config, folder, "pending_review", error=message)
        return TargetedSyncResult(
            status="pending_review", review_paths=paths[:50], message=message
        )

    if config.batch_sync_enabled and len(paths) >= config.batch_min_paths_per_cycle:
        step = apply_local_batch(
            folder,
            config,
            paths,
            local_by_path,
            operation_id=operation_id,
            progress=progress,
            backend=backend,
        )
        _merge_result(result, step)
        if result.status in ("degraded", "pending_review", "journal_pending"):
            mark_folder_status(config, folder, result.status, error=result.message)
            return result
        if result.status == "healthy":
            mark_folder_status(config, folder, "healthy")
        return result

    for path in paths:
        local = local_by_path.get(path)
        if progress:
            progress(f"  targeted {'upload' if local else 'delete'}: {path}")
        step = (
            apply_local_upload(
                folder, config, path, local, operation_id=operation_id, backend=backend
            )
            if local is not None
            else apply_local_delete(
                folder, config, path, operation_id=operation_id, backend=backend
            )
        )
        _merge_result(result, step)
        if result.status in ("degraded", "pending_review", "journal_pending"):
            mark_folder_status(config, folder, result.status, error=result.message)
            return result

    if result.status == "healthy":
        mark_folder_status(config, folder, "healthy")
    return result
