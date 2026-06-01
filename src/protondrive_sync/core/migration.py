"""Migration engine: upload local files → Proton Drive, then replace with symlink.

Also handles bisync setup (initial --resync) for bisync-mode folders.
"""

from __future__ import annotations

import fnmatch
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .config import AppConfig, FolderMapping
import threading

from .rclone import (
    sync_upload, verify_sync, run_bisync, rclone_mkdir,
    RcloneError, RcloneCancelled,
    rclone_lsjson, RemoteFileInfo,
)
from .symlinks import create_link, remove_link, SymlinkError


class MigrationError(Exception):
    """Raised when a migration fails."""


def _format_size(size_bytes: int) -> str:
    """Format bytes as human-readable string."""
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


@dataclass
class MigrationPlan:
    """Preview of what a mount-mode migration will do."""

    local_path: Path
    remote_subpath: str
    mount_target: Path
    backup_path: Path
    file_count: int
    total_size_bytes: int
    filtered_items: list[str] = field(default_factory=list)
    env_warnings: list[str] = field(default_factory=list)

    @property
    def total_size_human(self) -> str:
        return _format_size(self.total_size_bytes)


@dataclass
class DivergenceReport:
    """Comparison between local and remote content.

    Populated when both sides have files and we need to assess
    whether they're in sync or significantly different.
    """

    local_only_count: int = 0       # files only on local
    remote_only_count: int = 0      # files only on remote
    size_mismatch_count: int = 0    # files on both but different sizes
    local_total_files: int = 0
    remote_total_files: int = 0
    local_total_bytes: int = 0
    remote_total_bytes: int = 0
    is_significant: bool = False    # True if divergence exceeds threshold


@dataclass
class BisyncPlan:
    """Preview of what a bisync setup will do."""

    local_path: Path
    remote_subpath: str
    file_count: int
    total_size_bytes: int
    filtered_items: list[str] = field(default_factory=list)
    env_warnings: list[str] = field(default_factory=list)
    # Pull / divergence fields (populated when remote is queried)
    local_is_empty: bool = False
    remote_file_count: int = 0
    remote_size_bytes: int = 0
    divergence: Optional[DivergenceReport] = None

    @property
    def total_size_human(self) -> str:
        return _format_size(self.total_size_bytes)

    @property
    def remote_size_human(self) -> str:
        return _format_size(self.remote_size_bytes)


@dataclass
class MigrationResult:
    """Outcome of a migration."""

    success: bool
    mapping: FolderMapping
    message: str
    backup_path: Optional[Path] = None
    preserved_items: list[str] = field(default_factory=list)
    cancelled: bool = False


ProgressCallback = Callable[[str], None]


# --- Filter matching ---

def _parse_filter_pattern(rule: str) -> str | None:
    """Extract the glob pattern from an rclone filter rule like '- .git/**'.

    Returns the pattern string, or None if the rule is not an exclude rule.
    """
    rule = rule.strip()
    if rule.startswith("- "):
        return rule[2:].strip()
    return None


def _matches_filter(rel_path: str, filters: list[str]) -> bool:
    """Check if a relative path matches any exclude filter rule.

    Handles both directory patterns (e.g. '.git/**') and file patterns
    (e.g. '*.pyc', '.DS_Store').
    """
    for rule in filters:
        pattern = _parse_filter_pattern(rule)
        if pattern is None:
            continue

        # Pattern like ".git/**" — match the top-level directory name
        if pattern.endswith("/**"):
            dir_name = pattern[:-3]  # ".git"
            # Match if rel_path IS the dir or starts with dir/
            if rel_path == dir_name or rel_path.startswith(dir_name + "/"):
                return True

        # Pattern like "*.pyc" or ".DS_Store" — match the filename
        else:
            filename = Path(rel_path).name
            if fnmatch.fnmatch(filename, pattern):
                return True

    return False


