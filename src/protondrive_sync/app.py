"""ProtonDrive Sync — Textual TUI application entry point."""

from __future__ import annotations

import sys
import time

from textual.app import App, ComposeResult
from textual.binding import Binding

from .core.config import load_config, save_config, ConfigError, AppConfig
from .core.platform import (
    find_rclone,
    acquire_instance_lock,
    release_instance_lock,
    get_lock_holder_pid,
    kill_instance,
    InstanceAlreadyRunning,
)
from .screens.main import MainScreen


class ProtonDriveSyncApp(App):
    """TUI management tool for Proton Drive sync via rclone."""

    TITLE = "ProtonDrive Sync"
    SUB_TITLE = "rclone-based folder sync manager"

    CSS = """
    Screen {
        background: $surface;
    }

    .section-header {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }

    #status-text {
        margin: 0 1;
        padding: 0 1;
    }

    #hint-bar {
        dock: bottom;
        margin-top: 1;
    }

    #add-form, #pin-form, #settings-form, #service-form {
        padding: 1 2;
    }

    Input {
        margin: 0 1 1 1;
    }

    TextArea {
        margin: 0 1 1 1;
        height: 8;
    }

    .button-row {
        margin: 1 1;
        align: left middle;
    }

    .button-row Button {
        margin: 0 1 0 0;
    }

    FolderTable {
        margin: 0 1;
        height: auto;
        max-height: 16;
    }

    MigrationLog, ServiceLog {
        margin: 0 1;
        height: 12;
        border: solid $primary;
    }

    #log-view {
        height: 1fr;
    }

    DaemonLog {
        margin: 0 1;
        height: 1fr;
        border: solid $primary;
    }

    Switch {
        margin: 0 1;
    }

    RadioSet {
        margin: 0 1;
    }

    OptionList {
        margin: 0 1;
        height: 10;
    }

    .path-input-row {
        height: auto;
        margin: 0 0;
    }

    .path-input-row Input {
        width: 1fr;
    }

    .browse-btn {
        width: auto;
        min-width: 10;
        margin: 0 1 1 0;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._config: AppConfig | None = None

    def on_mount(self) -> None:
        # Pre-flight checks
        if not find_rclone():
            self.notify(
                "rclone not found! Install it first: https://rclone.org/install/",
                severity="error",
                timeout=10,
            )

        # Load or create config
        try:
            self._config = load_config()
        except ConfigError as exc:
            self.notify(f"Config error: {exc}", severity="error", timeout=10)
            self._config = AppConfig()

        # Save default config if none exists
        try:
            save_config(self._config)
        except OSError as exc:
            self.notify(f"Cannot save config: {exc}", severity="warning")

        self.push_screen(MainScreen(self._config))


def main() -> None:
    """CLI entry point."""
    try:
        acquire_instance_lock()
    except InstanceAlreadyRunning:
        pid = get_lock_holder_pid()
        print("Another ProtonDrive Sync instance is already running.")
        if pid:
            print(f"  Holder PID: {pid}")
        print()
        try:
            answer = input("Kill the other instance and start a new one? [y/N] ")
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(1)

        if answer.strip().lower() not in ("y", "yes"):
            sys.exit(0)

        if pid and kill_instance(pid):
            print("Previous instance terminated. Starting...")
            time.sleep(0.5)
            try:
                acquire_instance_lock()
            except InstanceAlreadyRunning:
                print("Failed to acquire lock after killing. Exiting.")
                input("Press Enter to exit...")
                sys.exit(1)
        else:
            msg = f"Failed to terminate PID {pid}." if pid else "Could not identify holder PID."
            print(f"{msg} You may need to kill it manually.")
            input("Press Enter to exit...")
            sys.exit(1)

    try:
        app = ProtonDriveSyncApp()
        app.run()
    finally:
        release_instance_lock()


if __name__ == "__main__":
    main()
