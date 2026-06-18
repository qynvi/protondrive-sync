"""Tests for scoped backup and journal retention cleanup."""

import os
from datetime import datetime, timezone

import pytest

from protondrive_sync.core.config import AppConfig, FolderMapping
from protondrive_sync.core.retention import (
    RetentionError,
    cleanup_local_backup_root,
    cleanup_remote_backup_root,
    validate_local_backup_scope,
)
from protondrive_sync.core.proton_cli import RemoteNode
from tests.fake_backend import FakeBackend


def test_local_backup_cleanup_deletes_only_aged_files(tmp_path):
    root = tmp_path / "proj" / ".protondrive-sync-backups" / "targeted"
    root.mkdir(parents=True)
    old = root / "old.txt"
    new = root / "new.txt"
    old.write_text("old", encoding="utf-8")
    new.write_text("new", encoding="utf-8")
    os.utime(old, (1, 1))
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    os.utime(new, (now.timestamp(), now.timestamp()))

    result = cleanup_local_backup_root(root, retention_days=90, now=now)

    assert old in result.deleted_local
    assert not old.exists()
    assert new.exists()


def test_local_backup_cleanup_refuses_unscoped_path(tmp_path):
    with pytest.raises(RetentionError):
        validate_local_backup_scope(tmp_path / "not-backups")


def test_remote_backup_cleanup_trashes_aged_files(monkeypatch):
    aged = ".protondrive-sync-backups/targeted/run/old.txt"
    backend = FakeBackend(
        {aged: RemoteNode(path=aged, name="old.txt", modtime="2020-01-01T00:00:00Z")}
    )
    monkeypatch.setattr(
        "protondrive_sync.core.retention.ProtonDriveCLI", lambda _config: backend
    )

    result = cleanup_remote_backup_root(
        AppConfig(),
        ".protondrive-sync-backups",
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    assert result.deleted_remote == [aged]
    assert backend.trashed == [aged]


def test_remote_backup_cleanup_refuses_unscoped_root():
    with pytest.raises(RetentionError):
        cleanup_remote_backup_root(AppConfig(), "workspace/project")
