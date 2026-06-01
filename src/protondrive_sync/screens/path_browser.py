"""Path browser modal screens — local DirectoryTree and remote list browser."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, Horizontal
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DirectoryTree,
    Footer,
    Label,
    OptionList,
    Static,
)
from textual.widgets.option_list import Option


# ---------------------------------------------------------------------------
# Local filesystem browser
# ---------------------------------------------------------------------------


class DirectoryOnlyTree(DirectoryTree):
    """DirectoryTree that only shows directories, not files."""

    def filter_paths(self, paths: Iterable[Path]) -> Iterable[Path]:
        return sorted(p for p in paths if p.is_dir())


class LocalBrowserScreen(ModalScreen[str]):
    """Modal for browsing local directories via a tree view.

    Returns the selected directory path as a string, or empty string on cancel.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "select", "Select", show=False),
    ]

    DEFAULT_CSS = """
    LocalBrowserScreen {
        align: center middle;
    }
    #local-browser-dialog {
        width: 80;
        height: 30;
        border: thick $accent;
        padding: 1 2;
        background: $surface;
    }
    #local-browser-dialog .browser-header {
        text-style: bold;
        margin-bottom: 1;
    }
    #local-browser-dialog .current-path {
        color: $text-muted;
        margin-bottom: 1;
    }
    #local-browser-dialog DirectoryOnlyTree {
        height: 1fr;
        margin-bottom: 1;
    }
    #local-browser-dialog .button-row {
        align: right middle;
        height: auto;
    }
    #local-browser-dialog .button-row Button {
        margin: 0 1;
    }
    """

    def __init__(self, start_path: str = "") -> None:
        super().__init__()
        # Determine starting directory
        if start_path:
            p = Path(start_path).expanduser().absolute()
            if p.is_dir():
                self._start = p
            elif p.parent.is_dir():
                self._start = p.parent
            else:
                self._start = Path.home()
        else:
            self._start = Path.home()
        self._selected: str = str(self._start)

    def compose(self) -> ComposeResult:
        with Vertical(id="local-browser-dialog"):
            yield Label("Browse Local Directory", classes="browser-header")
            yield Label(f"  {self._selected}", classes="current-path", id="path-display")
            yield DirectoryOnlyTree(str(self._start))
            with Horizontal(classes="button-row"):
                yield Button("Select", id="select-btn", variant="primary")
                yield Button("Cancel", id="cancel-btn")

    def on_directory_tree_directory_selected(
        self, event: DirectoryTree.DirectorySelected,
    ) -> None:
        """Update current selection when a directory is clicked/entered."""
        self._selected = str(event.path)
        try:
            self.query_one("#path-display", Label).update(f"  {self._selected}")
        except Exception:
            pass

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "select-btn":
            self.dismiss(self._selected)
        else:
            self.dismiss("")

    def action_select(self) -> None:
        self.dismiss(self._selected)

    def action_cancel(self) -> None:
        self.dismiss("")


# ---------------------------------------------------------------------------
# Remote path browser
# ---------------------------------------------------------------------------


