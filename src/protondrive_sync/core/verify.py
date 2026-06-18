"""Metadata-based local/remote verification for the Proton CLI backend."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

from .config import AppConfig, FolderMapping, normalize_symlink_mode
from .inventory import scan_local_inventory, sha1_file
from .proton_cli import ProtonDriveCLI, ProtonError, RemoteNode


LINK_BLOB_SUFFIX = ".rclonelink"


@dataclass
class VerifyReport:
    ok: bool
    missing_on_dst: list[str] = field(default_factory=list)
    missing_on_src: list[str] = field(default_factory=list)
    different: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    message: str | None = None
    combined_report: Path | None = None
    operation_log: Path | None = None


def _remote_rel_under(root: str, node: RemoteNode, *, symlink_mode: str) -> str:
    base = root.strip("/")
    path = node.path.strip("/")
    if base and path.startswith(base + "/"):
        path = path[len(base) + 1 :]
    elif path == base:
        path = ""
    if symlink_mode == "preserve" and path.endswith(LINK_BLOB_SUFFIX):
        path = path[: -len(LINK_BLOB_SUFFIX)]
    return path


def _local_signature(
    root: Path, rel: str, kind: str, local_sha1: str | None, link_target: str | None
) -> tuple[int | None, str | None]:
    path = root / rel
    if kind == "symlink":
        target = link_target
        if target is None:
            try:
                target = os.readlink(path)
            except OSError:
                return None, None
        blob = target.encode("utf-8")
        return len(blob), hashlib.sha1(blob).hexdigest()
    try:
        return path.stat().st_size, local_sha1 or sha1_file(path)
    except OSError:
        return None, None


def verify_subtree_targeted(
    local_subtree: str,
    remote_subtree: str,
    config: AppConfig,
    *,
    folder: FolderMapping | None = None,
    symlink_mode: str | None = None,
    timeout: int = 1800,
) -> VerifyReport:
    """Verify a selected subtree by comparing local sha1/size to remote metadata."""
    mode = normalize_symlink_mode(
        symlink_mode or (folder.symlink_mode if folder else config.symlink_mode)
    )
    local_root = Path(local_subtree).expanduser().absolute()
    mapping = FolderMapping(
        local_path=str(local_root),
        remote_subpath=remote_subtree.strip("/"),
        symlink_mode=mode,
        bisync_initialized=True,
    )
    try:
        local_entries = scan_local_inventory(mapping, config, hash_all=True)
        remote_nodes = ProtonDriveCLI(config).list_recursive(remote_subtree)
    except ProtonError as exc:
        return VerifyReport(ok=False, errors=[str(exc)], message=str(exc))

    remote_by_rel: dict[str, RemoteNode] = {}
    for node in remote_nodes:
        rel = _remote_rel_under(remote_subtree, node, symlink_mode=mode)
        if rel:
            remote_by_rel[rel] = node

    report = VerifyReport(ok=True)
    for rel, local in local_entries.items():
        node = remote_by_rel.get(rel)
        if node is None:
            report.missing_on_dst.append(rel)
            continue
        local_size, local_sha1 = _local_signature(
            local_root, rel, local.kind, local.local_sha1, local.link_target
        )
        if local_size is not None and node.size != local_size:
            report.different.append(rel)
            continue
        if local_sha1 and node.sha1 and local_sha1.lower() != node.sha1.lower():
            report.different.append(rel)

    for rel in sorted(set(remote_by_rel) - set(local_entries)):
        report.missing_on_src.append(rel)

    report.ok = not (
        report.missing_on_dst
        or report.missing_on_src
        or report.different
        or report.errors
    )
    if not report.ok:
        report.message = (
            f"missing_on_dst={len(report.missing_on_dst)}, "
            f"missing_on_src={len(report.missing_on_src)}, "
            f"different={len(report.different)}, errors={len(report.errors)}"
        )
    return report
