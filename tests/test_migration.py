"""Tests for migration module."""

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from protondrive_sync.core.config import AppConfig, FolderMapping
from protondrive_sync.core.migration import (
    plan_migration,
    execute_migration,
    rollback_migration,
    cleanup_backup,
    teardown_mount,
    plan_bisync_setup,
    compare_local_remote,
    _matches_filter,
    _find_filtered_toplevel,
    _find_env_files,
    MigrationError,
    MigrationPlan,
    BisyncPlan,
    DivergenceReport,
)
from protondrive_sync.core.rclone import RemoteFileInfo


@pytest.fixture
def config():
    return AppConfig(mount_point="/tmp/test-mount")


@pytest.fixture
def source_dir(tmp_path):
    """Create a source directory with some files."""
    src = tmp_path / "my-project"
    src.mkdir()
    (src / "file1.txt").write_text("hello")
    (src / "file2.txt").write_text("world")
    sub = src / "subdir"
    sub.mkdir()
    (sub / "nested.txt").write_text("nested content")
    return src


@pytest.fixture
def source_dir_with_git(tmp_path):
    """Create a source directory that looks like a git repo with filtered items."""
    src = tmp_path / "my-repo"
    src.mkdir()
    (src / "main.py").write_text("print('hello')")
    # .git directory
    git_dir = src / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/main")
    objects = git_dir / "objects"
    objects.mkdir()
    (objects / "abc123").write_text("blob data")
    # __pycache__
    cache = src / "__pycache__"
    cache.mkdir()
    (cache / "main.cpython-312.pyc").write_bytes(b"\x00\x01\x02")
    # .venv
    venv = src / ".venv"
    venv.mkdir()
    (venv / "pyvenv.cfg").write_text("home = /usr/bin")
    # .env file (should trigger warning)
    (src / ".env").write_text("SECRET_KEY=hunter2")
    (src / ".env.local").write_text("DB_PASS=letmein")
    return src


class TestPlanMigration:
    def test_creates_plan(self, source_dir, config):
        plan = plan_migration(str(source_dir), "projects/test", config)
        assert plan.file_count == 3
        assert plan.total_size_bytes > 0
        assert plan.remote_subpath == "projects/test"
        assert plan.local_path == source_dir
        assert "premigration-backup" in str(plan.backup_path)

    def test_nonexistent_source_raises(self, config):
        with pytest.raises(MigrationError, match="not a directory"):
            plan_migration("/tmp/definitely-nonexistent-12345", "test", config)

    def test_file_source_raises(self, tmp_path, config):
        f = tmp_path / "notadir.txt"
        f.write_text("x")
        with pytest.raises(MigrationError, match="not a directory"):
            plan_migration(str(f), "test", config)

    def test_size_human(self, source_dir, config):
        plan = plan_migration(str(source_dir), "test", config)
        # Should be some bytes
        assert "B" in plan.total_size_human


class TestExecuteMigration:
    @patch("protondrive_sync.core.migration.sync_upload")
    @patch("protondrive_sync.core.migration.verify_sync", return_value=True)
    @patch("protondrive_sync.core.migration.create_link")
    def test_successful_migration(self, mock_link, mock_verify, mock_upload, source_dir, config):
        plan = plan_migration(str(source_dir), "projects/test", config)
        logs = []
        result = execute_migration(plan, config, progress=logs.append)

        assert result.success
        assert "complete" in result.message.lower()
        assert result.backup_path is not None
        assert result.backup_path.exists()
        # Original dir should have been moved to backup
        assert not source_dir.exists()
        mock_upload.assert_called_once()
        mock_verify.assert_called_once()
        mock_link.assert_called_once()

    @patch("protondrive_sync.core.migration.sync_upload", side_effect=Exception("network error"))
    def test_upload_failure(self, mock_upload, source_dir, config):
        plan = plan_migration(str(source_dir), "test", config)
        result = execute_migration(plan, config)

        assert not result.success
        # Source should still exist (not moved)
        assert source_dir.exists()

    @patch("protondrive_sync.core.migration.sync_upload")
    @patch("protondrive_sync.core.migration.verify_sync", return_value=False)
    def test_verify_failure(self, mock_verify, mock_upload, source_dir, config):
        plan = plan_migration(str(source_dir), "test", config)
        result = execute_migration(plan, config)

        assert not result.success
        assert "verification" in result.message.lower()

    @patch("protondrive_sync.core.migration.sync_upload")
    @patch("protondrive_sync.core.migration.verify_sync", return_value=True)
    @patch("protondrive_sync.core.migration.create_link", side_effect=Exception("link failed"))
    def test_symlink_failure_rolls_back(self, mock_link, mock_verify, mock_upload, source_dir, config):
        plan = plan_migration(str(source_dir), "test", config)
        result = execute_migration(plan, config)

        assert not result.success
        # Rollback should have restored the original directory
        assert source_dir.exists()


