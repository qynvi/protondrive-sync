"""Cache pinning daemon — keeps designated folders warm in the rclone VFS cache."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from .config import AppConfig, FolderMapping


class Pinner:
    """Background thread that walks pinned directories to keep files in VFS cache.

    Reading a file on an rclone FUSE mount populates it in the VFS cache.
    By periodically walking pinned directories, we ensure those files remain
    cached and survive eviction (since cache eviction is based on last-access time).
    """

    def __init__(
        self,
        config: AppConfig,
        on_status: Optional[Callable[[str], None]] = None,
    ):
        self._config = config
        self._on_status = on_status or (lambda _: None)
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Start the pinning daemon in a background thread."""
        if self.running:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="pinner-daemon",
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the daemon to stop and wait for it."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None

    def pin_once(self) -> dict[str, int]:
        """Run a single pin pass. Returns {folder_path: files_touched}."""
        results: dict[str, int] = {}
        for folder in self._config.folders:
            if not folder.enabled:
                continue
            touched = self._pin_folder(folder)
            results[folder.local_path] = touched
        return results

    def _run_loop(self) -> None:
        """Main loop: pin, sleep, repeat."""
        interval = self._config.pin_interval_minutes * 60
        while not self._stop_event.is_set():
            try:
                self._on_status("Pinning pass started")
                results = self.pin_once()
                total = sum(results.values())
                self._on_status(f"Pinning pass complete: {total} files touched")
            except Exception as exc:
                self._on_status(f"Pinning error: {exc}")

            # Sleep in short intervals to respond to stop quickly
            for _ in range(interval):
                if self._stop_event.is_set():
                    return
                time.sleep(1)

    def _pin_folder(self, folder: FolderMapping) -> int:
        """Pin a single folder mapping. Returns count of files touched."""
        mount_base = Path(self._config.mount_point)

        if folder.pin_mode == "keep_offline":
            # Pin the entire folder
            target = mount_base / folder.remote_subpath
            return self._walk_and_touch(target)

        elif folder.pin_mode == "on_demand" and folder.pin_subdirs:
            # Pin only specific subdirectories
            total = 0
            for subdir in folder.pin_subdirs:
                target = mount_base / folder.remote_subpath / subdir
                total += self._walk_and_touch(target)
            return total

        return 0

    def _walk_and_touch(self, directory: Path) -> int:
        """Walk a directory tree, reading first bytes to populate VFS cache.

        We only need to open+read a tiny amount — this is enough
        to make rclone fetch and cache the file.
        """
        if not directory.exists():
            return 0

        count = 0
        try:
            for root, _dirs, files in os.walk(directory):
                for filename in files:
                    filepath = Path(root) / filename
                    try:
                        # Read first 1 byte — triggers VFS cache fetch
                        with open(filepath, "rb") as f:
                            f.read(1)
                        count += 1
                    except (OSError, PermissionError):
                        continue
        except (OSError, PermissionError):
            pass

        return count
