"""Tests for partitioned audit."""

import pytest

from protondrive_sync.core.audit import build_audit_partitions, run_partitioned_audit
from protondrive_sync.core.config import AppConfig, FolderMapping
from protondrive_sync.core.inventory import (
    InventoryEntry,
    get_audit_state,
    upsert_inventory_entry,
)
from protondrive_sync.core.verify import VerifyReport
from protondrive_sync.core.state import folder_id, get_folder_state


@pytest.fixture
def config(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setattr(
        "protondrive_sync.core.config.get_config_dir", lambda: config_dir
    )
    return AppConfig(remote_audit_partition_max_files=2)


def _add_entry(config, folder, path):
    upsert_inventory_entry(
        config,
        InventoryEntry(
            folder_id=folder_id(folder.local_path, folder.remote_subpath),
            path=path,
            kind="file",
            local_size=1,
            remote_size=1,
        ),
    )


def test_build_audit_partitions_from_inventory(config, tmp_path):
    folder = FolderMapping(local_path=str(tmp_path / "proj"), remote_subpath="proj")
    _add_entry(config, folder, "src/a.py")
    _add_entry(config, folder, "src/b.py")
    _add_entry(config, folder, "data/a.bin")

    partitions = build_audit_partitions(config, folder)

    assert [partition.key for partition in partitions] == ["data", "src"]


def test_run_partitioned_audit_records_completion(config, tmp_path):
    folder = FolderMapping(local_path=str(tmp_path / "proj"), remote_subpath="proj")
    _add_entry(config, folder, "src/a.py")
    calls: list[tuple[str, str]] = []

    def verify(local, remote, _config, **_kwargs):
        calls.append((local, remote))
        return VerifyReport(ok=True)

    result = run_partitioned_audit(config, folder, verify_func=verify)

    assert result.completed
    assert calls[0][1] == "proj/src"
    state = get_audit_state(config, folder)
    assert state is not None
    assert state["incomplete"] == 0
    assert state["cursor"] is None


def test_run_partitioned_audit_marks_degraded_on_failure(config, tmp_path):
    folder = FolderMapping(local_path=str(tmp_path / "proj"), remote_subpath="proj")
    _add_entry(config, folder, "src/a.py")

    def verify(*_args, **_kwargs):
        return VerifyReport(ok=False, message="bad")

    result = run_partitioned_audit(config, folder, verify_func=verify)

    assert not result.completed
    assert result.failed == ["src"]
    assert get_folder_state(config, folder).status == "degraded"