def _find_filtered_toplevel(source: Path, filters: list[str]) -> list[str]:
    """Find top-level items in source that match filter rules.

    Returns relative paths (e.g. ['.git', '__pycache__', 'node_modules']).
    Only checks immediate children — these are the items we'll preserve
    after migration.
    """
    matched = []
    try:
        for entry in sorted(source.iterdir()):
            rel = entry.name
            if _matches_filter(rel, filters):
                matched.append(rel)
    except PermissionError:
        pass
    return matched


def _find_env_files(source: Path) -> list[str]:
    """Find .env files that will be synced (not filtered) and may contain secrets."""
    env_files = []
    env_patterns = (".env", ".env.local", ".env.production", ".env.development",
                    ".env.staging", ".env.test")
    try:
        for entry in source.iterdir():
            name = entry.name.lower()
            if entry.is_file() and (name in env_patterns or name.startswith(".env.")):
                env_files.append(entry.name)
    except PermissionError:
        pass
    return sorted(env_files)


def _scan_directory(path: Path, *, copy_links: bool = False) -> tuple[int, int]:
    """Count files and total bytes in a directory tree.

    When *copy_links* is True, symlinked files are included in the
    count (their targets' sizes are used) to match what rclone will
    actually transfer with ``--copy-links``.
    """
    file_count = 0
    total_bytes = 0
    try:
        for entry in path.rglob("*"):
            if not entry.is_file():
                continue
            # Skip symlinks unless copy_links is enabled
            if entry.is_symlink() and not copy_links:
                continue
            file_count += 1
            try:
                total_bytes += entry.stat().st_size
            except OSError:
                pass
    except PermissionError:
        pass
    return file_count, total_bytes


def plan_migration(
    local_path: str,
    remote_subpath: str,
    config: AppConfig,
) -> MigrationPlan:
    """Create a migration plan (preview) without executing anything."""
    src = Path(local_path).expanduser().absolute()
    if not src.is_dir():
        raise MigrationError(f"Source is not a directory: {src}")

    mount_target = Path(config.mount_point) / remote_subpath
    backup_path = src.parent / f"{src.name}.premigration-backup"

    file_count, total_bytes = _scan_directory(src, copy_links=config.copy_links)
    filtered_items = _find_filtered_toplevel(src, config.filters)
    env_warnings = _find_env_files(src)

    return MigrationPlan(
        local_path=src,
        remote_subpath=remote_subpath,
        mount_target=mount_target,
        backup_path=backup_path,
        file_count=file_count,
        total_size_bytes=total_bytes,
        filtered_items=filtered_items,
        env_warnings=env_warnings,
    )


def compare_local_remote(
    local_path: Path,
    remote_files: list[RemoteFileInfo],
    config: AppConfig,
) -> DivergenceReport:
    """Compare local filesystem with remote file listing.

    Scans local files and compares against the remote lsjson result
    to build a divergence report. Uses config safety thresholds to
    determine whether divergence is significant.
    """
    # Build local file inventory (relative path -> size)
    local_files: dict[str, int] = {}
    local_total_bytes = 0
    if local_path.is_dir():
        for entry in local_path.rglob("*"):
            if not entry.is_file():
                continue
            # Skip symlinks unless copy_links is enabled
            if entry.is_symlink() and not config.copy_links:
                continue
            rel = entry.relative_to(local_path).as_posix()
            if _matches_filter(rel, config.filters):
                continue
            try:
                size = entry.stat().st_size
                local_files[rel] = size
                local_total_bytes += size
            except OSError:
                pass

    # Build remote file inventory
    remote_map: dict[str, int] = {}
    remote_total_bytes = 0
    for rf in remote_files:
        if not rf.is_dir:
            remote_map[rf.path] = rf.size
            remote_total_bytes += rf.size

    all_paths = set(local_files.keys()) | set(remote_map.keys())
    local_only = 0
    remote_only = 0
    size_mismatch = 0

    for path in all_paths:
        in_local = path in local_files
        in_remote = path in remote_map
        if in_local and not in_remote:
            local_only += 1
        elif in_remote and not in_local:
            remote_only += 1
        elif in_local and in_remote:
            if local_files[path] != remote_map[path]:
                size_mismatch += 1

    # Determine significance: use the same thresholds as runtime safety
    total_files = max(len(local_files), len(remote_map), 1)
    diff_count = local_only + remote_only + size_mismatch
    diff_ratio = diff_count / total_files

    is_significant = (
        diff_ratio >= config.size_change_threshold
        and diff_count >= 3  # don't flag trivial counts
    )

    return DivergenceReport(
        local_only_count=local_only,
        remote_only_count=remote_only,
        size_mismatch_count=size_mismatch,
        local_total_files=len(local_files),
        remote_total_files=len(remote_map),
        local_total_bytes=local_total_bytes,
        remote_total_bytes=remote_total_bytes,
        is_significant=is_significant,
    )


