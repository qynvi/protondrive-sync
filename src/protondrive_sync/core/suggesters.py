"""Path suggesters for Input widgets — local filesystem and Proton Drive paths."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

from textual.suggester import Suggester


class LocalPathSuggester(Suggester):
    """Suggests local filesystem paths as the user types.

    Provides ghost-text inline completion for directory paths.
    Pressing Right arrow accepts the suggestion.

    Behaviour:
    - "/home/us" → suggests "/home/user/" (first matching entry)
    - "/home/user/" → suggests first child directory
    - Only suggests directories, not files
    """

    def __init__(self) -> None:
        super().__init__(use_cache=False, case_sensitive=True)

    async def get_suggestion(self, value: str) -> Optional[str]:
        """Return a path completion suggestion for the current input."""
        if not value:
            return None

        # Run the blocking I/O in a thread to avoid stalling the event loop
        return await asyncio.to_thread(self._suggest_path, value)

    def _suggest_path(self, value: str) -> Optional[str]:
        """Synchronous path suggestion logic."""
        if not value or not value.startswith(("/", "~")):
            # Only suggest for absolute paths or home-relative (~) paths
            return None

        path = Path(value).expanduser()

        # If value ends with '/', list children of that directory
        if value.endswith("/"):
            if not path.is_dir():
                return None
            return self._first_child_dir(path, prefix="")

        # Extract partial from raw string rather than Path-normalized form.
        # Path normalizes away trailing "." (e.g. "dir/." -> "dir"),
        # which loses the dot-prefix the user typed for hidden dirs.
        if "/" in value:
            raw_parent, partial = value.rsplit("/", 1)
            parent = Path(raw_parent).expanduser() if raw_parent else Path("/")
        else:
            parent = path.parent
            partial = value

        if not parent.is_dir():
            return None

        return self._first_child_dir(parent, prefix=partial)

    def _first_child_dir(self, parent: Path, prefix: str) -> Optional[str]:
        """Find the first directory in parent matching the prefix."""
        try:
            entries = sorted(parent.iterdir())
        except (PermissionError, OSError):
            return None

        for entry in entries:
            if not entry.is_dir():
                continue
            if entry.name.startswith(".") and not prefix.startswith("."):
                # Don't suggest hidden dirs unless user typed a dot
                continue
            if prefix and not entry.name.startswith(prefix):
                continue
            # Return full path with trailing slash
            return str(entry) + "/"

        return None


class RemotePathSuggester(Suggester):
    """Suggests remote directory paths from Proton Drive CLI list results.

    Caches directory listings to avoid repeated API calls. The cache
    is populated on first access and when the user navigates to a new
    directory level.

    Provides ghost-text inline completion for remote subpaths.
    """

    def __init__(self) -> None:
        super().__init__(use_cache=False, case_sensitive=True)
        # Cache: parent_path -> list of child dir names
        self._dir_cache: dict[str, list[str]] = {}

    async def get_suggestion(self, value: str) -> Optional[str]:
        """Return a remote path completion suggestion."""
        if not value:
            return await asyncio.to_thread(self._suggest_root)

        return await asyncio.to_thread(self._suggest_remote, value)

    def _suggest_root(self) -> Optional[str]:
        """Suggest from root level."""
        dirs = self._list_remote_dirs("")
        if dirs:
            return dirs[0]
        return None

    def _suggest_remote(self, value: str) -> Optional[str]:
        """Synchronous remote path suggestion logic."""
        # If value ends with '/', list children of that path
        if value.endswith("/"):
            parent = value.rstrip("/")
            dirs = self._list_remote_dirs(parent)
            if dirs:
                return value + dirs[0]
            return None

        # Otherwise, complete partial name within parent
        parts = value.rsplit("/", 1)
        if len(parts) == 2:
            parent, partial = parts
        else:
            parent, partial = "", parts[0]

        dirs = self._list_remote_dirs(parent)
        for d in dirs:
            if d.startswith(partial):
                return f"{parent}/{d}" if parent else d
        return None

    def _list_remote_dirs(self, path: str) -> list[str]:
        """List remote directories at path, with caching."""
        if path in self._dir_cache:
            return self._dir_cache[path]

        try:
            from .config import load_config
            from .proton_cli import ProtonDriveCLI

            dirs = [
                node.name
                for node in ProtonDriveCLI(load_config()).list_dir(path)
                if node.is_dir and node.name
            ]
            self._dir_cache[path] = sorted(dirs)
            return self._dir_cache[path]
        except Exception:
            return []

    def invalidate_cache(self) -> None:
        """Clear the cached directory listings."""
        self._dir_cache.clear()
