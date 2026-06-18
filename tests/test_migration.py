"""Tests for migration module."""

import pytest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from protondrive_sync.core.config import AppConfig, FolderMapping
from protondrive_sync.core.migration import (
    execute_bisync_setup,
    plan_bisync_setup,
    compare_local_remote,
    _matches_filter,
    _scan_directory,
    _is_large_initial_upload,
    _scan_symlinks,
    _find_filtered_toplevel,
    _find_env_files,
    _check_download_space,
    _download_temp_path,
    _publish_download_temp,
    _MAX_ATTEMPTS,
    MigrationError,
    MigrationCancelled,
    BisyncPlan,
    DivergenceReport,
)
from protondrive_sync.core.proton_cli import ProtonError, RemoteNode
from protondrive_sync.core.inventory import get_inventory_entry
from tests.fake_backend import FakeBackend

RemoteFileInfo = RemoteNode


@pytest.fixture
def config(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setattr(
        "protondrive_sync.core.config.get_config_dir", lambda: config_dir
    )
    return AppConfig()


@pytest.fixture(autouse=True)
def p1_setup_external_stubs(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "protondrive_sync.core.migration.ProtonDriveCLI", lambda _config: FakeBackend()
    )
    monkeypatch.setattr(
        "protondrive_sync.core.migration.probe_backend",
        lambda _config: "cli vtest",
        raising=False,
    )
    monkeypatch.setattr(
        "protondrive_sync.core.migration.acquire_remote_lease",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "protondrive_sync.core.migration.ensure_check_access_sentinel",
        lambda local_path, _remote_subpath, _config: (
            Path(local_path) / ".protondrive-sync-check"
        ),
        raising=False,
    )
    monkeypatch.setattr(
        "protondrive_sync.core.migration.verify_sync_detailed",
        lambda *args, **kwargs: SimpleNamespace(
            ok=True,
            missing_on_dst=[],
            missing_on_src=[],
            different=[],
            errors=[],
            operation_log=None,
        ),
        raising=False,
    )


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

    def test_recursive_directory_filter(self):
        filters = [
            "- **/node_modules/**",
            "- **/.venv/**",
            "- **/.local/**",
            "- **/.cache/**",
            "- **/.hf_cache/**",
            "- **/hf_cache/**",
            "- **/.huggingface/**",
            "- **/.ipynb_checkpoints/**",
            "- **/.gradio/**",
            "- **/build/**",
            "- **/dist/**",
            "- **/out/**",
            "- **/cmake-build-*/**",
        ]
        assert _matches_filter("src/node_modules/pkg/index.js", filters)
        assert _matches_filter("packages/app/.venv/pyvenv.cfg", filters)
        assert _matches_filter(
            "ts_etl/.local/lib/python3.11/site-packages/numpy/__init__.py", filters
        )
        assert _matches_filter("ts_etl/.cache/pip/http-v2/cache.body", filters)
        assert _matches_filter(
            "yin-agent/core/speculation/weights/.hf_cache/models/blob", filters
        )
        assert _matches_filter(
            "index-tts-2.0/checkpoints/hf_cache/models/blob", filters
        )
        assert _matches_filter(
            "yin-agent/core/speculation/weights/.huggingface/token", filters
        )
        assert _matches_filter(
            "index-tts-2.0/indextts/s2mel/modules/.ipynb_checkpoints/model.py", filters
        )
        assert _matches_filter("index-tts-2.0/.gradio/certificate.pem", filters)
        assert _matches_filter("onnxruntime/build/Linux/Release/Makefile", filters)
        assert _matches_filter("onnxruntime/build/Linux/Release/dist/pkg.whl", filters)
        assert _matches_filter("repo/out/generated.bin", filters)
        assert _matches_filter("repo/cmake-build-debug/CMakeCache.txt", filters)
        assert not _matches_filter("src/modules/pkg/index.js", filters)

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


class TestInitialUploadRepair:
    def test_large_initial_upload_threshold(self):
        assert not _is_large_initial_upload(49 * 1024**3)
        assert _is_large_initial_upload(50 * 1024**3)


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


class TestScanSymlinks:
    def test_counts_external_symlinks(self, tmp_path, config):
        root = tmp_path / "root"
        root.mkdir()
        target = tmp_path / "outside.txt"
        target.write_text("x")
        (root / "outside-link").symlink_to(target)

        total, external, samples = _scan_symlinks(root, config.filters)

        assert total == 1
        assert external == 1
        assert samples == ["outside-link"]

    def test_skips_filtered_symlinks(self, tmp_path, config):
        root = tmp_path / "root"
        root.mkdir()
        node_modules = root / "node_modules"
        node_modules.mkdir()
        (node_modules / "pkg-link").symlink_to(tmp_path)

        total, external, samples = _scan_symlinks(root, config.filters)

        assert total == 0
        assert external == 0
        assert samples == []


class TestScanDirectorySymlinkMode:
    def test_preserve_counts_file_symlink_as_metadata(self, tmp_path, config):
        root = tmp_path / "root"
        root.mkdir()
        target = tmp_path / "target.txt"
        target.write_text("x" * 1000)
        link = root / "link.txt"
        link.symlink_to(target)

        count, size = _scan_directory(root, config.filters, symlink_mode="preserve")

        assert count == 1
        assert size == len(str(target).encode("utf-8"))

    def test_copy_counts_file_symlink_target_size(self, tmp_path, config):
        root = tmp_path / "root"
        root.mkdir()
        target = tmp_path / "target.txt"
        target.write_text("x" * 1000)
        (root / "link.txt").symlink_to(target)

        count, size = _scan_directory(root, config.filters, symlink_mode="copy")

        assert count == 1
        assert size == 1000

    def test_skip_ignores_symlinks(self, tmp_path, config):
        root = tmp_path / "root"
        root.mkdir()
        target = tmp_path / "target.txt"
        target.write_text("x")
        (root / "link.txt").symlink_to(target)

        count, size = _scan_directory(root, config.filters, symlink_mode="skip")

        assert count == 0
        assert size == 0


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
            RemoteFileInfo(
                path="main.py", size=(source_dir_with_git / "main.py").stat().st_size
            ),
            RemoteFileInfo(
                path=".env", size=(source_dir_with_git / ".env").stat().st_size
            ),
            RemoteFileInfo(
                path=".env.local",
                size=(source_dir_with_git / ".env.local").stat().st_size,
            ),
        ]
        report = compare_local_remote(source_dir_with_git, remote_files, config)
        # .git, __pycache__, .venv should NOT appear in the comparison
        assert report.local_only_count == 0
        assert report.remote_only_count == 0


