"""Main screen — folder list, status indicators, service controls."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Label,
    Static,
)
from textual.timer import Timer

from ..core.config import AppConfig, FolderMapping, load_config, save_config
from ..core.bisync import has_pending_review
from ..core.git_meta import read_metadata, rehydration_summary
from ..core.state import get_folder_state
from pathlib import Path


class StatusBar(Static):
    """Top status bar showing connection and cache info."""

    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self._config = config

    def compose(self) -> ComposeResult:
        yield Label(self._build_status(), id="status-text")

    def _build_status(self) -> str:
        parts: list[str] = ["Remote: Proton Drive"]

        if self._config.has_enabled_folders():
            from ..service.systemd import is_service_active, BISYNC_SERVICE_NAME

            try:
                active = is_service_active(BISYNC_SERVICE_NAME)
                bstatus = "[green]running[/]" if active else "[yellow]stopped[/]"
            except Exception:
                bstatus = "[dim]unknown[/]"
            parts.append(f"Bisync: {bstatus}")

        if self._config.low_footprint:
            parts.append("[yellow][low-footprint][/]")

        return "  ".join(parts)

    def refresh_status(self, config: AppConfig) -> None:
        self._config = config
        try:
            label = self.query_one("#status-text", Label)
            label.update(self._build_status())
        except Exception:
            pass


class FolderTable(DataTable):
    """Table showing all synced folder mappings."""

    def __init__(self) -> None:
        super().__init__()
        self.cursor_type = "row"
        self.zebra_stripes = True


class MainScreen(Screen):
    """Primary application screen."""

    BINDINGS = [
        Binding("a", "add_folder", "Add folder"),
        Binding("e", "edit_folder", "Edit"),
        Binding("r", "remove_folder", "Remove"),
        Binding("g", "rehydrate_git", "Git rehydrate"),
        Binding("v", "review", "Review"),
        Binding("y", "verify", "Verify"),
        Binding("l", "logs", "Logs"),
        Binding("d", "service_control", "Service"),
        Binding("s", "settings", "Settings"),
        Binding("f", "refresh", "Refresh"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self._config = config
        self._refresh_timer: Timer | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical():
            yield StatusBar(self._config)
            yield Label("")
            yield Label(" Synced Folders", classes="section-header")
            with VerticalScroll():
                yield FolderTable()
        yield Footer()

    def on_mount(self) -> None:
        self._setup_table()
        self._populate_table()
        self._refresh_timer = self.set_interval(10, self._auto_refresh)

    def _setup_table(self) -> None:
        table = self.query_one(FolderTable)
        table.add_columns("Status", "Local Path", "Remote Path", "Git", "Info")

    def _populate_table(self) -> None:
        table = self.query_one(FolderTable)
        table.clear()

        for folder in self._config.folders:
            status, info = self._bisync_folder_status(folder)

            git_col = self._git_column(folder)

            table.add_row(
                status,
                str(folder.local_path),
                folder.remote_subpath,
                git_col,
                info,
            )

    def _bisync_folder_status(self, folder: FolderMapping) -> tuple[str, str]:
        """Get status and info for a bisync-mode folder."""
        if not folder.enabled:
            return "[yellow]paused[/]", ""

        if has_pending_review(self._config, folder.local_path):
            return "[red][review required][/]", "press v to review"

        runtime = get_folder_state(self._config, folder)
        if runtime.status == "degraded":
            info = runtime.last_error or "manual verify/repair required"
            return "[red]degraded[/]", info[:80]
        if runtime.status == "pending_review":
            return "[red][review required][/]", (
                runtime.last_error or "press v to review"
            )[:80]
        if runtime.status == "syncing":
            return "[yellow]syncing[/]", "daemon/setup in progress"
        if runtime.status == "verifying":
            return "[yellow]verifying[/]", "integrity check in progress"
        if runtime.status == "journal_pending":
            return "[red]journal pending[/]", (
                runtime.last_error or "retry journal before syncing"
            )[:80]
        if runtime.status == "journal_stale":
            return (
                "[yellow]journal stale[/]",
                "journal poll failed; exact local sync only",
            )
        if runtime.status == "audit_due":
            return "[yellow]audit due[/]", "partitioned audit recommended"

        if not folder.bisync_initialized:
            return "[yellow]not initialized[/]", "run setup first"

        return "[green]synced[/]", ""

    def _git_column(self, folder: FolderMapping) -> str:
        """Build the Git column text for a folder row."""
        try:
            local_path = Path(folder.local_path)
            meta = read_metadata(local_path)
            if meta is None or not meta.git_repos:
                return ""
            total, rehydrated = rehydration_summary(local_path, meta)
            if rehydrated == total:
                return f"[green]{total} repo{'s' if total != 1 else ''}[/]"
            else:
                return f"[yellow]{total} repo{'s' if total != 1 else ''}[/]"
        except Exception:
            return ""

    def _auto_refresh(self) -> None:
        try:
            status_bar = self.query_one(StatusBar)
            status_bar.refresh_status(self._config)
        except Exception:
            pass

    def reload_config(self, config: AppConfig) -> None:
        self._config = config
        self._populate_table()
        status_bar = self.query_one(StatusBar)
        status_bar.refresh_status(config)

    # --- Actions ---

    def action_add_folder(self) -> None:
        from .add_folder import AddFolderScreen

        self.app.push_screen(AddFolderScreen(self._config))

    def action_edit_folder(self) -> None:
        table = self.query_one(FolderTable)
        if table.cursor_row is not None and table.cursor_row < len(
            self._config.folders
        ):
            folder = self._config.folders[table.cursor_row]
            from .add_folder import AddFolderScreen

            self.app.push_screen(AddFolderScreen(self._config, editing=folder))

    def action_remove_folder(self) -> None:
        table = self.query_one(FolderTable)
        if table.cursor_row is not None and table.cursor_row < len(
            self._config.folders
        ):
            folder = self._config.folders[table.cursor_row]
            from .confirm import ConfirmScreen

            msg = (
                f"Remove sync folder?\n\n"
                f"Local: {folder.local_path}\n"
                f"Remote: {folder.remote_subpath}\n\n"
                f"This removes the sync mapping only.\n"
                f"Your local files and remote files are unaffected."
            )

            self.app.push_screen(
                ConfirmScreen(msg),
                callback=lambda confirmed, f=folder: (
                    self._do_remove(f) if confirmed else None
                ),
            )

    def _do_remove(self, folder: FolderMapping) -> None:
        from ..core.config import remove_folder

        remove_folder(self._config, folder.local_path)
        save_config(self._config)
        self._populate_table()
        self.notify(f"Removed: {folder.local_path}")

    def action_rehydrate_git(self) -> None:
        table = self.query_one(FolderTable)
        if table.cursor_row is None or table.cursor_row >= len(self._config.folders):
            return
        folder = self._config.folders[table.cursor_row]

        # Check for metadata
        meta = read_metadata(Path(folder.local_path))
        if meta is None or not meta.git_repos:
            self.notify("No git metadata found for this folder.", severity="warning")
            return

        from .rehydrate import RehydrateScreen

        self.app.push_screen(RehydrateScreen(self._config, folder))

    def action_review(self) -> None:
        table = self.query_one(FolderTable)
        if table.cursor_row is not None and table.cursor_row < len(
            self._config.folders
        ):
            folder = self._config.folders[table.cursor_row]
            if has_pending_review(self._config, folder.local_path):
                from .review import ReviewScreen

                self.app.push_screen(ReviewScreen(self._config, folder))
            else:
                self.notify("No pending review for this folder.")

    def action_verify(self) -> None:
        table = self.query_one(FolderTable)
        if table.cursor_row is not None and table.cursor_row < len(
            self._config.folders
        ):
            folder = self._config.folders[table.cursor_row]
            from .verify import VerifyScreen

            self.app.push_screen(VerifyScreen(self._config, folder))

    def action_logs(self) -> None:
        from .logs import LogScreen

        self.app.push_screen(LogScreen())

    def action_service_control(self) -> None:
        from .service_control import ServiceControlScreen

        self.app.push_screen(ServiceControlScreen(self._config))

    def action_settings(self) -> None:
        from .settings import SettingsScreen

        self.app.push_screen(SettingsScreen(self._config))

    def action_refresh(self) -> None:
        try:
            self._config = load_config()
        except Exception:
            pass
        self.reload_config(self._config)
        self.notify("Refreshed")

    def action_quit(self) -> None:
        self.app.exit()
