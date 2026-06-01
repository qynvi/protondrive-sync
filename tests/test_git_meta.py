"""Tests for git metadata scanning, persistence, and rehydration."""

import json
import os
import subprocess
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock, call

from protondrive_sync.core.git_meta import (
    METADATA_FILENAME,
    GitRepoInfo,
    SyncMeta,
    git_available,
    scan_git_repos,
    write_metadata,
    read_metadata,
    check_rehydration_status,
    rehydration_summary,
    rehydrate_repo,
    rehydrate_all,
    refresh_git_metadata,
    RehydrationResult,
    _git,
    _git_remotes,
    _git_branch,
    _git_commit,
    _git_has_submodules,
    _serialize_meta,
    _deserialize_meta,
    _add_to_git_exclude,
    _find_default_branch,
)
from protondrive_sync.core.config import AppConfig


DEFAULT_FILTERS = AppConfig().filters


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _init_git_repo(path: Path, remote_url: str = "https://github.com/user/repo.git",
                   branch: str = "main", commit: bool = True,
                   add_submodule: bool = False) -> None:
    """Create a real git repo at path for testing."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", branch], cwd=str(path),
                    capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(path),
                    capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(path),
                    capture_output=True, check=True)
    if remote_url:
        subprocess.run(["git", "remote", "add", "origin", remote_url], cwd=str(path),
                        capture_output=True, check=True)
    if commit:
        (path / "README.md").write_text("# Test\n")
        subprocess.run(["git", "add", "."], cwd=str(path),
                        capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=str(path),
                        capture_output=True, check=True)
    if add_submodule:
        (path / ".gitmodules").write_text(
            '[submodule "sub"]\n\tpath = sub\n\turl = https://github.com/user/sub.git\n'
        )


# ---------------------------------------------------------------------------
# TestGitAvailable
# ---------------------------------------------------------------------------

class TestGitAvailable:
    def test_git_is_available(self):
        """git should be available on the test system."""
        # Reset cache
        import protondrive_sync.core.git_meta as mod
        mod._git_available_cache = None
        assert git_available() is True

    def test_git_unavailable(self):
        """When git is not found, git_available returns False."""
        import protondrive_sync.core.git_meta as mod
        mod._git_available_cache = None
        with patch("protondrive_sync.core.git_meta.subprocess.run",
                    side_effect=FileNotFoundError):
            assert git_available() is False
        # Reset cache for other tests
        mod._git_available_cache = None


# ---------------------------------------------------------------------------
# TestGitHelpers
# ---------------------------------------------------------------------------

class TestGitHelpers:
    def test_git_remotes(self, tmp_path):
        """_git_remotes parses remote -v output correctly."""
        repo = tmp_path / "repo"
        _init_git_repo(repo, remote_url="https://github.com/user/repo.git")
        remotes = _git_remotes(repo)
        assert "origin" in remotes
        assert remotes["origin"]["fetch"] == "https://github.com/user/repo.git"
        assert remotes["origin"]["push"] == "https://github.com/user/repo.git"

    def test_git_remotes_multiple(self, tmp_path):
        """_git_remotes handles multiple remotes."""
        repo = tmp_path / "repo"
        _init_git_repo(repo, remote_url="https://github.com/user/repo.git")
        subprocess.run(["git", "remote", "add", "upstream", "https://github.com/orig/repo.git"],
                        cwd=str(repo), capture_output=True, check=True)
        remotes = _git_remotes(repo)
        assert "origin" in remotes
        assert "upstream" in remotes
        assert remotes["upstream"]["fetch"] == "https://github.com/orig/repo.git"

    def test_git_remotes_none(self, tmp_path):
        """_git_remotes returns empty dict when no remotes configured."""
        repo = tmp_path / "repo"
        _init_git_repo(repo, remote_url="")  # no remote
        remotes = _git_remotes(repo)
        assert remotes == {}

    def test_git_branch(self, tmp_path):
        """_git_branch returns current branch name."""
        repo = tmp_path / "repo"
        _init_git_repo(repo, branch="develop")
        assert _git_branch(repo) == "develop"

    def test_git_branch_detached(self, tmp_path):
        """_git_branch returns empty string for detached HEAD."""
        repo = tmp_path / "repo"
        _init_git_repo(repo)
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo),
                              capture_output=True, text=True).stdout.strip()
        subprocess.run(["git", "checkout", "--detach"], cwd=str(repo),
                        capture_output=True, check=True)
        assert _git_branch(repo) == ""

    def test_git_commit(self, tmp_path):
        """_git_commit returns a valid SHA."""
        repo = tmp_path / "repo"
        _init_git_repo(repo)
        sha = _git_commit(repo)
        assert len(sha) == 40
        assert all(c in "0123456789abcdef" for c in sha)

    def test_git_has_submodules(self, tmp_path):
        """_git_has_submodules detects .gitmodules file."""
        repo = tmp_path / "repo"
        _init_git_repo(repo, add_submodule=True)
        assert _git_has_submodules(repo) is True

    def test_git_no_submodules(self, tmp_path):
        """_git_has_submodules returns False when no .gitmodules."""
        repo = tmp_path / "repo"
        _init_git_repo(repo)
        assert _git_has_submodules(repo) is False


# ---------------------------------------------------------------------------
# TestScanGitRepos
# ---------------------------------------------------------------------------

class TestScanGitRepos:
    def test_scan_single_repo(self, tmp_path):
        """Scan finds a git repo at the sync root."""
        repo = tmp_path / "project"
        _init_git_repo(repo, remote_url="https://github.com/user/project.git",
                        branch="main")
        repos = scan_git_repos(repo, DEFAULT_FILTERS)
        assert len(repos) == 1
        assert repos[0].relative_path == "."
        assert "origin" in repos[0].remotes
        assert repos[0].branch == "main"
        assert len(repos[0].commit) == 40

    def test_scan_nested_repos(self, tmp_path):
        """Scan finds nested git repos in subdirectories."""
        root = tmp_path / "workspace"
        root.mkdir()
        (root / "notes.txt").write_text("workspace notes")

        sub1 = root / "project-a"
        _init_git_repo(sub1, remote_url="https://github.com/user/project-a.git")

        sub2 = root / "libs" / "shared"
        _init_git_repo(sub2, remote_url="https://github.com/user/shared.git",
                        branch="develop")

        repos = scan_git_repos(root, DEFAULT_FILTERS)
        assert len(repos) == 2
        paths = [r.relative_path for r in repos]
        assert "project-a" in paths
        assert "libs/shared" in paths

    def test_scan_no_remote_skipped(self, tmp_path):
        """Repos with no remotes are silently skipped."""
        repo = tmp_path / "local-only"
        _init_git_repo(repo, remote_url="")  # no remote
        repos = scan_git_repos(repo, DEFAULT_FILTERS)
        assert len(repos) == 0

    def test_scan_respects_filters(self, tmp_path):
        """Doesn't descend into filtered directories like node_modules."""
        root = tmp_path / "project"
        root.mkdir()

        # Real repo at root
        _init_git_repo(root, remote_url="https://github.com/user/project.git")

        # Fake git repo inside node_modules (should be skipped)
        nm_repo = root / "node_modules" / "some-pkg"
        nm_repo.mkdir(parents=True)
        (nm_repo / ".git").mkdir()

        repos = scan_git_repos(root, DEFAULT_FILTERS)
        assert len(repos) == 1
        assert repos[0].relative_path == "."

    def test_scan_with_submodules(self, tmp_path):
        """Scan detects repos with submodules."""
        repo = tmp_path / "project"
        _init_git_repo(repo, remote_url="https://github.com/user/project.git",
                        add_submodule=True)
        repos = scan_git_repos(repo, DEFAULT_FILTERS)
        assert len(repos) == 1
        assert repos[0].has_submodules is True

    def test_scan_nonexistent_dir(self, tmp_path):
        """Scan returns empty list for nonexistent directory."""
        repos = scan_git_repos(tmp_path / "nope", DEFAULT_FILTERS)
        assert repos == []

    def test_scan_empty_dir(self, tmp_path):
        """Scan returns empty list for directory with no git repos."""
        empty = tmp_path / "empty"
        empty.mkdir()
        repos = scan_git_repos(empty, DEFAULT_FILTERS)
        assert repos == []

    def test_scan_git_unavailable(self, tmp_path):
        """When git is not available, scan returns empty list."""
        repo = tmp_path / "project"
        _init_git_repo(repo, remote_url="https://github.com/user/project.git")
        import protondrive_sync.core.git_meta as mod
        mod._git_available_cache = None
        with patch.object(mod, "git_available", return_value=False):
            repos = scan_git_repos(repo, DEFAULT_FILTERS)
        assert repos == []
        mod._git_available_cache = None


