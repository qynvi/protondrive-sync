"""Preflight checks for Proton Drive path normalization hazards."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from .migration import _matches_filter, _walk_unfiltered


@dataclass
class PathSafetyIssue:
    """One path-safety warning or blocker."""

    path: str
    message: str
    blocking: bool = False


@dataclass
class PathSafetyReport:
    """Result of scanning local paths before setup."""

    issues: list[PathSafetyIssue] = field(default_factory=list)

    @property
    def blocking_issues(self) -> list[PathSafetyIssue]:
        return [issue for issue in self.issues if issue.blocking]

    @property
    def ok(self) -> bool:
        return not self.blocking_issues


def _proton_segment_name(segment: str, *, cross_platform: bool) -> str:
    normalized = unicodedata.normalize("NFC", segment).strip(" ")
    if cross_platform:
        normalized = normalized.casefold()
    return normalized


def _proton_path_key(path: str, *, cross_platform: bool) -> str:
    return "/".join(
        _proton_segment_name(part, cross_platform=cross_platform)
        for part in Path(path).parts
    )


def scan_path_safety(
    local_path: Path,
    filters: list[str],
    *,
    cross_platform: bool = False,
) -> PathSafetyReport:
    """Scan for local names likely to mutate/collide on Proton Drive.

    Proton Drive can normalize some names. Collisions must block setup because
    they make verification and later repair ambiguous.
    """
    report = PathSafetyReport()
    seen: dict[str, str] = {}
    root = local_path.absolute()
    if not root.exists():
        return report

    def check_rel(rel: str) -> None:
        parts = Path(rel).parts
        for segment in parts:
            if segment != segment.strip(" "):
                report.issues.append(
                    PathSafetyIssue(
                        rel,
                        "path segment has leading or trailing spaces that Proton Drive may strip",
                        blocking=False,
                    )
                )
            if any(0xDC80 <= ord(char) <= 0xDCFF for char in segment):
                report.issues.append(
                    PathSafetyIssue(
                        rel,
                        "path segment contains undecodable surrogate characters",
                        blocking=True,
                    )
                )
        key = _proton_path_key(rel, cross_platform=cross_platform)
        existing = seen.get(key)
        if existing is not None and existing != rel:
            report.issues.append(
                PathSafetyIssue(
                    rel,
                    f"path collides with {existing!r} after Proton Drive normalization",
                    blocking=True,
                )
            )
        else:
            seen[key] = rel

    try:
        for current, dirnames, filenames in _walk_unfiltered(root, filters):
            for name in [*dirnames, *filenames]:
                rel = (current / name).relative_to(root).as_posix()
                if _matches_filter(rel, filters):
                    continue
                check_rel(rel)
    except (OSError, PermissionError):
        # Other setup scans surface access failures; path safety stays focused
        # on deterministic naming hazards.
        pass
    return report