# --- plan_bisync_setup (modified to handle empty/nonexistent dirs) ---


class TestPlanBisyncSetup:
    def test_existing_dir_with_files(self, source_dir, config):
        """Standard case: local has files, remote empty."""
        plan = plan_bisync_setup(str(source_dir), "test/proj", config)
        assert plan.file_count == 3
        assert plan.total_size_bytes > 0
        assert not plan.local_is_empty
        assert plan.remote_file_count == 0

    def test_empty_local_pull_from_remote(self, tmp_path, config, monkeypatch):
        """Pull scenario: local doesn't exist, remote has files."""
        backend = FakeBackend(
            {
                "workspace/proj/readme.md": RemoteNode(
                    path="workspace/proj/readme.md", name="readme.md", size=500
                ),
                "workspace/proj/src/main.py": RemoteNode(
                    path="workspace/proj/src/main.py", name="main.py", size=1200
                ),
            }
        )
        monkeypatch.setattr(
            "protondrive_sync.core.migration.ProtonDriveCLI", lambda _config: backend
        )
        local = tmp_path / "new-project"
        plan = plan_bisync_setup(str(local), "workspace/proj", config)
        assert plan.local_is_empty
        assert plan.remote_file_count == 2
        assert plan.remote_size_bytes == 1700
        # Directory should have been created
        assert local.exists()

    def test_nonexistent_local_both_empty(self, tmp_path, config):
        """Both sides empty: creates dir, sets local_is_empty."""
        local = tmp_path / "fresh-dir"
        plan = plan_bisync_setup(str(local), "empty/path", config)
        assert plan.local_is_empty
        assert plan.remote_file_count == 0
        assert local.exists()

    def test_both_have_files_divergence(self, source_dir, config, monkeypatch):
        """Both have files with divergence."""
        backend = FakeBackend(
            {
                "test/file1.txt": RemoteNode(
                    path="test/file1.txt", name="file1.txt", size=99999
                ),
                "test/remote_only.py": RemoteNode(
                    path="test/remote_only.py", name="remote_only.py", size=500
                ),
            }
        )
        monkeypatch.setattr(
            "protondrive_sync.core.migration.ProtonDriveCLI", lambda _config: backend
        )
        config.size_change_threshold = 0.5
        plan = plan_bisync_setup(str(source_dir), "test", config)
        assert not plan.local_is_empty
        assert plan.remote_file_count == 2
        assert plan.divergence is not None
        assert plan.divergence.remote_only_count >= 1
        assert plan.divergence.size_mismatch_count >= 1

    def test_remote_unreachable(self, source_dir, config, monkeypatch):
        """Remote listing failures are surfaced instead of reported as empty."""

        class FailingBackend(FakeBackend):
            def list_recursive(self, rel):
                raise ProtonError("offline")

        monkeypatch.setattr(
            "protondrive_sync.core.migration.ProtonDriveCLI",
            lambda _config: FailingBackend(),
        )
        plan = plan_bisync_setup(str(source_dir), "test", config)
        assert plan.file_count == 3
        assert plan.remote_file_count == 0
        assert plan.remote_listing_error.startswith("offline")
        assert plan.divergence is None

    def test_remote_listing_error_keeps_local_counts(
        self, source_dir, config, monkeypatch
    ):
        class FailingBackend(FakeBackend):
            def list_recursive(self, rel):
                raise ProtonError("token expired")

        monkeypatch.setattr(
            "protondrive_sync.core.migration.ProtonDriveCLI",
            lambda _config: FailingBackend(),
        )
        plan = plan_bisync_setup(str(source_dir), "test", config)
        assert plan.file_count == 3
        assert plan.remote_listing_error.startswith("token expired")

    def test_cli_listing_error_is_reported(self, source_dir, config, monkeypatch):
        class FailingBackend(FakeBackend):
            def list_recursive(self, rel):
                raise ProtonError("CLI list timed out")

        monkeypatch.setattr(
            "protondrive_sync.core.migration.ProtonDriveCLI",
            lambda _config: FailingBackend(),
        )
        plan = plan_bisync_setup(str(source_dir), "test", config)

        assert plan.remote_listing_error == "CLI list timed out"

    def test_missing_remote_is_empty_not_listing_error(self, source_dir, config):
        plan = plan_bisync_setup(str(source_dir), "deleted/remote", config)

        assert not plan.local_is_empty
        assert plan.remote_file_count == 0
        assert plan.remote_size_bytes == 0
        assert plan.remote_listing_error is None
        assert plan.divergence is None

    def test_empty_local_and_missing_remote_are_both_empty(self, tmp_path, config):
        local = tmp_path / "new-empty"

        plan = plan_bisync_setup(str(local), "deleted/remote", config)

        assert plan.local_is_empty
        assert plan.remote_file_count == 0
        assert plan.remote_listing_error is None
        assert local.exists()

    def test_symlink_counts_in_plan(self, tmp_path, config):
        root = tmp_path / "root"
        root.mkdir()
        target = tmp_path / "outside.txt"
        target.write_text("x")
        (root / "outside-link").symlink_to(target)

        plan = plan_bisync_setup(str(root), "test", config)

        assert plan.symlink_count == 1
        assert plan.external_symlink_count == 1
        assert plan.symlink_samples == ["outside-link"]

    def test_rejects_file_path(self, tmp_path, config):
        """Rejects path that exists but is a file."""
        f = tmp_path / "not-a-dir.txt"
        f.write_text("x")
        with pytest.raises(MigrationError, match="not a directory"):
            plan_bisync_setup(str(f), "test", config)

    def test_empty_existing_dir(self, tmp_path, config):
        """Empty existing directory treated as pull mode."""
        empty = tmp_path / "empty-dir"
        empty.mkdir()
        plan = plan_bisync_setup(str(empty), "test", config)
        assert plan.local_is_empty


