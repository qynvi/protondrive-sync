"""Configuration management for ProtonDrive Sync."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

from .platform import get_config_dir


@dataclass
class FolderMapping:
    """A mapping between a local directory and a Proton Drive remote subpath."""

    local_path: str
    remote_subpath: str
    sync_mode: str = "bisync"  # "bisync" | "mount"
    pin_mode: str = "on_demand"  # "on_demand" | "keep_offline" (mount mode only)
    pin_subdirs: list[str] = field(default_factory=list)
    bisync_initialized: bool = False  # tracks whether first --resync has completed
    enabled: bool = True

    def __post_init__(self) -> None:
        if self.sync_mode not in ("bisync", "mount"):
            raise ValueError(f"Invalid sync_mode: {self.sync_mode!r}")
        if self.pin_mode not in ("on_demand", "keep_offline"):
            raise ValueError(f"Invalid pin_mode: {self.pin_mode!r}")
        # Normalise paths — use absolute() not resolve() to preserve symlinks.
        # Mount-mode folders ARE symlinks; resolve() would follow the symlink
        # to the mount target, breaking backup detection and teardown.
        self.local_path = str(Path(self.local_path).expanduser().absolute())
        self.remote_subpath = self.remote_subpath.strip("/")


@dataclass
class AppConfig:
    """Top-level application configuration."""

    remote_name: str = "protondrive"
    mount_point: str = "~/ProtonDrive"

    # Mount mode settings
    cache_max_size: str = "20G"
    cache_max_age: str = "72h"
    poll_interval: str = "30s"
    dir_cache_time: str = "30s"
    write_back: str = "2s"

    # Shared settings
    transfers: int = 8
    checkers: int = 16
    low_footprint: bool = False
    copy_links: bool = True  # follow symlinks inside sync folders (--copy-links)
    log_level: str = "INFO"
    pin_interval_minutes: int = 30

    # Bisync adaptive timing
    bisync_check_interval: int = 15       # filesystem scan frequency (seconds)
    bisync_quiet_threshold: int = 120     # seconds of no changes before sync
    bisync_max_burst: int = 1800          # forced sync ceiling (30 min = 1800s)

    # Safety thresholds
    size_change_threshold: float = 0.5    # 50% size change triggers review
    size_change_min_bytes: int = 10240    # only flag files > 10KB

    filters: list[str] = field(default_factory=lambda: [
        # Version control internals
        "- .git/**",
        # Python
        "- __pycache__/**",
        "- *.pyc",
        "- *.pyo",
        "- *.egg-info/**",
        "- __pypackages__/**",
        "- .venv/**",
        "- venv/**",
        "- .mypy_cache/**",
        "- .pytest_cache/**",
        "- .ruff_cache/**",
        # JavaScript / Node
        "- node_modules/**",
        "- .npm/**",
        # Build artifacts
        "- *.o",
        "- *.so",
        "- *.dylib",
        # Terraform
        "- .terraform/**",
        # OS junk
        "- .DS_Store",
        "- Thumbs.db",
    ])
    folders: list[FolderMapping] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.mount_point = str(Path(self.mount_point).expanduser().resolve())
        # Rehydrate dicts into FolderMapping instances (from JSON load)
        rehydrated = []
        for f in self.folders:
            if isinstance(f, dict):
                # Legacy config migration: detect mount-mode folders by
                # checking if the local path is currently a symlink
                if "sync_mode" not in f:
                    local = Path(f.get("local_path", "")).expanduser()
                    if local.is_symlink():
                        f["sync_mode"] = "mount"
                    # else: defaults to "bisync"
                rehydrated.append(FolderMapping(**f))
            else:
                rehydrated.append(f)
        self.folders = rehydrated

    @property
    def mount_path(self) -> Path:
        return Path(self.mount_point)

    @property
    def log_file(self) -> Path:
        return get_config_dir() / "rclone.log"

    @property
    def filter_file(self) -> Path:
        return get_config_dir() / "filters.txt"

    @property
    def pending_review_file(self) -> Path:
        return get_config_dir() / "pending_review.json"

    def has_mount_folders(self) -> bool:
        return any(f.sync_mode == "mount" and f.enabled for f in self.folders)

    def has_bisync_folders(self) -> bool:
        return any(f.sync_mode == "bisync" and f.enabled for f in self.folders)


def _config_path() -> Path:
    return get_config_dir() / "config.json"


def _ensure_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def load_config() -> AppConfig:
    """Load config from disk, returning defaults if none exists."""
    path = _config_path()
    if not path.exists():
        return AppConfig()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return AppConfig(**data)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        # Corrupt config — return defaults but don't overwrite
        raise ConfigError(f"Failed to parse config at {path}: {exc}") from exc


def save_config(config: AppConfig) -> Path:
    """Persist config to disk. Returns the path written."""
    path = _config_path()
    _ensure_dir(path)
    data = asdict(config)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def write_filter_file(config: AppConfig) -> Path:
    """Write the rclone filter file from config.filters.

    Automatically prepends an exclusion rule for the delete-protection
    backup directory so backup files are never synced back to local.
    """
    from .bisync import BACKUP_DIR_NAME

    rules = [f"- {BACKUP_DIR_NAME}/**"] + config.filters
    path = config.filter_file
    _ensure_dir(path)
    path.write_text("\n".join(rules) + "\n", encoding="utf-8")
    return path


def add_folder(config: AppConfig, mapping: FolderMapping) -> AppConfig:
    """Add a folder mapping, checking for conflicts."""
    for existing in config.folders:
        if existing.local_path == mapping.local_path:
            raise ConfigError(
                f"Local path already mapped: {mapping.local_path}"
            )
        if existing.remote_subpath == mapping.remote_subpath:
            raise ConfigError(
                f"Remote subpath already in use: {mapping.remote_subpath}"
            )
    config.folders.append(mapping)
    return config


def remove_folder(config: AppConfig, local_path: str) -> Optional[FolderMapping]:
    """Remove a folder mapping by local path. Returns the removed mapping or None."""
    resolved = str(Path(local_path).expanduser().absolute())
    for i, f in enumerate(config.folders):
        if f.local_path == resolved:
            return config.folders.pop(i)
    return None


class ConfigError(Exception):
    """Raised on configuration errors."""
