"""SQLite inventory for P3 targeted sync.

The inventory stores the app's last verified view of each synced path. It is
not a live remote listing cache; targeted sync still probes exact remote paths
before mutating either side.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Optional

from .config import AppConfig, FolderMapping, effective_filters, normalize_symlink_mode
from .migration import _matches_filter
from .state import folder_id, utc_now


SCHEMA_VERSION = 1
APP_METADATA_PATHS = {".protondrive-sync-check", ".protondrive-sync.json"}


class InventoryError(Exception):
    """Raised when inventory state cannot be trusted."""


@dataclass
class InventoryEntry:
    folder_id: str
    path: str
    kind: str = "file"
    local_size: int | None = None
    local_mtime_ns: int | None = None
    local_sha1: str | None = None
    remote_size: int | None = None
    remote_sha1: str | None = None
    remote_modtime: str | None = None
    link_target: str | None = None
    last_verified_at: str | None = None
    last_changed_at: str | None = None
    last_source: str = "local"
    deleted_at: str | None = None


def is_app_metadata_path(path: str) -> bool:
    """Return True for root-level app-owned metadata not managed as user data."""
    return path.strip("/") in APP_METADATA_PATHS


@dataclass
class OutboxEntry:
    id: str
    folder_id: str
    remote_path: str
    entry_json: str
    attempts: int = 0
    last_error: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


def inventory_path(config: AppConfig) -> Path:
    return config.inventory_file


def connect_inventory(config: AppConfig) -> sqlite3.Connection:
    path = inventory_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init_inventory(config: AppConfig) -> Path:
    """Create or migrate the inventory database."""
    path = inventory_path(config)
    with connect_inventory(config) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS folder_inventory (
                folder_id TEXT NOT NULL,
                path TEXT NOT NULL,
                kind TEXT NOT NULL,
                local_size INTEGER,
                local_mtime_ns INTEGER,
                local_sha1 TEXT,
                remote_size INTEGER,
                remote_sha1 TEXT,
                remote_modtime TEXT,
                link_target TEXT,
                last_verified_at TEXT,
                last_changed_at TEXT,
                last_source TEXT NOT NULL,
                deleted_at TEXT,
                PRIMARY KEY (folder_id, path)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS journal_seen (
                folder_id TEXT NOT NULL,
                entry_id TEXT NOT NULL,
                seen_at TEXT NOT NULL,
                PRIMARY KEY (folder_id, entry_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS journal_outbox (
                id TEXT PRIMARY KEY,
                folder_id TEXT NOT NULL,
                remote_path TEXT NOT NULL,
                entry_json TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_state (
                folder_id TEXT PRIMARY KEY,
                cursor TEXT,
                last_started_at TEXT,
                last_completed_at TEXT,
                incomplete INTEGER NOT NULL DEFAULT 0,
                last_error TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sync_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                folder_id TEXT NOT NULL,
                operation_id TEXT,
                path TEXT,
                event_type TEXT NOT NULL,
                status TEXT NOT NULL,
                message TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
    return path


def _entry_from_row(row: sqlite3.Row) -> InventoryEntry:
    return InventoryEntry(
        **{key: row[key] for key in InventoryEntry.__dataclass_fields__}
    )


def upsert_inventory_entry(config: AppConfig, entry: InventoryEntry) -> None:
    init_inventory(config)
    fields = asdict(entry)
    columns = list(fields)
    placeholders = ", ".join("?" for _ in columns)
    updates = ", ".join(
        f"{col}=excluded.{col}" for col in columns if col not in {"folder_id", "path"}
    )
    with connect_inventory(config) as conn:
        conn.execute(
            f"""
            INSERT INTO folder_inventory ({", ".join(columns)})
            VALUES ({placeholders})
            ON CONFLICT(folder_id, path) DO UPDATE SET {updates}
            """,
            [fields[col] for col in columns],
        )


def upsert_inventory_entries(
    config: AppConfig, entries: Iterable[InventoryEntry]
) -> int:
    init_inventory(config)
    rows = [asdict(entry) for entry in entries]
    if not rows:
        return 0
    columns = list(rows[0])
    placeholders = ", ".join("?" for _ in columns)
    updates = ", ".join(
        f"{col}=excluded.{col}" for col in columns if col not in {"folder_id", "path"}
    )
    values = [[row[col] for col in columns] for row in rows]
    with connect_inventory(config) as conn:
        conn.executemany(
            f"""
            INSERT INTO folder_inventory ({", ".join(columns)})
            VALUES ({placeholders})
            ON CONFLICT(folder_id, path) DO UPDATE SET {updates}
            """,
            values,
        )
    return len(rows)


def get_inventory_entry(
    config: AppConfig, folder: FolderMapping, path: str
) -> InventoryEntry | None:
    init_inventory(config)
    fid = folder_id(folder.local_path, folder.remote_subpath)
    with connect_inventory(config) as conn:
        row = conn.execute(
            "SELECT * FROM folder_inventory WHERE folder_id=? AND path=?",
            (fid, path),
        ).fetchone()
    return _entry_from_row(row) if row else None


def list_inventory_entries(
    config: AppConfig, folder: FolderMapping
) -> list[InventoryEntry]:
    init_inventory(config)
    fid = folder_id(folder.local_path, folder.remote_subpath)
    with connect_inventory(config) as conn:
        rows = conn.execute(
            "SELECT * FROM folder_inventory WHERE folder_id=? ORDER BY path",
            (fid,),
        ).fetchall()
    return [_entry_from_row(row) for row in rows]


def mark_inventory_deleted(
    config: AppConfig, folder: FolderMapping, path: str, *, source: str
) -> None:
    entry = get_inventory_entry(config, folder, path)
    now = utc_now()
    if entry is None:
        entry = InventoryEntry(
            folder_id=folder_id(folder.local_path, folder.remote_subpath),
            path=path,
            last_source=source,
            deleted_at=now,
        )
    else:
        entry.deleted_at = now
        entry.last_changed_at = now
        entry.last_source = source
    upsert_inventory_entry(config, entry)


def record_sync_event(
    config: AppConfig,
    folder: FolderMapping,
    event_type: str,
    status: str,
    *,
    path: str | None = None,
    operation_id: str | None = None,
    message: str | None = None,
) -> None:
    init_inventory(config)
    fid = folder_id(folder.local_path, folder.remote_subpath)
    with connect_inventory(config) as conn:
        conn.execute(
            """
            INSERT INTO sync_events (folder_id, operation_id, path, event_type, status, message, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (fid, operation_id, path, event_type, status, message, utc_now()),
        )


