"""Review screen — approve or reject flagged changes before sync proceeds."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Label,
)

from ..core.config import AppConfig, FolderMapping
from ..core.bisync import (
    read_pending_review,
    clear_pending_review,
    FlaggedChange,
)


def _human_size(size_bytes: int) -> str:
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


class ReviewScreen(Screen):
    """Review flagged changes that are blocking sync for a folder."""

    BINDINGS = [
        Binding("a", "approve", "Approve sync"),
        Binding("s", "skip", "Keep paused"),
        Binding("escape", "cancel", "Back"),
    ]

    def __init__(self, config: AppConfig, folder: FolderMapping) -> None:
        super().__init__()
        self._config = config
        self._folder = folder
        self._flagged: list[FlaggedChange] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical(id="review-form"):
            yield Label(" Review Required", classes="section-header")
            yield Label(f" Folder: {self._folder.local_path}")
            yield Label("")
            yield Label(" The following files changed significantly in size:")
            yield Label(" Sync is paused until you approve or reject these changes.")
            yield Label("")

            with VerticalScroll():
                yield DataTable(id="review-table")

            yield Label("")
            with Horizontal(classes="button-row"):
                yield Button("Approve sync [a]", id="approve-btn", variant="primary")
                yield Button("Keep paused [s]", id="skip-btn", variant="warning")
                yield Button("Back [esc]", id="back-btn")

        yield Footer()

    def on_mount(self) -> None:
        reviews = read_pending_review(self._config)
        self._flagged = reviews.get(self._folder.local_path, [])

        table = self.query_one("#review-table", DataTable)
        table.add_columns("File", "Local Size", "Remote Size", "Change")
        table.zebra_stripes = True

        for fc in self._flagged:
            direction = "+" if fc.local_size > fc.remote_size else ""
            table.add_row(
                fc.path,
                _human_size(fc.local_size),
                _human_size(fc.remote_size),
                f"{direction}{fc.change_pct:.0f}%",
            )

        if not self._flagged:
            table.add_row("No flagged changes", "", "", "")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "approve-btn":
            self._do_approve()
        elif event.button.id == "skip-btn":
            self.app.pop_screen()
        elif event.button.id == "back-btn":
            self.app.pop_screen()

    def _do_approve(self) -> None:
        clear_pending_review(self._config, self._folder.local_path)
        self.notify("Sync approved. Changes will sync on next cycle.")

        # Refresh main screen
        for screen in self.app.screen_stack:
            if hasattr(screen, "reload_config"):
                screen.reload_config(self._config)

        self.app.pop_screen()

    def action_approve(self) -> None:
        self._do_approve()

    def action_skip(self) -> None:
        self.app.pop_screen()

    def action_cancel(self) -> None:
        self.app.pop_screen()
