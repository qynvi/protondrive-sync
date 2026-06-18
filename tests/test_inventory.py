"""Tests for P3 SQLite inventory."""

import sqlite3

import pytest

from protondrive_sync.core.config import AppConfig, FolderMapping
from protondrive_sync.core.inventory import (
    OutboxEntry,
    connect_inventory,
    enqueue_journal_outbox,
    folder_inventory_count,
    init_inventory,
    list_journal_outbox,
    outbox_has_pending,
    scan_local_inventory,
)


@pytest.fixture
def config(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setattr("protondrive_sync.core.config.get_config_dir", lambda: config_dir)
    return AppConfig()


def test_init_inventory_creates_tables(config):
    path = init_inventory(config)
    assert path.exists()

    with connect_inventory(config) as conn:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }

    assert "folder_inventory" in tables
    assert "journal_seen" in tables
    assert "journal_outbox" in tables
    assert "audit_state" in tables
    assert "sync_events" in tables


def test_scan_local_inventory_respects_filters_and_symlink_mode(config, tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "keep.txt").write_text("keep", encoding="utf-8")
    cache = root / "__pycache__"
    cache.mkdir()
    (cache / "skip.pyc").write_text("skip", encoding="utf-8")
    target = tmp_path / "target.txt"
    target.write_text("target", encoding="utf-8")
    (root / "link.txt").symlink_to(target)
    folder = FolderMapping(local_path=str(root), remote_subpath="proj", symlink_mode="preserve")

    entries = scan_local_inventory(folder, config, hash_paths={"keep.txt"})

    assert "keep.txt" in entries
    assert entries["keep.txt"].local_sha1 is not None
    assert "__pycache__/skip.pyc" not in entries
    assert entries["link.txt"].kind == "symlink"
    assert entries["link.txt"].link_target == str(target)


def test_scan_local_inventory_ignores_app_metadata(config, tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / ".protondrive-sync-check").write_text("check", encoding="utf-8")
    (root / ".protondrive-sync.json").write_text("{}", encoding="utf-8")
    nested = root / "nested"
    nested.mkdir()
    (nested / ".protondrive-sync-check").write_text("user", encoding="utf-8")
    folder = FolderMapping(local_path=str(root), remote_subpath="proj")

    entries = scan_local_inventory(folder, config, hash_all=True)

    assert ".protondrive-sync-check" not in entries
    assert ".protondrive-sync.json" not in entries
    assert "nested/.protondrive-sync-check" not in entries



def test_journal_outbox_persists(config):
    outbox = OutboxEntry(
        id="entry-1",
        folder_id="folder-1",
        remote_path=".protondrive-sync-journal/folder/day/entry-1.json",
        entry_json='{"entry_id":"entry-1"}',
    )

    enqueue_journal_outbox(config, outbox)

    assert outbox_has_pending(config)
    assert list_journal_outbox(config, "folder-1")[0].id == "entry-1"
