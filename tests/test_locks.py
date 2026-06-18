"""Tests for local locks and remote advisory leases."""

import pytest
from datetime import datetime, timezone

from protondrive_sync.core.config import AppConfig, FolderMapping
from protondrive_sync.core.locks import (
    LockError,
    acquire_remote_lease,
    override_remote_lease,
    release_remote_lease,
    remote_lease_can_manual_override,
    remote_lease_dir,
    remote_lease_is_stale,
)
from protondrive_sync.core.proton_cli import RemoteNode
from tests.fake_backend import FakeBackend


def _patch_backend(monkeypatch, backend):
    monkeypatch.setattr(
        "protondrive_sync.core.proton_cli.ProtonDriveCLI", lambda _config: backend
    )


def test_remote_lease_refuses_other_machine(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setattr(
        "protondrive_sync.core.locks.get_config_dir", lambda: config_dir
    )
    monkeypatch.setattr(
        "protondrive_sync.core.locks.machine_id", lambda: "this-machine"
    )
    folder = FolderMapping(local_path=str(tmp_path / "proj"), remote_subpath="proj")
    lease_dir = remote_lease_dir(folder)
    _patch_backend(
        monkeypatch,
        FakeBackend(
            {
                f"{lease_dir}/other-machine.json": RemoteNode(
                    path=f"{lease_dir}/other-machine.json", name="other-machine.json"
                )
            }
        ),
    )

    with pytest.raises(LockError, match="Another remote lease"):
        acquire_remote_lease(folder, AppConfig(), operation="test")


def test_remote_lease_writes_and_releases(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setattr(
        "protondrive_sync.core.locks.get_config_dir", lambda: config_dir
    )
    monkeypatch.setattr(
        "protondrive_sync.core.locks.machine_id", lambda: "this-machine"
    )
    backend = FakeBackend()
    _patch_backend(monkeypatch, backend)

    folder = FolderMapping(local_path=str(tmp_path / "proj"), remote_subpath="proj")
    lease = acquire_remote_lease(
        folder, AppConfig(), operation="test"
    )
    release_remote_lease(lease, AppConfig())

    assert backend.uploaded == [lease.remote_path]
    assert backend.trashed == [lease.remote_path]


def test_remote_lease_age_policy():
    config = AppConfig(
        remote_lease_stale_after_hours=168, remote_lease_manual_override_after_hours=24
    )

    assert remote_lease_can_manual_override(
        "2026-01-01T00:00:00Z",
        config,
        now=datetime(2026, 1, 2, 1, tzinfo=timezone.utc),
    )
    assert remote_lease_is_stale(
        "2026-01-01T00:00:00Z",
        config,
        now=datetime(2026, 1, 8, 1, tzinfo=timezone.utc),
    )


def test_stale_remote_lease_is_deleted_before_acquire(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setattr(
        "protondrive_sync.core.locks.get_config_dir", lambda: config_dir
    )
    monkeypatch.setattr(
        "protondrive_sync.core.locks.machine_id", lambda: "this-machine"
    )
    folder = FolderMapping(local_path=str(tmp_path / "proj"), remote_subpath="proj")
    lease_dir = remote_lease_dir(folder)
    stale = f"{lease_dir}/other-machine.json"
    backend = FakeBackend(
        {
            stale: RemoteNode(
                path=stale, name="other-machine.json", modtime="2020-01-01T00:00:00Z"
            )
        }
    )
    _patch_backend(monkeypatch, backend)

    folder = FolderMapping(local_path=str(tmp_path / "proj"), remote_subpath="proj")
    lease = acquire_remote_lease(
        folder, AppConfig(), operation="test"
    )

    assert backend.trashed == [stale]  # stale lease trashed first
    assert backend.uploaded == [lease.remote_path]  # then own lease written


def test_manual_override_refuses_fresh_lease(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setattr(
        "protondrive_sync.core.locks.get_config_dir", lambda: config_dir
    )
    folder = FolderMapping(local_path=str(tmp_path / "proj"), remote_subpath="proj")
    lease_dir = remote_lease_dir(folder)
    fresh = f"{lease_dir}/other.json"
    _patch_backend(
        monkeypatch,
        FakeBackend(
            {
                fresh: RemoteNode(
                    path=fresh, name="other.json", modtime="2999-01-01T00:00:00Z"
                )
            }
        ),
    )

    with pytest.raises(LockError, match="not old enough"):
        override_remote_lease(folder, "other", AppConfig())
