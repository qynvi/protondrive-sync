"""Tests for daemon-start preflight."""

import pytest

from protondrive_sync.core.config import AppConfig, FolderMapping
from protondrive_sync.core.inventory import InventoryEntry, OutboxEntry, enqueue_journal_outbox, upsert_inventory_entry
from protondrive_sync.core.preflight import evaluate_daemon_preflight
from protondrive_sync.core.state import folder_id, mark_folder_status


@pytest.fixture
def config(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setattr("protondrive_sync.core.config.get_config_dir", lambda: config_dir)
    return AppConfig()


def _folder(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    return FolderMapping(local_path=str(root), remote_subpath="proj")


def _seed_clean(config, folder):
    from pathlib import Path

    Path(folder.local_path, "a.txt").write_text("x", encoding="utf-8")
    upsert_inventory_entry(
        config,
        InventoryEntry(
            folder_id=folder_id(folder.local_path, folder.remote_subpath),
            path="a.txt",
            kind="file",
            local_size=1,
            remote_size=1,
            last_source="test",
        ),
    )


def test_preflight_ok_for_clean_inventory(config, tmp_path):
    folder = _folder(tmp_path)
    config.folders = [folder]
    _seed_clean(config, folder)

    report = evaluate_daemon_preflight(config)

    assert report.ok
    assert report.outbox_pending == 0
    assert report.blockers == []


def test_preflight_blocks_outbox(config, tmp_path):
    folder = _folder(tmp_path)
    config.folders = [folder]
    _seed_clean(config, folder)
    enqueue_journal_outbox(
        config,
        OutboxEntry(id="entry", folder_id="folder", remote_path="journal/entry.json", entry_json="{}"),
    )

    report = evaluate_daemon_preflight(config)

    assert not report.ok
    assert "journal outbox pending" in report.blockers[0]


def test_preflight_blocks_local_drift(config, tmp_path):
    folder = _folder(tmp_path)
    config.folders = [folder]
    _seed_clean(config, folder)
    (tmp_path / "proj" / "new.txt").write_text("new", encoding="utf-8")

    report = evaluate_daemon_preflight(config)

    assert not report.ok
    assert any("local drift" in blocker for blocker in report.blockers)


def test_preflight_flags_large_folder_limitation(config, tmp_path):
    folder = _folder(tmp_path)
    config.remote_audit_large_folder_file_count = 1
    config.folders = [folder]
    _seed_clean(config, folder)

    report = evaluate_daemon_preflight(config)

    assert report.ok
    assert report.folders[0].is_large
    assert report.limitations


def test_preflight_blocks_degraded_status(config, tmp_path):
    folder = _folder(tmp_path)
    config.folders = [folder]
    _seed_clean(config, folder)
    mark_folder_status(config, folder, "degraded", error="bad")

    report = evaluate_daemon_preflight(config)

    assert not report.ok
    assert any("blocking state" in blocker for blocker in report.blockers)