# ---------------------------------------------------------------------------
# TestMetadataIO
# ---------------------------------------------------------------------------

class TestMetadataIO:
    def test_write_read_roundtrip(self, tmp_path):
        """write_metadata + read_metadata round-trips correctly."""
        repos = [
            GitRepoInfo(
                relative_path=".",
                remotes={"origin": {"fetch": "https://github.com/u/r.git",
                                     "push": "https://github.com/u/r.git"}},
                branch="main",
                commit="a" * 40,
                has_submodules=False,
            ),
            GitRepoInfo(
                relative_path="libs/utils",
                remotes={"origin": {"fetch": "git@github.com:u/utils.git",
                                     "push": "git@github.com:u/utils.git"}},
                branch="develop",
                commit="b" * 40,
                has_submodules=True,
            ),
        ]
        path = write_metadata(tmp_path, repos)
        assert path is not None
        assert path == tmp_path / METADATA_FILENAME

        meta = read_metadata(tmp_path)
        assert meta is not None
        assert meta.version == 1
        assert meta.hostname != ""
        assert len(meta.git_repos) == 2
        assert meta.git_repos[0].relative_path == "."
        assert meta.git_repos[0].branch == "main"
        assert meta.git_repos[1].relative_path == "libs/utils"
        assert meta.git_repos[1].has_submodules is True

    def test_write_unchanged_noop(self, tmp_path):
        """write_metadata returns None if repo data hasn't changed."""
        repos = [
            GitRepoInfo(
                relative_path=".",
                remotes={"origin": {"fetch": "https://example.com/repo.git",
                                     "push": "https://example.com/repo.git"}},
                branch="main",
                commit="c" * 40,
            ),
        ]
        # First write
        result1 = write_metadata(tmp_path, repos)
        assert result1 is not None

        # Second write with same data — should be a no-op
        result2 = write_metadata(tmp_path, repos)
        assert result2 is None

    def test_write_changed_data(self, tmp_path):
        """write_metadata writes when repo data changes."""
        repos_v1 = [
            GitRepoInfo(relative_path=".", remotes={"origin": {"fetch": "url1"}},
                         branch="main", commit="a" * 40),
        ]
        repos_v2 = [
            GitRepoInfo(relative_path=".", remotes={"origin": {"fetch": "url1"}},
                         branch="develop", commit="b" * 40),
        ]
        write_metadata(tmp_path, repos_v1)
        result = write_metadata(tmp_path, repos_v2)
        assert result is not None

        meta = read_metadata(tmp_path)
        assert meta.git_repos[0].branch == "develop"

    def test_read_nonexistent(self, tmp_path):
        """read_metadata returns None for missing file."""
        assert read_metadata(tmp_path) is None

    def test_read_malformed(self, tmp_path):
        """read_metadata returns None for malformed JSON."""
        (tmp_path / METADATA_FILENAME).write_text("not json {{{")
        assert read_metadata(tmp_path) is None

    def test_read_forward_compatible(self, tmp_path):
        """read_metadata ignores unknown fields (forward-compatible)."""
        data = {
            "version": 2,
            "generated_at": "2026-01-01T00:00:00Z",
            "hostname": "test",
            "future_field": "unknown",
            "git_repos": [
                {
                    "relative_path": ".",
                    "remotes": {"origin": {"fetch": "url"}},
                    "branch": "main",
                    "commit": "d" * 40,
                    "has_submodules": False,
                    "another_future_field": True,
                }
            ],
        }
        (tmp_path / METADATA_FILENAME).write_text(json.dumps(data))
        meta = read_metadata(tmp_path)
        assert meta is not None
        assert meta.version == 2
        assert len(meta.git_repos) == 1

    def test_serialize_deserialize(self):
        """_serialize_meta and _deserialize_meta round-trip."""
        meta = SyncMeta(
            version=1,
            generated_at="2026-01-01T00:00:00Z",
            hostname="test-host",
            git_repos=[
                GitRepoInfo(
                    relative_path=".",
                    remotes={"origin": {"fetch": "url", "push": "url"}},
                    branch="main",
                    commit="e" * 40,
                ),
            ],
        )
        text = _serialize_meta(meta)
        restored = _deserialize_meta(text)
        assert restored.version == 1
        assert restored.hostname == "test-host"
        assert len(restored.git_repos) == 1
        assert restored.git_repos[0].commit == "e" * 40


