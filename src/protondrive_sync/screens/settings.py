"""Global settings screen — bisync timing, safety thresholds, filters."""

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
    Select,
    Switch,
    TextArea,
)

from ..core.config import (
    AppConfig,
    INTEGRITY_MODES,
    SYMLINK_MODE_LABELS,
    SYMLINK_MODES,
    normalize_symlink_mode,
    save_config,
    write_filter_file,
)


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

            yield Label(" Bisync Timing:", classes="section-header")

            with Horizontal():
                with Vertical():
                    yield Label(" Check interval (sec):")
                    yield Input(
                        value=str(self._config.bisync_check_interval), id="bisync-check"
                    )
                with Vertical():
                    yield Label(" Quiet threshold (sec):")
                    yield Input(
                        value=str(self._config.bisync_quiet_threshold),
                        id="bisync-quiet",
                    )
                with Vertical():
                    yield Label(" Max burst (sec):")
                    yield Input(
                        value=str(self._config.bisync_max_burst), id="bisync-burst"
                    )

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
                    yield Label(" CLI concurrency:")
                    yield Input(
                        value=str(self._config.proton_cli_concurrency),
                        id="cli-concurrency",
                    )

            yield Label("")
            with Horizontal():
                yield Label(" Low-footprint mode: ")
                yield Switch(value=self._config.low_footprint, id="low-footprint")
            yield Label(
                " [dim]Runs the daemon with low OS priority. Restarts services on change.[/]"
            )

            yield Label("")
            yield Label(" Targeted Sync:", classes="section-header")
            with Horizontal():
                yield Label(" Enable targeted P3 sync: ")
                yield Switch(
                    value=self._config.targeted_sync_enabled, id="targeted-sync"
                )
            with Horizontal():
                with Vertical():
                    yield Label(" Integrity mode:")
                    yield Select(
                        [(mode.replace("_", " "), mode) for mode in INTEGRITY_MODES],
                        value=self._config.integrity_mode,
                        allow_blank=False,
                        id="integrity-mode",
                    )
                with Vertical():
                    yield Label(" Journal poll (sec):")
                    yield Input(
                        value=str(self._config.journal_poll_interval_seconds),
                        id="journal-poll",
                    )
                with Vertical():
                    yield Label(" Audit budget (min):")
                    yield Input(
                        value=str(self._config.remote_audit_time_budget_minutes),
                        id="audit-budget",
                    )
                with Vertical():
                    yield Label(" Backup retention (days):")
                    yield Input(
                        value=str(self._config.backup_retention_days),
                        id="backup-retention",
                    )

            yield Label("")
            with Horizontal():
                yield Label(" Default symlink mode: ")
                yield Select(
                    [(SYMLINK_MODE_LABELS[mode], mode) for mode in SYMLINK_MODES],
                    value=self._config.symlink_mode,
                    allow_blank=False,
                    id="symlink-mode",
                )
            yield Label(
                " [dim]New sync folders default to preserving symlinks as link "
                "metadata blobs, not traversing target content.[/]"
            )

            yield Label("")
            yield Label(" Filter rules (one per line):")
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

            self._config.proton_cli_concurrency = int(
                self.query_one("#cli-concurrency", Input).value.strip()
            )
            self._config.low_footprint = self.query_one("#low-footprint", Switch).value
            self._config.targeted_sync_enabled = self.query_one(
                "#targeted-sync", Switch
            ).value
            self._config.integrity_mode = str(
                self.query_one("#integrity-mode", Select).value
            )
            self._config.journal_poll_interval_seconds = int(
                self.query_one("#journal-poll", Input).value.strip()
            )
            self._config.remote_audit_time_budget_minutes = int(
                self.query_one("#audit-budget", Input).value.strip()
            )
            self._config.backup_retention_days = int(
                self.query_one("#backup-retention", Input).value.strip()
            )
            self._config.symlink_mode = normalize_symlink_mode(
                str(self.query_one("#symlink-mode", Select).value)
            )

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
                    name for name in ALL_SERVICE_NAMES if is_service_active(name)
                }
                if active_before:
                    stop_services(self._config)
                install_services(self._config)
                if active_before:
                    start_services(self._config)
                mode = "on" if self._config.low_footprint else "off"
                self.notify(f"Low-footprint {mode} \u2014 services restarted.")
            else:
                self.notify(
                    "Service restart not supported on this platform. Restart manually."
                )
        except Exception as exc:
            self.notify(f"Service restart failed: {exc}", severity="error")

    def action_cancel(self) -> None:
        self.app.pop_screen()