class TestRollbackMigration:
    def test_rollback(self, tmp_path):
        # Simulate post-migration state: symlink + backup
        original = tmp_path / "project"
        backup = tmp_path / "project.premigration-backup"
        backup.mkdir()
        (backup / "file.txt").write_text("content")
        original.symlink_to("/tmp/nonexistent-mount-point")

        assert rollback_migration(str(original), str(backup))
        assert original.is_dir()
        assert not original.is_symlink()
        assert (original / "file.txt").read_text() == "content"

    def test_rollback_no_backup(self, tmp_path):
        assert not rollback_migration(str(tmp_path / "nonexistent"))


class TestCleanupBackup:
    def test_cleanup(self, tmp_path):
        backup = tmp_path / "project.premigration-backup"
        backup.mkdir()
        (backup / "file.txt").write_text("x")
        assert cleanup_backup(str(backup))
        assert not backup.exists()

    def test_cleanup_nonexistent(self):
        assert not cleanup_backup("/tmp/no-such-backup-dir-12345")


class TestFilterMatching:
    def test_git_dir_matches(self):
        filters = ["- .git/**"]
        assert _matches_filter(".git", filters)
        assert _matches_filter(".git/objects/abc", filters)
        assert not _matches_filter("src/main.py", filters)

    def test_pycache_matches(self):
        filters = ["- __pycache__/**"]
        assert _matches_filter("__pycache__", filters)
        assert _matches_filter("__pycache__/foo.pyc", filters)

    def test_pyc_file_matches(self):
        filters = ["- *.pyc"]
        assert _matches_filter("foo.pyc", filters)
        assert _matches_filter("subdir/bar.pyc", filters)
        assert not _matches_filter("foo.py", filters)

    def test_exact_name_matches(self):
        filters = ["- .DS_Store"]
        assert _matches_filter(".DS_Store", filters)
        assert not _matches_filter("other.txt", filters)

    def test_venv_matches(self):
        filters = ["- .venv/**", "- venv/**"]
        assert _matches_filter(".venv", filters)
        assert _matches_filter("venv", filters)
        assert not _matches_filter("myenv", filters)

    def test_multiple_filters(self):
        filters = ["- .git/**", "- __pycache__/**", "- *.pyc", "- node_modules/**"]
        assert _matches_filter(".git", filters)
        assert _matches_filter("__pycache__", filters)
        assert _matches_filter("foo.pyc", filters)
        assert _matches_filter("node_modules", filters)
        assert not _matches_filter("src/app.py", filters)

    def test_include_rules_ignored(self):
        filters = ["+ important/**", "- .git/**"]
        # Include rules should not cause matches
        assert not _matches_filter("important", filters)
        assert _matches_filter(".git", filters)


class TestFindFilteredToplevel:
    def test_finds_git_and_pycache(self, source_dir_with_git, config):
        items = _find_filtered_toplevel(source_dir_with_git, config.filters)
        assert ".git" in items
        assert "__pycache__" in items
        assert ".venv" in items

    def test_does_not_include_regular_files(self, source_dir_with_git, config):
        items = _find_filtered_toplevel(source_dir_with_git, config.filters)
        assert "main.py" not in items
        assert ".env" not in items  # .env is NOT in default filters

    def test_empty_dir(self, tmp_path, config):
        empty = tmp_path / "empty"
        empty.mkdir()
        assert _find_filtered_toplevel(empty, config.filters) == []


