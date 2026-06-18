"""Configuration management for ProtonDrive Sync."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

from .platform import get_config_dir


DEFAULT_FILTERS = [
    # Version control internals
    "- .git/**",
    "- **/.git/**",
    "- .gitnexus/**",
    "- **/.gitnexus/**",
    "- **/gitnexus/**",
    "- **/*gitnexus*",
    "- .protondrive-sync-check",
    "- .protondrive-sync.json",
    # Python
    "- __pycache__/**",
    "- **/__pycache__/**",
    "- *.pyc",
    "- *.pyo",
    "- *.egg-info/**",
    "- **/*.egg-info/**",
    "- __pypackages__/**",
    "- **/__pypackages__/**",
    "- .venv/**",
    "- **/.venv/**",
    "- venv/**",
    "- **/venv/**",
    "- .mypy_cache/**",
    "- **/.mypy_cache/**",
    "- .pytest_cache/**",
    "- **/.pytest_cache/**",
    "- .ruff_cache/**",
    "- **/.ruff_cache/**",
    # Project-local installs and caches
    "- .local/**",
    "- **/.local/**",
    "- .cache/**",
    "- **/.cache/**",
    # ML/model caches
    "- .hf_cache/**",
    "- **/.hf_cache/**",
    "- hf_cache/**",
    "- **/hf_cache/**",
    "- .huggingface/**",
    "- **/.huggingface/**",
    # Generated notebooks/UI state
    "- .ipynb_checkpoints/**",
    "- **/.ipynb_checkpoints/**",
    "- .gradio/**",
    "- **/.gradio/**",
    # JavaScript / Node
    "- node_modules/**",
    "- **/node_modules/**",
    "- .npm/**",
    "- **/.npm/**",
    "- .opencode/**",
    "- **/.opencode/**",
    "- .turbo/**",
    "- **/.turbo/**",
    "- .next/**",
    "- **/.next/**",
    "- .nuxt/**",
    "- **/.nuxt/**",
    "- .svelte-kit/**",
    "- **/.svelte-kit/**",
    "- .vite/**",
    "- **/.vite/**",
    "- .parcel-cache/**",
    "- **/.parcel-cache/**",
    "- coverage/**",
    "- **/coverage/**",
    "- .terraform/**",
    "- **/.terraform/**",
    "- **/terraform.tfstate",
    "- **/terraform.tfstate.backup",
    "- **/tfplan",
    "- **/*.tfplan",
    "- **/infra/terraform/lambda/*.zip",
    # Build artifacts
    "- build/**",
    "- **/build/**",
    "- dist/**",
    "- **/dist/**",
    "- dist-old/**",
    "- **/dist-old/**",
    "- dist-custom/**",
    "- **/dist-custom/**",
    "- out/**",
    "- **/out/**",
    "- cmake-build-*/**",
    "- **/cmake-build-*/**",
    "- *.o",
    "- *.obj",
    "- *.a",
    "- *.la",
    "- *.lo",
    "- *.so",
    "- *.dylib",
    "- *.dll",
    "- *.exe",
    "- *.class",
    "- *.jar",
    "- *.wasm",
    "- *.node",
    "- *.log",
    "- opencode/opencode",
    "- opencode/opencode-*",
    "- **/opencode/opencode",
    "- **/opencode/opencode-*",
    # Terraform
    "- .terraform/**",
    "- **/.terraform/**",
    # OS junk
    "- .DS_Store",
    "- Thumbs.db",
]

SYMLINK_MODES = ("preserve", "copy", "skip")

SYMLINK_MODE_LABELS = {
    "preserve": "Preserve links",
    "copy": "Copy targets",
    "skip": "Skip links",
}

INTEGRITY_MODES = ("metadata", "changed_hash", "deep_hash")


def normalize_symlink_mode(mode: str | None) -> str:
    """Return a supported symlink mode, defaulting to preserve."""
    if mode in SYMLINK_MODES:
        return mode
    return "preserve"


def merge_default_filters(filters: list[str]) -> list[str]:
    """Return *filters* with any missing current default excludes appended."""
    merged: list[str] = []
    seen: set[str] = set()
    for rule in filters + DEFAULT_FILTERS:
        normalized = rule.strip()
        if not normalized or normalized in seen:
            continue
        merged.append(normalized)
        seen.add(normalized)
    return merged


@dataclass
class FolderMapping:
    """A mapping between a local directory and a Proton Drive remote subpath."""

    local_path: str
    remote_subpath: str
    symlink_mode: str = "preserve"
    filters: list[str] = field(default_factory=list)
    bisync_initialized: bool = False
    enabled: bool = True

    def __post_init__(self) -> None:
        self.symlink_mode = normalize_symlink_mode(self.symlink_mode)
        # Normalise paths — use absolute() not resolve() to preserve symlinks.
        self.local_path = str(Path(self.local_path).expanduser().absolute())
        self.remote_subpath = self.remote_subpath.strip("/")


@dataclass
class AppConfig:
    """Top-level application configuration."""

    # Proton Drive CLI backend
    proton_cli_path: Optional[str] = None  # explicit binary path override
    proton_cli_concurrency: int = 4  # parallel CLI invocations for remote walks

    # Shared settings
    low_footprint: bool = False
    symlink_mode: str = "preserve"
    log_level: str = "INFO"

    # Bisync adaptive timing
    bisync_check_interval: int = 15  # filesystem scan frequency (seconds)
    bisync_quiet_threshold: int = 120  # seconds of no changes before sync
    bisync_max_burst: int = 1800  # forced sync ceiling (30 min = 1800s)
    scan_overlap_seconds: int = 5
    stable_check_delay_seconds: int = 10
    remote_poll_interval_seconds: int = 900

    # P3 targeted sync / remote journal settings
    targeted_sync_enabled: bool = True
    batch_sync_enabled: bool = True
    integrity_mode: str = "changed_hash"
    journal_poll_interval_seconds: int = 120
    journal_retention_days: int = 90
    batch_min_paths_per_cycle: int = 2
    batch_max_paths_per_cycle: int = 5000
    targeted_max_paths_per_cycle: int = 1000
    targeted_max_bytes_per_cycle: int = 10 * 1024**3
    targeted_large_file_threshold: int = 1 * 1024**3
    targeted_huge_file_threshold: int = 10 * 1024**3
    remote_audit_interval_hours_small: int = 24
    remote_audit_interval_hours_large: int = 168
    remote_audit_time_budget_minutes: int = 120
    remote_audit_large_folder_file_count: int = 20000
    remote_audit_partition_max_files: int = 5000
    remote_lease_heartbeat_seconds: int = 600
    remote_lease_stale_after_hours: int = 168
    remote_lease_manual_override_after_hours: int = 24
    backup_retention_days: int = 90

    # Safety thresholds
    size_change_threshold: float = 0.5  # 50% size change triggers review
    size_change_min_bytes: int = 10240  # only flag files > 10KB
    protect_delete_min_bytes: int = 100 * 1024 * 1024
    protect_directory_delete_min_files: int = 25
    protect_directory_delete_min_bytes: int = 1 * 1024**3
    bisync_max_delete_percent: int = 10
    download_space_headroom_pct: int = 10

    filters: list[str] = field(default_factory=lambda: list(DEFAULT_FILTERS))
    folders: list[FolderMapping] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.symlink_mode = normalize_symlink_mode(self.symlink_mode)
        if self.integrity_mode not in INTEGRITY_MODES:
            self.integrity_mode = "changed_hash"
        # Rehydrate dicts into FolderMapping instances (from JSON load)
        rehydrated: list[FolderMapping] = []
        folder_known = set(FolderMapping.__dataclass_fields__)
        for f in self.folders:
            if isinstance(f, dict):
                if f.get("sync_mode") == "mount":
                    continue
                if "symlink_mode" not in f:
                    f["symlink_mode"] = self.symlink_mode
                rehydrated.append(
                    FolderMapping(
                        **{
                            key: value
                            for key, value in f.items()
                            if key in folder_known
                        }
                    )
                )
            else:
                rehydrated.append(f)
        self.folders = rehydrated

    @property
    def log_file(self) -> Path:
        return get_config_dir() / "protondrive-sync.log"

    @property
    def filter_file(self) -> Path:
        return get_config_dir() / "filters.txt"

    @property
    def pending_review_file(self) -> Path:
        return get_config_dir() / "pending_review.json"

    @property
    def state_file(self) -> Path:
        return get_config_dir() / "state.json"

    @property
    def setup_sessions_file(self) -> Path:
        return get_config_dir() / "setup_sessions.json"

    @property
    def inventory_file(self) -> Path:
        return get_config_dir() / "inventory.sqlite3"

    def has_enabled_folders(self) -> bool:
        return any(f.enabled for f in self.folders)


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
        if "symlink_mode" not in data:
            data["symlink_mode"] = "preserve"
        # Drop keys from removed/legacy settings instead of failing to load.
        known = set(AppConfig.__dataclass_fields__)
        folder_known = set(FolderMapping.__dataclass_fields__)
        data = {key: value for key, value in data.items() if key in known}
        folders: list[FolderMapping | dict | object] = []
        for folder in data.get("folders", []):
            if isinstance(folder, dict):
                if folder.get("sync_mode") == "mount":
                    continue
                if "symlink_mode" not in folder:
                    folder["symlink_mode"] = data["symlink_mode"]
                folders.append(
                    {key: value for key, value in folder.items() if key in folder_known}
                )
            else:
                folders.append(folder)
        data["folders"] = folders
        config = AppConfig(**data)
        config.filters = merge_default_filters(config.filters)
        return config
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


def effective_filters(
    config: AppConfig, folder: FolderMapping | None = None
) -> list[str]:
    """Return global filters plus any folder-scoped filters."""
    scoped = folder.filters if folder is not None else []
    merged: list[str] = []
    seen: set[str] = set()
    for rule in [*config.filters, *scoped]:
        normalized = rule.strip()
        if not normalized or normalized in seen:
            continue
        merged.append(normalized)
        seen.add(normalized)
    return merged


def _expanded_link_filter_rules(rules: list[str]) -> list[str]:
    """Expand excludes so preserved symlink blobs obey app filters."""
    expanded: list[str] = []
    seen: set[str] = set()
    for rule in rules:
        normalized = rule.strip()
        if not normalized or normalized in seen:
            continue
        expanded.append(normalized)
        seen.add(normalized)
        if not normalized.startswith("- "):
            continue
        pattern = normalized[2:].strip()
        variants: list[str] = []
        if pattern.endswith("/**"):
            base = pattern[:-3].rstrip("/")
            variants.extend([f"- {base}", f"- {base}.rclonelink"])
        elif not pattern.endswith(".rclonelink"):
            variants.append(f"- {pattern}.rclonelink")
        for variant in variants:
            if variant not in seen:
                expanded.append(variant)
                seen.add(variant)
    return expanded


def write_filter_file(config: AppConfig, folder: FolderMapping | None = None) -> Path:
    """Write the effective app filter file from config/folder filters.

    Automatically prepends an exclusion rule for the delete-protection
    backup directory so backup files are never synced back to local.
    """
    from .bisync import BACKUP_DIR_NAME

    rules = _expanded_link_filter_rules(
        [f"- {BACKUP_DIR_NAME}/**"] + effective_filters(config, folder)
    )
    path = config.filter_file
    _ensure_dir(path)
    path.write_text("\n".join(rules) + "\n", encoding="utf-8")
    return path


def add_folder(config: AppConfig, mapping: FolderMapping) -> AppConfig:
    """Add a folder mapping, checking for conflicts."""
    for existing in config.folders:
        if existing.local_path == mapping.local_path:
            raise ConfigError(f"Local path already mapped: {mapping.local_path}")
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