def sha1_file(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _local_entry(
    root: Path,
    entry: Path,
    folder: FolderMapping,
    config: AppConfig,
    *,
    hash_paths: set[str] | None,
    hash_all: bool,
) -> InventoryEntry | None:
    rel = entry.relative_to(root).as_posix()
    try:
        stat = (
            entry.lstat()
            if entry.is_symlink() and folder.symlink_mode == "preserve"
            else entry.stat()
        )
    except OSError:
        return None
    kind = "file"
    link_target = None
    size = stat.st_size
    local_sha1 = None
    if entry.is_symlink() and folder.symlink_mode == "preserve":
        kind = "symlink"
        try:
            link_target = os.readlink(entry)
            size = len(link_target.encode("utf-8"))
        except OSError:
            link_target = None
            size = 0
    elif entry.is_dir():
        kind = "dir"
        size = 0
    elif hash_all or (hash_paths is not None and rel in hash_paths):
        try:
            local_sha1 = sha1_file(entry)
        except OSError:
            local_sha1 = None
    return InventoryEntry(
        folder_id=folder_id(folder.local_path, folder.remote_subpath),
        path=rel,
        kind=kind,
        local_size=size,
        local_mtime_ns=stat.st_mtime_ns,
        local_sha1=local_sha1,
        link_target=link_target,
        last_source="local",
    )


def scan_local_inventory(
    folder: FolderMapping,
    config: AppConfig,
    *,
    hash_paths: set[str] | None = None,
    hash_all: bool = False,
) -> dict[str, InventoryEntry]:
    """Return current local file/symlink entries, respecting effective filters."""
    mode = normalize_symlink_mode(folder.symlink_mode)
    root = Path(folder.local_path).expanduser().absolute()
    filters = effective_filters(config, folder)
    entries: dict[str, InventoryEntry] = {}
    if not root.is_dir():
        return entries
    for dirpath, dirnames, filenames in os.walk(
        root, topdown=True, followlinks=(mode == "copy")
    ):
        current = Path(dirpath)
        kept_dirs: list[str] = []
        for dirname in dirnames:
            child = current / dirname
            rel = child.relative_to(root).as_posix()
            if is_app_metadata_path(rel):
                continue
            if _matches_filter(rel, filters):
                continue
            if child.is_symlink() and mode == "preserve":
                local = _local_entry(
                    root,
                    child,
                    folder,
                    config,
                    hash_paths=hash_paths,
                    hash_all=hash_all,
                )
                if local is not None:
                    entries[local.path] = local
                continue
            if mode != "skip" or not child.is_symlink():
                kept_dirs.append(dirname)
        dirnames[:] = kept_dirs
        for filename in filenames:
            child = current / filename
            rel = child.relative_to(root).as_posix()
            if is_app_metadata_path(rel):
                continue
            if _matches_filter(rel, filters):
                continue
            if child.is_symlink() and mode == "skip":
                continue
            local = _local_entry(
                root, child, folder, config, hash_paths=hash_paths, hash_all=hash_all
            )
            if local is not None and local.kind != "dir":
                entries[local.path] = local
    return entries


def folder_inventory_count(config: AppConfig, folder: FolderMapping) -> int:
    init_inventory(config)
    fid = folder_id(folder.local_path, folder.remote_subpath)
    with connect_inventory(config) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM folder_inventory WHERE folder_id=?", (fid,)
        ).fetchone()
    return int(row["count"] if row else 0)