def plan_bisync_setup(
    local_path: str,
    remote_subpath: str,
    config: AppConfig,
) -> BisyncPlan:
    """Create a bisync setup plan (preview) without executing anything.

    Unlike mount migration, bisync keeps the local directory as-is —
    no backup, no symlink. Just an initial --resync to establish baseline.

    Handles three scenarios:
    - Local has files, remote empty → standard upload setup
    - Local empty/nonexistent → pull from remote
    - Both have files → check for divergence
    """
    src = Path(local_path).expanduser().absolute()

    # Allow nonexistent or empty dirs (pull-from-remote scenario)
    if src.exists() and not src.is_dir():
        raise MigrationError(f"Path exists but is not a directory: {src}")

    local_is_empty = not src.exists() or not any(src.iterdir())

    # Create dir if needed (so bisync --resync has a target)
    if not src.exists():
        src.mkdir(parents=True, exist_ok=True)

    file_count, total_bytes = _scan_directory(src, copy_links=config.copy_links)
    filtered_items = _find_filtered_toplevel(src, config.filters)
    env_warnings = _find_env_files(src) if not local_is_empty else []

    # Query remote for existing content
    remote_file_count = 0
    remote_size_bytes = 0
    divergence: Optional[DivergenceReport] = None
    try:
        remote_files = rclone_lsjson(
            config.remote_name, remote_subpath, recursive=True,
        )
        remote_file_count = len(remote_files)
        remote_size_bytes = sum(rf.size for rf in remote_files)

        # Check divergence if both sides have content
        if not local_is_empty and remote_file_count > 0:
            divergence = compare_local_remote(src, remote_files, config)
    except Exception:
        # Remote unreachable — plan without remote info
        pass

    return BisyncPlan(
        local_path=src,
        remote_subpath=remote_subpath,
        file_count=file_count,
        total_size_bytes=total_bytes,
        filtered_items=filtered_items,
        env_warnings=env_warnings,
        local_is_empty=local_is_empty,
        remote_file_count=remote_file_count,
        remote_size_bytes=remote_size_bytes,
        divergence=divergence,
    )


_RETRY_DELAYS = (15, 45, 90)  # seconds between automatic retries
_MAX_ATTEMPTS = 1 + len(_RETRY_DELAYS)  # 4 total


def _wait_with_cancel(
    seconds: int, cancel_event: Optional[threading.Event],
) -> bool:
    """Sleep for *seconds*, checking *cancel_event* every 0.5s.

    Returns True if cancelled, False if the wait completed normally.
    """
    for _ in range(seconds * 2):
        if cancel_event is not None and cancel_event.is_set():
            return True
        time.sleep(0.5)
    return False


