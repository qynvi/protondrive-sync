"""Tests for config module."""

import json
import pytest
from unittest.mock import patch

from protondrive_sync.core.config import (
    AppConfig,
    FolderMapping,
    DEFAULT_FILTERS,
    INTEGRITY_MODES,
    normalize_symlink_mode,
    add_folder,
    remove_folder,
    save_config,
    load_config,
    write_filter_file,
    effective_filters,
    merge_default_filters,
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
        assert m.symlink_mode == "preserve"
        assert m.bisync_initialized is False
        assert m.enabled is True

    def test_invalid_symlink_mode_defaults_to_preserve(self):
        m = FolderMapping(local_path="/tmp/test", remote_subpath="test", symlink_mode="bad")
        assert m.symlink_mode == "preserve"

    def test_path_normalization(self):
        m = FolderMapping(local_path="/tmp/test/", remote_subpath="/foo/bar/")
        assert not m.remote_subpath.startswith("/")
        assert not m.remote_subpath.endswith("/")

    def test_defaults(self):
        config = AppConfig()
        assert config.proton_cli_path is None
        assert config.proton_cli_concurrency == 4
        assert config.symlink_mode == "preserve"
        assert config.bisync_check_interval == 15
        assert config.bisync_quiet_threshold == 120
        assert config.bisync_max_burst == 1800
        assert config.size_change_threshold == 0.5
        assert config.size_change_min_bytes == 10240
        assert config.scan_overlap_seconds == 5
        assert config.stable_check_delay_seconds == 10
        assert config.remote_poll_interval_seconds == 900
        assert config.bisync_max_delete_percent == 10
        assert config.download_space_headroom_pct == 10
        assert config.targeted_sync_enabled is True
        assert config.batch_sync_enabled is True
        assert config.integrity_mode == "changed_hash"
        assert "changed_hash" in INTEGRITY_MODES
        assert config.journal_poll_interval_seconds == 120
        assert config.journal_retention_days == 90
        assert config.batch_min_paths_per_cycle == 2
        assert config.batch_max_paths_per_cycle == 5000
        assert config.targeted_max_paths_per_cycle == 1000
        assert config.targeted_max_bytes_per_cycle == 10 * 1024 ** 3
        assert config.remote_audit_interval_hours_small == 24
        assert config.remote_audit_interval_hours_large == 168
        assert config.remote_audit_time_budget_minutes == 120
        assert config.remote_audit_partition_max_files == 5000
        assert config.remote_lease_heartbeat_seconds == 600
        assert config.remote_lease_stale_after_hours == 168
        assert config.remote_lease_manual_override_after_hours == 24
        assert config.backup_retention_days == 90
        assert len(config.filters) > 0
        assert "- .gitnexus/**" in config.filters
        assert "- **/.gitnexus/**" in config.filters
        assert "- **/gitnexus/**" in config.filters
        assert "- **/*gitnexus*" in config.filters
        assert "- .turbo/**" in config.filters
        assert "- **/.turbo/**" in config.filters
        assert "- .opencode/**" in config.filters
        assert "- **/.opencode/**" in config.filters
        assert "- **/.terraform/**" in config.filters
        assert "- **/terraform.tfstate" in config.filters
        assert "- **/tfplan" in config.filters
        assert "- **/infra/terraform/lambda/*.zip" in config.filters
        assert "- **/dist-old/**" in config.filters
        assert "- **/dist-custom/**" in config.filters
        assert "- opencode/opencode" in config.filters
        assert "- opencode/opencode-*" in config.filters
        assert "- **/opencode/opencode" in config.filters
        assert "- **/opencode/opencode-*" in config.filters
        assert "- *.log" in config.filters
        assert "- *.exe" in config.filters
        assert config.folders == []

    def test_invalid_integrity_mode_defaults_to_changed_hash(self):
        config = AppConfig(integrity_mode="bad")
        assert config.integrity_mode == "changed_hash"

    def test_rehydrate_dicts(self):
        """Folders passed as dicts (from JSON) should become FolderMapping."""
        config = AppConfig(
            folders=[{"local_path": "/tmp/a", "remote_subpath": "a"}]
        )
        assert isinstance(config.folders[0], FolderMapping)
        assert config.folders[0].remote_subpath == "a"
        assert config.folders[0].symlink_mode == "preserve"

    def test_rehydrate_dicts_use_global_symlink_default(self):
        config = AppConfig(
            symlink_mode="skip",
            folders=[{"local_path": "/tmp/a", "remote_subpath": "a"}],
        )
        assert config.folders[0].symlink_mode == "skip"

    def test_has_enabled_folders(self):
        config = AppConfig(folders=[
            FolderMapping(local_path="/tmp/a", remote_subpath="a"),
        ])
        assert config.has_enabled_folders()
        config.folders[0].enabled = False
        assert not config.has_enabled_folders()


class TestSaveLoad:
    def test_round_trip(self, tmp_config_dir):
        config = AppConfig(proton_cli_concurrency=8, log_level="DEBUG")
        mapping = FolderMapping(local_path="/tmp/proj", remote_subpath="proj")
        config.folders.append(mapping)

        path = save_config(config)
        assert path.exists()

        loaded = load_config()
        assert loaded.proton_cli_concurrency == 8
        assert loaded.log_level == "DEBUG"
        assert len(loaded.folders) == 1
        assert loaded.folders[0].remote_subpath == "proj"
        assert loaded.folders[0].symlink_mode == "preserve"
        assert not hasattr(loaded, "copy_links")

    def test_load_missing_returns_defaults(self, tmp_config_dir):
        config = load_config()
        assert config.proton_cli_concurrency == 4

    def test_load_corrupt_raises(self, tmp_config_dir):
        config_path = tmp_config_dir / "config.json"
        config_path.write_text("not valid json{{{", encoding="utf-8")
        with pytest.raises(ConfigError):
            load_config()

    def test_load_merges_missing_default_filters(self, tmp_config_dir):
        config_path = tmp_config_dir / "config.json"
        config_path.write_text(
            json.dumps({"filters": ["- .git/**", "- node_modules/**"]}),
            encoding="utf-8",
        )
        config = load_config()
        assert "- **/.venv/**" in config.filters
        assert "- **/node_modules/**" in config.filters
        assert "- **/.local/**" in config.filters
        assert "- **/.cache/**" in config.filters
        assert "- **/.hf_cache/**" in config.filters
        assert "- **/hf_cache/**" in config.filters
        assert "- **/.huggingface/**" in config.filters
        assert "- **/.ipynb_checkpoints/**" in config.filters
        assert "- **/.gradio/**" in config.filters
        assert "- **/build/**" in config.filters
        assert "- **/dist/**" in config.filters
        assert "- **/out/**" in config.filters
        assert "- **/cmake-build-*/**" in config.filters
        assert "- **/.gitnexus/**" in config.filters
        assert "- **/gitnexus/**" in config.filters
        assert "- **/.opencode/**" in config.filters
        assert "- **/.turbo/**" in config.filters
        assert "- **/dist-old/**" in config.filters
        assert "- opencode/opencode-*" in config.filters
        assert "- **/opencode/opencode-*" in config.filters
        assert "- **/.terraform/**" in config.filters
        assert "- *.log" in config.filters
        assert config.filters[0] == "- .git/**"

    def test_load_drops_removed_legacy_keys(self, tmp_config_dir):
        config_path = tmp_config_dir / "config.json"
        config_path.write_text(
            json.dumps({
                "remote_name": "old",
                "mount_point": "/mnt/old",
                "transfers": 8,
                "copy_links": True,
                "folders": [
                    {
                        "local_path": "/tmp/a",
                        "remote_subpath": "a",
                        "sync_mode": "bisync",
                        "pin_mode": "keep_offline",
                    },
                    {
                        "local_path": "/tmp/mount",
                        "remote_subpath": "mount",
                        "sync_mode": "mount",
                    },
                ],
            }),
            encoding="utf-8",
        )
        config = load_config()
        assert len(config.folders) == 1
        assert config.folders[0].remote_subpath == "a"
        assert config.folders[0].symlink_mode == "preserve"
        assert not hasattr(config, "remote_name")
        assert not hasattr(config, "mount_point")

    def test_symlink_mode_helpers(self):
        assert normalize_symlink_mode("preserve") == "preserve"
        assert normalize_symlink_mode("copy") == "copy"
        assert normalize_symlink_mode("skip") == "skip"
        assert normalize_symlink_mode("bad") == "preserve"


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

    def test_effective_filters_include_folder_filters(self):
        config = AppConfig(filters=["- global/**"])
        folder = FolderMapping(
            local_path="/tmp/a",
            remote_subpath="a",
            filters=["- folder/**", "- global/**"],
        )

        assert effective_filters(config, folder) == ["- global/**", "- folder/**"]

    def test_write_filter_file_includes_folder_filters(self, tmp_config_dir):
        config = AppConfig(filters=["- global/**"])
        folder = FolderMapping(local_path="/tmp/a", remote_subpath="a", filters=["- folder/**"])

        content = write_filter_file(config, folder).read_text()

        assert "- global/**" in content
        assert "- folder/**" in content

    def test_write_filter_file_expands_rclonelink_excludes(self, tmp_config_dir):
        config = AppConfig(filters=["- *.so", "- **/.git/**"])

        lines = write_filter_file(config).read_text().splitlines()

        assert "- *.so" in lines
        assert "- *.so.rclonelink" in lines
        assert "- **/.git/**" in lines
        assert "- **/.git" in lines
        assert "- **/.git.rclonelink" in lines

    def test_backup_dir_excluded_first(self, tmp_config_dir):
        """Backup directory exclusion is auto-injected as the first rule."""
        config = AppConfig(filters=["- .git/**"])
        path = write_filter_file(config)
        lines = path.read_text().strip().splitlines()
        assert lines[0] == "- .protondrive-sync-backups/**"
        assert "- .git/**" in lines

    def test_merge_default_filters_preserves_custom_rules(self):
        merged = merge_default_filters(["- custom/**", "- .git/**"])
        assert merged[0] == "- custom/**"
        assert merged[1] == "- .git/**"
        assert "- **/.venv/**" in merged
        assert len(merged) == len(set(merged))
        assert set(DEFAULT_FILTERS).issubset(set(merged))
