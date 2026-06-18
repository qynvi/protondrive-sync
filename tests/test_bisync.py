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
    detect_unstable_writes,
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
from protondrive_sync.core.proton_cli import ProtonError, RemoteNode
from protondrive_sync.bisync_main import ScheduledSync, _scheduled_sync_sort_key


@pytest.fixture
def config(tmp_path, monkeypatch):
    cfg = AppConfig()
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setattr(
        "protondrive_sync.core.config.get_config_dir", lambda: config_dir
    )
    # Point pending_review_file to tmp
    return cfg


@pytest.fixture
def config_with_tmp_review(tmp_path, monkeypatch):
    """Config with pending_review_file in a temp directory."""
    cfg = AppConfig()
    # Monkey-patch the property to use tmp dir
    review_path = tmp_path / "pending_review.json"
    monkeypatch.setattr(
        type(cfg),
        "pending_review_file",
        property(lambda self: review_path),
        raising=False,
    )
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
            start_time=now - 1900,  # 1900s burst (> 1800 max)
            last_change_time=now - 5,  # still active (5s quiet)
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
        )
        since = time.time() - 1  # 1 second ago
        assert scan_for_modifications(folder, since, [])

    def test_no_changes_after_future(self, project_dir):
        folder = FolderMapping(
            local_path=str(project_dir),
            remote_subpath="test",
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
        )

        since = time.time()
        time.sleep(0.05)

        # Delete an existing file
        (project_dir / "main.py").unlink()

        assert scan_for_modifications(folder, since, [])

    def test_preserve_mode_detects_symlink_metadata_change(self, project_dir, tmp_path):
        target = tmp_path / "target.txt"
        target.write_text("target")
        link = project_dir / "link.txt"
        link.symlink_to(target)
        folder = FolderMapping(
            local_path=str(project_dir),
            remote_subpath="test",
            symlink_mode="preserve",
        )

        since = time.time()
        time.sleep(0.05)
        link.unlink()
        link.symlink_to(tmp_path / "new-target.txt")

        assert scan_for_modifications(folder, since, [])

    def test_skip_mode_ignores_symlink_file_target_changes(self, project_dir, tmp_path):
        target = tmp_path / "target.txt"
        target.write_text("old")
        link = project_dir / "link.txt"
        link.symlink_to(target)
        folder = FolderMapping(
            local_path=str(project_dir),
            remote_subpath="test",
            symlink_mode="skip",
        )

        since = time.time()
        time.sleep(0.05)
        target.write_text("new")

        assert not scan_for_modifications(folder, since, [])


# --- detect_local_deletions ---


class TestDetectLocalDeletions:
    def test_detects_deleted_work_file(self, project_dir, config):
        remote_files = [
            RemoteNode(path="main.py", size=100),
            RemoteNode(path="deleted.py", size=200),  # not on disk
        ]
        deleted, dirs = detect_local_deletions(
            str(project_dir), remote_files, [], config
        )
        assert len(deleted) == 1
        assert deleted[0].path == "deleted.py"

    def test_ignores_non_work_files(self, project_dir, config):
        remote_files = [
            RemoteNode(path="main.py", size=100),
            RemoteNode(path="image.png", size=5000),  # not on disk, not work file
        ]
        deleted, dirs = detect_local_deletions(
            str(project_dir), remote_files, [], config
        )
        assert len(deleted) == 0

    def test_detects_directory_delete(self, tmp_path, config):
        """When all work files in a directory are deleted, detect as dir delete."""
        local = tmp_path / "proj"
        local.mkdir()
        # src/ directory doesn't exist locally
        remote_files = [
            RemoteNode(path="src/a.py", size=100),
            RemoteNode(path="src/b.py", size=200),
        ]
        deleted, dirs = detect_local_deletions(str(local), remote_files, [], config)
        assert "src" in dirs
        assert len(deleted) == 0  # handled at dir level

    def test_skips_filtered_files(self, project_dir, config):
        remote_files = [
            RemoteNode(path=".git/HEAD", size=50),
        ]
        filters = ["- .git/**"]
        deleted, dirs = detect_local_deletions(
            str(project_dir), remote_files, filters, config
        )
        assert len(deleted) == 0

    def test_preserve_mode_uses_lexists_for_broken_symlink(self, project_dir, config):
        broken = project_dir / "broken.py"
        broken.symlink_to(project_dir / "missing.py")
        remote_files = [RemoteNode(path="broken.py", size=20)]

        deleted, dirs = detect_local_deletions(
            str(project_dir),
            remote_files,
            [],
            symlink_mode="preserve",
            config=config,
        )

        assert deleted == []

    def test_large_deleted_binary_is_protected(self, project_dir, config):
        config.protect_delete_min_bytes = 100
        remote_files = [RemoteNode(path="checkpoint.bin", size=500)]

        deleted, dirs = detect_local_deletions(
            str(project_dir), remote_files, [], config
        )

        assert [item.path for item in deleted] == ["checkpoint.bin"]

    def test_broad_directory_delete_is_protected(self, tmp_path, config):
        config.protect_directory_delete_min_files = 2
        local = tmp_path / "proj"
        local.mkdir()
        remote_files = [
            RemoteNode(path="dataset/a.raw", size=1),
            RemoteNode(path="dataset/b.raw", size=1),
        ]

        deleted, dirs = detect_local_deletions(str(local), remote_files, [], config)

        assert "dataset" in dirs


