"""Tests for durable runtime state."""

from protondrive_sync.core.config import AppConfig, FolderMapping
from protondrive_sync.core.state import (
    folder_blocks_automatic_sync,
    get_folder_state,
    mark_folder_status,
    update_folder_state,
)


def test_state_round_trip(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setattr("protondrive_sync.core.config.get_config_dir", lambda: config_dir)
    config = AppConfig()
    folder = FolderMapping(local_path=str(tmp_path / "proj"), remote_subpath="proj")

    update_folder_state(config, folder, status="syncing", setup_session_id="abc")

    state = get_folder_state(config, folder)
    assert state.status == "syncing"
    assert state.setup_session_id == "abc"


def test_degraded_blocks_automatic_sync(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setattr("protondrive_sync.core.config.get_config_dir", lambda: config_dir)
    config = AppConfig()
    folder = FolderMapping(local_path=str(tmp_path / "proj"), remote_subpath="proj")

    mark_folder_status(config, folder, "degraded", error="verify failed")

    assert folder_blocks_automatic_sync(config, folder)
    assert get_folder_state(config, folder).last_error == "verify failed"


def test_healthy_clears_error(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setattr("protondrive_sync.core.config.get_config_dir", lambda: config_dir)
    config = AppConfig()
    folder = FolderMapping(local_path=str(tmp_path / "proj"), remote_subpath="proj")

    mark_folder_status(config, folder, "degraded", error="bad")
    mark_folder_status(config, folder, "healthy")

    state = get_folder_state(config, folder)
    assert state.status == "healthy"
    assert state.last_error is None
    assert state.last_success is not None


def test_journal_pending_blocks_but_audit_due_does_not(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setattr("protondrive_sync.core.config.get_config_dir", lambda: config_dir)
    config = AppConfig()
    folder = FolderMapping(local_path=str(tmp_path / "proj"), remote_subpath="proj")

    mark_folder_status(config, folder, "journal_pending", error="outbox pending")
    assert folder_blocks_automatic_sync(config, folder)

    mark_folder_status(config, folder, "audit_due")
    assert not folder_blocks_automatic_sync(config, folder)