# --- execute_bisync_setup retry logic ---


def _make_bisync_plan(
    tmp_path,
    *,
    local_is_empty=True,
    file_count=3,
    symlink_mode="preserve",
    remote_file_count=0,
    remote_size_bytes=100,
):
    """Helper: build a BisyncPlan for testing."""
    src = tmp_path / "proj"
    src.mkdir(exist_ok=True)
    if not local_is_empty:
        (src / "a.py").write_text("a")
    return BisyncPlan(
        local_path=src,
        remote_subpath="workspace/proj",
        file_count=file_count,
        total_size_bytes=100,
        local_is_empty=local_is_empty,
        remote_file_count=remote_file_count,
        remote_size_bytes=remote_size_bytes,
        symlink_mode=symlink_mode,
    )


class TestCliBisyncSetup:
    def test_initial_upload_uses_cli_and_seeds_inventory(
        self, tmp_path, config, monkeypatch
    ):
        plan = _make_bisync_plan(tmp_path, local_is_empty=False, remote_file_count=0)
        backend = FakeBackend()
        monkeypatch.setattr(
            "protondrive_sync.core.migration.ProtonDriveCLI", lambda _config: backend
        )
        result = execute_bisync_setup(plan, config)

        assert result.success
        assert "workspace/proj/a.py" in backend.uploaded
        entry = get_inventory_entry(config, result.mapping, "a.py")
        assert entry is not None
        assert entry.remote_sha1
        assert entry.last_source == "setup"

    def test_initial_download_stages_publishes_and_seeds_inventory(
        self, tmp_path, config, monkeypatch
    ):
        import hashlib

        content = b"remote data"
        sha1 = hashlib.sha1(content).hexdigest()
        remote_path = "workspace/proj/remote.txt"
        backend = FakeBackend(
            nodes={
                remote_path: RemoteNode(
                    path=remote_path,
                    name="remote.txt",
                    size=len(content),
                    sha1=sha1,
                    modtime="2026-01-01T00:00:00Z",
                )
            },
            blobs={remote_path: content},
        )
        monkeypatch.setattr(
            "protondrive_sync.core.migration.ProtonDriveCLI", lambda _config: backend
        )
        plan = _make_bisync_plan(
            tmp_path,
            local_is_empty=True,
            remote_file_count=1,
            remote_size_bytes=len(content),
        )

        result = execute_bisync_setup(plan, config)

        assert result.success
        assert (plan.local_path / "remote.txt").read_bytes() == content
        assert not list(plan.local_path.parent.glob("proj.protondrive-download-tmp.*"))
        entry = get_inventory_entry(config, result.mapping, "remote.txt")
        assert entry is not None
        assert entry.remote_sha1 == sha1

    def test_initial_upload_retries_cli_failures(self, tmp_path, config, monkeypatch):
        class FlakyBackend(FakeBackend):
            def __init__(self):
                super().__init__()
                self.calls = 0

            def upload_many(self, *args, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise ProtonError("offline")
                return super().upload_many(*args, **kwargs)

        backend = FlakyBackend()
        monkeypatch.setattr(
            "protondrive_sync.core.migration.ProtonDriveCLI", lambda _config: backend
        )
        monkeypatch.setattr(
            "protondrive_sync.core.migration.time.sleep", lambda _seconds: None
        )
        logs: list[str] = []

        result = execute_bisync_setup(
            _make_bisync_plan(tmp_path, local_is_empty=False, remote_file_count=0),
            config,
            progress=logs.append,
        )

        assert result.success
        assert backend.calls == 2
        assert any("Retrying resume upload" in line for line in logs)