# --- detect_suspicious_changes ---


class TestDetectSuspiciousChanges:
    def test_flags_large_size_change(self, project_dir, config):
        # main.py is ~15 bytes locally, say 30000 bytes on remote
        remote_files = [
            RemoteNode(path="main.py", size=30000),
        ]
        config.size_change_min_bytes = 100  # lower threshold for test
        config.size_change_threshold = 0.5
        folder = FolderMapping(local_path=str(project_dir), remote_subpath="test")
        flagged = detect_suspicious_changes(folder, remote_files, config)
        assert len(flagged) == 1
        assert flagged[0].path == "main.py"

    def test_ignores_small_files(self, project_dir, config):
        remote_files = [
            RemoteNode(path="main.py", size=20),
        ]
        config.size_change_min_bytes = 100000  # high threshold
        folder = FolderMapping(local_path=str(project_dir), remote_subpath="test")
        flagged = detect_suspicious_changes(folder, remote_files, config)
        assert len(flagged) == 0

    def test_ignores_similar_sizes(self, project_dir, config):
        local_size = (project_dir / "main.py").stat().st_size
        remote_files = [
            RemoteNode(path="main.py", size=local_size),  # same size
        ]
        config.size_change_min_bytes = 1
        folder = FolderMapping(local_path=str(project_dir), remote_subpath="test")
        flagged = detect_suspicious_changes(folder, remote_files, config)
        assert len(flagged) == 0

    def test_preserve_mode_uses_symlink_target_size(
        self, project_dir, tmp_path, config
    ):
        target = tmp_path / "target.txt"
        target.write_text("x" * 1000)
        link = project_dir / "link.py"
        link.symlink_to(target)
        folder = FolderMapping(
            local_path=str(project_dir),
            remote_subpath="test",
            symlink_mode="preserve",
        )
        remote_files = [RemoteNode(path="link.py", size=len(str(target)))]
        config.size_change_min_bytes = 1

        flagged = detect_suspicious_changes(folder, remote_files, config)

        assert flagged == []


class TestStableWrites:
    def test_detects_file_still_changing(self, project_dir, config, monkeypatch):
        folder = FolderMapping(local_path=str(project_dir), remote_subpath="test")
        changing = project_dir / "changing.py"
        changing.write_text("one")
        since = time.time() - 1

        def mutate(_seconds):
            changing.write_text("two")

        monkeypatch.setattr("protondrive_sync.core.bisync.time.sleep", mutate)

        unstable = detect_unstable_writes(folder, since, [], delay_seconds=1)

        assert unstable == ["changing.py"]

    def test_stable_file_not_reported(self, project_dir, config, monkeypatch):
        folder = FolderMapping(local_path=str(project_dir), remote_subpath="test")
        monkeypatch.setattr(
            "protondrive_sync.core.bisync.time.sleep", lambda _seconds: None
        )

        unstable = detect_unstable_writes(folder, time.time() - 1, [], delay_seconds=1)

        assert unstable == []