class TestFindEnvFiles:
    def test_finds_env_files(self, source_dir_with_git):
        envs = _find_env_files(source_dir_with_git)
        assert ".env" in envs
        assert ".env.local" in envs

    def test_no_env_files(self, source_dir):
        envs = _find_env_files(source_dir)
        assert envs == []


class TestPlanMigrationFiltered:
    def test_plan_includes_filtered_items(self, source_dir_with_git, config):
        plan = plan_migration(str(source_dir_with_git), "test", config)
        assert ".git" in plan.filtered_items
        assert "__pycache__" in plan.filtered_items
        assert ".venv" in plan.filtered_items

    def test_plan_includes_env_warnings(self, source_dir_with_git, config):
        plan = plan_migration(str(source_dir_with_git), "test", config)
        assert ".env" in plan.env_warnings
        assert ".env.local" in plan.env_warnings

    def test_plan_no_warnings_for_clean_dir(self, source_dir, config):
        plan = plan_migration(str(source_dir), "test", config)
        assert plan.filtered_items == []
        assert plan.env_warnings == []


class TestPreserveFilteredItems:
    @patch("protondrive_sync.core.migration.sync_upload")
    @patch("protondrive_sync.core.migration.verify_sync", return_value=True)
    @patch("protondrive_sync.core.migration.create_link")
    def test_git_preserved_after_migration(
        self, mock_link, mock_verify, mock_upload, source_dir_with_git, config, tmp_path
    ):
        # Set mount point to a real temp dir so shutil.move works
        mount_dir = tmp_path / "mount" / "test"
        mount_dir.mkdir(parents=True)
        config.mount_point = str(tmp_path / "mount")

        plan = plan_migration(str(source_dir_with_git), "test", config)
        logs = []
        result = execute_migration(plan, config, progress=logs.append)

        assert result.success
        assert ".git" in result.preserved_items
        assert "__pycache__" in result.preserved_items
        assert ".venv" in result.preserved_items

        # Verify the items were actually moved to the mount target
        assert (mount_dir / ".git" / "HEAD").exists()
        assert (mount_dir / ".git" / "objects" / "abc123").exists()
        assert (mount_dir / "__pycache__" / "main.cpython-312.pyc").exists()
        assert (mount_dir / ".venv" / "pyvenv.cfg").exists()

        # Verify they're gone from backup
        assert not (plan.backup_path / ".git").exists()
        assert not (plan.backup_path / "__pycache__").exists()

    @patch("protondrive_sync.core.migration.sync_upload")
    @patch("protondrive_sync.core.migration.verify_sync", return_value=True)
    @patch("protondrive_sync.core.migration.create_link")
    def test_no_preservation_for_clean_dir(
        self, mock_link, mock_verify, mock_upload, source_dir, config, tmp_path
    ):
        mount_dir = tmp_path / "mount" / "test"
        mount_dir.mkdir(parents=True)
        config.mount_point = str(tmp_path / "mount")

        plan = plan_migration(str(source_dir), "test", config)
        result = execute_migration(plan, config)

        assert result.success
        assert result.preserved_items == []


