"""Tests for bisync module — adaptive timing, safety checks, delete protection."""

import json
import time
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from protondrive_sync.core.config import AppConfig, FolderMapping
from protondrive_sync.core.bisync import (
    BurstState,
    should_sync,
    scan_for_modifications,
    is_work_file,
    detect_local_deletions,
    protect_deleted_work_files,
    detect_suspicious_changes,
    write_pending_review,
    read_pending_review,
    has_pending_review,
    clear_pending_review,
    run_safety_checks,
    FlaggedChange,
    DeletedWorkFile,
    WORK_EXTENSIONS,
)
from protondrive_sync.core.rclone import RemoteFileInfo


@pytest.fixture
def config(tmp_path):
    cfg = AppConfig(mount_point=str(tmp_path / "mount"))
    # Point pending_review_file to tmp
    return cfg


@pytest.fixture
def config_with_tmp_review(tmp_path):
    """Config with pending_review_file in a temp directory."""
    cfg = AppConfig(mount_point=str(tmp_path / "mount"))
    # Monkey-patch the property to use tmp dir
    review_path = tmp_path / "pending_review.json"
    type(cfg).pending_review_file = property(lambda self: review_path)
    return cfg


@pytest.fixture
def project_dir(tmp_path):
    """Create a project directory with various files."""
    proj = tmp_path / "project"
    proj.mkdir()
    (proj / "main.py").write_text("print('hello')")
    (proj / "utils.py").write_text("def foo(): pass")
    (proj / "README.md").write_text("# My project")
    (proj / "data.csv").write_text("a,b,c\n1,2,3")
    (proj / "build.o").write_bytes(b"\x00" * 100)  # not a work file
    sub = proj / "src"
    sub.mkdir()
    (sub / "app.py").write_text("class App: pass")
    return proj


# --- is_work_file ---


class TestIsWorkFile:
    def test_python_file(self):
        assert is_work_file("main.py")

    def test_cpp_file(self):
        assert is_work_file("engine.cpp")

    def test_document(self):
        assert is_work_file("report.docx")

    def test_pdf(self):
        assert is_work_file("paper.pdf")

    def test_object_file_not_work(self):
        assert not is_work_file("main.o")

    def test_shared_lib_not_work(self):
        assert not is_work_file("libfoo.so")

    def test_makefile(self):
        assert is_work_file("Makefile")

    def test_dockerfile(self):
        assert is_work_file("Dockerfile")

    def test_nested_path(self):
        assert is_work_file("src/deep/module.py")

    def test_random_binary(self):
        assert not is_work_file("image.png")


# --- BurstState + should_sync ---


class TestBurstState:
    def test_inactive_no_sync(self):
        state = BurstState()
        assert not should_sync(state, quiet_threshold=120, max_burst=1800)

    def test_quiet_threshold_triggers(self):
        state = BurstState(
            active=True,
            start_time=time.time() - 200,
            last_change_time=time.time() - 130,  # 130s quiet
        )
        assert should_sync(state, quiet_threshold=120, max_burst=1800)

    def test_within_quiet_no_sync(self):
        state = BurstState(
            active=True,
            start_time=time.time() - 50,
            last_change_time=time.time() - 10,  # only 10s quiet
        )
        assert not should_sync(state, quiet_threshold=120, max_burst=1800)

    def test_max_burst_triggers(self):
        now = time.time()
        state = BurstState(
            active=True,
            start_time=now - 1900,       # 1900s burst (> 1800 max)
            last_change_time=now - 5,    # still active (5s quiet)
        )
        assert should_sync(state, quiet_threshold=120, max_burst=1800)

    def test_record_change_activates(self):
        state = BurstState()
        assert not state.active
        state.record_change()
        assert state.active
        assert state.start_time > 0
        assert state.last_change_time > 0

    def test_reset_clears(self):
        state = BurstState(active=True, start_time=100, last_change_time=200)
        state.reset()
        assert not state.active
        assert state.start_time == 0.0


# --- scan_for_modifications ---