# ---------------------------------------------------------------------------
# TestRehydrationStatus
# ---------------------------------------------------------------------------

class TestRehydrationStatus:
    def test_check_not_rehydrated(self, tmp_path):
        """Repos without .git/ are reported as not rehydrated."""
        (tmp_path / "project").mkdir()
        repos = [GitRepoInfo(relative_path="project", remotes={"origin": {"fetch": "url"}})]
        status = check_rehydration_status(tmp_path, repos)
        assert status == {"project": False}

    def test_check_rehydrated(self, tmp_path):
        """Repos with .git/ are reported as rehydrated."""
        project = tmp_path / "project"
        project.mkdir()
        (project / ".git").mkdir()
        repos = [GitRepoInfo(relative_path="project", remotes={"origin": {"fetch": "url"}})]
        status = check_rehydration_status(tmp_path, repos)
        assert status == {"project": True}

    def test_check_root_repo(self, tmp_path):
        """Root-level repo (relative_path='.') is checked correctly."""
        (tmp_path / ".git").mkdir()
        repos = [GitRepoInfo(relative_path=".", remotes={"origin": {"fetch": "url"}})]
        status = check_rehydration_status(tmp_path, repos)
        assert status == {".": True}

    def test_check_mixed(self, tmp_path):
        """Mixed rehydration status is reported correctly."""
        proj_a = tmp_path / "a"
        proj_a.mkdir()
        (proj_a / ".git").mkdir()

        proj_b = tmp_path / "b"
        proj_b.mkdir()
        # No .git for b

        repos = [
            GitRepoInfo(relative_path="a", remotes={"origin": {"fetch": "url"}}),
            GitRepoInfo(relative_path="b", remotes={"origin": {"fetch": "url"}}),
        ]
        status = check_rehydration_status(tmp_path, repos)
        assert status == {"a": True, "b": False}

    def test_summary(self, tmp_path):
        """rehydration_summary returns (total, rehydrated) counts."""
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / ".git").mkdir()

        meta = SyncMeta(git_repos=[
            GitRepoInfo(relative_path="proj", remotes={"origin": {"fetch": "url"}}),
            GitRepoInfo(relative_path="other", remotes={"origin": {"fetch": "url"}}),
        ])
        (tmp_path / "other").mkdir()

        total, rehydrated = rehydration_summary(tmp_path, meta)
        assert total == 2
        assert rehydrated == 1

    def test_summary_empty(self, tmp_path):
        """rehydration_summary returns (0, 0) for empty metadata."""
        meta = SyncMeta()
        assert rehydration_summary(tmp_path, meta) == (0, 0)


