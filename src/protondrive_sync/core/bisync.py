"""Bisync intelligence — adaptive timing, delete protection, change detection.

This module handles all the smart logic around bisync:
- Activity-window coalescing (adaptive sync timing)
- Work file delete protection (rename-backup on remote)
- Large change detection (flag suspicious size changes for review)
- Pending review management (block sync until user approves)
"""

from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

from .config import AppConfig, FolderMapping, effective_filters
from .proton_cli import ProtonDriveCLI, ProtonError, RemoteNode
from .migration import _matches_filter


# --- Backup directory for delete-protected files ---

BACKUP_DIR_NAME = ".protondrive-sync-backups"


def _folder_relative_nodes(
    remote_nodes: list[RemoteNode], folder: FolderMapping
) -> list[RemoteNode]:
    """Return copies whose paths are relative to the folder mapping root."""
    base = folder.remote_subpath.strip("/")
    out: list[RemoteNode] = []
    for node in remote_nodes:
        rel = node.path.strip("/")
        if base and rel.startswith(base + "/"):
            rel = rel[len(base) + 1 :]
        elif rel == base:
            rel = ""
        if folder.symlink_mode == "preserve" and rel.endswith(".rclonelink"):
            rel = rel[: -len(".rclonelink")]
        if not rel:
            continue
        out.append(
            RemoteNode(
                path=rel,
                size=node.size,
                is_dir=node.is_dir,
                sha1=node.sha1,
                modtime=node.modtime,
                uid=node.uid,
                name=node.name,
            )
        )
    return out


# --- Work file extensions (protected from delete-sync) ---

WORK_EXTENSIONS: set[str] = {
    # Code
    ".py",
    ".pyw",
    ".pyi",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".mjs",
    ".cjs",
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".cc",
    ".cxx",
    ".hh",
    ".java",
    ".kt",
    ".kts",
    ".go",
    ".rs",
    ".rb",
    ".php",
    ".swift",
    ".scala",
    ".sh",
    ".bash",
    ".zsh",
    ".fish",
    ".sql",
    ".r",
    ".m",
    ".mm",
    ".cs",
    ".fs",
    ".vb",
    ".lua",
    ".pl",
    ".pm",
    ".ex",
    ".exs",
    ".zig",
    ".nim",
    ".v",
    ".d",
    # Config / data
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".xml",
    ".csv",
    ".ini",
    ".cfg",
    ".html",
    ".htm",
    ".css",
    ".scss",
    ".sass",
    ".less",
    ".md",
    ".rst",
    ".tex",
    ".txt",
    # Documents
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".odt",
    ".ods",
    ".odp",
    # Notebooks
    ".ipynb",
    # Other
    ".proto",
    ".graphql",
    ".gql",
    ".makefile",
    ".dockerfile",
    # Model/checkpoint/data artifacts
    ".pt",
    ".pth",
    ".safetensors",
    ".ckpt",
    ".onnx",
    ".gguf",
    ".bin",
    ".pkl",
    ".pickle",
    ".joblib",
    ".npz",
    ".npy",
    ".parquet",
    ".arrow",
    ".sqlite",
    ".sqlite3",
    ".db",
    ".wav",
    ".flac",
    ".mp3",
    ".ogg",
    ".mp4",
    ".mkv",
}


def is_work_file(filepath: str) -> bool:
    """Check if a file has a protected work extension."""
    ext = Path(filepath).suffix.lower()
    # Also match extensionless known filenames
    name = Path(filepath).name.lower()
    if name in ("makefile", "dockerfile", "readme", "license", "changelog"):
        return True
    return ext in WORK_EXTENSIONS


# --- Activity-window coalescing ---


@dataclass
class BurstState:
    """Tracks the activity burst window for a single folder."""

    active: bool = False
    start_time: float = 0.0
    last_change_time: float = 0.0
    last_check_time: float = 0.0

    def record_change(self, now: Optional[float] = None) -> None:
        now = now or time.time()
        if not self.active:
            self.active = True
            self.start_time = now
        self.last_change_time = now

    def reset(self) -> None:
        self.active = False
        self.start_time = 0.0
        self.last_change_time = 0.0


def should_sync(
    state: BurstState,
    quiet_threshold: int = 120,
    max_burst: int = 1800,
) -> bool:
    """Determine if a sync should fire based on burst state.

    Sync triggers when EITHER:
    - No new changes for quiet_threshold seconds (burst ended naturally)
    - Burst has been active for max_burst seconds (forced sync)
    """
    if not state.active:
        return False

    now = time.time()
    quiet_elapsed = now - state.last_change_time
    burst_elapsed = now - state.start_time

    return quiet_elapsed >= quiet_threshold or burst_elapsed >= max_burst