class TestScanForModifications:
    def test_detects_new_file(self, project_dir):
        folder = FolderMapping(
            local_path=str(project_dir),
            remote_subpath="test",
            sync_mode="bisync",
        )
        since = time.time() - 1  # 1 second ago
        assert scan_for_modifications(folder, since, [])

    def test_no_changes_after_future(self, project_dir):
        folder = FolderMapping(
            local_path=str(project_dir),
            remote_subpath="test",
            sync_mode="bisync",
        )
        since = time.time() + 100  # in the future
        assert not scan_for_modifications(folder, since, [])

    def test_skips_filtered_dirs(self, project_dir):
        # Create a __pycache__ with a very new file
        cache = project_dir / "__pycache__"
        cache.mkdir()
        (cache / "new.pyc").write_bytes(b"\x00")

        folder = FolderMapping(
            local_path=str(project_dir),
            remote_subpath="test",
            sync_mode="bisync",
        )
        # Set since to the future so only __pycache__/new.pyc would match
        since = time.time() + 100
        filters = ["- __pycache__/**"]
        assert not scan_for_modifications(folder, since, filters)

    def test_detects_added_file_with_old_mtime(self, project_dir):
        """A file with a preserved mtime (e.g. GNOME copy-paste) still
        triggers detection because the parent directory mtime is updated."""
        import os

        folder = FolderMapping(
            local_path=str(project_dir),
            remote_subpath="test",
            sync_mode="bisync",
        )

        # Record time before the copy
        since = time.time()
        time.sleep(0.05)  # small gap

        # Simulate a file manager copy: create file, set old mtime
        copied = project_dir / "copied.txt"
        copied.write_text("copy")
        old_mtime = since - 86400  # 1 day old
        os.utime(copied, (old_mtime, old_mtime))

        # The file mtime is old, but directory mtime is fresh
        assert copied.stat().st_mtime < since
        assert project_dir.stat().st_mtime > since

        assert scan_for_modifications(folder, since, [])

    def test_detects_deleted_file_via_dir_mtime(self, project_dir):
        """Removing a file updates the parent directory mtime."""
        folder = FolderMapping(
            local_path=str(project_dir),
            remote_subpath="test",
            sync_mode="bisync",
        )

        since = time.time()
        time.sleep(0.05)

        # Delete an existing file
        (project_dir / "main.py").unlink()

        assert scan_for_modifications(folder, since, [])


# --- detect_local_deletions ---


class TestDetectLocalDeletions:
    def test_detects_deleted_work_file(self, project_dir):
        remote_files = [
            RemoteFileInfo(path="main.py", size=100),
            RemoteFileInfo(path="deleted.py", size=200),  # not on disk
        ]
        deleted, dirs = detect_local_deletions(str(project_dir), remote_files, [])
        assert len(deleted) == 1
        assert deleted[0].path == "deleted.py"

    def test_ignores_non_work_files(self, project_dir):
        remote_files = [
            RemoteFileInfo(path="main.py", size=100),
            RemoteFileInfo(path="image.png", size=5000),  # not on disk, not work file
        ]
        deleted, dirs = detect_local_deletions(str(project_dir), remote_files, [])
        assert len(deleted) == 0

    def test_detects_directory_delete(self, tmp_path):
        """When all work files in a directory are deleted, detect as dir delete."""
        local = tmp_path / "proj"
        local.mkdir()
        # src/ directory doesn't exist locally
        remote_files = [
            RemoteFileInfo(path="src/a.py", size=100),
            RemoteFileInfo(path="src/b.py", size=200),
        ]
        deleted, dirs = detect_local_deletions(str(local), remote_files, [])
        assert "src" in dirs
        assert len(deleted) == 0  # handled at dir level

    def test_skips_filtered_files(self, project_dir):
        remote_files = [
            RemoteFileInfo(path=".git/HEAD", size=50),
        ]
        filters = ["- .git/**"]
        deleted, dirs = detect_local_deletions(str(project_dir), remote_files, filters)
        assert len(deleted) == 0


# --- detect_suspicious_changes ---