def record_journal_seen(config: AppConfig, folder_id_value: str, entry_id: str) -> None:
    init_inventory(config)
    with connect_inventory(config) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO journal_seen (folder_id, entry_id, seen_at) VALUES (?, ?, ?)",
            (folder_id_value, entry_id, utc_now()),
        )


def journal_entry_seen(config: AppConfig, folder_id_value: str, entry_id: str) -> bool:
    init_inventory(config)
    with connect_inventory(config) as conn:
        row = conn.execute(
            "SELECT 1 FROM journal_seen WHERE folder_id=? AND entry_id=?",
            (folder_id_value, entry_id),
        ).fetchone()
    return row is not None


def enqueue_journal_outbox(config: AppConfig, outbox: OutboxEntry) -> None:
    init_inventory(config)
    now = utc_now()
    created = outbox.created_at or now
    updated = outbox.updated_at or now
    with connect_inventory(config) as conn:
        conn.execute(
            """
            INSERT INTO journal_outbox (id, folder_id, remote_path, entry_json, attempts, last_error, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                remote_path=excluded.remote_path,
                entry_json=excluded.entry_json,
                attempts=excluded.attempts,
                last_error=excluded.last_error,
                updated_at=excluded.updated_at
            """,
            (
                outbox.id,
                outbox.folder_id,
                outbox.remote_path,
                outbox.entry_json,
                outbox.attempts,
                outbox.last_error,
                created,
                updated,
            ),
        )


def list_journal_outbox(
    config: AppConfig, folder_id_value: str | None = None
) -> list[OutboxEntry]:
    init_inventory(config)
    with connect_inventory(config) as conn:
        if folder_id_value is None:
            rows = conn.execute(
                "SELECT * FROM journal_outbox ORDER BY created_at"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM journal_outbox WHERE folder_id=? ORDER BY created_at",
                (folder_id_value,),
            ).fetchall()
    return [
        OutboxEntry(**{key: row[key] for key in OutboxEntry.__dataclass_fields__})
        for row in rows
    ]


def delete_journal_outbox(config: AppConfig, entry_id: str) -> None:
    init_inventory(config)
    with connect_inventory(config) as conn:
        conn.execute("DELETE FROM journal_outbox WHERE id=?", (entry_id,))


def mark_journal_outbox_error(config: AppConfig, entry_id: str, error: str) -> None:
    init_inventory(config)
    with connect_inventory(config) as conn:
        conn.execute(
            """
            UPDATE journal_outbox
            SET attempts=attempts + 1, last_error=?, updated_at=?
            WHERE id=?
            """,
            (error, utc_now(), entry_id),
        )


def outbox_has_pending(config: AppConfig, folder: FolderMapping | None = None) -> bool:
    fid = (
        folder_id(folder.local_path, folder.remote_subpath)
        if folder is not None
        else None
    )
    return bool(list_journal_outbox(config, fid))


def get_audit_state(
    config: AppConfig, folder: FolderMapping
) -> dict[str, object] | None:
    init_inventory(config)
    fid = folder_id(folder.local_path, folder.remote_subpath)
    with connect_inventory(config) as conn:
        row = conn.execute(
            "SELECT * FROM audit_state WHERE folder_id=?", (fid,)
        ).fetchone()
    return dict(row) if row else None


def update_audit_state(
    config: AppConfig, folder: FolderMapping, **updates: object
) -> None:
    init_inventory(config)
    fid = folder_id(folder.local_path, folder.remote_subpath)
    current = get_audit_state(config, folder) or {
        "folder_id": fid,
        "cursor": None,
        "last_started_at": None,
        "last_completed_at": None,
        "incomplete": 0,
        "last_error": None,
    }
    for key, value in updates.items():
        if key in current:
            current[key] = value
    with connect_inventory(config) as conn:
        conn.execute(
            """
            INSERT INTO audit_state (folder_id, cursor, last_started_at, last_completed_at, incomplete, last_error)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(folder_id) DO UPDATE SET
                cursor=excluded.cursor,
                last_started_at=excluded.last_started_at,
                last_completed_at=excluded.last_completed_at,
                incomplete=excluded.incomplete,
                last_error=excluded.last_error
            """,
            (
                fid,
                current["cursor"],
                current["last_started_at"],
                current["last_completed_at"],
                int(current["incomplete"] or 0),
                current["last_error"],
            ),
        )


def inventory_entry_to_json(entry: InventoryEntry | None) -> dict[str, object] | None:
    return asdict(entry) if entry is not None else None


def json_dumps(data: object) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"))