def execute_bisync_setup(
    plan: BisyncPlan,
    config: AppConfig,
    progress: Optional[ProgressCallback] = None,
    cancel_event: Optional[threading.Event] = None,
) -> MigrationResult:
    """Execute initial bisync setup:
    1. Run rclone bisync --resync to establish baseline
    2. Mark folder as initialized

    Much simpler than mount migration — no backup, no symlink, no file moving.
    The local directory stays exactly as-is.

    Automatically retries on transient failures (up to 4 attempts with
    exponential backoff).  rclone bisync --resync is idempotent — already-
    uploaded files are skipped on retry, so each attempt makes incremental
    progress.

    If cancel_event is provided and set, the operation is aborted.
    """
    mapping = FolderMapping(
        local_path=str(plan.local_path),
        remote_subpath=plan.remote_subpath,
        sync_mode="bisync",
        bisync_initialized=True,
    )
    _log = progress or (lambda _msg: None)

    try:
        if plan.local_is_empty and plan.remote_file_count > 0:
            _log(f"Pulling {plan.remote_file_count} files ({plan.remote_size_human}) from {config.remote_name}:{plan.remote_subpath} ...")
        elif plan.local_is_empty:
            _log(f"Initializing empty sync for {plan.local_path} ...")
        else:
            _log(f"Syncing {plan.file_count} files ({plan.total_size_human}) with {config.remote_name}:{plan.remote_subpath} ...")

        # Ensure both sides exist — bisync requires it
        plan.local_path.mkdir(parents=True, exist_ok=True)
        rclone_mkdir(plan.remote_subpath, config)

        # Check for cancellation before starting the long operation
        if cancel_event is not None and cancel_event.is_set():
            raise RcloneCancelled("Operation cancelled by user")

        # Retry loop — rclone bisync --resync is idempotent
        last_error: Optional[Exception] = None
        for attempt in range(_MAX_ATTEMPTS):
            if attempt > 0:
                delay = _RETRY_DELAYS[attempt - 1]
                _log("")
                _log(f"Retrying in {delay}s (attempt {attempt + 1}/{_MAX_ATTEMPTS}) ...")
                if _wait_with_cancel(delay, cancel_event):
                    raise RcloneCancelled("Operation cancelled by user")

            try:
                _log(f"Running initial bisync (--resync) for {plan.local_path} ...")
                run_bisync(
                    str(plan.local_path),
                    plan.remote_subpath,
                    config,
                    resync=True,
                    progress=_log,
                    cancel_event=cancel_event,
                )
                _log("Initial bisync complete. Folder is now tracked.")
                return MigrationResult(
                    success=True,
                    mapping=mapping,
                    message="Bisync setup complete. Folder synced with Proton Drive.",
                )
            except RcloneCancelled:
                raise  # never retry cancellation
            except (RcloneError, Exception) as exc:
                last_error = exc
                _log(f"Attempt {attempt + 1} failed: {exc}")

        # All attempts exhausted
        return MigrationResult(
            success=False,
            mapping=mapping,
            message=f"Bisync setup failed after {_MAX_ATTEMPTS} attempts: {last_error}",
        )
    except RcloneCancelled:
        _log("Setup cancelled.")
        return MigrationResult(
            success=False,
            mapping=mapping,
            message="Setup cancelled by user.",
            cancelled=True,
        )
    except Exception as exc:
        return MigrationResult(
            success=False,
            mapping=mapping,
            message=f"Bisync setup failed: {exc}",
        )