def scan_for_modifications(
    folder: FolderMapping,
    since: float,
    filters: list[str],
) -> bool:
    """Check if any file in a folder tree has been modified since `since`.

    Uses os.stat() only — no file content reading. Respects filter rules
    to skip excluded directories.

    Checks both file mtimes and directory mtimes. Directory mtime changes
    when files are added, removed, or renamed — this catches structural
    changes even when copied files preserve the original mtime (e.g.
    GNOME Files copy-paste).
    """
    local_path = Path(folder.local_path)
    if not local_path.is_dir():
        return False

    try:
        for root, dirs, files in os.walk(
            local_path,
            followlinks=(folder.symlink_mode == "copy"),
        ):
            # Prune filtered directories from traversal
            dirs[:] = [
                d
                for d in dirs
                if not _matches_filter(
                    (Path(root) / d).relative_to(local_path).as_posix(), filters
                )
            ]

            for dirname in dirs:
                dirpath = Path(root) / dirname
                if not dirpath.is_symlink() or folder.symlink_mode == "copy":
                    continue
                if folder.symlink_mode == "skip":
                    continue
                try:
                    if dirpath.lstat().st_mtime > since:
                        return True
                except OSError:
                    continue

            # Check directory mtime — catches file additions/deletions
            # even when file mtimes are preserved
            try:
                if Path(root).stat().st_mtime > since:
                    return True
            except OSError:
                pass

            for filename in files:
                rel_file = (Path(root) / filename).relative_to(local_path).as_posix()
                if _matches_filter(rel_file, filters):
                    continue
                filepath = Path(root) / filename
                if filepath.is_symlink() and folder.symlink_mode == "skip":
                    continue
                try:
                    stat = (
                        filepath.lstat()
                        if filepath.is_symlink() and folder.symlink_mode == "preserve"
                        else filepath.stat()
                    )
                    if stat.st_mtime > since:
                        return True
                except OSError:
                    continue
    except (OSError, PermissionError):
        pass

    return False


# --- Delete protection ---


@dataclass
class DeletedWorkFile:
    """A work file that was deleted locally but exists on remote."""

    path: str
    remote_size: int


def detect_local_deletions(
    local_path: str,
    remote_files: list[RemoteNode],
    filters: list[str],
    config: AppConfig,
    *,
    symlink_mode: str = "preserve",
) -> tuple[list[DeletedWorkFile], list[str]]:
    """Find protected files deleted locally. Groups full-directory deletes.

    Returns:
        (deleted_work_files, deleted_work_dirs)
        - deleted_work_files: individual files to protect
        - deleted_work_dirs: directories where ALL contents are work files
          that were deleted — these get a directory-level rename
    """
    local_base = Path(local_path)
    deleted_files: list[DeletedWorkFile] = []
    missing_files: list[DeletedWorkFile] = []

    for rf in remote_files:
        if rf.is_dir:
            continue
        # Skip files that match filter rules (they wouldn't be local anyway)
        if _matches_filter(rf.path, filters):
            continue

        local_file = local_base / rf.path
        exists = (
            os.path.lexists(local_file)
            if symlink_mode == "preserve"
            else local_file.exists()
        )
        if not exists:
            missing = DeletedWorkFile(path=rf.path, remote_size=rf.size)
            missing_files.append(missing)
            if is_work_file(rf.path) or rf.size >= config.protect_delete_min_bytes:
                deleted_files.append(missing)

    # Group by parent directory to detect full-directory deletes
    dir_deletes: dict[str, list[DeletedWorkFile]] = defaultdict(list)
    for df in deleted_files:
        parent = str(Path(df.path).parent)
        if parent == ".":
            continue  # root-level files handled individually
        dir_deletes[parent].append(df)

    # Check if every work file in a directory was deleted
    deleted_work_dirs: list[str] = []
    files_handled_by_dir: set[str] = set()

    for dir_path, dir_deleted_files in dir_deletes.items():
        remote_protected_in_dir = [
            rf
            for rf in remote_files
            if not rf.is_dir
            and str(Path(rf.path).parent) == dir_path
            and (is_work_file(rf.path) or rf.size >= config.protect_delete_min_bytes)
        ]
        local_dir = local_base / dir_path

        if (
            len(dir_deleted_files) == len(remote_protected_in_dir)
            and len(remote_protected_in_dir) > 0
            and not local_dir.exists()
        ):
            deleted_work_dirs.append(dir_path)
            files_handled_by_dir.update(df.path for df in dir_deleted_files)

    # Also protect broad directory deletes even when extensions are not known.
    broad_dirs: dict[str, list[DeletedWorkFile]] = defaultdict(list)
    for missing in missing_files:
        parent = str(Path(missing.path).parent)
        if parent != ".":
            broad_dirs[parent].append(missing)
    for dir_path, items in broad_dirs.items():
        if dir_path in deleted_work_dirs:
            continue
        local_dir = local_base / dir_path
        total_bytes = sum(item.remote_size for item in items)
        if not local_dir.exists() and (
            len(items) >= config.protect_directory_delete_min_files
            or total_bytes >= config.protect_directory_delete_min_bytes
        ):
            deleted_work_dirs.append(dir_path)
            files_handled_by_dir.update(item.path for item in items)

    # Remove files that are handled at the directory level
    individual_deleted = [
        df for df in deleted_files if df.path not in files_handled_by_dir
    ]

    return individual_deleted, deleted_work_dirs