# ---------------------------------------------------------------------------
# TestRehydrateRepo
# ---------------------------------------------------------------------------

class TestRehydrateRepo:
    def test_skip_already_rehydrated(self, tmp_path):
        """Already-rehydrated repos are skipped."""
        repo_path = tmp_path / "project"
        repo_path.mkdir()
        (repo_path / ".git").mkdir()

        repo = GitRepoInfo(
            relative_path=".",
            remotes={"origin": {"fetch": "https://github.com/u/r.git"}},
            branch="main",
            commit="a" * 40,
        )
        result = rehydrate_repo(tmp_path / "project", repo)
        assert result.success is True
        assert result.skipped is True
        assert "already rehydrated" in result.message.lower()

    def test_rehydrate_success(self, tmp_path):
        """Full rehydration with a local bare repo as remote."""
        # Create a "remote" bare repo
        bare = tmp_path / "bare.git"
        bare.mkdir()
        subprocess.run(["git", "init", "--bare", "-b", "main"], cwd=str(bare),
                        capture_output=True, check=True)

        # Create a source repo, push to bare
        source = tmp_path / "source"
        _init_git_repo(source, remote_url=str(bare), branch="main")
        subprocess.run(["git", "push", "-u", "origin", "main"], cwd=str(source),
                        capture_output=True, check=True)

        # Simulate synced files (working tree without .git)
        target = tmp_path / "target"
        target.mkdir()
        (target / "README.md").write_text("# Test\n")

        repo = GitRepoInfo(
            relative_path=".",
            remotes={"origin": {"fetch": str(bare), "push": str(bare)}},
            branch="main",
            commit=_git_commit(source),
        )

        log_messages = []
        result = rehydrate_repo(target, repo, log=log_messages.append)
        assert result.success is True
        assert result.skipped is False
        assert (target / ".git").is_dir()

        # Verify branch and tracking
        branch = subprocess.run(["git", "branch", "--show-current"], cwd=str(target),
                                 capture_output=True, text=True).stdout.strip()
        assert branch == "main"

    def test_rehydrate_detached_head(self, tmp_path):
        """Rehydration with detached HEAD (empty branch) uses commit SHA."""
        bare = tmp_path / "bare.git"
        bare.mkdir()
        subprocess.run(["git", "init", "--bare", "-b", "main"], cwd=str(bare),
                        capture_output=True, check=True)

        source = tmp_path / "source"
        _init_git_repo(source, remote_url=str(bare))
        subprocess.run(["git", "push", "-u", "origin", "main"], cwd=str(source),
                        capture_output=True, check=True)
        commit_sha = _git_commit(source)

        target = tmp_path / "target"
        target.mkdir()
        (target / "README.md").write_text("# Test\n")

        repo = GitRepoInfo(
            relative_path=".",
            remotes={"origin": {"fetch": str(bare), "push": str(bare)}},
            branch="",  # detached
            commit=commit_sha,
        )

        result = rehydrate_repo(target, repo)
        assert result.success is True
        assert (target / ".git").is_dir()

    def test_rehydrate_with_submodules(self, tmp_path):
        """Rehydration attempts submodule init when has_submodules=True."""
        bare = tmp_path / "bare.git"
        bare.mkdir()
        subprocess.run(["git", "init", "--bare", "-b", "main"], cwd=str(bare),
                        capture_output=True, check=True)

        source = tmp_path / "source"
        _init_git_repo(source, remote_url=str(bare), add_submodule=True)
        subprocess.run(["git", "add", "."], cwd=str(source), capture_output=True)
        subprocess.run(["git", "commit", "-m", "add submodule ref"], cwd=str(source),
                        capture_output=True)
        subprocess.run(["git", "push", "-u", "origin", "main"], cwd=str(source),
                        capture_output=True, check=True)

        target = tmp_path / "target"
        target.mkdir()
        (target / "README.md").write_text("# Test\n")
        (target / ".gitmodules").write_text(
            '[submodule "sub"]\n\tpath = sub\n\turl = https://github.com/user/sub.git\n'
        )

        repo = GitRepoInfo(
            relative_path=".",
            remotes={"origin": {"fetch": str(bare), "push": str(bare)}},
            branch="main",
            commit=_git_commit(source),
            has_submodules=True,
        )

        log_messages = []
        result = rehydrate_repo(target, repo, log=log_messages.append)
        # Should succeed overall even if submodule URL is unreachable
        assert result.success is True
        # Should have attempted submodule init
        assert any("submodule" in msg.lower() for msg in log_messages)

    def test_rehydrate_creates_dir(self, tmp_path):
        """Rehydration creates the directory if it doesn't exist."""
        bare = tmp_path / "bare.git"
        bare.mkdir()
        subprocess.run(["git", "init", "--bare", "-b", "main"], cwd=str(bare),
                        capture_output=True, check=True)
        source = tmp_path / "source"
        _init_git_repo(source, remote_url=str(bare))
        subprocess.run(["git", "push", "-u", "origin", "main"], cwd=str(source),
                        capture_output=True, check=True)

        target = tmp_path / "target" / "nested"
        # target doesn't exist yet

        repo = GitRepoInfo(
            relative_path=".",
            remotes={"origin": {"fetch": str(bare), "push": str(bare)}},
            branch="main",
            commit=_git_commit(source),
        )

        result = rehydrate_repo(target, repo)
        assert result.success is True
        assert target.is_dir()
        assert (target / ".git").is_dir()

    def test_rehydrate_fetch_failure(self, tmp_path):
        """Rehydration reports failure when fetch fails."""
        target = tmp_path / "target"
        target.mkdir()

        repo = GitRepoInfo(
            relative_path=".",
            remotes={"origin": {"fetch": "https://nonexistent.invalid/repo.git",
                                 "push": "https://nonexistent.invalid/repo.git"}},
            branch="main",
            commit="a" * 40,
        )

        result = rehydrate_repo(target, repo)
        assert result.success is False
        assert "fetch failed" in result.message.lower() or "fatal" in result.message.lower()


