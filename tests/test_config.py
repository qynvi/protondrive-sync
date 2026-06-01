"""Tests for config module."""

import json
import pytest
from pathlib import Path
from unittest.mock import patch

from protondrive_sync.core.config import (
    AppConfig,
    FolderMapping,
    add_folder,
    remove_folder,
    save_config,
    load_config,
    write_filter_file,
    ConfigError,
)


@pytest.fixture
def tmp_config_dir(tmp_path):
    """Redirect config dir to a temp directory."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    with patch("protondrive_sync.core.config.get_config_dir", return_value=config_dir):
        yield config_dir


class TestFolderMapping:
    def test_defaults(self):
        m = FolderMapping(local_path="/tmp/test", remote_subpath="test")
        assert m.sync_mode == "bisync"
        assert m.pin_mode == "on_demand"
        assert m.pin_subdirs == []
        assert m.bisync_initialized is False
        assert m.enabled is True

    def test_path_normalization(self):
        m = FolderMapping(local_path="/tmp/test/", remote_subpath="/foo/bar/")
        assert not m.remote_subpath.startswith("/")
        assert not m.remote_subpath.endswith("/")

    def test_invalid_pin_mode(self):
        with pytest.raises(ValueError, match="Invalid pin_mode"):
            FolderMapping(local_path="/tmp", remote_subpath="x", pin_mode="invalid")

    def test_invalid_sync_mode(self):
        with pytest.raises(ValueError, match="Invalid sync_mode"):
            FolderMapping(local_path="/tmp", remote_subpath="x", sync_mode="invalid")

    def test_mount_mode(self):
        m = FolderMapping(local_path="/tmp/test", remote_subpath="test", sync_mode="mount")
        assert m.sync_mode == "mount"


class TestAppConfig:
    def test_defaults(self):
        config = AppConfig()
        assert config.remote_name == "protondrive"
        assert config.cache_max_size == "20G"
        assert config.transfers == 8
        assert config.checkers == 16
        assert config.copy_links is True
        assert config.bisync_check_interval == 15
        assert config.bisync_quiet_threshold == 120
        assert config.bisync_max_burst == 1800
        assert config.size_change_threshold == 0.5
        assert config.size_change_min_bytes == 10240
        assert len(config.filters) > 0
        assert config.folders == []

    def test_rehydrate_dicts(self):
        """Folders passed as dicts (from JSON) should become FolderMapping."""
        config = AppConfig(
            folders=[{"local_path": "/tmp/a", "remote_subpath": "a"}]
        )
        assert isinstance(config.folders[0], FolderMapping)
        assert config.folders[0].remote_subpath == "a"
        assert config.folders[0].sync_mode == "bisync"

    def test_rehydrate_legacy_dict_no_sync_mode(self, tmp_path):
        """Legacy config dicts without sync_mode default to bisync."""
        config = AppConfig(
            folders=[{"local_path": str(tmp_path), "remote_subpath": "a"}]
        )
        assert config.folders[0].sync_mode == "bisync"

    def test_rehydrate_legacy_dict_symlink_detects_mount(self, tmp_path):
        """Legacy config with a symlink local path auto-detects mount mode."""
        target = tmp_path / "target"
        target.mkdir()
        link = tmp_path / "mylink"
        link.symlink_to(target)
        config = AppConfig(
            folders=[{"local_path": str(link), "remote_subpath": "a"}]
        )
        assert config.folders[0].sync_mode == "mount"

    def test_has_mount_folders(self):
        config = AppConfig(folders=[
            FolderMapping(local_path="/tmp/a", remote_subpath="a", sync_mode="mount"),
        ])
        assert config.has_mount_folders()
        assert not config.has_bisync_folders()

    def test_has_bisync_folders(self):
        config = AppConfig(folders=[
            FolderMapping(local_path="/tmp/a", remote_subpath="a", sync_mode="bisync"),
        ])
        assert config.has_bisync_folders()
        assert not config.has_mount_folders()

    def test_mount_path(self):
        config = AppConfig(mount_point="/mnt/proton")
        assert config.mount_path == Path("/mnt/proton")


class TestSaveLoad:
    def test_round_trip(self, tmp_config_dir):
        config = AppConfig(remote_name="myremote", transfers=8)
        mapping = FolderMapping(local_path="/tmp/proj", remote_subpath="proj")
        config.folders.append(mapping)

        path = save_config(config)
        assert path.exists()

        loaded = load_config()
        assert loaded.remote_name == "myremote"
        assert loaded.transfers == 8
        assert len(loaded.folders) == 1
        assert loaded.folders[0].remote_subpath == "proj"

    def test_load_missing_returns_defaults(self, tmp_config_dir):
        config = load_config()
        assert config.remote_name == "protondrive"

    def test_load_corrupt_raises(self, tmp_config_dir):
        config_path = tmp_config_dir / "config.json"
        config_path.write_text("not valid json{{{", encoding="utf-8")
        with pytest.raises(ConfigError):
            load_config()


class TestAddRemoveFolder:
    def test_add_folder(self):
        config = AppConfig()
        m = FolderMapping(local_path="/tmp/a", remote_subpath="a")
        add_folder(config, m)
        assert len(config.folders) == 1

    def test_add_duplicate_local_path_raises(self):
        config = AppConfig()
        m1 = FolderMapping(local_path="/tmp/a", remote_subpath="a")
        add_folder(config, m1)
        m2 = FolderMapping(local_path="/tmp/a", remote_subpath="b")
        with pytest.raises(ConfigError, match="already mapped"):
            add_folder(config, m2)

    def test_add_duplicate_remote_raises(self):
        config = AppConfig()
        m1 = FolderMapping(local_path="/tmp/a", remote_subpath="shared")
        add_folder(config, m1)
        m2 = FolderMapping(local_path="/tmp/b", remote_subpath="shared")
        with pytest.raises(ConfigError, match="already in use"):
            add_folder(config, m2)

    def test_remove_folder(self):
        config = AppConfig()
        m = FolderMapping(local_path="/tmp/a", remote_subpath="a")
        config.folders.append(m)
        removed = remove_folder(config, "/tmp/a")
        assert removed is not None
        assert len(config.folders) == 0

    def test_remove_nonexistent(self):
        config = AppConfig()
        assert remove_folder(config, "/tmp/nonexistent") is None


class TestFilterFile:
    def test_write_filter_file(self, tmp_config_dir):
        config = AppConfig(filters=["- .git/**", "- node_modules/**"])
        # Patch filter_file property to use tmp dir
        path = write_filter_file(config)
        assert path.exists()
        content = path.read_text()
        assert "- .git/**" in content
        assert "- node_modules/**" in content

    def test_backup_dir_excluded_first(self, tmp_config_dir):
        """Backup directory exclusion is auto-injected as the first rule."""
        config = AppConfig(filters=["- .git/**"])
        path = write_filter_file(config)
        lines = path.read_text().strip().splitlines()
        assert lines[0] == "- .protondrive-sync-backups/**"
        assert lines[1] == "- .git/**"
