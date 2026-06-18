"""Tests for P3 remote journal and outbox."""

import json
from pathlib import Path

import pytest

from protondrive_sync.core.config import AppConfig, FolderMapping
from protondrive_sync.core.inventory import journal_entry_seen, list_journal_outbox
from protondrive_sync.core.journal import (
    JournalChange,
    journal_remote_path,
    make_journal_entry,
    poll_journal,
    retry_journal_outbox,
    write_journal_entry,
)
from protondrive_sync.core.proton_cli import RemoteNode
from tests.fake_backend import FakeBackend


@pytest.fixture
def config(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setattr(
        "protondrive_sync.core.config.get_config_dir", lambda: config_dir
    )
    monkeypatch.setattr("protondrive_sync.core.journal.machine_id", lambda: "machine-a")
    return AppConfig()


def _patch_backend(monkeypatch, backend):
    monkeypatch.setattr(
        "protondrive_sync.core.journal.ProtonDriveCLI", lambda _config: backend
    )


def test_make_journal_entry_shape(config, tmp_path):
    folder = FolderMapping(local_path=str(tmp_path / "proj"), remote_subpath="proj")
    entry = make_journal_entry(
        folder,
        config,
        [JournalChange(path="a.txt", action="upload", before=None, after={"size": 1})],
        operation_id="op-1",
    )

    data = entry.to_dict()
    assert data["schema"] == 1
    assert data["machine_id"] == "machine-a"
    assert data["operation_id"] == "op-1"
    assert data["changes"][0]["path"] == "a.txt"
    assert journal_remote_path(entry).endswith(f"/{entry.entry_id}.json")


def test_write_journal_entry_uploads_and_marks_seen(config, tmp_path, monkeypatch):
    backend = FakeBackend()
    _patch_backend(monkeypatch, backend)
    folder = FolderMapping(local_path=str(tmp_path / "proj"), remote_subpath="proj")
    entry = make_journal_entry(
        folder, config, [JournalChange(path="a.txt", action="upload")]
    )

    assert write_journal_entry(config, entry)

    assert backend.uploaded == [journal_remote_path(entry)]
    assert list_journal_outbox(config) == []


def test_write_journal_entry_persists_outbox_on_failure(config, tmp_path, monkeypatch):
    backend = FakeBackend(fail_upload=True)
    _patch_backend(monkeypatch, backend)
    folder = FolderMapping(local_path=str(tmp_path / "proj"), remote_subpath="proj")
    entry = make_journal_entry(
        folder, config, [JournalChange(path="a.txt", action="upload")]
    )

    assert not write_journal_entry(config, entry)

    outbox = list_journal_outbox(config, entry.folder_id)
    assert len(outbox) == 1
    assert outbox[0].id == entry.entry_id
    assert "offline" in (outbox[0].last_error or "")


def test_retry_journal_outbox_sends_and_deletes(config, tmp_path, monkeypatch):
    backend = FakeBackend(fail_upload=True)
    _patch_backend(monkeypatch, backend)
    folder = FolderMapping(local_path=str(tmp_path / "proj"), remote_subpath="proj")
    entry = make_journal_entry(
        folder, config, [JournalChange(path="a.txt", action="upload")]
    )
    assert not write_journal_entry(config, entry)

    backend.fail_upload = False  # link restored
    sent = retry_journal_outbox(config, entry.folder_id)

    assert sent == 1
    assert list_journal_outbox(config, entry.folder_id) == []


def test_poll_journal_downloads_unseen_entries(config, tmp_path, monkeypatch):
    folder = FolderMapping(local_path=str(tmp_path / "proj"), remote_subpath="proj")
    entry = make_journal_entry(
        folder, config, [JournalChange(path="a.txt", action="upload")]
    )
    entry.machine_id = "machine-b"
    remote_path = journal_remote_path(entry)
    backend = FakeBackend(
        nodes={
            remote_path: RemoteNode(path=remote_path, name=f"{entry.entry_id}.json")
        },
        blobs={remote_path: json.dumps(entry.to_dict())},
    )
    _patch_backend(monkeypatch, backend)

    polled = poll_journal(config, folder)

    assert [item.entry_id for item in polled] == [entry.entry_id]
    assert not journal_entry_seen(config, entry.folder_id, entry.entry_id)
    assert [item.entry_id for item in poll_journal(config, folder)] == [entry.entry_id]


def test_poll_journal_marks_own_entries_seen(config, tmp_path, monkeypatch):
    folder = FolderMapping(local_path=str(tmp_path / "proj"), remote_subpath="proj")
    entry = make_journal_entry(
        folder, config, [JournalChange(path="a.txt", action="upload")]
    )
    entry.machine_id = "machine-a"
    remote_path = journal_remote_path(entry)
    backend = FakeBackend(
        nodes={
            remote_path: RemoteNode(path=remote_path, name=f"{entry.entry_id}.json")
        },
        blobs={remote_path: json.dumps(entry.to_dict())},
    )
    _patch_backend(monkeypatch, backend)

    assert poll_journal(config, folder) == []
    assert journal_entry_seen(config, entry.folder_id, entry.entry_id)
