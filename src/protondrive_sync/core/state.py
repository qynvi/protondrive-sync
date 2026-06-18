"""Durable runtime health state for synced folders."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import AppConfig, FolderMapping


FOLDER_STATUSES = (
    "healthy",
    "syncing",
    "verifying",
    "audit_due",
    "journal_stale",
    "journal_pending",
    "degraded",
    "pending_review",
)
BLOCKING_STATUSES = ("degraded", "pending_review", "syncing", "verifying", "journal_pending")


def utc_now() -> str:
    """Return a compact UTC timestamp for state files."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def folder_id(local_path: str, remote_subpath: str | None = None) -> str:
    """Return a stable, filesystem-safe id for a folder mapping."""
    key = local_path if remote_subpath is None else f"{local_path}\0{remote_subpath}"
    digest = hashlib.sha256(key.encode("utf-8", "surrogateescape")).hexdigest()[:16]
    safe = "".join(ch if ch.isalnum() else "-" for ch in Path(local_path).name).strip("-")
    return f"{safe or 'folder'}-{digest}"


@dataclass
class FolderState:
    """Persisted health state for one folder mapping."""

    status: str = "healthy"
    last_error: str | None = None
    last_success: str | None = None
    last_verify: str | None = None
    last_remote_poll: str | None = None
    last_scan_started_ns: int | None = None
    last_inventory_id: str | None = None
    setup_session_id: str | None = None
    backend_version: str | None = None
    backend_probe_ok_at: str | None = None
    journal_status: str | None = None
    audit_cursor: str | None = None

    def __post_init__(self) -> None:
        if self.status not in FOLDER_STATUSES:
            self.status = "degraded"


@dataclass
class RuntimeState:
    """Top-level runtime state document."""

    folders: dict[str, FolderState] = field(default_factory=dict)


def _state_path(config: AppConfig) -> Path:
    return config.state_file


def load_state(config: AppConfig) -> RuntimeState:
    """Load runtime state, returning an empty state if missing/corrupt."""
    path = _state_path(config)
    if not path.exists():
        return RuntimeState()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return RuntimeState()
    folders: dict[str, FolderState] = {}
    for key, value in raw.get("folders", {}).items():
        if isinstance(value, dict):
            folders[key] = FolderState(**{k: v for k, v in value.items() if k in FolderState.__dataclass_fields__})
    return RuntimeState(folders=folders)


def save_state(config: AppConfig, state: RuntimeState) -> Path:
    """Persist runtime state and return the written path."""
    path = _state_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"folders": {key: asdict(value) for key, value in state.folders.items()}}
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def state_key(folder: FolderMapping) -> str:
    """Return the state key for a folder mapping."""
    return folder_id(folder.local_path, folder.remote_subpath)


def get_folder_state(config: AppConfig, folder: FolderMapping) -> FolderState:
    """Return a folder state, creating a healthy default in memory if missing."""
    state = load_state(config)
    return state.folders.get(state_key(folder), FolderState())


def update_folder_state(config: AppConfig, folder: FolderMapping, **updates: object) -> FolderState:
    """Apply updates to one folder state and persist them."""
    runtime = load_state(config)
    key = state_key(folder)
    folder_state = runtime.folders.get(key, FolderState())
    for name, value in updates.items():
        if name in FolderState.__dataclass_fields__:
            setattr(folder_state, name, value)
    folder_state.__post_init__()
    runtime.folders[key] = folder_state
    save_state(config, runtime)
    return folder_state


def mark_folder_status(
    config: AppConfig,
    folder: FolderMapping,
    status: str,
    *,
    error: str | None = None,
) -> FolderState:
    """Persist a status transition with conventional timestamps."""
    updates: dict[str, object] = {"status": status}
    now = utc_now()
    if status == "healthy":
        updates["last_success"] = now
        updates["last_error"] = None
    elif status == "verifying":
        updates["last_verify"] = now
    elif status in ("degraded", "pending_review", "journal_pending"):
        updates["last_error"] = error
    elif status == "journal_stale":
        updates["last_remote_poll"] = now
    return update_folder_state(config, folder, **updates)


def folder_blocks_automatic_sync(config: AppConfig, folder: FolderMapping) -> bool:
    """Return True if persisted state should block automatic daemon sync."""
    return get_folder_state(config, folder).status in BLOCKING_STATUSES
