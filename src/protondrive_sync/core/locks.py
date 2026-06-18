"""Local locks and best-effort remote advisory leases."""

from __future__ import annotations

import json
import os
import socket
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import AppConfig, FolderMapping
from .platform import get_config_dir
from .state import folder_id, utc_now


class LockError(Exception):
    """Raised when a local or remote lock cannot be acquired."""


@dataclass
class LocalFolderLock:
    """A non-blocking local lock file for one folder pair."""

    path: Path
    fd: int | None = None

    def acquire(self) -> LocalFolderLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.fd = os.open(
                str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
            )
        except FileExistsError as exc:
            raise LockError(f"Folder lock already exists: {self.path}") from exc
        payload = {
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "started": utc_now(),
        }
        os.write(self.fd, (json.dumps(payload) + "\n").encode("utf-8"))
        return self

    def release(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def __enter__(self) -> LocalFolderLock:
        return self.acquire()

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.release()


def local_lock_path(folder: FolderMapping) -> Path:
    return (
        get_config_dir()
        / "locks"
        / f"{folder_id(folder.local_path, folder.remote_subpath)}.lock"
    )


def local_folder_lock(folder: FolderMapping) -> LocalFolderLock:
    return LocalFolderLock(local_lock_path(folder))


@dataclass
class RemoteLease:
    """Best-effort lease marker stored on Proton Drive."""

    folder_id: str
    machine_id: str
    hostname: str
    operation: str
    remote_path: str = ""
    started: str = field(default_factory=utc_now)
    heartbeat: str = field(default_factory=utc_now)
    pid: int = field(default_factory=os.getpid)


def machine_id() -> str:
    """Return a stable-ish local machine id for lease filenames."""
    path = get_config_dir() / "machine-id"
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    value = f"{socket.gethostname()}-{os.getpid()}-{int(time.time())}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n", encoding="utf-8")
    return value


def remote_lease_subpath(folder: FolderMapping, machine: str | None = None) -> str:
    fid = folder_id(folder.local_path, folder.remote_subpath)
    return f".protondrive-sync-locks/{fid}/{machine or machine_id()}.json"


def remote_lease_dir(folder: FolderMapping) -> str:
    fid = folder_id(folder.local_path, folder.remote_subpath)
    return f".protondrive-sync-locks/{fid}"


def _parse_remote_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        # Accept compact UTC offsets sometimes returned by remote metadata.
        if normalized.endswith("+0000"):
            normalized = normalized[:-5] + "+00:00"
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def remote_lease_age_hours(
    modtime: str | None, *, now: datetime | None = None
) -> float | None:
    """Return the remote lease marker age in hours, if its timestamp is parseable."""
    parsed = _parse_remote_time(modtime)
    if parsed is None:
        return None
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return max(0.0, (current.astimezone(timezone.utc) - parsed).total_seconds() / 3600)


def remote_lease_is_stale(
    modtime: str | None, config: AppConfig, *, now: datetime | None = None
) -> bool:
    age = remote_lease_age_hours(modtime, now=now)
    return age is not None and age >= config.remote_lease_stale_after_hours


def remote_lease_can_manual_override(
    modtime: str | None, config: AppConfig, *, now: datetime | None = None
) -> bool:
    age = remote_lease_age_hours(modtime, now=now)
    return age is not None and age >= config.remote_lease_manual_override_after_hours


def acquire_remote_lease(
    folder: FolderMapping,
    config: AppConfig,
    *,
    operation: str,
) -> RemoteLease:
    """Write a best-effort remote lease marker.

    This is advisory, not atomic. It still reduces common 2-3 machine races by
    making concurrent operations visible before the expensive sync command.
    """
    from .proton_cli import ProtonDriveCLI, ProtonError

    backend = ProtonDriveCLI(config)
    machine = machine_id()
    try:
        existing = backend.list_dir(remote_lease_dir(folder))
    except ProtonError as exc:
        raise LockError(f"Could not inspect remote leases: {exc}") from exc
    for node in existing:
        name = node.name or ""
        if not name.endswith(".json") or name == f"{machine}.json":
            continue
        remote_path = f"{remote_lease_dir(folder)}/{name}"
        if remote_lease_is_stale(node.modtime, config):
            try:
                backend.trash(remote_path)
                continue
            except ProtonError as exc:
                raise LockError(
                    f"Could not clear stale remote lease {name}: {exc}"
                ) from exc
        raise LockError(f"Another remote lease exists for this folder: {name}")

    lease = RemoteLease(
        folder_id=folder_id(folder.local_path, folder.remote_subpath),
        machine_id=machine,
        hostname=socket.gethostname(),
        operation=operation,
        remote_path=remote_lease_subpath(folder, machine),
    )
    fd, tmp_name = tempfile.mkstemp(prefix="protondrive-sync-lease-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(lease), indent=2) + "\n")
        backend.upload(tmp_name, lease.remote_path, replace=True)
    finally:
        try:
            Path(tmp_name).unlink()
        except OSError:
            pass
    return lease


def release_remote_lease(lease: RemoteLease | None, config: AppConfig) -> None:
    """Best-effort removal of a previously written remote lease marker."""
    if lease is None or not lease.remote_path:
        return
    from .proton_cli import ProtonDriveCLI, ProtonError

    try:
        ProtonDriveCLI(config).trash(lease.remote_path)
    except ProtonError:
        pass


def heartbeat_remote_lease(
    lease: RemoteLease | None, config: AppConfig
) -> RemoteLease | None:
    """Refresh a remote lease marker heartbeat."""
    if lease is None or not lease.remote_path:
        return lease
    from .proton_cli import ProtonDriveCLI

    lease.heartbeat = utc_now()
    fd, tmp_name = tempfile.mkstemp(
        prefix="protondrive-sync-lease-heartbeat-", suffix=".json"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(lease), indent=2) + "\n")
        ProtonDriveCLI(config).upload(tmp_name, lease.remote_path, replace=True)
    finally:
        try:
            Path(tmp_name).unlink()
        except OSError:
            pass
    return lease


def override_remote_lease(
    folder: FolderMapping, machine: str, config: AppConfig
) -> None:
    """Delete another machine's lease after the manual override age threshold."""
    from .proton_cli import ProtonDriveCLI, ProtonError

    backend = ProtonDriveCLI(config)
    try:
        entries = backend.list_dir(remote_lease_dir(folder))
    except ProtonError as exc:
        raise LockError(f"Could not inspect remote leases: {exc}") from exc
    target = f"{machine}.json"
    for node in entries:
        if node.name != target:
            continue
        if not remote_lease_can_manual_override(node.modtime, config):
            raise LockError(f"Remote lease is not old enough to override: {node.name}")
        backend.trash(f"{remote_lease_dir(folder)}/{node.name}")
        return