def execute_migration(
    plan: MigrationPlan,
    config: AppConfig,
    progress: Optional[ProgressCallback] = None,
    cancel_event: Optional[threading.Event] = None,
) -> MigrationResult:
    """Execute a migration:
    1. Upload local files to Proton Drive
    2. Verify the upload
    3. Move local dir to backup
    4. Create symlink from original path to mount subdirectory
    5. On failure: rollback

    If cancel_event is provided and set, the operation is aborted.
    Returns a MigrationResult.
    """
    mapping = FolderMapping(
        local_path=str(plan.local_path),
        remote_subpath=plan.remote_subpath,
    )
    _log = progress or (lambda _msg: None)

    try:
        # Step 0: Scan for git repos and write metadata BEFORE upload.
        # After migration, .git/** is filtered from the FUSE mount, so
        # scanning must happen while the local directory is still real.
        # The metadata file rides along with the upload to remote.
        from .git_meta import scan_git_repos, write_metadata
        git_repos = scan_git_repos(plan.local_path, config.filters)
        if git_repos:
            _log(f"Found {len(git_repos)} git repo(s) — writing metadata for rehydration ...")
            write_metadata(plan.local_path, git_repos)

        # Check for cancellation before starting the long upload
        if cancel_event is not None and cancel_event.is_set():
            raise RcloneCancelled("Operation cancelled by user")

        # Step 1: Upload (with automatic retry — rclone copy is idempotent)
        _log(f"Uploading {plan.file_count} files ({plan.total_size_human}) to {config.remote_name}:{plan.remote_subpath} ...")
        upload_last_error: Optional[Exception] = None
        for attempt in range(_MAX_ATTEMPTS):
            if attempt > 0:
                delay = _RETRY_DELAYS[attempt - 1]
                _log("")
                _log(f"Retrying upload in {delay}s (attempt {attempt + 1}/{_MAX_ATTEMPTS}) ...")
                if _wait_with_cancel(delay, cancel_event):
                    raise RcloneCancelled("Operation cancelled by user")
            try:
                sync_upload(str(plan.local_path), plan.remote_subpath, config,
                             progress=_log, cancel_event=cancel_event)
                upload_last_error = None
                break
            except RcloneCancelled:
                raise
            except (RcloneError, Exception) as exc:
                upload_last_error = exc
                _log(f"Upload attempt {attempt + 1} failed: {exc}")
        if upload_last_error is not None:
            raise MigrationError(
                f"Upload failed after {_MAX_ATTEMPTS} attempts: {upload_last_error}"
            )
        _log("Upload complete.")

        # Step 2: Verify
        _log("Verifying upload integrity ...")
        if not verify_sync(str(plan.local_path), plan.remote_subpath, config):
            raise MigrationError(
                "Verification failed: local and remote content differ after upload."
            )
        _log("Verification passed.")

        # Step 3: Backup local dir
        _log(f"Backing up {plan.local_path} → {plan.backup_path} ...")
        if plan.backup_path.exists():
            raise MigrationError(
                f"Backup path already exists: {plan.backup_path}. "
                f"Remove it first or choose a different source."
            )
        plan.local_path.rename(plan.backup_path)
        _log("Backup created.")

        # Step 4: Create symlink
        _log(f"Creating link {plan.local_path} → {plan.mount_target} ...")
        try:
            create_link(plan.local_path, plan.mount_target)
        except Exception as exc:
            # Rollback: restore backup
            _log("Symlink failed, rolling back ...")
            plan.backup_path.rename(plan.local_path)
            raise MigrationError(f"Symlink creation failed: {exc}") from exc
        _log("Link created successfully.")

        # Step 5: Preserve filtered items (e.g. .git, __pycache__, .venv, etc.)
        preserved = _preserve_filtered_items(plan, config.filters, _log)

        return MigrationResult(
            success=True,
            mapping=mapping,
            message="Migration complete. Backup at: " + str(plan.backup_path),
            backup_path=plan.backup_path,
            preserved_items=preserved,
        )

    except RcloneCancelled:
        _log("Migration cancelled.")
        return MigrationResult(
            success=False,
            mapping=mapping,
            message="Migration cancelled by user.",
            cancelled=True,
        )
    except MigrationError as exc:
        return MigrationResult(
            success=False,
            mapping=mapping,
            message=str(exc),
        )
    except Exception as exc:
        return MigrationResult(
            success=False,
            mapping=mapping,
            message=f"Migration failed: {exc}",
        )