# ---------------------------------------------------------------------------
# TestRehydrateAll
# ---------------------------------------------------------------------------

class TestRehydrateAll:
    def test_rehydrate_all_mixed(self, tmp_path):
        """rehydrate_all processes multiple repos, skipping rehydrated ones."""
        bare = tmp_path / "bare.git"
        bare.mkdir()
        subprocess.run(["git", "init", "--bare", "-b", "main"], cwd=str(bare),
                        capture_output=True, check=True)
        source = tmp_path / "source"
        _init_git_repo(source, remote_url=str(bare))
        subprocess.run(["git", "push", "-u", "origin", "main"], cwd=str(source),
                        capture_output=True, check=True)

        sync_root = tmp_path / "sync"
        sync_root.mkdir()

        # Already rehydrated
        already = sync_root / "already"
        already.mkdir()
        (already / ".git").mkdir()

        # Needs rehydration
        needs = sync_root / "needs"
        needs.mkdir()
        (needs / "README.md").write_text("# Test\n")

        meta = SyncMeta(git_repos=[
            GitRepoInfo(relative_path="already",
                         remotes={"origin": {"fetch": str(bare)}}, branch="main"),
            GitRepoInfo(relative_path="needs",
                         remotes={"origin": {"fetch": str(bare)}}, branch="main",
                         commit=_git_commit(source)),
        ])

        results = rehydrate_all(sync_root, meta)
        assert len(results) == 2
        assert results[0].skipped is True   # already
        assert results[1].success is True   # needs
        assert results[1].skipped is False

    def test_rehydrate_all_git_unavailable(self, tmp_path):
        """rehydrate_all returns error when git is not installed."""
        meta = SyncMeta(git_repos=[
            GitRepoInfo(relative_path=".", remotes={"origin": {"fetch": "url"}}),
        ])
        with patch("protondrive_sync.core.git_meta.git_available", return_value=False):
            results = rehydrate_all(tmp_path, meta)
        assert len(results) == 1
        assert results[0].success is False
        assert "not installed" in results[0].message

    def test_rehydrate_all_empty(self, tmp_path):
        """rehydrate_all returns empty list for metadata with no repos."""
        meta = SyncMeta()
        results = rehydrate_all(tmp_path, meta)
        assert results == []


