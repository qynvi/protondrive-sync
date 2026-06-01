"""Global settings screen — bisync timing, safety thresholds, mount settings, filters."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, Horizontal
from textual.screen import Screen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    Switch,
    TextArea,
)

from ..core.config import AppConfig, save_config, write_filter_file
from ..core.platform import is_linux


class SettingsScreen(Screen):
    """Edit global application settings."""

    BINDINGS = [
        Binding("escape", "cancel", "Back"),
    ]

    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self._config = config

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical(id="settings-form"):
            yield Label(" Settings", classes="section-header")
            yield Label("")

            yield Label(" Remote name:")
            yield Input(value=self._config.remote_name, id="remote-name")

            yield Label("")
            yield Label(" Bisync Timing:", classes="section-header")

            with Horizontal():
                with Vertical():
                    yield Label(" Check interval (sec):")
                    yield Input(value=str(self._config.bisync_check_interval), id="bisync-check")
                with Vertical():
                    yield Label(" Quiet threshold (sec):")
                    yield Input(value=str(self._config.bisync_quiet_threshold), id="bisync-quiet")
                with Vertical():
                    yield Label(" Max burst (sec):")
                    yield Input(value=str(self._config.bisync_max_burst), id="bisync-burst")

            yield Label("")
            yield Label(" Safety Thresholds:", classes="section-header")

            with Horizontal():
                with Vertical():
                    yield Label(" Size change threshold (%):")
                    yield Input(
                        value=str(int(self._config.size_change_threshold * 100)),
                        id="size-threshold",
                    )
                with Vertical():
                    yield Label(" Min file size to flag (KB):")
                    yield Input(
                        value=str(self._config.size_change_min_bytes // 1024),
                        id="size-min-kb",
                    )

            yield Label("")
            yield Label(" Shared:")

            with Horizontal():
                with Vertical():
                    yield Label(" Transfers:")
                    yield Input(value=str(self._config.transfers), id="transfers")
                with Vertical():
                    yield Label(" Pin interval (min):")
                    yield Input(value=str(self._config.pin_interval_minutes), id="pin-interval")

            yield Label("")
            with Horizontal():
                yield Label(" Low-footprint mode: ")
                yield Switch(value=self._config.low_footprint, id="low-footprint")
            yield Label(
                " [dim]Limits CPU (transfers=1, checkers=1, nice=19) and "
                "network (2M up / 10M down). Restarts services on change.[/]"
            )

            yield Label("")
            with Horizontal():
                yield Label(" Follow symlinks: ")
                yield Switch(value=self._config.copy_links, id="copy-links")
            yield Label(
                " [dim]Dereference symlinks inside sync folders and sync their "
                "target content as regular files (--copy-links).[/]"
            )

            yield Label("")
            yield Label(" Mount Mode Settings:", classes="section-header")
            yield Label(" [dim]These only affect mount-mode folders, not bisync.[/]")

            yield Label(" Mount point:")
            yield Input(value=self._config.mount_point, id="mount-point")

            with Horizontal():
                with Vertical():
                    yield Label(" Max cache size:")
                    yield Input(value=self._config.cache_max_size, id="cache-size")
                with Vertical():
                    yield Label(" Max cache age:")
                    yield Input(value=self._config.cache_max_age, id="cache-age")

            with Horizontal():
                with Vertical():
                    yield Label(" Poll interval:")
                    yield Input(value=self._config.poll_interval, id="poll-interval")
                with Vertical():
                    yield Label(" Write-back delay:")
                    yield Input(value=self._config.write_back, id="write-back")

            yield Label(" Dir cache time:")
            yield Input(value=self._config.dir_cache_time, id="dir-cache-time")

            yield Label("")
            yield Label(" Filter rules (one per line, rclone filter syntax):")
            yield TextArea(
                "\n".join(self._config.filters),
                id="filters",
                language="text",
            )

            yield Label("")
            with Horizontal(classes="button-row"):
                yield Button("Save", id="save-btn", variant="primary")
                yield Button("Cancel", id="cancel-btn")

        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel-btn":
            self.app.pop_screen()
        elif event.button.id == "save-btn":
            self._save()

    def _save(self) -> None:
        try:
            prev_low_footprint = self._config.low_footprint

            self._config.remote_name = self.query_one("#remote-name", Input).value.strip()
            self._config.mount_point = self.query_one("#mount-point", Input).value.strip()
            self._config.cache_max_size = self.query_one("#cache-size", Input).value.strip()
            self._config.cache_max_age = self.query_one("#cache-age", Input).value.strip()
            self._config.poll_interval = self.query_one("#poll-interval", Input).value.strip()
            self._config.write_back = self.query_one("#write-back", Input).value.strip()
            self._config.dir_cache_time = self.query_one("#dir-cache-time", Input).value.strip()
            self._config.pin_interval_minutes = int(
                self.query_one("#pin-interval", Input).value.strip()
            )
            self._config.transfers = int(
                self.query_one("#transfers", Input).value.strip()
            )
            self._config.low_footprint = self.query_one("#low-footprint", Switch).value
            self._config.copy_links = self.query_one("#copy-links", Switch).value

            # Bisync timing
            self._config.bisync_check_interval = int(
                self.query_one("#bisync-check", Input).value.strip()
            )
            self._config.bisync_quiet_threshold = int(
                self.query_one("#bisync-quiet", Input).value.strip()
            )
            self._config.bisync_max_burst = int(
                self.query_one("#bisync-burst", Input).value.strip()
            )

            # Safety thresholds
            pct = int(self.query_one("#size-threshold", Input).value.strip())
            self._config.size_change_threshold = pct / 100.0
            kb = int(self.query_one("#size-min-kb", Input).value.strip())
            self._config.size_change_min_bytes = kb * 1024

            filters_text = self.query_one("#filters", TextArea).text
            self._config.filters = [
                line for line in filters_text.splitlines() if line.strip()
            ]

            save_config(self._config)
            write_filter_file(self._config)

            # Auto-restart services if low-footprint changed
            if self._config.low_footprint != prev_low_footprint:
                self._restart_services()

            self.notify("Settings saved.")

            for screen in self.app.screen_stack:
                if hasattr(screen, "reload_config"):
                    screen.reload_config(self._config)

            self.app.pop_screen()

        except ValueError as exc:
            self.notify(f"Invalid value: {exc}", severity="error")

    def _restart_services(self) -> None:
        """Reinstall and restart services to apply changes."""
        try:
            if is_linux():
                from ..service.systemd import (
                    install_services,
                    is_service_active,
                    start_services,
                    stop_services,
                    ALL_SERVICE_NAMES,
                )
                active_before = {
                    name for name in ALL_SERVICE_NAMES
                    if is_service_active(name)
                }
                if active_before:
                    stop_services(self._config)
                install_services(self._config)
                if active_before:
                    start_services(self._config)
                mode = "on" if self._config.low_footprint else "off"
                self.notify(f"Low-footprint {mode} \u2014 services restarted.")
            else:
                self.notify("Service restart not supported on this platform. Restart manually.")
        except Exception as exc:
            self.notify(f"Service restart failed: {exc}", severity="error")

    def action_cancel(self) -> None:
        self.app.pop_screen()
