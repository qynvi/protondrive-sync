"""Resumable setup session tracking for large initial syncs."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import AppConfig
from .state import utc_now


SETUP_STAGES = ("preflight", "copying", "verifying", "baselining", "done", "failed")


def filter_fingerprint(filters: list[str]) -> str:
    """Return a stable fingerprint for the active filter set."""
    payload = "\n".join(rule.strip() for rule in filters if rule.strip())
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class SetupSession:
    """Persistent identity and progress for one setup operation."""

    id: str
    local_path: str
    remote_subpath: str
    remote_initial_state: str
    symlink_mode: str
    filter_fingerprint: str
    direction: str = "upload"
    stage: str = "preflight"
    failed_paths: list[str] = field(default_factory=list)
    operation_logs: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if self.direction not in ("upload", "download"):
            self.direction = "upload"
        if self.remote_initial_state not in ("missing", "empty", "nonempty"):
            self.remote_initial_state = "nonempty"
        if self.stage not in SETUP_STAGES:
            self.stage = "failed"

    def matches(
        self,
        *,
        local_path: str,
        remote_subpath: str,
        direction: str | None = None,
        symlink_mode: str,
        filter_fingerprint: str,
    ) -> bool:
        """Return True if a setup resume is for the exact same tuple."""
        return (
            self.local_path == local_path
            and self.remote_subpath == remote_subpath.strip("/")
            and (direction is None or self.direction == direction)
            and self.symlink_mode == symlink_mode
            and self.filter_fingerprint == filter_fingerprint
        )


def _session_path(config: AppConfig) -> Path:
    return config.setup_sessions_file


def load_setup_sessions(config: AppConfig) -> dict[str, SetupSession]:
    path = _session_path(config)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    sessions: dict[str, SetupSession] = {}
    for session_id, data in raw.get("sessions", {}).items():
        if isinstance(data, dict):
            allowed = {k: v for k, v in data.items() if k in SetupSession.__dataclass_fields__}
            sessions[session_id] = SetupSession(**allowed)
    return sessions


def save_setup_sessions(config: AppConfig, sessions: dict[str, SetupSession]) -> Path:
    path = _session_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"sessions": {sid: asdict(session) for sid, session in sessions.items()}}
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def create_setup_session(
    config: AppConfig,
    *,
    local_path: str,
    remote_subpath: str,
    remote_initial_state: str,
    symlink_mode: str,
    filters: list[str],
    direction: str = "upload",
) -> SetupSession:
    """Create and persist a new setup session."""
    sessions = load_setup_sessions(config)
    session = SetupSession(
        id=uuid.uuid4().hex,
        local_path=local_path,
        remote_subpath=remote_subpath.strip("/"),
        direction=direction,
        remote_initial_state=remote_initial_state,
        symlink_mode=symlink_mode,
        filter_fingerprint=filter_fingerprint(filters),
    )
    sessions[session.id] = session
    save_setup_sessions(config, sessions)
    return session


def update_setup_session(
    config: AppConfig,
    session: SetupSession,
    *,
    stage: str | None = None,
    failed_paths: list[str] | None = None,
    operation_log: str | None = None,
) -> SetupSession:
    """Persist a setup session stage/log update."""
    sessions = load_setup_sessions(config)
    current = sessions.get(session.id, session)
    if stage is not None:
        current.stage = stage
    if failed_paths:
        for path in failed_paths:
            if path not in current.failed_paths:
                current.failed_paths.append(path)
    if operation_log and operation_log not in current.operation_logs:
        current.operation_logs.append(operation_log)
    current.updated_at = utc_now()
    current.__post_init__()
    sessions[current.id] = current
    save_setup_sessions(config, sessions)
    return current


def find_resumable_setup_session(
    config: AppConfig,
    *,
    local_path: str,
    remote_subpath: str,
    symlink_mode: str,
    filters: list[str],
    direction: str | None = None,
) -> SetupSession | None:
    """Return an unfinished matching session, if any."""
    fingerprint = filter_fingerprint(filters)
    for session in load_setup_sessions(config).values():
        if session.stage in ("done", "failed"):
            continue
        if session.matches(
            local_path=local_path,
            remote_subpath=remote_subpath,
            direction=direction,
            symlink_mode=symlink_mode,
            filter_fingerprint=fingerprint,
        ):
            return session
    return None