def protect_deleted_work_files(
    remote_subpath: str,
    deleted_files: list[DeletedWorkFile],
    deleted_dirs: list[str],
    config: AppConfig | None = None,
) -> list[str]:
    """Move deleted work files/dirs on remote to Proton Drive trash.

    Returns list of paths that were protected.
    """
    protected: list[str] = []
    backend = ProtonDriveCLI(config or AppConfig())

    # Directory-level trash first
    for dir_path in deleted_dirs:
        src = f"{remote_subpath}/{dir_path}".strip("/")
        try:
            backend.trash(src)
            protected.append(f"{dir_path}/ → Proton Drive trash")
        except ProtonError:
            pass

    # Individual file trash
    for df in deleted_files:
        src = f"{remote_subpath}/{df.path}".strip("/")
        try:
            backend.trash(src)
            protected.append(f"{df.path} → Proton Drive trash")
        except ProtonError:
            pass  # best-effort — don't block sync for protection failures

    return protected


# --- Large change detection ---


@dataclass
class FlaggedChange:
    """A file that changed significantly in size."""

    path: str
    local_size: int
    remote_size: int
    change_pct: float

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "local_size": self.local_size,
            "remote_size": self.remote_size,
            "change_pct": round(self.change_pct, 1),
        }

    @classmethod
    def from_dict(cls, d: dict) -> FlaggedChange:
        return cls(
            path=d["path"],
            local_size=d["local_size"],
            remote_size=d["remote_size"],
            change_pct=d["change_pct"],
        )


def detect_suspicious_changes(
    folder: FolderMapping,
    remote_files: list[RemoteNode],
    config: AppConfig,
) -> list[FlaggedChange]:
    """Compare local vs remote file sizes. Flag changes exceeding threshold.

    Only flags files larger than config.size_change_min_bytes that changed
    by more than config.size_change_threshold (as a ratio).
    """
    local_base = Path(folder.local_path)
    flagged: list[FlaggedChange] = []

    # Build remote size lookup
    remote_sizes: dict[str, int] = {
        rf.path: rf.size for rf in remote_files if not rf.is_dir
    }

    for rel_path, remote_size in remote_sizes.items():
        if remote_size < config.size_change_min_bytes:
            continue

        local_file = local_base / rel_path
        exists = (
            os.path.lexists(local_file)
            if folder.symlink_mode == "preserve"
            else local_file.exists()
        )
        if not exists:
            continue  # deletion, handled by delete protection

        try:
            if local_file.is_symlink() and folder.symlink_mode == "preserve":
                local_size = len(os.readlink(local_file).encode("utf-8"))
            else:
                local_size = local_file.stat().st_size
        except OSError:
            continue

        if remote_size == 0:
            continue

        change_ratio = abs(local_size - remote_size) / remote_size
        if change_ratio >= config.size_change_threshold:
            flagged.append(
                FlaggedChange(
                    path=rel_path,
                    local_size=local_size,
                    remote_size=remote_size,
                    change_pct=change_ratio * 100,
                )
            )

    return flagged


def _entry_signature(
    path: Path, *, preserve_symlink: bool
) -> tuple[str, int, int, str | None] | None:
    """Return a cheap stability signature for a local path."""
    try:
        if path.is_symlink() and preserve_symlink:
            stat = path.lstat()
            return (
                "symlink",
                len(os.readlink(path).encode("utf-8")),
                stat.st_mtime_ns,
                os.readlink(path),
            )
        stat = path.stat()
        kind = "dir" if path.is_dir() else "file"
        return (kind, stat.st_size, stat.st_mtime_ns, None)
    except OSError:
        return None