def _preserve_filtered_items(
    plan: MigrationPlan,
    filters: list[str],
    log: ProgressCallback,
) -> list[str]:
    """Move filtered items from the backup into the mount target.

    These items (e.g. .git/, __pycache__/, .venv/) were excluded from the
    rclone upload by filter rules, but need to exist locally for the project
    to function. Since they're filtered, rclone will never try to sync them —
    they exist only on the local VFS cache.

    Returns list of item names that were preserved.
    """
    preserved: list[str] = []
    filtered_items = _find_filtered_toplevel(plan.backup_path, filters)

    if not filtered_items:
        return preserved

    log(f"Preserving {len(filtered_items)} filtered item(s) from backup ...")

    for item_name in filtered_items:
        src_item = plan.backup_path / item_name
        dst_item = plan.mount_target / item_name

        if not src_item.exists():
            continue

        if dst_item.exists():
            log(f"  Skipping {item_name} (already exists at destination)")
            continue

        try:
            # Use shutil.move for cross-device compatibility (backup may be
            # on a different filesystem than the FUSE mount)
            shutil.move(str(src_item), str(dst_item))
            preserved.append(item_name)
            log(f"  Preserved: {item_name}")
        except (OSError, shutil.Error) as exc:
            log(f"  Warning: could not preserve {item_name}: {exc}")

    return preserved


def rollback_migration(
    local_path: str,
    backup_path: Optional[str] = None,
) -> bool:
    """Rollback a migration: remove symlink, restore backup.

    Returns True if rollback succeeded.
    """
    # Use absolute() not resolve() to avoid following dangling symlinks
    src = Path(local_path).expanduser().absolute()
    bak = Path(backup_path).absolute() if backup_path else src.parent / f"{src.name}.premigration-backup"

    if not bak.exists():
        return False

    # Remove symlink if present
    remove_link(src)

    # If src still exists as a real dir (not a link), we can't restore
    if src.exists():
        return False

    bak.rename(src)
    return True


def _merge_newer_from_mount(
    backup: Path,
    mount_target: Path,
    filters: list[str],
    log: ProgressCallback,
) -> int:
    """Merge files from mount that are newer than the backup copy.

    The backup is a snapshot from migration time. Any edits made through
    the mount after migration exist only on the mount (and Proton Drive).
    This function copies newer/new files from the mount into the backup
    so the restored directory has the latest version of everything.

    Skips filtered items (.git/, __pycache__/, etc.) since those are
    handled separately.

    Returns count of files updated/added.
    """
    count = 0
    try:
        for mount_file in mount_target.rglob("*"):
            if not mount_file.is_file():
                continue

            rel = mount_file.relative_to(mount_target)

            # Skip filtered files/dirs
            if _matches_filter(rel.as_posix(), filters):
                continue

            backup_file = backup / rel

            try:
                mount_mtime = mount_file.stat().st_mtime
            except OSError:
                continue

            if not backup_file.exists():
                # New file created after migration
                backup_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(mount_file), str(backup_file))
                count += 1
                log(f"  Added new file: {rel}")
            elif mount_mtime > backup_file.stat().st_mtime:
                # Modified since migration
                shutil.copy2(str(mount_file), str(backup_file))
                count += 1
                log(f"  Updated: {rel}")
    except (OSError, PermissionError) as exc:
        log(f"  Warning: merge scan incomplete: {exc}")

    return count


@dataclass
class TeardownResult:
    """Outcome of a mount folder teardown."""

    success: bool
    message: str
    method: str = ""  # "backup_restored" | "copied_from_mount" | "mapping_only"


