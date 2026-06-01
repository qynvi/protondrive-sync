"""Cross-platform symlink and junction management."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .platform import is_windows


class SymlinkError(Exception):
    """Raised on symlink/junction failures."""


def create_link(source: Path, target: Path) -> None:
    """Create a directory symlink (Linux/macOS) or junction (Windows).

    Args:
        source: The path where the link will be created (the user's original dir).
        target: The path the link points to (the mount subdirectory).
    """
    if source.exists() and not source.is_symlink():
        raise SymlinkError(
            f"Cannot create link: {source} already exists and is not a symlink. "
            f"Move or remove it first."
        )

    if source.is_symlink():
        # Already a link — check if it points to the right target
        current = source.resolve()
        if current == target.resolve():
            return  # Already correct
        # Points elsewhere — remove and recreate
        source.unlink()

    if is_windows():
        _create_junction(source, target)
    else:
        source.symlink_to(target)


def remove_link(path: Path) -> bool:
    """Remove a symlink or junction. Returns True if removed."""
    if not path.exists() and not path.is_symlink():
        return False

    if is_windows():
        if _is_junction(path):
            # Junctions are removed with rmdir, not unlink
            subprocess.run(["cmd", "/c", "rmdir", str(path)], check=True)
            return True
    if path.is_symlink():
        path.unlink()
        return True

    return False


def is_link(path: Path) -> bool:
    """Check if a path is a symlink or junction."""
    if path.is_symlink():
        return True
    if is_windows() and _is_junction(path):
        return True
    return False


def link_target(path: Path) -> Path | None:
    """Get the target of a symlink/junction, or None if not a link."""
    if path.is_symlink():
        return Path(os.readlink(path))
    if is_windows() and _is_junction(path):
        return _junction_target(path)
    return None


def verify_link(source: Path, expected_target: Path) -> bool:
    """Verify a link exists and points to the expected target."""
    if not is_link(source):
        return False
    actual = link_target(source)
    if actual is None:
        return False
    return actual.resolve() == expected_target.resolve()


# --- Windows-specific helpers ---

def _create_junction(source: Path, target: Path) -> None:
    """Create a Windows directory junction via mklink /J."""
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(source), str(target)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SymlinkError(
            f"Failed to create junction {source} → {target}: {result.stderr}"
        )


def _is_junction(path: Path) -> bool:
    """Check if a path is a Windows junction point."""
    if not is_windows():
        return False
    try:
        import ctypes
        FILE_ATTRIBUTE_REPARSE_POINT = 0x400
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))  # type: ignore[attr-defined]
        return bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)
    except (AttributeError, OSError):
        return False


def _junction_target(path: Path) -> Path | None:
    """Resolve the target of a Windows junction. Fallback approach."""
    try:
        # os.readlink works for junctions on Python 3.8+ on Windows
        return Path(os.readlink(path))
    except OSError:
        return None
