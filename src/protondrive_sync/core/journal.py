"""Remote journal and local outbox for app-to-app targeted sync."""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import AppConfig, FolderMapping
from .inventory import (
    OutboxEntry,
    delete_journal_outbox,
    enqueue_journal_outbox,
    journal_entry_seen,
    list_journal_outbox,
    mark_journal_outbox_error,
    record_journal_seen,
)
from .locks import machine_id
from .proton_cli import ProtonDriveCLI, ProtonError
from .state import folder_id, utc_now


JOURNAL_SCHEMA = 1


@dataclass
class JournalChange:
    path: str
    action: str
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None


@dataclass
class JournalEntry:
    schema: int
    entry_id: str
    folder_id: str
    machine_id: str
    sequence: int
    operation_id: str
    created_at: str
    app_version: str
    backend_version: str | None
    filter_fingerprint: str | None
    symlink_mode: str
    changes: list[JournalChange] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["changes"] = [asdict(change) for change in self.changes]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> JournalEntry:
        changes = [JournalChange(**change) for change in data.get("changes", [])]
        payload = {
            key: data.get(key) for key in cls.__dataclass_fields__ if key != "changes"
        }
        payload["changes"] = changes
        return cls(**payload)


def _utc_date(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        parsed = datetime.now(timezone.utc)
    return parsed.astimezone(timezone.utc).date().isoformat()


def journal_root(folder_id_value: str) -> str:
    return f".protondrive-sync-journal/{folder_id_value}"


def journal_remote_path(entry: JournalEntry) -> str:
    day = _utc_date(entry.created_at)
    return f"{journal_root(entry.folder_id)}/{day}/{entry.entry_id}.json"


def make_journal_entry(
    folder: FolderMapping,
    config: AppConfig,
    changes: list[JournalChange],
    *,
    operation_id: str | None = None,
    sequence: int = 0,
    backend_version: str | None = None,
    filter_fingerprint: str | None = None,
) -> JournalEntry:
    return JournalEntry(
        schema=JOURNAL_SCHEMA,
        entry_id=str(uuid.uuid4()),
        folder_id=folder_id(folder.local_path, folder.remote_subpath),
        machine_id=machine_id(),
        sequence=sequence,
        operation_id=operation_id or str(uuid.uuid4()),
        created_at=utc_now(),
        app_version="0.1.0",
        backend_version=backend_version,
        filter_fingerprint=filter_fingerprint,
        symlink_mode=folder.symlink_mode,
        changes=changes,
    )


def _write_temp_json(data: dict[str, Any]) -> str:
    fd, tmp_name = tempfile.mkstemp(prefix="protondrive-sync-journal-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(data, indent=2, sort_keys=True) + "\n")
    return tmp_name


def write_journal_entry(
    config: AppConfig,
    entry: JournalEntry,
    *,
    persist_outbox: bool = True,
    backend: ProtonDriveCLI | None = None,
) -> bool:
    """Upload a journal entry. Return False and persist outbox on upload failure."""
    backend = backend if backend is not None else ProtonDriveCLI(config)
    remote_path = journal_remote_path(entry)
    tmp_name = _write_temp_json(entry.to_dict())
    try:
        backend.upload(tmp_name, remote_path, replace=True)
        record_journal_seen(config, entry.folder_id, entry.entry_id)
        return True
    except ProtonError as exc:
        if persist_outbox:
            enqueue_journal_outbox(
                config,
                OutboxEntry(
                    id=entry.entry_id,
                    folder_id=entry.folder_id,
                    remote_path=remote_path,
                    entry_json=json.dumps(entry.to_dict(), sort_keys=True),
                    last_error=str(exc),
                ),
            )
            return False
        raise
    finally:
        try:
            Path(tmp_name).unlink()
        except OSError:
            pass


def retry_journal_outbox(config: AppConfig, folder_id_value: str | None = None) -> int:
    """Retry pending journal uploads and return the number successfully sent."""
    sent = 0
    backend = ProtonDriveCLI(config)
    for item in list_journal_outbox(config, folder_id_value):
        tmp_name = _write_temp_json(json.loads(item.entry_json))
        try:
            backend.upload(tmp_name, item.remote_path, replace=True)
            delete_journal_outbox(config, item.id)
            record_journal_seen(config, item.folder_id, item.id)
            sent += 1
        except ProtonError as exc:
            mark_journal_outbox_error(config, item.id, str(exc))
        finally:
            try:
                Path(tmp_name).unlink()
            except OSError:
                pass
    return sent


def _load_remote_journal_entry(
    remote_path: str, config: AppConfig, backend: ProtonDriveCLI
) -> JournalEntry:
    data = json.loads(backend.download_text(remote_path))
    return JournalEntry.from_dict(data)


def poll_journal(
    config: AppConfig, folder: FolderMapping, *, include_own: bool = False
) -> list[JournalEntry]:
    """Poll the app-owned remote journal for unseen entries."""
    fid = folder_id(folder.local_path, folder.remote_subpath)
    backend = ProtonDriveCLI(config)
    # list_recursive returns full app-relative paths and an empty list for a
    # missing journal root, so absence is not surfaced as an error here.
    nodes = backend.list_recursive(journal_root(fid))

    current_machine = machine_id()
    unseen: list[JournalEntry] = []
    for item in sorted(nodes, key=lambda node: node.path):
        if not item.path.endswith(".json"):
            continue
        entry_id = Path(item.path).stem
        if journal_entry_seen(config, fid, entry_id):
            continue
        remote_path = item.path
        try:
            entry = _load_remote_journal_entry(remote_path, config, backend)
        except (OSError, json.JSONDecodeError, ProtonError):
            continue
        if entry.folder_id != fid:
            continue
        if not include_own and entry.machine_id == current_machine:
            record_journal_seen(config, fid, entry.entry_id)
            continue
        unseen.append(entry)
    return sorted(
        unseen, key=lambda entry: (entry.created_at, entry.sequence, entry.entry_id)
    )
