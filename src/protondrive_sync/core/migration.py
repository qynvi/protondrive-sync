"""CLI-backed initial setup for synced folders."""

from __future__ import annotations

import fnmatch
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable, Optional

from .config import AppConfig, FolderMapping, effective_filters, normalize_symlink_mode
import threading

from .locks import acquire_remote_lease, local_folder_lock, release_remote_lease
from .proton_cli import ProtonDriveCLI, ProtonError, RemoteNode
from .setup_session import (
    create_setup_session,
    find_resumable_setup_session,
    update_setup_session,
)
from .state import folder_id, mark_folder_status, update_folder_state, utc_now


class MigrationError(Exception):
    """Raised when a migration fails."""


class MigrationCancelled(MigrationError):
    """Raised when a long-running setup is cancelled by the user."""


LINK_BLOB_SUFFIX = ".rclonelink"


def _setup_remote_storage_path(
    folder: FolderMapping, path: str, *, kind: str | None = None
) -> str:
    """Return the app-relative remote storage path used during setup."""
    stored = (
        f"{path}{LINK_BLOB_SUFFIX}"
        if folder.symlink_mode == "preserve" and kind == "symlink"
        else path
    )
    return f"{folder.remote_subpath.strip('/')}/{stored}".strip("/")


def _format_size(size_bytes: int) -> str:
    """Format bytes as human-readable string."""
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


@dataclass
class DivergenceReport:
    """Comparison between local and remote content.

    Populated when both sides have files and we need to assess
    whether they're in sync or significantly different.
    """

    local_only_count: int = 0  # files only on local
    remote_only_count: int = 0  # files only on remote
    size_mismatch_count: int = 0  # files on both but different sizes
    local_total_files: int = 0
    remote_total_files: int = 0
    local_total_bytes: int = 0
    remote_total_bytes: int = 0
    is_significant: bool = False  # True if divergence exceeds threshold


