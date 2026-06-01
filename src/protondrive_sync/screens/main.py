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
from ..core.rclone import is_mounted
from ..core.symlinks import is_link, verify_link
from ..core.bisync import has_pending_review
from ..core.git_meta import read_metadata, rehydration_summary
from pathlib import Path


class StatusBar(Static):
    """Top status bar showing connection and cache info."""

    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self._config = config

    def compose(self) -> ComposeResult:
        yield Label(self._build_status(), id="status-text")

    def _build_status(self) -> str:
        parts: list[str] = [f"Remote: {self._config.remote_name}"]

        # Mount status (only if mount-mode folders exist)
        if self._config.has_mount_folders():
            mounted = is_mounted(self._config.mount_point)
            mstatus = "[green]mounted[/]" if mounted else "[red]not mounted[/]"
            parts.append(f"Mount: {mstatus}")

        if self._config.has_bisync_folders():
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
        Binding("p", "pin_settings", "Pin settings"),
        Binding("v", "review", "Review"),
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
        table.add_columns(
            "Status",
            "Mode",
            "Local Path",
            "Remote Path",
            "Git",
            "Info",
        )

    def _populate_table(self) -> None:
        table = self.query_one(FolderTable)
        table.clear()

        mounted = is_mounted(self._config.mount_point)

        for folder in self._config.folders:
            mode = folder.sync_mode

            if mode == "bisync":
                status, info = self._bisync_folder_status(folder)
            else:
                status, info = self._mount_folder_status(folder, mounted)

            git_col = self._git_column(folder)

            table.add_row(
                status,
                mode,
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
            return "[red][review required][/]", "press [v] to review"

        if not folder.bisync_initialized:
            return "[yellow]not initialized[/]", "run setup first"

        return "[green]synced[/]", ""

    def _mount_folder_status(self, folder: FolderMapping, mounted: bool) -> tuple[str, str]:
        """Get status and info for a mount-mode folder."""
        mount_target = Path(self._config.mount_point) / folder.remote_subpath
        local = Path(folder.local_path)

        if not folder.enabled:
            return "[yellow]paused[/]", ""
        if not mounted:
            return "[red]mount down[/]", ""

        # Check symlink health
        if is_link(local):
            link_ok = verify_link(local, mount_target)
            link_info = "link OK" if link_ok else "[red]link broken[/]"
        else:
            link_info = "no link"

        pin = folder.pin_mode.replace("_", " ")
        if folder.pin_subdirs and folder.pin_mode == "on_demand":
            pin += f" (+{len(folder.pin_subdirs)})"

        return "[green]synced[/]", f"{pin} | {link_info}"

    def _git_column(self, folder: FolderMapping) -> str:
        """Build the Git column text for a folder row."""
        try:
            local_path = Path(folder.local_path)
            meta = read_metadata(local_path)
            if meta is None or not meta.git_repos:
                return ""
            total, rehydrated = rehydration_summary(local_path, meta)
            if folder.sync_mode == "mount":
                # Mount mode: show count but grayed out (can't rehydrate)
                return f"[dim]{total} repo{'s' if total != 1 else ''}[/]"
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
        if table.cursor_row is not None and table.cursor_row < len(self._config.folders):
            folder = self._config.folders[table.cursor_row]
            from .add_folder import AddFolderScreen
            self.app.push_screen(AddFolderScreen(self._config, editing=folder))

    def _needs_mount_teardown(self, folder: FolderMapping) -> bool:
        """Detect whether a folder needs mount teardown based on filesystem state.

        Don't trust sync_mode alone — check for symlinks and backups on disk.
        This catches cases where sync_mode was incorrectly set (e.g. due to
        path resolution bugs in older config versions).
        """
        src = Path(folder.local_path)
        backup = src.parent / f"{src.name}.premigration-backup"
        return (
            folder.sync_mode == "mount"
            or src.is_symlink()
            or backup.exists()
        )

    def action_remove_folder(self) -> None:
        table = self.query_one(FolderTable)
        if table.cursor_row is not None and table.cursor_row < len(self._config.folders):
            folder = self._config.folders[table.cursor_row]
            from .confirm import ConfirmScreen

            if self._needs_mount_teardown(folder):
                msg = (
                    f"Remove folder (mount teardown required)?\n\n"
                    f"Local: {folder.local_path}\n"
                    f"Remote: {folder.remote_subpath}\n\n"
                    f"This will:\n"
                    f"- Restore your local directory from backup\n"
                    f"- Remove the symlink to the mount\n"
                    f"- Merge any files modified since migration\n"
                    f"- Recover .git/ and other filtered items\n\n"
                    f"Files on Proton Drive are NOT deleted."
                )
            else:
                msg = (
                    f"Remove bisync folder?\n\n"
                    f"Local: {folder.local_path}\n"
                    f"Remote: {folder.remote_subpath}\n\n"
                    f"This removes the sync mapping only.\n"
                    f"Your local files and remote files are unaffected."
                )

            self.app.push_screen(
                ConfirmScreen(msg),
                callback=lambda confirmed, f=folder: self._do_remove(f) if confirmed else None,
            )

    def _do_remove(self, folder: FolderMapping) -> None:
        from ..core.config import remove_folder
        from ..core.migration import teardown_mount

        if self._needs_mount_teardown(folder):
            result = teardown_mount(
                local_path=folder.local_path,
                mount_point=self._config.mount_point,
                remote_subpath=folder.remote_subpath,
                filters=self._config.filters,
                progress=lambda msg: None,  # silent — result message shown via notify
            )
            remove_folder(self._config, folder.local_path)
            save_config(self._config)
            self._populate_table()

            if result.success:
                self.notify(f"Removed: {result.message}")
            else:
                self.notify(f"Warning: {result.message}", severity="warning")
        else:
            # Pure bisync: just remove the mapping
            remove_folder(self._config, folder.local_path)
            save_config(self._config)
            self._populate_table()
            self.notify(f"Removed: {folder.local_path}")

    def action_pin_settings(self) -> None:
        table = self.query_one(FolderTable)
        if table.cursor_row is not None and table.cursor_row < len(self._config.folders):
            folder = self._config.folders[table.cursor_row]
            from .pin_settings import PinSettingsScreen
            self.app.push_screen(PinSettingsScreen(self._config, folder))

    def action_rehydrate_git(self) -> None:
        table = self.query_one(FolderTable)
        if table.cursor_row is None or table.cursor_row >= len(self._config.folders):
            return
        folder = self._config.folders[table.cursor_row]

        # Mount-mode guard
        if folder.sync_mode == "mount":
            self.notify(
                "Git rehydration requires a local directory (bisync mode). "
                "It cannot create .git/ inside a FUSE mount. "
                "To rehydrate, remove this folder and re-add it in bisync mode.",
                severity="warning",
                timeout=8,
            )
            return

        # Check for metadata
        meta = read_metadata(Path(folder.local_path))
        if meta is None or not meta.git_repos:
            self.notify("No git metadata found for this folder.", severity="warning")
            return

        from .rehydrate import RehydrateScreen
        self.app.push_screen(RehydrateScreen(self._config, folder))

    def action_review(self) -> None:
        table = self.query_one(FolderTable)
        if table.cursor_row is not None and table.cursor_row < len(self._config.folders):
            folder = self._config.folders[table.cursor_row]
            if has_pending_review(self._config, folder.local_path):
                from .review import ReviewScreen
                self.app.push_screen(ReviewScreen(self._config, folder))
            else:
                self.notify("No pending review for this folder.")

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