# ---------------------------------------------------------------------------
# TestGitInfoExclude
# ---------------------------------------------------------------------------

class TestGitInfoExclude:
    def test_add_to_exclude(self, tmp_path):
        """_add_to_git_exclude adds metadata filename to .git/info/exclude."""
        repo = tmp_path / "repo"
        _init_git_repo(repo, remote_url="https://example.com/repo.git")
        _add_to_git_exclude(repo)

        exclude = (repo / ".git" / "info" / "exclude").read_text()
        assert METADATA_FILENAME in exclude

    def test_add_to_exclude_idempotent(self, tmp_path):
        """_add_to_git_exclude doesn't duplicate entries."""
        repo = tmp_path / "repo"
        _init_git_repo(repo, remote_url="https://example.com/repo.git")
        _add_to_git_exclude(repo)
        _add_to_git_exclude(repo)

        exclude = (repo / ".git" / "info" / "exclude").read_text()
        assert exclude.count(METADATA_FILENAME) == 1

    def test_add_to_exclude_no_git(self, tmp_path):
        """_add_to_git_exclude is a no-op if .git/info doesn't exist."""
        # Should not raise
        _add_to_git_exclude(tmp_path / "nope")


# ---------------------------------------------------------------------------
# TestFindDefaultBranch
# ---------------------------------------------------------------------------