@dataclass
class BisyncPlan:
    """Preview of what a bisync setup will do."""

    local_path: Path
    remote_subpath: str
    file_count: int
    total_size_bytes: int
    filtered_items: list[str] = field(default_factory=list)
    env_warnings: list[str] = field(default_factory=list)
    symlink_mode: str = "preserve"
    # Pull / divergence fields (populated when remote is queried)
    local_is_empty: bool = False
    remote_file_count: int = 0
    remote_size_bytes: int = 0
    remote_listing_error: Optional[str] = None
    remote_detail_error: Optional[str] = None
    remote_listing_limited: bool = False
    divergence: Optional[DivergenceReport] = None
    symlink_count: int = 0
    external_symlink_count: int = 0
    symlink_samples: list[str] = field(default_factory=list)

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
    """Extract the glob pattern from an app filter rule like '- .git/**'.

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

        if fnmatch.fnmatch(rel_path, pattern):
            return True

        # Pattern like ".git/**" or "**/.git/**" — match directories.
        if pattern.endswith("/**"):
            dir_name = pattern[:-3].rstrip("/")
            if dir_name.startswith("**/"):
                segment_pattern = dir_name[3:]
                if any(
                    fnmatch.fnmatch(part, segment_pattern)
                    for part in Path(rel_path).parts
                ):
                    return True
            # Match if rel_path IS the top-level dir or starts with dir/
            elif rel_path == dir_name or rel_path.startswith(dir_name + "/"):
                return True

        # Pattern like "*.pyc" or ".DS_Store" — match the filename
        else:
            filename = Path(rel_path).name
            if fnmatch.fnmatch(filename, pattern):
                return True

    return False


def _walk_unfiltered(
    path: Path,
    filters: Optional[list[str]] = None,
):
    """Yield os.walk entries while pruning filtered directories."""
    root = path.absolute()
    for dirpath, dirnames, filenames in os.walk(
        root,
        topdown=True,
        followlinks=False,
        onerror=lambda _exc: None,
    ):
        current = Path(dirpath)
        kept_dirs: list[str] = []
        for dirname in dirnames:
            rel = (current / dirname).relative_to(root).as_posix()
            if filters and _matches_filter(rel, filters):
                continue
            kept_dirs.append(dirname)
        dirnames[:] = kept_dirs
        yield current, dirnames, filenames


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


def _find_env_files(source: Path, filters: Optional[list[str]] = None) -> list[str]:
    """Find .env files that will be synced (not filtered) and may contain secrets."""
    env_files: list[str] = []
    env_patterns = (
        ".env",
        ".env.local",
        ".env.production",
        ".env.development",
        ".env.staging",
        ".env.test",
    )
    try:
        root = source.absolute()
        for current, _dirnames, filenames in _walk_unfiltered(source, filters):
            for filename in filenames:
                name = filename.lower()
                if name in env_patterns or name.startswith(".env."):
                    env_files.append((current / filename).relative_to(root).as_posix())
    except PermissionError:
        pass
    return sorted(env_files)


def _scan_directory(
    path: Path,
    filters: Optional[list[str]] = None,
    *,
    symlink_mode: str = "preserve",
) -> tuple[int, int]:
    """Count files and total bytes in a directory tree.

    ``preserve`` counts each symlink as a small metadata file containing its
    target. ``copy`` follows file symlink targets. ``skip`` ignores symlinks.
    """
    symlink_mode = normalize_symlink_mode(symlink_mode)
    file_count = 0
    total_bytes = 0
    try:
        root = path.absolute()
        for current, dirnames, filenames in _walk_unfiltered(path, filters):
            for dirname in dirnames:
                entry = current / dirname
                if not entry.is_symlink():
                    continue
                if symlink_mode == "skip":
                    continue
                file_count += 1
                if symlink_mode == "preserve":
                    try:
                        total_bytes += len(os.readlink(entry).encode("utf-8"))
                    except OSError:
                        pass
                elif symlink_mode == "copy":
                    # os.walk does not descend symlinked dirs when followlinks
                    # is false; warn elsewhere rather than estimating a copied
                    # subtree recursively here.
                    pass
            for filename in filenames:
                entry = current / filename
                rel = entry.relative_to(root).as_posix()
                if filters and _matches_filter(rel, filters):
                    continue
                if entry.is_symlink():
                    if symlink_mode == "skip":
                        continue
                    file_count += 1
                    if symlink_mode == "preserve":
                        try:
                            total_bytes += len(os.readlink(entry).encode("utf-8"))
                        except OSError:
                            pass
                    elif symlink_mode == "copy":
                        try:
                            total_bytes += entry.stat().st_size
                        except OSError:
                            pass
                    continue
                file_count += 1
                try:
                    total_bytes += entry.stat().st_size
                except OSError:
                    pass
    except PermissionError:
        pass
    return file_count, total_bytes


def _scan_symlinks(
    path: Path,
    filters: list[str],
    *,
    sample_limit: int = 8,
) -> tuple[int, int, list[str]]:
    """Count symlinks under *path*, noting links that point outside *path*."""
    total = 0
    external = 0
    samples: list[str] = []
    if not path.is_dir():
        return total, external, samples

    root = path.absolute()
    try:
        for current, dirnames, filenames in _walk_unfiltered(path, filters):
            for name in [*dirnames, *filenames]:
                entry = current / name
                if not entry.is_symlink():
                    continue
                rel = entry.relative_to(root).as_posix()
                total += 1
                try:
                    target = entry.resolve(strict=False)
                    try:
                        target.relative_to(root)
                    except ValueError:
                        external += 1
                except OSError:
                    external += 1
                if len(samples) < sample_limit:
                    samples.append(rel)
    except PermissionError:
        pass
    return total, external, samples


def compare_local_remote(
    local_path: Path,
    remote_files: list[RemoteNode],
    config: AppConfig,
    *,
    symlink_mode: str | None = None,
) -> DivergenceReport:
    """Compare local filesystem with remote file listing.

    Scans local files and compares against the remote lsjson result
    to build a divergence report. Uses config safety thresholds to
    determine whether divergence is significant.
    """
    # Build local file inventory (relative path -> size)
    mode = normalize_symlink_mode(symlink_mode or config.symlink_mode)
    local_files: dict[str, int] = {}
    local_total_bytes = 0
    if local_path.is_dir():
        root = local_path.absolute()
        for current, dirnames, filenames in _walk_unfiltered(
            local_path, config.filters
        ):
            for name in [*dirnames, *filenames]:
                entry = current / name
                rel = entry.relative_to(root).as_posix()
                if _matches_filter(rel, config.filters):
                    continue
                if entry.is_symlink():
                    if mode == "skip":
                        continue
                    try:
                        size = (
                            len(os.readlink(entry).encode("utf-8"))
                            if mode == "preserve"
                            else entry.stat().st_size
                        )
                    except OSError:
                        continue
                    local_files[rel] = size
                    local_total_bytes += size
                    continue
                if not entry.is_file():
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
    *,
    symlink_mode: str | None = None,
) -> BisyncPlan:
    """Create a bisync setup plan (preview) without executing anything.

    CLI setup keeps the local directory as-is. It verifies local/remote
    metadata and seeds the app inventory baseline.

    Handles three scenarios:
    - Local has files, remote empty → standard upload setup
    - Local empty/nonexistent → pull from remote
    - Both have files → check for divergence
    """
    src = Path(local_path).expanduser().absolute()
    mode = normalize_symlink_mode(symlink_mode or config.symlink_mode)

    # Allow nonexistent or empty dirs (pull-from-remote scenario)
    if src.exists() and not src.is_dir():
        raise MigrationError(f"Path exists but is not a directory: {src}")

    local_is_empty = not src.exists() or not any(src.iterdir())

    # Create dir if needed for empty-local setup/download targets.
    if not src.exists():
        src.mkdir(parents=True, exist_ok=True)

    file_count, total_bytes = _scan_directory(
        src,
        config.filters,
        symlink_mode=mode,
    )
    filtered_items = _find_filtered_toplevel(src, config.filters)
    env_warnings = _find_env_files(src, config.filters) if not local_is_empty else []
    symlink_count, external_symlink_count, symlink_samples = _scan_symlinks(
        src,
        config.filters,
    )

    # Query remote for existing content. The Proton Drive CLI does not expose a
    # recursive size primitive, so setup preview uses the same app-side walk that
    # initial baseline creation uses. Missing remote targets list as empty.
    remote_file_count = 0
    remote_size_bytes = 0
    remote_listing_error: Optional[str] = None
    remote_detail_error: Optional[str] = None
    remote_listing_limited = False
    divergence: Optional[DivergenceReport] = None
    remote_files: Optional[list[RemoteNode]] = None
    remote_files_for_compare: Optional[list[RemoteNode]] = None
    try:
        remote_files = ProtonDriveCLI(config).list_recursive(remote_subpath)
        filtered_remote = _remote_map_for_inventory(
            remote_subpath,
            remote_files,
            symlink_mode=mode,
            filters=config.filters,
        )
        remote_file_count = len(filtered_remote)
        remote_size_bytes = sum(node.size for node in filtered_remote.values())
        remote_files_for_compare = [
            RemoteNode(
                path=rel,
                size=node.size,
                is_dir=node.is_dir,
                sha1=node.sha1,
                modtime=node.modtime,
                uid=node.uid,
                name=node.name,
            )
            for rel, node in filtered_remote.items()
        ]
        if remote_file_count > _REMOTE_DETAIL_LIMIT:
            remote_listing_limited = True
    except ProtonError as exc:
        remote_listing_error = str(exc)

    # Check divergence if both sides have content and a detailed listing exists
    if not local_is_empty and remote_files_for_compare and not remote_listing_limited:
        divergence = compare_local_remote(
            src, remote_files_for_compare, config, symlink_mode=mode
        )

    return BisyncPlan(
        local_path=src,
        remote_subpath=remote_subpath,
        file_count=file_count,
        total_size_bytes=total_bytes,
        filtered_items=filtered_items,
        env_warnings=env_warnings,
        symlink_mode=mode,
        local_is_empty=local_is_empty,
        remote_file_count=remote_file_count,
        remote_size_bytes=remote_size_bytes,
        remote_listing_error=remote_listing_error,
        remote_detail_error=remote_detail_error,
        remote_listing_limited=remote_listing_limited,
        divergence=divergence,
        symlink_count=symlink_count,
        external_symlink_count=external_symlink_count,
        symlink_samples=symlink_samples,
    )


_RETRY_DELAYS = (15, 45, 90)  # seconds between automatic retries
_MAX_ATTEMPTS = 1 + len(_RETRY_DELAYS)  # 4 total
_REMOTE_DETAIL_LIMIT = 5000  # max files for preview-time divergence details
_LARGE_INITIAL_UPLOAD_BYTES = 50 * 1024**3


def _is_large_initial_upload(total_size_bytes: int) -> bool:
    return total_size_bytes >= _LARGE_INITIAL_UPLOAD_BYTES


def _download_temp_path(local_path: Path, session_id: str) -> Path:
    """Return the sibling temp directory used for staged downloads."""
    return local_path.with_name(
        f"{local_path.name}.protondrive-download-tmp.{session_id}"
    )


def _is_empty_dir(path: Path) -> bool:
    """Return True if path is an existing empty directory."""
    return path.is_dir() and not any(path.iterdir())


def _ensure_download_target_empty(local_path: Path) -> None:
    """Require a final download target to be missing or empty."""
    if not local_path.exists():
        local_path.parent.mkdir(parents=True, exist_ok=True)
        return
    if not local_path.is_dir():
        raise MigrationError(
            f"Download target exists but is not a directory: {local_path}"
        )
    if not _is_empty_dir(local_path):
        raise MigrationError(
            f"Download target is no longer empty: {local_path}. "
            "Refusing to merge remote content into an existing local tree."
        )


def _check_download_space(
    local_path: Path, remote_size_bytes: int, config: AppConfig
) -> None:
    """Ensure enough free disk space exists for staged download."""
    parent = (
        local_path.parent if local_path.parent.exists() else local_path.parent.parent
    )
    if not parent.exists():
        parent = Path.cwd()
    required = int(
        remote_size_bytes * (1 + max(config.download_space_headroom_pct, 0) / 100)
    )
    free = shutil.disk_usage(parent).free
    if free < required:
        raise MigrationError(
            "Not enough disk space for staged download: "
            f"need {_format_size(required)} including {config.download_space_headroom_pct}% headroom, "
            f"available {_format_size(free)}."
        )


def _publish_download_temp(temp_path: Path, final_path: Path) -> None:
    """Atomically publish a verified sibling temp download into final path."""
    _ensure_download_target_empty(final_path)
    if final_path.exists():
        final_path.rmdir()
    temp_path.rename(final_path)


def _wait_with_cancel(
    seconds: int,
    cancel_event: Optional[threading.Event],
) -> bool:
    """Sleep for *seconds*, checking *cancel_event* every 0.5s.

    Returns True if cancelled, False if the wait completed normally.
    """
    for _ in range(seconds * 2):
        if cancel_event is not None and cancel_event.is_set():
            return True
        time.sleep(0.5)
    return False


def _summarize_verify_report(report: object) -> str:
    """Return a concise human summary for a VerifyReport-like object."""
    missing_on_dst = len(getattr(report, "missing_on_dst", []))
    missing_on_src = len(getattr(report, "missing_on_src", []))
    different = len(getattr(report, "different", []))
    errors = len(getattr(report, "errors", []))
    return (
        f"missing_on_dst={missing_on_dst}, missing_on_src={missing_on_src}, "
        f"different={different}, errors={errors}"
    )


def _raise_if_cancelled(cancel_event: Optional[threading.Event]) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise MigrationCancelled("Operation cancelled by user")


def _remote_rel_under_folder(remote_subpath: str, node_path: str) -> str:
    """Convert a backend app-relative node path to a folder-relative path."""
    base = remote_subpath.strip("/")
    clean = node_path.strip("/")
    if base and clean == base:
        return ""
    if base and clean.startswith(base + "/"):
        return clean[len(base) + 1 :]
    return clean


def _remote_inventory_rel(
    remote_subpath: str, node: RemoteNode, *, symlink_mode: str
) -> str:
    rel = _remote_rel_under_folder(remote_subpath, node.path)
    if symlink_mode == "preserve" and rel.endswith(LINK_BLOB_SUFFIX):
        return rel[: -len(LINK_BLOB_SUFFIX)]
    return rel


def _remote_map_for_inventory(
    remote_subpath: str,
    remote_nodes: list[RemoteNode],
    *,
    symlink_mode: str,
    filters: list[str],
) -> dict[str, RemoteNode]:
    mapped: dict[str, RemoteNode] = {}
    for node in remote_nodes:
        if node.is_dir:
            continue
        rel = _remote_inventory_rel(remote_subpath, node, symlink_mode=symlink_mode)
        if not rel or _matches_filter(rel, filters):
            continue
        mapped[rel] = node
    return mapped


def _expected_upload_signature(
    local_path: Path,
    rel_path: str,
    local: InventoryEntry,
) -> tuple[int | None, str | None]:
    """Return expected remote plaintext (size, sha1) for a local inventory row."""
    if local.kind == "symlink":
        target = local.link_target
        if target is None:
            try:
                target = os.readlink(local_path / rel_path)
            except OSError:
                return None, None
        import hashlib

        blob = target.encode("utf-8")
        return len(blob), hashlib.sha1(blob).hexdigest()
    sha1 = local.local_sha1
    if sha1 is None:
        from .inventory import sha1_file

        try:
            sha1 = sha1_file(local_path / rel_path)
        except OSError:
            sha1 = None
    return local.local_size, sha1


def _remote_matches_local(
    local_path: Path, rel_path: str, local: InventoryEntry, node: RemoteNode | None
) -> bool:
    if node is None:
        return False
    expected_size, expected_sha1 = _expected_upload_signature(
        local_path, rel_path, local
    )
    if expected_size is not None and node.size != expected_size:
        return False
    if expected_sha1 and node.sha1 and node.sha1.lower() != expected_sha1.lower():
        return False
    return True


def _stage_upload_source(
    stack: object,
    local_path: Path,
    rel_path: str,
    local: InventoryEntry,
    remote_rel: str,
) -> str:
    """Return a local file path with the basename required by the CLI upload."""
    desired_name = PurePosixPath(remote_rel.strip("/")).name
    source = local_path / rel_path
    if local.kind == "symlink":
        tmp = stack.enter_context(
            tempfile.TemporaryDirectory(prefix="protondrive-setup-link-")
        )
        staged = Path(tmp) / desired_name
        target = (
            local.link_target if local.link_target is not None else os.readlink(source)
        )
        staged.write_text(target, encoding="utf-8")
        return str(staged)
    if source.name == desired_name:
        return str(source)
    tmp = stack.enter_context(
        tempfile.TemporaryDirectory(prefix="protondrive-setup-stage-")
    )
    staged = Path(tmp) / desired_name
    shutil.copy2(source, staged)
    return str(staged)


def _verify_remote_matches_local_tree(
    backend: ProtonDriveCLI,
    mapping: FolderMapping,
    config: AppConfig,
    local_entries: dict[str, InventoryEntry],
) -> list[RemoteNode]:
    """Verify remote metadata exactly matches the local setup inventory."""
    remote_nodes = backend.list_recursive(mapping.remote_subpath)
    remote_by_rel = _remote_map_for_inventory(
        mapping.remote_subpath,
        remote_nodes,
        symlink_mode=mapping.symlink_mode,
        filters=effective_filters(config, mapping),
    )
    missing_or_bad: list[str] = []
    for rel, local in local_entries.items():
        if not _remote_matches_local(
            Path(mapping.local_path), rel, local, remote_by_rel.get(rel)
        ):
            missing_or_bad.append(rel)
    extra = sorted(set(remote_by_rel) - set(local_entries))
    if missing_or_bad or extra:
        parts = []
        if missing_or_bad:
            parts.append(f"mismatch/missing={missing_or_bad[:5]}")
        if extra:
            parts.append(f"remote_extra={extra[:5]}")
        raise MigrationError("Initial upload verification failed: " + ", ".join(parts))
    return remote_nodes


def _upload_initial_tree_cli(
    backend: ProtonDriveCLI,
    mapping: FolderMapping,
    config: AppConfig,
    *,
    progress: ProgressCallback,
    cancel_event: Optional[threading.Event],
) -> list[RemoteNode]:
    """Upload local tree to an empty/resumable remote using verified metadata."""
    from .inventory import scan_local_inventory

    local_path = Path(mapping.local_path)
    local_entries = scan_local_inventory(mapping, config, hash_all=True)
    backend.ensure_dir(mapping.remote_subpath)
    remote_nodes = backend.list_recursive(mapping.remote_subpath)
    remote_by_rel = _remote_map_for_inventory(
        mapping.remote_subpath,
        remote_nodes,
        symlink_mode=mapping.symlink_mode,
        filters=effective_filters(config, mapping),
    )
    to_upload: dict[str, InventoryEntry] = {}
    for rel, local in local_entries.items():
        _raise_if_cancelled(cancel_event)
        existing = remote_by_rel.get(rel)
        if existing is not None:
            if _remote_matches_local(local_path, rel, local, existing):
                continue
            raise MigrationError(
                f"Remote path already exists with different content during setup: {rel}"
            )
        to_upload[rel] = local

    if not to_upload:
        progress(
            "Initial upload skipped: all local files already match remote metadata."
        )
        return _verify_remote_matches_local_tree(
            backend, mapping, config, local_entries
        )

    from contextlib import ExitStack
    from collections import defaultdict

    groups: dict[str, list[str]] = defaultdict(list)
    sizes: dict[str, int] = defaultdict(int)
    with ExitStack() as stack:
        for rel, local in to_upload.items():
            _raise_if_cancelled(cancel_event)
            remote_rel = _setup_remote_storage_path(mapping, rel, kind=local.kind)
            parent = str(PurePosixPath(remote_rel.strip("/")).parent)
            if parent == ".":
                parent = ""
            groups[parent].append(
                _stage_upload_source(stack, local_path, rel, local, remote_rel)
            )
            sizes[parent] += local.local_size or 0
        for parent, sources in groups.items():
            _raise_if_cancelled(cancel_event)
            progress(
                f"Uploading {len(sources)} file(s) into {parent or '(remote root)'} ..."
            )
            backend.upload_many(
                sources, parent, replace=False, total_size_hint=sizes[parent]
            )

    return _verify_remote_matches_local_tree(backend, mapping, config, local_entries)


def _download_initial_tree_cli(
    backend: ProtonDriveCLI,
    mapping: FolderMapping,
    config: AppConfig,
    temp_path: Path,
    *,
    progress: ProgressCallback,
    cancel_event: Optional[threading.Event],
) -> list[RemoteNode]:
    """Download remote tree into a staged local temp directory and verify it."""
    from .inventory import scan_local_inventory

    filters = effective_filters(config, mapping)
    remote_nodes = backend.list_recursive(mapping.remote_subpath)
    remote_by_rel = _remote_map_for_inventory(
        mapping.remote_subpath,
        remote_nodes,
        symlink_mode=mapping.symlink_mode,
        filters=filters,
    )
    temp_path.mkdir(parents=True, exist_ok=True)
    for rel, node in sorted(remote_by_rel.items()):
        _raise_if_cancelled(cancel_event)
        target = temp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if mapping.symlink_mode == "preserve" and node.path.endswith(LINK_BLOB_SUFFIX):
            if os.path.lexists(target):
                target.unlink()
            os.symlink(backend.download_text(node.path), target)
        else:
            backend.download(
                node.path,
                str(target),
                claimed_modtime=node.modtime,
                size_hint=node.size,
            )

    temp_mapping = FolderMapping(
        local_path=str(temp_path),
        remote_subpath=mapping.remote_subpath,
        symlink_mode=mapping.symlink_mode,
        bisync_initialized=True,
    )
    local_entries = scan_local_inventory(temp_mapping, config, hash_all=True)
    missing_or_bad: list[str] = []
    for rel, node in remote_by_rel.items():
        local = local_entries.get(rel)
        if local is None:
            missing_or_bad.append(rel)
            continue
        if mapping.symlink_mode == "preserve" and node.path.endswith(LINK_BLOB_SUFFIX):
            expected_size, expected_sha1 = _expected_upload_signature(
                temp_path, rel, local
            )
            if expected_size != node.size or (
                node.sha1
                and expected_sha1
                and node.sha1.lower() != expected_sha1.lower()
            ):
                missing_or_bad.append(rel)
        elif local.local_size != node.size or (
            node.sha1
            and local.local_sha1
            and local.local_sha1.lower() != node.sha1.lower()
        ):
            missing_or_bad.append(rel)
    extra = sorted(set(local_entries) - set(remote_by_rel))
    if missing_or_bad or extra:
        parts = []
        if missing_or_bad:
            parts.append(f"mismatch/missing={missing_or_bad[:5]}")
        if extra:
            parts.append(f"local_extra={extra[:5]}")
        raise MigrationError("Staged download verification failed: " + ", ".join(parts))
    progress("Staged download verification passed.")
    return remote_nodes


def _seed_inventory_from_verified_state(
    config: AppConfig,
    mapping: FolderMapping,
    remote_nodes: list[RemoteNode],
) -> int:
    """Replace folder inventory with the verified local+remote baseline."""
    from .inventory import (
        InventoryEntry,
        connect_inventory,
        init_inventory,
        scan_local_inventory,
        upsert_inventory_entries,
    )

    fid = folder_id(mapping.local_path, mapping.remote_subpath)
    local_entries = scan_local_inventory(mapping, config, hash_all=True)
    remote_by_rel = _remote_map_for_inventory(
        mapping.remote_subpath,
        remote_nodes,
        symlink_mode=mapping.symlink_mode,
        filters=effective_filters(config, mapping),
    )
    now = utc_now()
    entries: list[InventoryEntry] = []
    for rel, local in sorted(local_entries.items()):
        node = remote_by_rel.get(rel)
        if node is None:
            raise MigrationError(
                f"Cannot seed inventory; remote metadata missing for {rel}"
            )
        entries.append(
            InventoryEntry(
                folder_id=fid,
                path=rel,
                kind=local.kind,
                local_size=local.local_size,
                local_mtime_ns=local.local_mtime_ns,
                local_sha1=local.local_sha1,
                remote_size=node.size,
                remote_sha1=node.sha1,
                remote_modtime=node.modtime,
                link_target=local.link_target,
                last_verified_at=now,
                last_changed_at=now,
                last_source="setup",
                deleted_at=None,
            )
        )
    init_inventory(config)
    with connect_inventory(config) as conn:
        conn.execute("DELETE FROM folder_inventory WHERE folder_id=?", (fid,))
    upsert_inventory_entries(config, entries)
    return len(entries)


def execute_bisync_setup(
    plan: BisyncPlan,
    config: AppConfig,
    progress: Optional[ProgressCallback] = None,
    cancel_event: Optional[threading.Event] = None,
) -> MigrationResult:
    """Execute initial bisync setup:
    1. Copy missing data with Proton CLI where needed
    2. Verify local vs remote sha1/size metadata
    3. Seed the app inventory baseline and mark folder initialized

    The local directory stays exactly as-is unless this is a verified
    empty-local download setup.

    Automatically retries on transient failures. Matching already-uploaded
    files are skipped on retry, so each attempt makes incremental progress.

    If cancel_event is provided and set, the operation is aborted.
    """
    mapping = FolderMapping(
        local_path=str(plan.local_path),
        remote_subpath=plan.remote_subpath,
        symlink_mode=plan.symlink_mode,
        bisync_initialized=True,
    )
    _log = progress or (lambda _msg: None)
    backend = ProtonDriveCLI(config)
    lock = None
    lease = None
    session = None
    download_setup = plan.local_is_empty and plan.remote_file_count > 0
    setup_direction = "download" if download_setup else "upload"
    remote_nodes: list[RemoteNode] = []

    try:
        lock = local_folder_lock(mapping).acquire()
        mark_folder_status(config, mapping, "syncing")
        version = backend.probe()
        update_folder_state(config, mapping, backend_version=version)
        lease = acquire_remote_lease(
            mapping, config, operation=f"setup-{setup_direction}"
        )

        filters = effective_filters(config, mapping)
        from .path_safety import scan_path_safety

        path_report = scan_path_safety(plan.local_path, filters)
        for issue in path_report.issues[:10]:
            prefix = "Blocking path issue" if issue.blocking else "Path warning"
            _log(f"{prefix}: {issue.path}: {issue.message}")
        if path_report.blocking_issues:
            raise MigrationError(
                f"Path preflight blocked setup: {len(path_report.blocking_issues)} blocking issue(s)."
            )

        if plan.remote_listing_error:
            raise MigrationError(
                "Remote listing state is unknown; refusing to baseline or upload. "
                f"Proton CLI error: {plan.remote_listing_error}"
            )

        session = find_resumable_setup_session(
            config,
            local_path=str(plan.local_path),
            remote_subpath=plan.remote_subpath,
            direction=setup_direction,
            symlink_mode=plan.symlink_mode,
            filters=filters,
        )
        if not plan.local_is_empty and plan.remote_file_count > 0 and session is None:
            raise MigrationError(
                "Remote is non-empty and no matching setup session exists; "
                "fresh upload setup requires a missing/empty remote."
            )
        if (
            not plan.local_is_empty
            and plan.remote_file_count > 0
            and session is not None
            and session.remote_initial_state not in ("missing", "empty")
        ):
            raise MigrationError(
                "Remote is non-empty and the resumable setup session is not an "
                "upload-to-empty session; refusing to baseline automatically."
            )
        if session is None:
            remote_initial_state = "nonempty" if plan.remote_file_count > 0 else "empty"
            session = create_setup_session(
                config,
                local_path=str(plan.local_path),
                remote_subpath=plan.remote_subpath,
                direction=setup_direction,
                remote_initial_state=remote_initial_state,
                symlink_mode=plan.symlink_mode,
                filters=filters,
            )
        update_folder_state(config, mapping, setup_session_id=session.id)

        if plan.local_is_empty and plan.remote_file_count > 0:
            _log(
                f"Pulling {plan.remote_file_count} files ({plan.remote_size_human}) from Proton Drive:{plan.remote_subpath} ..."
            )
        elif plan.local_is_empty:
            _log(f"Initializing empty sync for {plan.local_path} ...")
        else:
            _log(
                f"Syncing {plan.file_count} files ({plan.total_size_human}) with Proton Drive:{plan.remote_subpath} ..."
            )

        if download_setup:
            _ensure_download_target_empty(plan.local_path)
            _check_download_space(plan.local_path, plan.remote_size_bytes, config)
        else:
            plan.local_path.mkdir(parents=True, exist_ok=True)
            backend.ensure_dir(plan.remote_subpath)

        # Check for cancellation before starting the long operation
        if cancel_event is not None and cancel_event.is_set():
            raise MigrationCancelled("Operation cancelled by user")

        if download_setup:
            update_setup_session(config, session, stage="copying")
            temp_path = _download_temp_path(plan.local_path, session.id)
            if temp_path.exists() and not temp_path.is_dir():
                raise MigrationError(
                    f"Download temp path exists but is not a directory: {temp_path}"
                )
            temp_path.mkdir(parents=True, exist_ok=True)
            large_download = _is_large_initial_upload(plan.remote_size_bytes)
            _log("")
            _log(f"Downloading remote into staged temp directory: {temp_path}")
            _log("The final local folder will remain empty until verification passes.")
            if large_download:
                _log(
                    "Large download mode enabled: conservative large-file pass, then parallel small-file pass."
                )
            download_last_error: Optional[Exception] = None
            for attempt in range(_MAX_ATTEMPTS):
                if attempt > 0:
                    delay = _RETRY_DELAYS[attempt - 1]
                    _log("")
                    _log(
                        f"Retrying staged download in {delay}s (attempt {attempt + 1}/{_MAX_ATTEMPTS}) ..."
                    )
                    if _wait_with_cancel(delay, cancel_event):
                        raise MigrationCancelled("Operation cancelled by user")
                try:
                    remote_nodes = _download_initial_tree_cli(
                        backend,
                        mapping,
                        config,
                        temp_path,
                        progress=_log,
                        cancel_event=cancel_event,
                    )
                    download_last_error = None
                    break
                except MigrationCancelled:
                    raise
                except (ProtonError, MigrationError) as exc:
                    download_last_error = exc
                    _log(f"Staged download attempt {attempt + 1} failed: {exc}")
                except Exception as exc:
                    download_last_error = exc
                    _log(f"Staged download attempt {attempt + 1} failed: {exc}")
            if download_last_error is not None:
                raise MigrationError(
                    f"Staged download failed after {_MAX_ATTEMPTS} attempts: {download_last_error}"
                )

            update_setup_session(config, session, stage="verifying")
            _log(f"Publishing verified download to {plan.local_path} ...")
            _publish_download_temp(temp_path, plan.local_path)
            _log("Verified download published.")

        # For local-to-empty setup, first finish a one-way copy from
        # local to remote. Proton CLI upload is idempotent and skips completed files,
        # so this makes large initial syncs resumable before bisync builds its
        # baseline.
        elif not plan.local_is_empty and (
            plan.remote_file_count == 0
            or session.remote_initial_state in ("missing", "empty")
        ):
            update_setup_session(config, session, stage="copying")
            large_upload = _is_large_initial_upload(plan.total_size_bytes)
            _log("")
            _log("Remote is empty or setup-resumable; uploading local files.")
            _log("Already-uploaded files with matching sha1/size will be skipped.")
            if large_upload:
                _log(
                    "Large upload mode enabled: conservative large-file pass, then parallel small-file pass."
                )
            upload_last_error: Optional[Exception] = None
            for attempt in range(_MAX_ATTEMPTS):
                if attempt > 0:
                    delay = _RETRY_DELAYS[attempt - 1]
                    _log("")
                    _log(
                        f"Retrying resume upload in {delay}s (attempt {attempt + 1}/{_MAX_ATTEMPTS}) ..."
                    )
                    if _wait_with_cancel(delay, cancel_event):
                        raise MigrationCancelled("Operation cancelled by user")
                try:
                    remote_nodes = _upload_initial_tree_cli(
                        backend,
                        mapping,
                        config,
                        progress=_log,
                        cancel_event=cancel_event,
                    )
                    upload_last_error = None
                    break
                except MigrationCancelled:
                    raise
                except (ProtonError, MigrationError) as exc:
                    upload_last_error = exc
                    _log(f"Resume upload attempt {attempt + 1} failed: {exc}")
                except Exception as exc:
                    upload_last_error = exc
                    _log(f"Resume upload attempt {attempt + 1} failed: {exc}")
            if upload_last_error is not None:
                raise MigrationError(
                    f"Resume upload failed after {_MAX_ATTEMPTS} attempts: {upload_last_error}"
                )
            _log("Resume upload complete.")

            update_setup_session(config, session, stage="verifying")
            _log("Initial upload metadata verification passed.")

        update_setup_session(config, session, stage="baselining")
        if not remote_nodes:
            remote_nodes = backend.list_recursive(plan.remote_subpath)
        seeded = _seed_inventory_from_verified_state(config, mapping, remote_nodes)
        update_setup_session(config, session, stage="done")
        mark_folder_status(config, mapping, "healthy")
        _log(
            f"Initial metadata baseline seeded ({seeded} file(s)). Folder is now tracked."
        )
        return MigrationResult(
            success=True,
            mapping=mapping,
            message="Bisync setup complete. Folder synced with Proton Drive.",
        )
    except MigrationCancelled:
        _log("Setup cancelled.")
        if session is not None:
            update_setup_session(config, session, stage="failed")
        mark_folder_status(config, mapping, "degraded", error="Setup cancelled by user")
        return MigrationResult(
            success=False,
            mapping=mapping,
            message="Setup cancelled by user.",
            cancelled=True,
        )
    except Exception as exc:
        if session is not None:
            update_setup_session(config, session, stage="failed")
        mark_folder_status(config, mapping, "degraded", error=str(exc))
        return MigrationResult(
            success=False,
            mapping=mapping,
            message=f"Bisync setup failed: {exc}",
        )
    finally:
        if lock is not None:
            lock.release()
        release_remote_lease(lease, config)