def detect_unstable_writes(
    folder: FolderMapping,
    since: float,
    filters: list[str],
    *,
    delay_seconds: int,
) -> list[str]:
    """Return changed local paths that are still mutating after a delay."""
    local_path = Path(folder.local_path)
    if not local_path.is_dir() or delay_seconds <= 0:
        return []
    preserve = folder.symlink_mode == "preserve"
    candidates: dict[str, tuple[str, int, int, str | None]] = {}

    try:
        for root, dirs, files in os.walk(
            local_path, followlinks=(folder.symlink_mode == "copy")
        ):
            dirs[:] = [
                d
                for d in dirs
                if not _matches_filter(
                    (Path(root) / d).relative_to(local_path).as_posix(), filters
                )
            ]
            for name in [*dirs, *files]:
                path = Path(root) / name
                rel = path.relative_to(local_path).as_posix()
                if _matches_filter(rel, filters):
                    continue
                sig = _entry_signature(path, preserve_symlink=preserve)
                if sig is None:
                    continue
                if sig[2] / 1_000_000_000 > since:
                    candidates[rel] = sig
    except (OSError, PermissionError):
        return []

    if not candidates:
        return []
    time.sleep(delay_seconds)

    unstable: list[str] = []
    for rel, before in candidates.items():
        after = _entry_signature(local_path / rel, preserve_symlink=preserve)
        if after != before:
            unstable.append(rel)
    return sorted(unstable)


# --- Pending review management ---


def write_pending_review(
    config: AppConfig,
    folder_path: str,
    flagged: list[FlaggedChange],
) -> None:
    """Write flagged changes to pending_review.json, blocking sync for that folder."""
    review_file = config.pending_review_file
    existing = _load_pending_reviews(review_file)
    existing[folder_path] = [f.to_dict() for f in flagged]
    review_file.parent.mkdir(parents=True, exist_ok=True)
    review_file.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")


def read_pending_review(config: AppConfig) -> dict[str, list[FlaggedChange]]:
    """Read all pending reviews. Returns {folder_path: [FlaggedChange]}."""
    raw = _load_pending_reviews(config.pending_review_file)
    return {
        folder: [FlaggedChange.from_dict(d) for d in items]
        for folder, items in raw.items()
    }


def has_pending_review(config: AppConfig, folder_path: str) -> bool:
    """Check if a folder is blocked awaiting review."""
    reviews = _load_pending_reviews(config.pending_review_file)
    return folder_path in reviews and len(reviews[folder_path]) > 0


def clear_pending_review(config: AppConfig, folder_path: str) -> None:
    """Clear review for a specific folder, unblocking sync."""
    review_file = config.pending_review_file
    existing = _load_pending_reviews(review_file)
    existing.pop(folder_path, None)
    if existing:
        review_file.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    elif review_file.exists():
        review_file.unlink()


def _load_pending_reviews(path: Path) -> dict:
    """Load pending_review.json, returning empty dict if missing/corrupt."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


# --- Pre-sync orchestrator ---


@dataclass
class SafetyReport:
    """Full pre-sync safety report."""

    deleted_work_files: list[DeletedWorkFile] = field(default_factory=list)
    deleted_work_dirs: list[str] = field(default_factory=list)
    suspicious_changes: list[FlaggedChange] = field(default_factory=list)
    protected_paths: list[str] = field(default_factory=list)
    remote_listing_error: str | None = None
    sentinel_error: str | None = None
    unstable_paths: list[str] = field(default_factory=list)

    @property
    def safe_to_sync(self) -> bool:
        """False unless safety checks positively proved the sync is safe."""
        return not (
            self.suspicious_changes
            or self.remote_listing_error
            or self.sentinel_error
            or self.unstable_paths
        )

    @property
    def has_deletions(self) -> bool:
        return len(self.deleted_work_files) > 0 or len(self.deleted_work_dirs) > 0


def run_safety_checks(
    folder: FolderMapping,
    config: AppConfig,
) -> SafetyReport:
    """Full pre-sync safety check. Called before every bisync run.

    1. Fetch remote file list (metadata only, no downloads)
    2. Detect local deletions of work files
    3. Detect suspicious large changes
    4. Protect deleted work files (rename-backup on remote)

    Returns a SafetyReport. If not safe_to_sync, the caller should
    write a pending review and skip bisync.
    """
    report = SafetyReport()

    try:
        remote_files = _folder_relative_nodes(
            ProtonDriveCLI(config).list_recursive(folder.remote_subpath), folder
        )
    except ProtonError as exc:
        report.remote_listing_error = str(exc)
        return report

    # Detect deletions
    report.deleted_work_files, report.deleted_work_dirs = detect_local_deletions(
        folder.local_path,
        remote_files,
        effective_filters(config, folder),
        config,
        symlink_mode=folder.symlink_mode,
    )

    # Detect suspicious changes
    report.suspicious_changes = detect_suspicious_changes(
        folder,
        remote_files,
        config,
    )

    # Protect deleted work files on remote (rename-backup)
    if report.has_deletions:
        report.protected_paths = protect_deleted_work_files(
            folder.remote_subpath,
            report.deleted_work_files,
            report.deleted_work_dirs,
            config,
        )

    return report