class TestFindDefaultBranch:
    def test_find_default_main(self, tmp_path):
        """_find_default_branch finds 'main' after fetch."""
        bare = tmp_path / "bare.git"
        bare.mkdir()
        subprocess.run(["git", "init", "--bare", "-b", "main"], cwd=str(bare),
                        capture_output=True, check=True)
        source = tmp_path / "source"
        _init_git_repo(source, remote_url=str(bare))
        subprocess.run(["git", "push", "-u", "origin", "main"], cwd=str(source),
                        capture_output=True, check=True)

        target = tmp_path / "target"
        target.mkdir()
        subprocess.run(["git", "init"], cwd=str(target), capture_output=True, check=True)
        subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=str(target),
                        capture_output=True, check=True)
        subprocess.run(["git", "fetch", "--all"], cwd=str(target), capture_output=True, check=True)

        branch = _find_default_branch(target, "origin")
        assert branch == "main"


# ---------------------------------------------------------------------------
# TestRefreshGitMetadata
# ---------------------------------------------------------------------------

class TestRefreshGitMetadata:
    def test_refresh_creates_metadata(self, tmp_path):
        """refresh_git_metadata creates metadata for a repo."""
        _init_git_repo(tmp_path, remote_url="https://github.com/u/r.git")
        result = refresh_git_metadata(tmp_path, DEFAULT_FILTERS)
        assert result is not None
        assert (tmp_path / METADATA_FILENAME).exists()

    def test_refresh_noop_no_repos(self, tmp_path):
        """refresh_git_metadata returns None when no repos found."""
        tmp_path.mkdir(exist_ok=True)
        result = refresh_git_metadata(tmp_path, DEFAULT_FILTERS)
        assert result is None

    def test_refresh_noop_unchanged(self, tmp_path):
        """refresh_git_metadata returns None when nothing changed."""
        _init_git_repo(tmp_path, remote_url="https://github.com/u/r.git")
        refresh_git_metadata(tmp_path, DEFAULT_FILTERS)
        result = refresh_git_metadata(tmp_path, DEFAULT_FILTERS)
        assert result is None  # No change


# ---------------------------------------------------------------------------
# TestEndToEnd
# ---------------------------------------------------------------------------

class TestEndToEnd:
    def test_scan_write_rehydrate_cycle(self, tmp_path):
        """Full cycle: scan source, write metadata, rehydrate on target."""
        # 1. Create source repo
        bare = tmp_path / "bare.git"
        bare.mkdir()
        subprocess.run(["git", "init", "--bare", "-b", "main"], cwd=str(bare),
                        capture_output=True, check=True)

        source = tmp_path / "source"
        _init_git_repo(source, remote_url=str(bare), branch="main")
        (source / "app.py").write_text("print('hello')\n")
        subprocess.run(["git", "add", "."], cwd=str(source), capture_output=True)
        subprocess.run(["git", "commit", "-m", "add app"], cwd=str(source), capture_output=True)
        subprocess.run(["git", "push", "-u", "origin", "main"], cwd=str(source),
                        capture_output=True, check=True)

        # 2. Scan and write metadata
        repos = scan_git_repos(source, DEFAULT_FILTERS)
        assert len(repos) == 1
        write_metadata(source, repos)

        # 3. Simulate sync: copy files (not .git/) + metadata to target
        target = tmp_path / "target"
        target.mkdir()
        (target / "README.md").write_text("# Test\n")
        (target / "app.py").write_text("print('hello')\n")
        # Copy metadata file
        import shutil
        shutil.copy2(source / METADATA_FILENAME, target / METADATA_FILENAME)

        # 4. Read metadata on target
        meta = read_metadata(target)
        assert meta is not None
        assert len(meta.git_repos) == 1

        # 5. Check rehydration needed
        total, rehydrated = rehydration_summary(target, meta)
        assert total == 1
        assert rehydrated == 0

        # 6. Rehydrate — use local bare repo path from metadata
        # (The metadata recorded the bare repo path as the remote URL)
        results = rehydrate_all(target, meta)
        assert len(results) == 1
        assert results[0].success is True
        assert results[0].skipped is False

        # 7. Verify rehydrated state
        assert (target / ".git").is_dir()
        branch = subprocess.run(["git", "branch", "--show-current"], cwd=str(target),
                                 capture_output=True, text=True).stdout.strip()
        assert branch == "main"

        # 8. Verify rehydration summary updated
        total, rehydrated = rehydration_summary(target, meta)
        assert total == 1
        assert rehydrated == 1

        # 9. Second rehydration skips
        results2 = rehydrate_all(target, meta)
        assert results2[0].skipped is True