class TestTeardownMount:
    def test_teardown_with_backup(self, tmp_path):
        """Teardown restores backup and removes symlink."""
        # Simulate post-migration state
        mount_dir = tmp_path / "mount" / "proj"
        mount_dir.mkdir(parents=True)
        (mount_dir / "file.py").write_text("content")

        backup = tmp_path / "proj.premigration-backup"
        backup.mkdir()
        (backup / "file.py").write_text("original content")

        local = tmp_path / "proj"
        local.symlink_to(mount_dir)

        result = teardown_mount(
            local_path=str(local),
            mount_point=str(tmp_path / "mount"),
            remote_subpath="proj",
            filters=[],
        )

        assert result.success
        assert result.method == "backup_restored"
        assert local.is_dir()
        assert not local.is_symlink()
        assert (local / "file.py").read_text() == "original content"
        assert not backup.exists()

    def test_teardown_recovers_git_from_mount(self, tmp_path):
        """Teardown moves .git/ from mount back to restored dir."""
        mount_dir = tmp_path / "mount" / "proj"
        mount_dir.mkdir(parents=True)
        (mount_dir / "file.py").write_text("code")
        git_dir = mount_dir / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/main")

        backup = tmp_path / "proj.premigration-backup"
        backup.mkdir()
        (backup / "file.py").write_text("code")

        local = tmp_path / "proj"
        local.symlink_to(mount_dir)

        filters = ["- .git/**"]
        result = teardown_mount(
            local_path=str(local),
            mount_point=str(tmp_path / "mount"),
            remote_subpath="proj",
            filters=filters,
        )

        assert result.success
        assert result.method == "backup_restored"
        # .git should be in restored dir
        assert (local / ".git" / "HEAD").exists()
        assert (local / ".git" / "HEAD").read_text() == "ref: refs/heads/main"

    def test_teardown_copy_from_mount_no_backup(self, tmp_path):
        """When no backup exists, copy files from mount target."""
        mount_dir = tmp_path / "mount" / "proj"
        mount_dir.mkdir(parents=True)
        (mount_dir / "file.py").write_text("from mount")
        sub = mount_dir / "sub"
        sub.mkdir()
        (sub / "nested.py").write_text("nested")

        local = tmp_path / "proj"
        local.symlink_to(mount_dir)

        result = teardown_mount(
            local_path=str(local),
            mount_point=str(tmp_path / "mount"),
            remote_subpath="proj",
            filters=[],
        )

        assert result.success
        assert result.method == "copied_from_mount"
        assert local.is_dir()
        assert not local.is_symlink()
        assert (local / "file.py").read_text() == "from mount"
        assert (local / "sub" / "nested.py").read_text() == "nested"

    def test_teardown_symlink_mount_down(self, tmp_path):
        """Symlink exists but mount target doesn't — remove symlink, warn."""
        local = tmp_path / "proj"
        local.symlink_to(tmp_path / "nonexistent-mount")

        result = teardown_mount(
            local_path=str(local),
            mount_point=str(tmp_path / "mount"),
            remote_subpath="proj",
            filters=[],
        )

        assert result.success
        assert result.method == "mapping_only"
        assert not local.exists()
        assert not local.is_symlink()

    def test_teardown_no_symlink_no_backup(self, tmp_path):
        """No symlink, no backup — just remove the mapping."""
        local = tmp_path / "proj"
        # local doesn't exist at all

        result = teardown_mount(
            local_path=str(local),
            mount_point=str(tmp_path / "mount"),
            remote_subpath="proj",
            filters=[],
        )

        assert result.success
        assert result.method == "mapping_only"

    def test_teardown_merges_newer_files_from_mount(self, tmp_path):
        """Files edited after migration are merged from mount into backup."""
        import time

        # Simulate post-migration state: backup has original content
        mount_dir = tmp_path / "mount" / "proj"
        mount_dir.mkdir(parents=True)

        backup = tmp_path / "proj.premigration-backup"
        backup.mkdir()
        (backup / "original.py").write_text("old version")
        (backup / "unchanged.py").write_text("same")

        # Wait a moment so mount files get a definitively newer mtime
        time.sleep(0.05)

        # Mount has: edited file, new file, unchanged file
        (mount_dir / "original.py").write_text("EDITED via mount after migration")
        (mount_dir / "new_file.py").write_text("created after migration")
        (mount_dir / "unchanged.py").write_text("same")
        # Make unchanged.py have an older mtime than backup
        import os
        backup_mtime = (backup / "unchanged.py").stat().st_mtime
        os.utime(mount_dir / "unchanged.py", (backup_mtime - 10, backup_mtime - 10))

        # Also put a new file in a subdirectory
        (mount_dir / "sub").mkdir()
        (mount_dir / "sub" / "deep.py").write_text("deep new file")

        local = tmp_path / "proj"
        local.symlink_to(mount_dir)

        result = teardown_mount(
            local_path=str(local),
            mount_point=str(tmp_path / "mount"),
            remote_subpath="proj",
            filters=[],
        )

        assert result.success
        assert result.method == "backup_restored"

        # Edited file should have the newer version from mount
        assert (local / "original.py").read_text() == "EDITED via mount after migration"

        # New file should be present
        assert (local / "new_file.py").read_text() == "created after migration"

        # Unchanged file should still have original content
        assert (local / "unchanged.py").read_text() == "same"

        # Deeply nested new file should be present
        assert (local / "sub" / "deep.py").read_text() == "deep new file"

    def test_teardown_merge_skips_filtered_files(self, tmp_path):
        """Merge step does not copy filtered files (e.g. *.pyc) into backup."""
        import time

        mount_dir = tmp_path / "mount" / "proj"
        mount_dir.mkdir(parents=True)

        backup = tmp_path / "proj.premigration-backup"
        backup.mkdir()
        (backup / "main.py").write_text("code")

        time.sleep(0.05)

        (mount_dir / "main.py").write_text("updated code")
        # A loose .pyc file at root level — should be skipped by merge
        (mount_dir / "stale.pyc").write_bytes(b"\x00\x01")
        # A new non-filtered file — should be merged
        (mount_dir / "new_util.py").write_text("new utility")

        local = tmp_path / "proj"
        local.symlink_to(mount_dir)

        filters = ["- *.pyc"]
        result = teardown_mount(
            local_path=str(local),
            mount_point=str(tmp_path / "mount"),
            remote_subpath="proj",
            filters=filters,
        )

        assert result.success
        # .pyc should NOT have been merged
        assert not (local / "stale.pyc").exists()
        # Regular files should have been merged
        assert (local / "main.py").read_text() == "updated code"
        assert (local / "new_util.py").read_text() == "new utility"


