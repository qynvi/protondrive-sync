"""Proton Drive CLI backend — remote data plane.

Wraps the official Proton Drive CLI (`filesystem` subcommands) as the remote
backend. All remote metadata (plaintext size, sha1, source mtime) is read from
`filesystem info -j` / `filesystem list -j` without downloading file content.

Behaviors encoded here, validated against cli-drive@0.4.3 (Phase 0/0.5 study):

- `-j/--json` must follow the subcommand: `filesystem info -j PATH`.
- The remote root namespace is ``/my-files``; app-relative paths map under it.
- File metadata lives under
  ``activeRevision.value.{claimedSize, claimedModificationTime,
  claimedDigests.sha1}``. ``modificationTime`` at the node level is the
  Proton-side upload time, NOT the source mtime — never use it for verification.
- ``upload`` emits no JSON payload; success is confirmed by a follow-up read.
- "not found" is printed as plain text (not JSON) with exit code 1.
- Some mutations (notably ``restore``) can print a per-item failure marker yet
  still exit 0. Always parse per-item JSON ``ok`` flags; never trust exit codes
  alone for batch mutations.
- ``upload -f replace`` trashes the previous node and creates a new one, so
  node uids churn on every overwrite. Never key app state on remote uids.
- Remote deletion uses ``trash`` (recoverable). ``delete`` only works on
  already-trashed nodes; ``empty-trash`` is NEVER invoked by this app.
- Mutations have ~5-10s eventual consistency; verification reads retry.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Optional, Sequence

from .config import AppConfig
from .platform import find_proton_cli


# Proton Drive's personal files namespace. App remote_subpath values are
# relative to this root.
REMOTE_ROOT = "/my-files"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ProtonError(Exception):
    """A Proton Drive CLI operation failed."""

    def __init__(self, message: str, *, returncode: int | None = None):
        super().__init__(message)
        self.returncode = returncode


class ProtonNotFound(ProtonError):
    """A remote path does not exist."""


class ProtonConflict(ProtonError):
    """A remote name conflict blocked an operation (default upload safety)."""


class ProtonAuthError(ProtonError):
    """The CLI could not authenticate (session/keyring unavailable)."""


class ProtonCliMissing(ProtonError):
    """The Proton Drive CLI binary could not be located."""


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


@dataclass
class RemoteNode:
    """One remote node's app-relevant metadata.

    ``path`` is app-relative (relative to REMOTE_ROOT), using ``/`` separators
    and no leading slash, matching the inventory's path convention.
    """

    path: str
    size: int = 0
    is_dir: bool = False
    sha1: str | None = None
    modtime: str | None = None  # claimedModificationTime (source mtime), ISO-8601
    uid: str | None = None
    name: str | None = None


# ---------------------------------------------------------------------------
# Log sanitization
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Proton node uids look like two long base64url chunks joined by '~'.
_UID_RE = re.compile(r"[A-Za-z0-9_-]{32,}={0,2}~[A-Za-z0-9_=~-]{32,}")


def sanitize(text: str | None) -> str:
    """Redact account emails and node uids from CLI output before logging."""
    if not text:
        return ""
    text = _UID_RE.sub("[uid]", text)
    text = _EMAIL_RE.sub("[email]", text)
    return text


# ---------------------------------------------------------------------------
# Path mapping
# ---------------------------------------------------------------------------


def to_cli_path(rel_path: str) -> str:
    """Map an app-relative remote path to an absolute CLI path under the root.

    POSIX node names are used as path segments. Literal backslashes in a name
    are escaped (the CLI treats ``\\/`` as an escaped slash within a name).
    """
    clean = rel_path.strip("/")
    if not clean:
        return REMOTE_ROOT
    segments = [seg.replace("\\", "\\\\") for seg in clean.split("/") if seg]
    return REMOTE_ROOT + "/" + "/".join(segments)


def _posix_parent(rel_path: str) -> str:
    clean = rel_path.strip("/")
    parent = str(PurePosixPath(clean).parent)
    return "" if parent == "." else parent


def _posix_name(rel_path: str) -> str:
    return PurePosixPath(rel_path.strip("/")).name


# ---------------------------------------------------------------------------
# JSON parsing helpers
# ---------------------------------------------------------------------------


def _ok_value(field: object) -> object | None:
    """Return ``value`` from a Proton ``{ok, value}`` wrapper, else None."""
    if isinstance(field, dict) and field.get("ok"):
        return field.get("value")
    return None


def _node_name(data: dict) -> str | None:
    value = _ok_value(data.get("name"))
    return value if isinstance(value, str) else None


def _node_from_json(rel_path: str, data: dict) -> RemoteNode:
    """Build a RemoteNode from an ``info``/``list`` JSON object."""
    is_dir = data.get("type") == "folder"
    size = 0
    sha1: str | None = None
    modtime: str | None = None
    revision = _ok_value(data.get("activeRevision"))
    if isinstance(revision, dict):
        claimed = revision.get("claimedSize")
        if isinstance(claimed, (int, float)):
            size = int(claimed)
        ctime = revision.get("claimedModificationTime")
        if isinstance(ctime, str):
            modtime = ctime
        digests = revision.get("claimedDigests")
        if isinstance(digests, dict):
            value = digests.get("sha1")
            if isinstance(value, str) and value:
                sha1 = value.lower()
    return RemoteNode(
        path=rel_path.strip("/"),
        size=size,
        is_dir=is_dir,
        sha1=sha1,
        modtime=modtime,
        uid=data.get("uid") if isinstance(data.get("uid"), str) else None,
        name=_node_name(data),
    )


def _looks_not_found(text: str) -> bool:
    return "not found" in text.lower()


def _looks_conflict(text: str) -> bool:
    lowered = text.lower()
    return "already exists" in lowered or "name conflict" in lowered


def _looks_auth_error(text: str) -> bool:
    lowered = text.lower()
    return (
        "err_secrets_platform_error" in lowered
        or "unauthorized" in lowered
        or "session" in lowered
        and "expired" in lowered
        or "log in" in lowered
    )


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


@dataclass
class _CliResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def combined(self) -> str:
        return (self.stderr.strip() + "\n" + self.stdout.strip()).strip()


# Eventual-consistency retry tuning for post-mutation verification reads.
_CONSISTENCY_ATTEMPTS = 4
_CONSISTENCY_DELAY = 3.0  # seconds between verification retries


class ProtonDriveCLI:
    """Subprocess wrapper around the Proton Drive CLI.

    One instance per process is sufficient. Directory-existence results are
    cached for the instance lifetime to avoid redundant create-folder probes.
    """

    def __init__(self, config: AppConfig):
        self._config = config
        binary = find_proton_cli(config.proton_cli_path)
        if not binary:
            raise ProtonCliMissing(
                "Proton Drive CLI not found. Run scripts/install-proton-cli.sh "
                "or set proton_cli_path in config."
            )
        self._binary = binary
        self._known_dirs: set[str] = {""}  # REMOTE_ROOT itself always exists

    # -- low-level -----------------------------------------------------------

    def _run(
        self,
        args: Sequence[str],
        *,
        timeout: int,
    ) -> _CliResult:
        try:
            proc = subprocess.run(
                [self._binary, "filesystem", *args],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise ProtonError(
                f"Proton CLI timed out after {timeout}s: filesystem {args[0]}"
            ) from exc
        except OSError as exc:
            raise ProtonError(f"Could not run Proton CLI: {exc}") from exc
        return _CliResult(proc.returncode, proc.stdout or "", proc.stderr or "")

    def _fail(self, action: str, result: _CliResult) -> ProtonError:
        text = result.combined
        message = f"{action} failed: {sanitize(text)}"
        if _looks_not_found(text):
            return ProtonNotFound(message, returncode=result.returncode)
        if _looks_conflict(text):
            return ProtonConflict(message, returncode=result.returncode)
        if _looks_auth_error(text):
            return ProtonAuthError(message, returncode=result.returncode)
        return ProtonError(message, returncode=result.returncode)

    @staticmethod
    def _parse_json(result: _CliResult, action: str) -> object:
        stdout = result.stdout.strip()
        if not stdout:
            raise ProtonError(f"{action}: empty JSON output")
        try:
            return json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise ProtonError(f"{action}: invalid JSON ({sanitize(str(exc))})") from exc

    # -- metadata ------------------------------------------------------------

    def version(self) -> str:
        try:
            proc = subprocess.run(
                [self._binary, "--version"],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProtonError(f"Could not get Proton CLI version: {exc}") from exc
        return (
            (proc.stdout or proc.stderr or "").strip().splitlines()[0]
            if (proc.stdout or proc.stderr)
            else "unknown"
        )

    def stat(self, rel_path: str) -> RemoteNode:
        """Return metadata for one remote path. Raises ProtonNotFound if absent."""
        result = self._run(["info", "-j", to_cli_path(rel_path)], timeout=120)
        if result.returncode != 0:
            raise self._fail(f"info {sanitize(rel_path)}", result)
        data = self._parse_json(result, "info")
        if not isinstance(data, dict):
            raise ProtonError(f"info {sanitize(rel_path)}: unexpected JSON shape")
        return _node_from_json(rel_path, data)

    def stat_or_none(self, rel_path: str) -> RemoteNode | None:
        try:
            return self.stat(rel_path)
        except ProtonNotFound:
            return None

    def exists(self, rel_path: str) -> bool:
        return self.stat_or_none(rel_path) is not None

    def list_dir(self, rel_path: str) -> list[RemoteNode]:
        """List immediate children of a remote directory (non-recursive).

        Returns an empty list if the directory does not exist.
        """
        result = self._run(["list", "-j", to_cli_path(rel_path)], timeout=300)
        if result.returncode != 0:
            if _looks_not_found(result.combined):
                return []
            raise self._fail(f"list {sanitize(rel_path)}", result)
        data = self._parse_json(result, "list")
        if not isinstance(data, list):
            raise ProtonError(f"list {sanitize(rel_path)}: expected JSON array")
        base = rel_path.strip("/")
        nodes: list[RemoteNode] = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            name = _node_name(entry)
            if not name:
                # Undecryptable name — cannot address it by path; skip but keep
                # it visible to callers via a uid-only node for diagnostics.
                nodes.append(_node_from_json(base, entry))
                continue
            child_rel = f"{base}/{name}".strip("/")
            nodes.append(_node_from_json(child_rel, entry))
        return nodes

    def list_recursive(self, rel_path: str) -> list[RemoteNode]:
        """Walk a remote subtree depth-first, returning file nodes.

        Directory listing is the per-call hot spot (~2-3s each), so directories
        are fanned out across a small thread pool. Files only are returned;
        directory nodes are traversed but not emitted.
        """
        from concurrent.futures import ThreadPoolExecutor

        root = rel_path.strip("/")
        files: list[RemoteNode] = []
        pending: list[str] = [root]
        workers = max(1, int(self._config.proton_cli_concurrency))

        while pending:
            batch = pending
            pending = []
            with ThreadPoolExecutor(max_workers=workers) as pool:
                results = list(pool.map(self.list_dir, batch))
            for children in results:
                for node in children:
                    if node.is_dir:
                        pending.append(node.path)
                    elif node.name is not None:
                        files.append(node)
        return files

    # -- directory creation --------------------------------------------------

    def _create_folder(self, parent_rel: str, name: str) -> None:
        result = self._run(
            ["create-folder", "-j", to_cli_path(parent_rel), name],
            timeout=120,
        )
        if result.returncode != 0 and not _looks_conflict(result.combined):
            raise self._fail(f"create-folder {sanitize(name)}", result)

    def ensure_dir(self, rel_path: str) -> None:
        """Create a remote directory and all parents, idempotently."""
        clean = rel_path.strip("/")
        if clean in self._known_dirs:
            return
        parts = [p for p in clean.split("/") if p]
        cur = ""
        for part in parts:
            nxt = f"{cur}/{part}".strip("/")
            if nxt not in self._known_dirs:
                self._create_folder(cur, part)
                self._known_dirs.add(nxt)
            cur = nxt

    # -- upload --------------------------------------------------------------

    def upload(
        self,
        local_path: str,
        dst_rel: str,
        *,
        replace: bool = False,
        size_hint: int | None = None,
    ) -> None:
        """Upload one local file to an exact remote path.

        ``replace=False`` keeps the CLI default (hard-fail on name conflict),
        which is the safety net for unexpected remote state. ``replace=True``
        overwrites (the previous revision is trashed and recoverable).

        The CLI uploads INTO a parent directory keeping the local basename, so
        when the desired remote name differs from the local basename the file
        is staged under the correct name first.
        """
        parent_rel = _posix_parent(dst_rel)
        name = _posix_name(dst_rel)
        self.ensure_dir(parent_rel)

        if size_hint is None:
            try:
                size_hint = os.path.getsize(local_path)
            except OSError:
                size_hint = 0
        timeout = _transfer_timeout(size_hint)

        if Path(local_path).name == name:
            self._upload_file(local_path, parent_rel, replace=replace, timeout=timeout)
            return
        with tempfile.TemporaryDirectory(prefix="protondrive-cli-stage-") as tmp:
            staged = Path(tmp) / name
            shutil.copy2(local_path, staged)
            self._upload_file(str(staged), parent_rel, replace=replace, timeout=timeout)

    def _upload_file(
        self, local_path: str, parent_rel: str, *, replace: bool, timeout: int
    ) -> None:
        args = ["upload", "-t"]  # -t: skip thumbnails (never needed for sync)
        if replace:
            args += ["-f", "replace"]
        args += [local_path, to_cli_path(parent_rel)]
        result = self._run(args, timeout=timeout)
        if result.returncode != 0:
            raise self._fail(f"upload {sanitize(_posix_name(parent_rel))}", result)

    def upload_many(
        self,
        local_paths: Sequence[str],
        parent_rel: str,
        *,
        replace: bool = False,
        total_size_hint: int = 0,
    ) -> None:
        """Upload several local files into one parent dir in a single invocation.

        The CLI uploads all files into the same parent keeping their local
        basenames, so callers must stage any file whose desired remote name
        differs from its local basename. Batching is the main throughput win
        over per-file invocations (~0.2s/file vs ~4s/file).
        """
        if not local_paths:
            return
        self.ensure_dir(parent_rel)
        args = ["upload", "-t"]
        if replace:
            args += ["-f", "replace"]
        args += [*local_paths, to_cli_path(parent_rel)]
        result = self._run(args, timeout=_transfer_timeout(total_size_hint))
        if result.returncode != 0:
            raise self._fail(f"batch upload into {sanitize(parent_rel)}", result)

    def upload_text(self, content: str, dst_rel: str, *, replace: bool = True) -> None:
        """Upload a small text blob (journal/lease/sentinel) to an exact path."""
        with tempfile.TemporaryDirectory(prefix="protondrive-cli-blob-") as tmp:
            staged = Path(tmp) / _posix_name(dst_rel)
            staged.write_text(content, encoding="utf-8")
            self.upload(
                str(staged),
                dst_rel,
                replace=replace,
                size_hint=len(content.encode("utf-8")),
            )

    # -- download ------------------------------------------------------------

    def download(
        self,
        src_rel: str,
        dst_local_path: str,
        *,
        claimed_modtime: str | None = None,
        size_hint: int | None = None,
    ) -> None:
        """Download one remote file to an exact local path.

        Downloads into a fresh temp dir (avoiding any local-name conflict),
        then moves into place. If ``claimed_modtime`` is given, the local
        file's mtime is set from it — REQUIRED so a freshly downloaded file is
        not misread as a local edit on the next scan.
        """
        dst = Path(dst_local_path)
        dst.parent.mkdir(parents=True, exist_ok=True)
        name = _posix_name(src_rel)
        timeout = _transfer_timeout(size_hint or 0)
        with tempfile.TemporaryDirectory(
            prefix="protondrive-cli-dl-", dir=str(dst.parent)
        ) as tmp:
            result = self._run(["download", to_cli_path(src_rel), tmp], timeout=timeout)
            if result.returncode != 0:
                raise self._fail(f"download {sanitize(name)}", result)
            downloaded = Path(tmp) / name
            if not downloaded.exists():
                # Fall back to the single produced entry, if any.
                produced = list(Path(tmp).iterdir())
                if len(produced) == 1:
                    downloaded = produced[0]
                else:
                    raise ProtonError(
                        f"download {sanitize(name)}: output file not found"
                    )
            os.replace(downloaded, dst)
        if claimed_modtime:
            _apply_mtime(dst, claimed_modtime)

    def download_text(self, src_rel: str) -> str:
        """Download a small text blob and return its contents."""
        with tempfile.TemporaryDirectory(prefix="protondrive-cli-blob-dl-") as tmp:
            target = Path(tmp) / _posix_name(src_rel)
            self.download(src_rel, str(target))
            return target.read_text(encoding="utf-8")

    # -- mutations -----------------------------------------------------------

    def trash(self, rel_path: str) -> bool:
        """Move one remote node to trash (recoverable). Returns True on success.

        A missing node is treated as already-removed (True). Per-item ``ok`` in
        the JSON array is authoritative — exit code alone is not trusted.
        """
        result = self._run(["trash", "-j", to_cli_path(rel_path)], timeout=120)
        if result.returncode != 0:
            if _looks_not_found(result.combined):
                return True
            raise self._fail(f"trash {sanitize(_posix_name(rel_path))}", result)
        try:
            data = self._parse_json(result, "trash")
        except ProtonError:
            # Some versions may print nothing on success; verify by absence.
            return not self.exists(rel_path)
        if isinstance(data, list) and data:
            first = data[0]
            if isinstance(first, dict):
                return bool(first.get("ok", False))
        return True

    def move(self, src_rel: str, dst_parent_rel: str) -> None:
        """Move a remote node into a target parent directory."""
        self.ensure_dir(dst_parent_rel)
        result = self._run(
            ["move", to_cli_path(src_rel), to_cli_path(dst_parent_rel)],
            timeout=300,
        )
        if result.returncode != 0:
            raise self._fail(f"move {sanitize(_posix_name(src_rel))}", result)

    def rename(self, rel_path: str, new_name: str) -> None:
        result = self._run(["rename", to_cli_path(rel_path), new_name], timeout=120)
        if result.returncode != 0:
            raise self._fail(f"rename {sanitize(_posix_name(rel_path))}", result)

    # -- health --------------------------------------------------------------

    def probe(self) -> str:
        """Confirm the CLI authenticates and can reach the drive. Returns version."""
        result = self._run(["info", "-j", REMOTE_ROOT], timeout=60)
        if result.returncode != 0:
            raise self._fail("probe", result)
        return self.version()

    def wait_until_absent(self, rel_path: str) -> bool:
        """Poll until a path is gone (post-trash eventual consistency)."""
        for attempt in range(_CONSISTENCY_ATTEMPTS):
            if not self.exists(rel_path):
                return True
            if attempt < _CONSISTENCY_ATTEMPTS - 1:
                time.sleep(_CONSISTENCY_DELAY)
        return not self.exists(rel_path)

    def wait_until_present(self, rel_path: str) -> RemoteNode | None:
        """Poll until a path appears (post-upload eventual consistency)."""
        for attempt in range(_CONSISTENCY_ATTEMPTS):
            node = self.stat_or_none(rel_path)
            if node is not None:
                return node
            if attempt < _CONSISTENCY_ATTEMPTS - 1:
                time.sleep(_CONSISTENCY_DELAY)
        return self.stat_or_none(rel_path)


# ---------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------


def _transfer_timeout(size_bytes: int) -> int:
    """Size-scaled timeout: generous base plus a conservative throughput floor."""
    floor_throughput = 512 * 1024  # assume >=0.5 MiB/s worst case
    return min(7200, 120 + int((size_bytes or 0) / floor_throughput))


def _apply_mtime(path: Path, iso_modtime: str) -> None:
    """Set a file's mtime from an ISO-8601 timestamp (claimedModificationTime)."""
    epoch = _iso_to_epoch(iso_modtime)
    if epoch is None:
        return
    try:
        os.utime(path, (epoch, epoch))
    except OSError:
        pass


def _iso_to_epoch(value: str) -> float | None:
    from datetime import datetime, timezone

    if not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        return None


def sha1_matches(node: RemoteNode | None, sha1: str | None) -> bool:
    """Compare a remote node's sha1 against a local sha1 (case-insensitive)."""
    if node is None or not node.sha1 or not sha1:
        return False
    return node.sha1.lower() == sha1.lower()