def teardown_mount(
    local_path: str,
    mount_point: str,
    remote_subpath: str,
    filters: list[str],
    progress: Optional[ProgressCallback] = None,
) -> TeardownResult:
    """Fully tear down a mount-mode folder mapping:

    1. If .premigration-backup exists: remove symlink, restore backup,
       move any filtered items (.git/ etc.) back from mount into restored dir.
    2. If no backup but symlink exists and mount is active: copy files from
       mount target to a real local directory, then remove symlink.
    3. If neither: just report mapping-only removal.

    Files on Proton Drive are never deleted.
    """
    _log = progress or (lambda _: None)
    src = Path(local_path).expanduser().absolute()
    backup = src.parent / f"{src.name}.premigration-backup"
    mount_target = Path(mount_point) / remote_subpath
    is_symlink = src.is_symlink()

    # --- Strategy 1: Restore from backup ---
    if backup.exists():
        _log(f"Backup found at {backup}")

        # Move filtered items (.git/ etc.) from mount target back to backup
        # before restoring, so they end up in the restored directory
        if is_symlink and mount_target.exists():
            filtered = _find_filtered_toplevel(mount_target, filters)
            for item_name in filtered:
                mount_item = mount_target / item_name
                backup_item = backup / item_name
                # Only recover filtered directories (.git/, .venv/, etc.)
                # Skip filtered files (*.pyc, .DS_Store) — they're regenerable junk
                if not mount_item.is_dir():
                    continue
                if mount_item.exists() and not backup_item.exists():
                    try:
                        shutil.move(str(mount_item), str(backup_item))
                        _log(f"  Recovered {item_name}/ from mount")
                    except (OSError, shutil.Error) as exc:
                        _log(f"  Warning: could not recover {item_name}: {exc}")

            # Merge files that were modified/created after migration.
            # The backup is a snapshot from migration time — any edits made
            # through the mount are only on the mount. Copy newer versions
            # into the backup so nothing is lost on restore.
            _log("Merging newer files from mount into backup ...")
            merged = _merge_newer_from_mount(backup, mount_target, filters, _log)
            if merged:
                _log(f"  {merged} file(s) updated from mount.")
            else:
                _log("  No newer files found.")

        # Remove symlink
        if is_symlink:
            remove_link(src)
            _log("Symlink removed.")

        # Restore backup
        if not src.exists():
            backup.rename(src)
            _log(f"Backup restored to {src}")
            return TeardownResult(
                success=True,
                message=f"Local directory restored from backup. Symlink removed.",
                method="backup_restored",
            )
        else:
            return TeardownResult(
                success=False,
                message=f"Cannot restore: {src} still exists after symlink removal.",
            )

    # --- Strategy 2: Copy from mount (no backup available) ---
    if is_symlink and mount_target.exists():
        _log("No backup found. Copying files from mount to local directory ...")

        # Create a temp dir, copy mount contents into it, then swap
        temp_dir = src.parent / f"{src.name}.teardown-temp"
        try:
            shutil.copytree(
                str(mount_target), str(temp_dir),
                symlinks=False, dirs_exist_ok=False,
            )
            _log(f"  Copied {mount_target} -> {temp_dir}")

            # Remove symlink, rename temp to original path
            remove_link(src)
            temp_dir.rename(src)
            _log(f"Local directory restored from mount copy.")

            return TeardownResult(
                success=True,
                message="Local directory restored by copying from mount. No backup was available.",
                method="copied_from_mount",
            )
        except (OSError, shutil.Error) as exc:
            # Cleanup temp if it exists
            if temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)
            return TeardownResult(
                success=False,
                message=f"Failed to copy from mount: {exc}",
            )

    # --- Strategy 3: Symlink exists but mount is down ---
    if is_symlink:
        _log("Warning: symlink exists but mount is not available and no backup found.")
        _log("Removing symlink. Local directory will be missing until you restore manually.")
        remove_link(src)
        return TeardownResult(
            success=True,
            message="Symlink removed. No backup or mount available to restore files. "
                    "Files remain on Proton Drive.",
            method="mapping_only",
        )

    # --- Strategy 4: No symlink, no backup — just a config entry ---
    return TeardownResult(
        success=True,
        message="Mapping removed. No symlink or backup to clean up.",
        method="mapping_only",
    )


def cleanup_backup(backup_path: str) -> bool:
    """Remove a migration backup directory after user confirms everything works."""
    bak = Path(backup_path)
    if not bak.exists():
        return False
    shutil.rmtree(bak)
    return True