# --- compare_local_remote ---


class TestCompareLocalRemote:
    def test_identical_content(self, source_dir, config):
        """No divergence when local and remote match."""
        remote_files = [
            RemoteFileInfo(path="file1.txt", size=5),
            RemoteFileInfo(path="file2.txt", size=5),
            RemoteFileInfo(path="subdir/nested.txt", size=14),
        ]
        report = compare_local_remote(source_dir, remote_files, config)
        assert report.local_only_count == 0
        assert report.remote_only_count == 0
        assert report.size_mismatch_count == 0
        assert not report.is_significant

    def test_local_only_files(self, source_dir, config):
        """Files only on local side detected."""
        # Remote has none of the local files
        remote_files = []
        report = compare_local_remote(source_dir, remote_files, config)
        assert report.local_only_count == 3  # file1, file2, subdir/nested
        assert report.remote_only_count == 0
        assert report.local_total_files == 3

    def test_remote_only_files(self, tmp_path, config):
        """Files only on remote side detected."""
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        remote_files = [
            RemoteFileInfo(path="remote1.py", size=100),
            RemoteFileInfo(path="remote2.py", size=200),
        ]
        report = compare_local_remote(empty_dir, remote_files, config)
        assert report.remote_only_count == 2
        assert report.local_only_count == 0
        assert report.remote_total_files == 2

    def test_size_mismatch(self, source_dir, config):
        """Files with different sizes flagged."""
        actual_size = (source_dir / "file1.txt").stat().st_size
        remote_files = [
            RemoteFileInfo(path="file1.txt", size=actual_size + 5000),
        ]
        report = compare_local_remote(source_dir, remote_files, config)
        assert report.size_mismatch_count == 1
        # 2 local files not on remote
        assert report.local_only_count == 2

    def test_significance_threshold(self, source_dir, config):
        """Divergence marked significant when exceeding threshold."""
        config.size_change_threshold = 0.5  # 50%
        # 3 local files, 0 on remote => 100% divergence, 3 diff files
        remote_files = []
        report = compare_local_remote(source_dir, remote_files, config)
        # 3 files differ, diff_ratio = 1.0 >= 0.5, diff_count=3 >= 3
        assert report.is_significant

    def test_not_significant_small_count(self, source_dir, config):
        """Small diff counts not flagged even if ratio is high."""
        config.size_change_threshold = 0.5
        # Remote has 2 of 3 files (only 1 missing)
        actual1 = (source_dir / "file1.txt").stat().st_size
        actual2 = (source_dir / "file2.txt").stat().st_size
        remote_files = [
            RemoteFileInfo(path="file1.txt", size=actual1),
            RemoteFileInfo(path="file2.txt", size=actual2),
        ]
        report = compare_local_remote(source_dir, remote_files, config)
        assert report.local_only_count == 1  # nested.txt
        # diff_count=1, <3 so not significant regardless of ratio
        assert not report.is_significant

    def test_filters_applied(self, source_dir_with_git, config):
        """Filtered files excluded from comparison."""
        # source_dir_with_git has .git/, __pycache__/, .venv/ which are filtered
        remote_files = [
            RemoteFileInfo(path="main.py", size=(source_dir_with_git / "main.py").stat().st_size),
            RemoteFileInfo(path=".env", size=(source_dir_with_git / ".env").stat().st_size),
            RemoteFileInfo(path=".env.local", size=(source_dir_with_git / ".env.local").stat().st_size),
        ]
        report = compare_local_remote(source_dir_with_git, remote_files, config)
        # .git, __pycache__, .venv should NOT appear in the comparison
        assert report.local_only_count == 0
        assert report.remote_only_count == 0