class RemoteBrowserScreen(ModalScreen[str]):
    """Modal for browsing remote directories via a list view.

    Fetches directory listings from rclone on demand, with caching.
    Navigation is breadcrumb-style: select a dir to enter, '..' to go up.

    Returns the selected remote path as a string, or empty string on cancel.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("backspace", "go_up", "Up"),
    ]

    DEFAULT_CSS = """
    RemoteBrowserScreen {
        align: center middle;
    }
    #remote-browser-dialog {
        width: 70;
        height: 26;
        border: thick $accent;
        padding: 1 2;
        background: $surface;
    }
    #remote-browser-dialog .browser-header {
        text-style: bold;
        margin-bottom: 1;
    }
    #remote-browser-dialog .current-path {
        color: $text-muted;
        margin-bottom: 1;
    }
    #remote-browser-dialog #remote-status {
        color: $text-muted;
        margin-bottom: 1;
    }
    #remote-browser-dialog OptionList {
        height: 1fr;
        margin-bottom: 1;
    }
    #remote-browser-dialog .button-row {
        align: right middle;
        height: auto;
    }
    #remote-browser-dialog .button-row Button {
        margin: 0 1;
    }
    """

    def __init__(self, remote_name: str, start_path: str = "") -> None:
        super().__init__()
        self._remote_name = remote_name
        self._current_path = start_path.strip("/")
        # Cache: path -> list of dir names
        self._dir_cache: dict[str, list[str]] = {}
        self._loading = False

    def compose(self) -> ComposeResult:
        display_path = self._current_path or "/"
        with Vertical(id="remote-browser-dialog"):
            yield Label(
                f"Browse Remote: {self._remote_name}",
                classes="browser-header",
            )
            yield Label(
                f"  {self._remote_name}:/{display_path}",
                classes="current-path",
                id="remote-path-display",
            )
            yield Label("Loading...", id="remote-status")
            yield OptionList(id="remote-dir-list")
            with Horizontal(classes="button-row"):
                yield Button("Select", id="select-btn", variant="primary")
                yield Button("Cancel", id="cancel-btn")

    def on_mount(self) -> None:
        self._load_current_dir()

    def _load_current_dir(self) -> None:
        """Fetch and display directories at the current path."""
        self._loading = True
        self._update_status("Loading...")

        # Run the network fetch in a background thread
        self.run_worker(
            lambda: self._fetch_dirs(self._current_path),
            thread=True,
            exit_on_error=False,
        )

    def _fetch_dirs(self, path: str) -> None:
        """Background thread: fetch directory listing from remote."""
        if path in self._dir_cache:
            dirs = self._dir_cache[path]
        else:
            from ..core.rclone import list_remote_dirs
            dirs = list_remote_dirs(self._remote_name, path)
            self._dir_cache[path] = dirs

        # Switch back to UI thread to update widgets
        self.app.call_from_thread(self._populate_list, dirs)

    def _populate_list(self, dirs: list[str]) -> None:
        """UI thread: populate the option list with directory entries."""
        self._loading = False
        option_list = self.query_one("#remote-dir-list", OptionList)
        option_list.clear_options()

        # Add '..' entry if not at root
        if self._current_path:
            option_list.add_option(Option("\u2190 ..", id="__parent__"))

        if not dirs:
            self._update_status("(empty directory)")
        else:
            self._update_status(f"{len(dirs)} subdirectories")
            for d in sorted(dirs):
                option_list.add_option(Option(f"\U0001f4c1 {d}", id=d))

        self._update_path_display()

    def _update_path_display(self) -> None:
        display_path = self._current_path or "/"
        try:
            self.query_one("#remote-path-display", Label).update(
                f"  {self._remote_name}:/{display_path}"
            )
        except Exception:
            pass

    def _update_status(self, msg: str) -> None:
        try:
            self.query_one("#remote-status", Label).update(f"  {msg}")
        except Exception:
            pass

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected,
    ) -> None:
        """Navigate into a subdirectory on selection."""
        if self._loading:
            return

        option_id = event.option_id
        if option_id == "__parent__":
            self.action_go_up()
        elif option_id is not None:
            # Navigate into subdirectory
            if self._current_path:
                self._current_path = f"{self._current_path}/{option_id}"
            else:
                self._current_path = option_id
            self._load_current_dir()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "select-btn":
            self.dismiss(self._current_path)
        else:
            self.dismiss("")

    def action_go_up(self) -> None:
        """Navigate to parent directory."""
        if not self._current_path:
            return
        parts = self._current_path.rsplit("/", 1)
        self._current_path = parts[0] if len(parts) > 1 else ""
        self._load_current_dir()

    def action_cancel(self) -> None:
        self.dismiss("")
