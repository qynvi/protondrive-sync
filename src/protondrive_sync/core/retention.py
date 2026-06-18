"""Scoped backup and journal retention cleanup."""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import AppConfig, FolderMapping
from .journal import journal_root
from .proton_cli import ProtonDriveCLI, ProtonError
from .state import folder_id


APP_BACKUP_DIR = ".protondrive-sync-backups"
APP_JOURNAL_DIR = ".protondrive-sync-journal"


class RetentionError(Exception):
    """Raised when cleanup scope is unsafe."""


@dataclass
class RetentionResult:
    deleted_local: list[Path] = field(default_factory=list)
    deleted_remote: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        if normalized.endswith("+0000"):
            normalized = normalized[:-5] + "+00:00"
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _is_app_local_backup_root(path: Path) -> bool:
    return APP_BACKUP_DIR in path.parts


def validate_local_backup_scope(path: Path) -> None:
    if not _is_app_local_backup_root(path):
        raise RetentionError(f"refusing cleanup outside app backup roots: {path}")


def cleanup_local_backup_root(
    root: Path,
    *,
    retention_days: int,
    now: datetime | None = None,
) -> RetentionResult:
    """Delete aged files under one app backup root and prune empty dirs."""
    root = root.expanduser().absolute()
    validate_local_backup_scope(root)
    result = RetentionResult()
    if not root.exists():
        return result
    cutoff = (now or _now()) - timedelta(days=retention_days)
    for path in sorted(root.rglob("*"), reverse=True):
        try:
            if path.is_dir():
                try:
                    path.rmdir()
                except OSError:
                    pass
                continue
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            if mtime <= cutoff:
                path.unlink()
                result.deleted_local.append(path)
        except OSError as exc:
            result.skipped.append(f"{path}: {exc}")
    try:
        root.rmdir()
    except OSError:
        pass
    return result


def cleanup_folder_local_backups(
    config: AppConfig,
    folder: FolderMapping,
    *,
    now: datetime | None = None,
) -> RetentionResult:
    root = Path(folder.local_path) / APP_BACKUP_DIR
    return cleanup_local_backup_root(
        root, retention_days=config.backup_retention_days, now=now
    )


def _validate_remote_scope(remote_path: str, *, allowed_roots: tuple[str, ...]) -> None:
    clean = remote_path.strip("/")
    if not any(clean == root or clean.startswith(root + "/") for root in allowed_roots):
        raise RetentionError(
            f"refusing cleanup outside app remote roots: {remote_path}"
        )


def cleanup_remote_backup_root(
    config: AppConfig,
    remote_root: str,
    *,
    retention_days: int | None = None,
    now: datetime | None = None,
) -> RetentionResult:
    """Delete aged files under a remote app backup root without broad purge."""
    root = remote_root.strip("/")
    _validate_remote_scope(root, allowed_roots=(APP_BACKUP_DIR,))
    cutoff = (now or _now()) - timedelta(
        days=retention_days or config.backup_retention_days
    )
    result = RetentionResult()
    backend = ProtonDriveCLI(config)
    try:
        nodes = backend.list_recursive(root)
    except ProtonError as exc:
        result.skipped.append(str(exc))
        return result
    for node in nodes:
        modtime = _parse_time(node.modtime)
        if modtime is None or modtime > cutoff:
            continue
        remote_path = node.path.strip("/")
        _validate_remote_scope(remote_path, allowed_roots=(APP_BACKUP_DIR,))
        try:
            backend.trash(remote_path)
            result.deleted_remote.append(remote_path)
        except ProtonError as exc:
            result.skipped.append(f"{remote_path}: {exc}")
    return result


def cleanup_remote_journal(
    config: AppConfig,
    folder: FolderMapping,
    *,
    now: datetime | None = None,
) -> RetentionResult:
    """Delete aged remote journal files for one folder."""
    fid = folder_id(folder.local_path, folder.remote_subpath)
    root = journal_root(fid)
    _validate_remote_scope(root, allowed_roots=(APP_JOURNAL_DIR,))
    cutoff = (now or _now()) - timedelta(days=config.journal_retention_days)
    result = RetentionResult()
    backend = ProtonDriveCLI(config)
    try:
        nodes = backend.list_recursive(root)
    except ProtonError as exc:
        result.skipped.append(str(exc))
        return result
    root_prefix = root.strip("/") + "/"
    for node in nodes:
        remote_path = node.path.strip("/")
        rel = (
            remote_path[len(root_prefix) :]
            if remote_path.startswith(root_prefix)
            else remote_path
        )
        # Journal paths are date-partitioned; prefer that over remote mtime.
        date_part = Path(rel).parts[0] if Path(rel).parts else ""
        try:
            created = datetime.fromisoformat(date_part).replace(tzinfo=timezone.utc)
        except ValueError:
            created = _parse_time(node.modtime) or _now()
        if created > cutoff:
            continue
        _validate_remote_scope(remote_path, allowed_roots=(APP_JOURNAL_DIR,))
        try:
            backend.trash(remote_path)
            result.deleted_remote.append(remote_path)
        except ProtonError as exc:
            result.skipped.append(f"{remote_path}: {exc}")
    return result


def remove_local_backup_tree(path: Path) -> None:
    """Remove an app backup tree after explicit UI confirmation."""
    path = path.expanduser().absolute()
    validate_local_backup_scope(path)
    if path.exists():
        shutil.rmtree(path)