# --- plan_bisync_setup (modified to handle empty/nonexistent dirs) ---


class TestPlanBisyncSetup:
    @patch("protondrive_sync.core.migration.rclone_lsjson", return_value=[])
    def test_existing_dir_with_files(self, mock_lsjson, source_dir, config):
        """Standard case: local has files, remote empty."""
        plan = plan_bisync_setup(str(source_dir), "test/proj", config)
        assert plan.file_count == 3
        assert plan.total_size_bytes > 0
        assert not plan.local_is_empty
        assert plan.remote_file_count == 0

    @patch("protondrive_sync.core.migration.rclone_lsjson", return_value=[
        RemoteFileInfo(path="readme.md", size=500),
        RemoteFileInfo(path="src/main.py", size=1200),
    ])
    def test_empty_local_pull_from_remote(self, mock_lsjson, tmp_path, config):
        """Pull scenario: local doesn't exist, remote has files."""
        local = tmp_path / "new-project"
        plan = plan_bisync_setup(str(local), "workspace/proj", config)
        assert plan.local_is_empty
        assert plan.remote_file_count == 2
        assert plan.remote_size_bytes == 1700
        # Directory should have been created
        assert local.exists()

    @patch("protondrive_sync.core.migration.rclone_lsjson", return_value=[])
    def test_nonexistent_local_both_empty(self, mock_lsjson, tmp_path, config):
        """Both sides empty: creates dir, sets local_is_empty."""
        local = tmp_path / "fresh-dir"
        plan = plan_bisync_setup(str(local), "empty/path", config)
        assert plan.local_is_empty
        assert plan.remote_file_count == 0
        assert local.exists()

    @patch("protondrive_sync.core.migration.rclone_lsjson")
    def test_both_have_files_divergence(self, mock_lsjson, source_dir, config):
        """Both have files with divergence."""
        mock_lsjson.return_value = [
            RemoteFileInfo(path="file1.txt", size=99999),  # different size
            RemoteFileInfo(path="remote_only.py", size=500),
        ]
        config.size_change_threshold = 0.5
        plan = plan_bisync_setup(str(source_dir), "test", config)
        assert not plan.local_is_empty
        assert plan.remote_file_count == 2
        assert plan.divergence is not None
        assert plan.divergence.remote_only_count >= 1
        assert plan.divergence.size_mismatch_count >= 1

    @patch("protondrive_sync.core.migration.rclone_lsjson", side_effect=Exception("offline"))
    def test_remote_unreachable(self, mock_lsjson, source_dir, config):
        """Graceful fallback when remote is unreachable."""
        plan = plan_bisync_setup(str(source_dir), "test", config)
        assert plan.file_count == 3
        assert plan.remote_file_count == 0
        assert plan.divergence is None

    def test_rejects_file_path(self, tmp_path, config):
        """Rejects path that exists but is a file."""
        f = tmp_path / "not-a-dir.txt"
        f.write_text("x")
        with pytest.raises(MigrationError, match="not a directory"):
            plan_bisync_setup(str(f), "test", config)

    @patch("protondrive_sync.core.migration.rclone_lsjson", return_value=[])
    def test_empty_existing_dir(self, mock_lsjson, tmp_path, config):
        """Empty existing directory treated as pull mode."""
        empty = tmp_path / "empty-dir"
        empty.mkdir()
        plan = plan_bisync_setup(str(empty), "test", config)
        assert plan.local_is_empty