# --- protect_deleted_work_files ---


class TestProtectDeletedWorkFiles:
    def test_trashes_files(self, monkeypatch):
        from tests.fake_backend import FakeBackend

        backend = FakeBackend()
        monkeypatch.setattr(
            "protondrive_sync.core.bisync.ProtonDriveCLI", lambda _config: backend
        )
        deleted = [DeletedWorkFile(path="old.py", remote_size=100)]
        protected = protect_deleted_work_files("proj", deleted, [])
        assert len(protected) == 1
        assert "old.py" in protected[0]
        assert "trash" in protected[0]
        assert backend.trashed == ["proj/old.py"]

    def test_trashes_directories(self, monkeypatch):
        from tests.fake_backend import FakeBackend

        backend = FakeBackend()
        monkeypatch.setattr(
            "protondrive_sync.core.bisync.ProtonDriveCLI", lambda _config: backend
        )
        protected = protect_deleted_work_files("proj", [], ["old_dir"])
        assert len(protected) == 1
        assert "old_dir" in protected[0]
        assert "trash" in protected[0]
        assert backend.trashed == ["proj/old_dir"]


# --- Pending review management ---


class TestPendingReview:
    def test_write_read_cycle(self, config_with_tmp_review):
        cfg = config_with_tmp_review
        flagged = [
            FlaggedChange(
                path="big.py", local_size=100, remote_size=50000, change_pct=200.0
            )
        ]
        write_pending_review(cfg, "/tmp/proj", flagged)

        reviews = read_pending_review(cfg)
        assert "/tmp/proj" in reviews
        assert len(reviews["/tmp/proj"]) == 1
        assert reviews["/tmp/proj"][0].path == "big.py"

    def test_has_pending_review(self, config_with_tmp_review):
        cfg = config_with_tmp_review
        assert not has_pending_review(cfg, "/tmp/proj")

        flagged = [
            FlaggedChange(path="x.py", local_size=1, remote_size=100, change_pct=99.0)
        ]
        write_pending_review(cfg, "/tmp/proj", flagged)
        assert has_pending_review(cfg, "/tmp/proj")

    def test_clear_pending_review(self, config_with_tmp_review):
        cfg = config_with_tmp_review
        flagged = [
            FlaggedChange(path="x.py", local_size=1, remote_size=100, change_pct=99.0)
        ]
        write_pending_review(cfg, "/tmp/proj", flagged)
        assert has_pending_review(cfg, "/tmp/proj")

        clear_pending_review(cfg, "/tmp/proj")
        assert not has_pending_review(cfg, "/tmp/proj")

    def test_clear_removes_file_when_empty(self, config_with_tmp_review):
        cfg = config_with_tmp_review
        flagged = [
            FlaggedChange(path="x.py", local_size=1, remote_size=100, change_pct=99.0)
        ]
        write_pending_review(cfg, "/tmp/proj", flagged)
        clear_pending_review(cfg, "/tmp/proj")
        assert not cfg.pending_review_file.exists()

    def test_flagged_change_serialization(self):
        fc = FlaggedChange(
            path="a.py", local_size=100, remote_size=200, change_pct=50.0
        )
        d = fc.to_dict()
        fc2 = FlaggedChange.from_dict(d)
        assert fc2.path == fc.path
        assert fc2.local_size == fc.local_size
        assert fc2.change_pct == fc.change_pct


class TestRunSafetyChecks:
    def test_remote_listing_failure_is_not_safe(self, monkeypatch, project_dir, config):
        class FailingBackend:
            def list_recursive(self, _path):
                raise ProtonError("offline")

        monkeypatch.setattr(
            "protondrive_sync.core.bisync.ProtonDriveCLI",
            lambda _config: FailingBackend(),
        )
        folder = FolderMapping(local_path=str(project_dir), remote_subpath="test")

        report = run_safety_checks(folder, config)

        assert not report.safe_to_sync
        assert "offline" in report.remote_listing_error