class TestDetectSuspiciousChanges:
    def test_flags_large_size_change(self, project_dir, config):
        # main.py is ~15 bytes locally, say 30000 bytes on remote
        remote_files = [
            RemoteFileInfo(path="main.py", size=30000),
        ]
        config.size_change_min_bytes = 100  # lower threshold for test
        config.size_change_threshold = 0.5
        flagged = detect_suspicious_changes(str(project_dir), remote_files, config)
        assert len(flagged) == 1
        assert flagged[0].path == "main.py"

    def test_ignores_small_files(self, project_dir, config):
        remote_files = [
            RemoteFileInfo(path="main.py", size=20),
        ]
        config.size_change_min_bytes = 100000  # high threshold
        flagged = detect_suspicious_changes(str(project_dir), remote_files, config)
        assert len(flagged) == 0

    def test_ignores_similar_sizes(self, project_dir, config):
        local_size = (project_dir / "main.py").stat().st_size
        remote_files = [
            RemoteFileInfo(path="main.py", size=local_size),  # same size
        ]
        config.size_change_min_bytes = 1
        flagged = detect_suspicious_changes(str(project_dir), remote_files, config)
        assert len(flagged) == 0


# --- protect_deleted_work_files ---


class TestProtectDeletedWorkFiles:
    @patch("protondrive_sync.core.bisync.rclone_moveto")
    def test_renames_files(self, mock_moveto):
        deleted = [DeletedWorkFile(path="old.py", remote_size=100)]
        protected = protect_deleted_work_files("remote", "proj", deleted, [])
        assert len(protected) == 1
        assert "old.py" in protected[0]
        assert ".protondrive-sync-backups/" in protected[0]
        mock_moveto.assert_called_once()
        # Verify destination routes to backup directory
        call_args = mock_moveto.call_args[0]
        assert call_args[0] == "remote:proj/old.py"
        assert "remote:proj/.protondrive-sync-backups/old.py." in call_args[1]

    @patch("protondrive_sync.core.bisync.rclone_moveto")
    def test_renames_directories(self, mock_moveto):
        protected = protect_deleted_work_files("remote", "proj", [], ["old_dir"])
        assert len(protected) == 1
        assert "old_dir" in protected[0]
        assert ".protondrive-sync-backups/" in protected[0]
        mock_moveto.assert_called_once()
        call_args = mock_moveto.call_args[0]
        assert call_args[0] == "remote:proj/old_dir"
        assert "remote:proj/.protondrive-sync-backups/old_dir." in call_args[1]


# --- Pending review management ---


class TestPendingReview:
    def test_write_read_cycle(self, config_with_tmp_review):
        cfg = config_with_tmp_review
        flagged = [FlaggedChange(path="big.py", local_size=100, remote_size=50000, change_pct=200.0)]
        write_pending_review(cfg, "/tmp/proj", flagged)

        reviews = read_pending_review(cfg)
        assert "/tmp/proj" in reviews
        assert len(reviews["/tmp/proj"]) == 1
        assert reviews["/tmp/proj"][0].path == "big.py"

    def test_has_pending_review(self, config_with_tmp_review):
        cfg = config_with_tmp_review
        assert not has_pending_review(cfg, "/tmp/proj")

        flagged = [FlaggedChange(path="x.py", local_size=1, remote_size=100, change_pct=99.0)]
        write_pending_review(cfg, "/tmp/proj", flagged)
        assert has_pending_review(cfg, "/tmp/proj")

    def test_clear_pending_review(self, config_with_tmp_review):
        cfg = config_with_tmp_review
        flagged = [FlaggedChange(path="x.py", local_size=1, remote_size=100, change_pct=99.0)]
        write_pending_review(cfg, "/tmp/proj", flagged)
        assert has_pending_review(cfg, "/tmp/proj")

        clear_pending_review(cfg, "/tmp/proj")
        assert not has_pending_review(cfg, "/tmp/proj")

    def test_clear_removes_file_when_empty(self, config_with_tmp_review):
        cfg = config_with_tmp_review
        flagged = [FlaggedChange(path="x.py", local_size=1, remote_size=100, change_pct=99.0)]
        write_pending_review(cfg, "/tmp/proj", flagged)
        clear_pending_review(cfg, "/tmp/proj")
        assert not cfg.pending_review_file.exists()

    def test_flagged_change_serialization(self):
        fc = FlaggedChange(path="a.py", local_size=100, remote_size=200, change_pct=50.0)
        d = fc.to_dict()
        fc2 = FlaggedChange.from_dict(d)
        assert fc2.path == fc.path
        assert fc2.local_size == fc.local_size
        assert fc2.change_pct == fc.change_pct
