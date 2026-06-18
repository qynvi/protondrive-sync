"""Git metadata scanning, persistence, and rehydration.

Scans sync directories for git repositories, records their remote URLs
and branch info in a metadata file that rides along with synced content.
On a new machine, reads the metadata and rehydrates repos by cloning
from their recorded remotes.

The metadata file (.protondrive-sync.json) is NOT excluded by default app
filters, so it syncs normally. The .git/ directory IS filtered, so
rehydration recreates it from git remotes rather than from sync.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import tempfile
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Callable

from .migration import _matches_filter

METADATA_FILENAME = ".protondrive-sync.json"

LogCallback = Callable[[str], None]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class GitRepoInfo:
    """Metadata for a single git repository within a sync directory."""

    relative_path: str  # "." for root-level repo, "libs/utils" for nested
    remotes: dict[str, dict[str, str]] = field(default_factory=dict)
    # e.g. {"origin": {"fetch": "https://...", "push": "https://..."}}
    branch: str = ""  # current branch (empty if detached HEAD)
    commit: str = ""  # current commit SHA
    has_submodules: bool = False  # True if .gitmodules exists


@dataclass
class SyncMeta:
    """Top-level metadata stored in .protondrive-sync.json."""

    version: int = 1
    generated_at: str = ""  # ISO 8601 timestamp
    hostname: str = ""
    git_repos: list[GitRepoInfo] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Git binary availability
# ---------------------------------------------------------------------------

_git_available_cache: Optional[bool] = None


def git_available() -> bool:
    """Check if git is installed and callable."""
    global _git_available_cache
    if _git_available_cache is not None:
        return _git_available_cache
    try:
        subprocess.run(
            ["git", "--version"],
            capture_output=True,
            timeout=5,
            check=False,
        )
        _git_available_cache = True
    except (FileNotFoundError, OSError):
        _git_available_cache = False
    return _git_available_cache


# ---------------------------------------------------------------------------
# Git helpers (subprocess wrappers)
# ---------------------------------------------------------------------------


def _git(args: list[str], cwd: Path, timeout: int = 30) -> subprocess.CompletedProcess:
    """Run a git command, returning the CompletedProcess."""
    return subprocess.run(
        ["git"] + args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _git_remotes(repo_path: Path) -> dict[str, dict[str, str]]:
    """Parse `git remote -v` output into {name: {fetch: url, push: url}}."""
    result = _git(["remote", "-v"], cwd=repo_path)
    if result.returncode != 0:
        return {}
    remotes: dict[str, dict[str, str]] = {}
    for line in result.stdout.strip().splitlines():
        parts = line.split()
        if len(parts) >= 2:
            name = parts[0]
            url = parts[1]
            kind = "push" if "(push)" in line else "fetch"
            if name not in remotes:
                remotes[name] = {}
            remotes[name][kind] = url
    return remotes


def _git_branch(repo_path: Path) -> str:
    """Get current branch name, or empty string if detached HEAD."""
    result = _git(["branch", "--show-current"], cwd=repo_path)
    if result.returncode == 0:
        return result.stdout.strip()
    return ""


def _git_commit(repo_path: Path) -> str:
    """Get current commit SHA, or empty string on error."""
    result = _git(["rev-parse", "HEAD"], cwd=repo_path)
    if result.returncode == 0:
        return result.stdout.strip()
    return ""


def _git_has_submodules(repo_path: Path) -> bool:
    """Check if .gitmodules exists in the repo."""
    return (repo_path / ".gitmodules").is_file()


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------


def _should_skip_dir(dir_name: str, filters: list[str]) -> bool:
    """Check if a directory should be skipped during git repo scanning.

    Skips directories that match filter rules (e.g. node_modules, .venv)
    and the .git directory itself (we're looking for it, not descending into it).
    """
    if dir_name == ".git":
        return True
    return _matches_filter(dir_name, filters)


def scan_git_repos(
    local_path: Path,
    filters: list[str],
) -> list[GitRepoInfo]:
    """Recursively scan a directory for git repositories.

    Finds all directories containing a .git/ subdirectory, extracts their
    remote URLs, branch, and commit info. Skips repos with no remotes
    (local-only repos can't be rehydrated).

    Respects filter rules to avoid descending into e.g. node_modules/.

    Returns a list of GitRepoInfo, sorted by relative_path.
    """
    if not git_available():
        return []

    if not local_path.is_dir():
        return []

    repos: list[GitRepoInfo] = []

    for dirpath, dirnames, _filenames in os.walk(local_path):
        current = Path(dirpath)

        # Prune filtered directories (modify dirnames in-place)
        dirnames[:] = [d for d in dirnames if not _should_skip_dir(d, filters)]

        # Check if this directory is a git repo
        git_dir = current / ".git"
        if git_dir.is_dir():
            # Found a repo — extract metadata
            remotes = _git_remotes(current)

            # Skip repos with no remotes (can't rehydrate without them)
            if not remotes:
                # Don't descend into this repo's subdirs for nested repos
                # that share the same .git — but DO allow independent nested repos
                continue

            rel = current.relative_to(local_path).as_posix()
            if rel == ".":
                rel = "."  # root-level repo

            branch = _git_branch(current)
            commit = _git_commit(current)
            has_submodules = _git_has_submodules(current)

            repos.append(
                GitRepoInfo(
                    relative_path=rel,
                    remotes=remotes,
                    branch=branch,
                    commit=commit,
                    has_submodules=has_submodules,
                )
            )

    repos.sort(key=lambda r: r.relative_path)
    return repos


# ---------------------------------------------------------------------------
# Metadata I/O
# ---------------------------------------------------------------------------


def _meta_path(local_path: Path) -> Path:
    """Path to the metadata file within a sync directory."""
    return local_path / METADATA_FILENAME


def _serialize_meta(meta: SyncMeta) -> str:
    """Serialize SyncMeta to a JSON string."""
    data = {
        "version": meta.version,
        "generated_at": meta.generated_at,
        "hostname": meta.hostname,
        "git_repos": [asdict(r) for r in meta.git_repos],
    }
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


def _deserialize_meta(text: str) -> SyncMeta:
    """Deserialize a SyncMeta from JSON text.

    Tolerant of unknown fields (forward-compatible).
    """
    data = json.loads(text)
    repos = []
    for r in data.get("git_repos", []):
        repos.append(
            GitRepoInfo(
                relative_path=r.get("relative_path", "."),
                remotes=r.get("remotes", {}),
                branch=r.get("branch", ""),
                commit=r.get("commit", ""),
                has_submodules=r.get("has_submodules", False),
            )
        )
    return SyncMeta(
        version=data.get("version", 1),
        generated_at=data.get("generated_at", ""),
        hostname=data.get("hostname", ""),
        git_repos=repos,
    )


def write_metadata(
    local_path: Path,
    repos: list[GitRepoInfo],
) -> Optional[Path]:
    """Write .protondrive-sync.json to the sync directory.

    Only writes if the meaningful content (repo list) has changed,
    to avoid triggering unnecessary sync bursts. Uses atomic
    write-temp-then-rename for safety.

    Returns the path written, or None if no write was needed.
    """
    meta = SyncMeta(
        version=1,
        generated_at=datetime.now(timezone.utc).isoformat(),
        hostname=platform.node(),
        git_repos=repos,
    )
    new_content = _serialize_meta(meta)
    target = _meta_path(local_path)

    # Check if the repo list has actually changed (ignore timestamp/hostname)
    if target.exists():
        try:
            existing = read_metadata(local_path)
            if existing is not None:
                # Compare repo data only (not generated_at or hostname)
                old_repos = sorted(
                    [asdict(r) for r in existing.git_repos],
                    key=lambda r: r["relative_path"],
                )
                new_repos = sorted(
                    [asdict(r) for r in repos],
                    key=lambda r: r["relative_path"],
                )
                if old_repos == new_repos:
                    return None  # No meaningful change
        except Exception:
            pass  # Re-write on any read error

    # Atomic write: temp file in same directory, then rename
    try:
        fd, tmp_path = tempfile.mkstemp(
            dir=str(local_path),
            prefix=".protondrive-sync-",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(new_content)
            os.replace(tmp_path, str(target))
        except Exception:
            # Clean up temp file on error
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except OSError:
        # Fallback: direct write if temp file fails
        target.write_text(new_content, encoding="utf-8")

    return target


def read_metadata(local_path: Path) -> Optional[SyncMeta]:
    """Read .protondrive-sync.json from a sync directory.

    Returns None if the file doesn't exist or is malformed.
    """
    target = _meta_path(local_path)
    if not target.is_file():
        return None
    try:
        text = target.read_text(encoding="utf-8")
        return _deserialize_meta(text)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Rehydration status
# ---------------------------------------------------------------------------


def check_rehydration_status(
    local_path: Path,
    repos: list[GitRepoInfo],
) -> dict[str, bool]:
    """Check which repos are already rehydrated (have .git/ locally).

    Returns {relative_path: is_rehydrated} for each repo.
    """
    status: dict[str, bool] = {}
    for repo in repos:
        if repo.relative_path == ".":
            repo_dir = local_path
        else:
            repo_dir = local_path / repo.relative_path
        status[repo.relative_path] = (repo_dir / ".git").is_dir()
    return status


def rehydration_summary(
    local_path: Path,
    meta: SyncMeta,
) -> tuple[int, int]:
    """Quick summary: (total_repos, rehydrated_count).

    Returns (0, 0) if no git repos in metadata.
    """
    if not meta.git_repos:
        return 0, 0
    status = check_rehydration_status(local_path, meta.git_repos)
    total = len(meta.git_repos)
    rehydrated = sum(1 for v in status.values() if v)
    return total, rehydrated


# ---------------------------------------------------------------------------
# Rehydration
# ---------------------------------------------------------------------------


@dataclass
class RehydrationResult:
    """Result of rehydrating a single repo."""

    relative_path: str
    success: bool
    message: str
    skipped: bool = False  # True if already rehydrated


def _add_to_git_exclude(repo_path: Path) -> None:
    """Add .protondrive-sync.json to .git/info/exclude.

    This prevents the metadata file from showing up in `git status`
    when the sync root is also a git repo root.
    Uses per-clone exclusion (doesn't modify .gitignore).
    """
    exclude_file = repo_path / ".git" / "info" / "exclude"
    if not exclude_file.parent.is_dir():
        return
    content = ""
    if exclude_file.exists():
        content = exclude_file.read_text(encoding="utf-8")
    if METADATA_FILENAME not in content:
        with open(exclude_file, "a", encoding="utf-8") as f:
            f.write(f"\n# protondrive-sync metadata\n{METADATA_FILENAME}\n")


def _find_default_branch(repo_path: Path, remote_name: str) -> str:
    """Try to determine the default branch for a remote.

    Checks symbolic-ref first, then falls back to common names.
    Returns the branch name or empty string if unable to determine.
    """
    # Try symbolic-ref (works after fetch)
    result = _git(
        ["symbolic-ref", f"refs/remotes/{remote_name}/HEAD"],
        cwd=repo_path,
    )
    if result.returncode == 0:
        ref = result.stdout.strip()
        # Format: refs/remotes/origin/main → extract "main"
        parts = ref.split("/")
        if len(parts) >= 4:
            return parts[-1]

    # Fallback: check common branch names
    for candidate in ("main", "master", "develop"):
        result = _git(
            ["rev-parse", "--verify", f"refs/remotes/{remote_name}/{candidate}"],
            cwd=repo_path,
        )
        if result.returncode == 0:
            return candidate

    return ""


def rehydrate_repo(
    local_path: Path,
    repo: GitRepoInfo,
    log: Optional[LogCallback] = None,
) -> RehydrationResult:
    """Rehydrate a single git repo from its recorded remote(s).

    Steps:
    1. Check if already rehydrated (skip if .git/ exists)
    2. git init -b <branch>
    3. git remote add <name> <url> for each remote
    4. git fetch --all
    5. git reset origin/<branch> (preserves working tree)
    6. git branch --set-upstream-to=origin/<branch>
    7. git submodule update --init --recursive (if submodules)
    8. Add .protondrive-sync.json to .git/info/exclude

    Returns a RehydrationResult.
    """
    _log = log or (lambda _: None)

    if repo.relative_path == ".":
        repo_path = local_path
    else:
        repo_path = local_path / repo.relative_path

    rel_display = repo.relative_path if repo.relative_path != "." else "(root)"

    # 0. Skip if already rehydrated
    if (repo_path / ".git").is_dir():
        _log(f"  {rel_display}: already rehydrated, skipping")
        return RehydrationResult(
            relative_path=repo.relative_path,
            success=True,
            message="Already rehydrated",
            skipped=True,
        )

    # Ensure directory exists
    if not repo_path.is_dir():
        try:
            repo_path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return RehydrationResult(
                relative_path=repo.relative_path,
                success=False,
                message=f"Cannot create directory: {exc}",
            )

    # Determine primary remote and branch
    primary_remote = "origin" if "origin" in repo.remotes else next(iter(repo.remotes))
    branch = repo.branch  # may be empty (detached HEAD)

    # 1. git init
    init_args = ["init"]
    if branch:
        init_args += ["-b", branch]
    result = _git(init_args, cwd=repo_path)
    if result.returncode != 0:
        # Fallback: git init without -b (git < 2.28)
        result = _git(["init"], cwd=repo_path)
        if result.returncode != 0:
            return RehydrationResult(
                relative_path=repo.relative_path,
                success=False,
                message=f"git init failed: {result.stderr.strip()}",
            )
        # Rename default branch if needed
        if branch:
            _git(["branch", "-m", branch], cwd=repo_path)

    _log(f"  {rel_display}: initialized")

    # 2. Add remotes
    for name, urls in repo.remotes.items():
        fetch_url = urls.get("fetch", urls.get("push", ""))
        if not fetch_url:
            continue
        result = _git(["remote", "add", name, fetch_url], cwd=repo_path)
        if result.returncode != 0:
            _log(
                f"  {rel_display}: warning: failed to add remote '{name}': {result.stderr.strip()}"
            )

    _log(f"  {rel_display}: remotes configured ({', '.join(repo.remotes.keys())})")

    # 3. Fetch all remotes
    _log(f"  {rel_display}: fetching from remotes ...")
    result = _git(["fetch", "--all"], cwd=repo_path, timeout=120)
    if result.returncode != 0:
        return RehydrationResult(
            relative_path=repo.relative_path,
            success=False,
            message=f"git fetch failed: {result.stderr.strip()}",
        )
    _log(f"  {rel_display}: fetch complete")

    # 4. Reset to the correct branch/commit
    if branch:
        # Verify the remote branch exists
        ref = f"{primary_remote}/{branch}"
        check = _git(["rev-parse", "--verify", ref], cwd=repo_path)
        if check.returncode != 0:
            # Branch doesn't exist on remote — try finding default branch
            _log(
                f"  {rel_display}: branch '{branch}' not found on '{primary_remote}', trying default ..."
            )
            fallback = _find_default_branch(repo_path, primary_remote)
            if fallback:
                _log(f"  {rel_display}: falling back to '{fallback}'")
                branch = fallback
                ref = f"{primary_remote}/{fallback}"
            else:
                return RehydrationResult(
                    relative_path=repo.relative_path,
                    success=False,
                    message=f"Branch '{repo.branch}' not found on remote, no default branch available",
                )

        result = _git(["reset", ref], cwd=repo_path)
        if result.returncode != 0:
            return RehydrationResult(
                relative_path=repo.relative_path,
                success=False,
                message=f"git reset {ref} failed: {result.stderr.strip()}",
            )

        # 5. Set upstream tracking
        _git(["branch", "--set-upstream-to", ref], cwd=repo_path)
        _log(f"  {rel_display}: HEAD -> {ref} (tracking)")
    else:
        # Detached HEAD — reset to the recorded commit SHA
        if repo.commit:
            check = _git(["rev-parse", "--verify", repo.commit], cwd=repo_path)
            if check.returncode == 0:
                _git(["reset", repo.commit], cwd=repo_path)
                _log(f"  {rel_display}: HEAD -> {repo.commit[:12]} (detached)")
            else:
                _log(
                    f"  {rel_display}: warning: recorded commit {repo.commit[:12]} not found"
                )
        else:
            _log(f"  {rel_display}: warning: no branch or commit recorded")

    # 6. Handle submodules
    if repo.has_submodules:
        _log(f"  {rel_display}: initializing submodules ...")
        result = _git(
            ["submodule", "update", "--init", "--recursive"],
            cwd=repo_path,
            timeout=120,
        )
        if result.returncode == 0:
            _log(f"  {rel_display}: submodules initialized")
        else:
            _log(
                f"  {rel_display}: warning: submodule init failed: {result.stderr.strip()}"
            )
            # Don't fail the whole rehydration — submodule issues are non-fatal

    # 7. Exclude metadata file from git status
    _add_to_git_exclude(repo_path)

    return RehydrationResult(
        relative_path=repo.relative_path,
        success=True,
        message=f"Rehydrated on branch '{branch}'"
        if branch
        else f"Rehydrated at {repo.commit[:12]}",
    )


def rehydrate_all(
    local_path: Path,
    meta: SyncMeta,
    log: Optional[LogCallback] = None,
) -> list[RehydrationResult]:
    """Rehydrate all git repos recorded in metadata.

    Skips repos that are already rehydrated (.git/ exists).
    Returns a list of RehydrationResult, one per repo.
    """
    _log = log or (lambda _: None)

    if not git_available():
        return [
            RehydrationResult(
                relative_path="*",
                success=False,
                message="git is not installed on this system",
            )
        ]

    if not meta.git_repos:
        return []

    results: list[RehydrationResult] = []
    for repo in meta.git_repos:
        result = rehydrate_repo(local_path, repo, log=_log)
        results.append(result)

    return results


# ---------------------------------------------------------------------------
# Convenience: scan + write in one call
# ---------------------------------------------------------------------------


def refresh_git_metadata(
    local_path: Path,
    filters: list[str],
) -> Optional[Path]:
    """Scan for git repos and update the metadata file if anything changed.

    Returns the path written, or None if no update was needed.
    """
    repos = scan_git_repos(local_path, filters)
    if not repos:
        # If there are no repos but a metadata file exists from before,
        # leave it alone (repos may have been removed intentionally,
        # but the metadata might still be useful for other machines).
        return None
    return write_metadata(local_path, repos)