class TestDaemonP3Routing:
    def test_scheduler_orders_local_changes_by_fewest_paths(self, tmp_path):
        folders = [
            FolderMapping(local_path=str(tmp_path / "large"), remote_subpath="large"),
            FolderMapping(local_path=str(tmp_path / "small"), remote_subpath="small"),
            FolderMapping(local_path=str(tmp_path / "medium"), remote_subpath="medium"),
        ]
        candidates = [
            ScheduledSync(
                folders[0],
                BurstState(active=True),
                "local",
                config_index=0,
                changed_count=80,
                upload_bytes=100,
            ),
            ScheduledSync(
                folders[1],
                BurstState(active=True),
                "local",
                config_index=1,
                changed_count=1,
                upload_bytes=10,
            ),
            ScheduledSync(
                folders[2],
                BurstState(active=True),
                "local",
                config_index=2,
                changed_count=5,
                upload_bytes=50,
            ),
        ]

        ordered = sorted(candidates, key=_scheduled_sync_sort_key)

        assert [item.folder.remote_subpath for item in ordered] == [
            "small",
            "medium",
            "large",
        ]

    def test_scheduler_puts_remote_background_after_local_changes(self, tmp_path):
        local = FolderMapping(
            local_path=str(tmp_path / "local"), remote_subpath="local"
        )
        remote = FolderMapping(
            local_path=str(tmp_path / "remote"), remote_subpath="remote"
        )
        candidates = [
            ScheduledSync(
                remote, BurstState(), "remote", config_index=0, remote_due=True
            ),
            ScheduledSync(
                local,
                BurstState(active=True),
                "local",
                config_index=1,
                changed_count=10,
                upload_bytes=1000,
            ),
        ]

        ordered = sorted(candidates, key=_scheduled_sync_sort_key)

        assert [item.folder.remote_subpath for item in ordered] == ["local", "remote"]

    def test_scheduler_tie_breaks_local_changes_by_bytes(self, tmp_path):
        small_bytes = FolderMapping(local_path=str(tmp_path / "a"), remote_subpath="a")
        large_bytes = FolderMapping(local_path=str(tmp_path / "b"), remote_subpath="b")
        candidates = [
            ScheduledSync(
                large_bytes,
                BurstState(active=True),
                "local",
                config_index=0,
                changed_count=2,
                upload_bytes=1000,
            ),
            ScheduledSync(
                small_bytes,
                BurstState(active=True),
                "local",
                config_index=1,
                changed_count=2,
                upload_bytes=10,
            ),
        ]

        ordered = sorted(candidates, key=_scheduled_sync_sort_key)

        assert [item.folder.remote_subpath for item in ordered] == ["a", "b"]

    def test_do_sync_uses_targeted_engine_when_enabled(
        self, monkeypatch, project_dir, config
    ):
        from protondrive_sync.bisync_main import _do_sync
        from protondrive_sync.core.sync_engine import TargetedSyncResult

        folder = FolderMapping(
            local_path=str(project_dir),
            remote_subpath="test",
            bisync_initialized=True,
        )
        calls: list[str] = []

        class FakeLock:
            def acquire(self):
                calls.append("lock")
                return self

            def release(self):
                calls.append("release")

        monkeypatch.setattr(
            "protondrive_sync.bisync_main.local_folder_lock", lambda _folder: FakeLock()
        )
        monkeypatch.setattr(
            "protondrive_sync.bisync_main.acquire_remote_lease",
            lambda *_args, **_kwargs: object(),
        )
        monkeypatch.setattr(
            "protondrive_sync.bisync_main.release_remote_lease",
            lambda *_args, **_kwargs: calls.append("lease-release"),
        )
        monkeypatch.setattr(
            "protondrive_sync.bisync_main.detect_unstable_writes",
            lambda *_args, **_kwargs: [],
        )
        monkeypatch.setattr(
            "protondrive_sync.core.sync_engine.run_targeted_sync_cycle",
            lambda *_args, **_kwargs: TargetedSyncResult(
                status="healthy", synced_paths=["a.txt"]
            ),
        )

        _do_sync(
            folder,
            config,
            BurstState(active=True, start_time=time.time() - 10),
            lambda msg: calls.append(msg),
        )

        assert "lock" in calls
        assert any("Targeted sync complete" in item for item in calls)
        assert "release" in calls
