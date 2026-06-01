"""Service control screen — start/stop/enable/disable systemd or Windows services."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, Horizontal
from textual.screen import Screen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Label,
    Log,
)

from ..core.config import AppConfig
from ..core.platform import is_linux, is_windows


class ServiceLog(Log):
    """Log for service operations."""
    pass


class ServiceControlScreen(Screen):
    """Manage background services (systemd on Linux, Task Scheduler on Windows)."""

    BINDINGS = [
        Binding("escape", "cancel", "Back"),
    ]

    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self._config = config

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical(id="service-form"):
            yield Label(" Service Control", classes="section-header")
            yield Label("")

            backend = "systemd" if is_linux() else "Task Scheduler" if is_windows() else "unsupported"
            yield Label(f" Backend: {backend}")

            modes: list[str] = []
            if self._config.has_bisync_folders():
                modes.append("bisync")
            if self._config.has_mount_folders():
                modes.append("mount+pinner")
            yield Label(f" Active modes: {', '.join(modes) if modes else 'none (add folders first)'}")
            yield Label("")

            with Horizontal(classes="button-row"):
                yield Button("Install", id="install-btn", variant="primary")
                yield Button("Start", id="start-btn", variant="success")
                yield Button("Stop", id="stop-btn", variant="warning")
                yield Button("Status", id="status-btn")
            yield Label("")
            with Horizontal(classes="button-row"):
                yield Button("Enable on login", id="enable-btn")
                yield Button("Disable on login", id="disable-btn")
                yield Button("Uninstall", id="uninstall-btn", variant="error")

            yield Label("")
            yield ServiceLog(id="service-log", max_lines=50)

            yield Label("")
            yield Button("Back", id="back-btn")

        yield Footer()

    def on_mount(self) -> None:
        self._show_status()

    def _log(self, msg: str) -> None:
        try:
            log_widget = self.query_one(ServiceLog)
            log_widget.write_line(msg)
        except Exception:
            pass

    def on_button_pressed(self, event: Button.Pressed) -> None:
        handlers = {
            "install-btn": self._do_install,
            "start-btn": self._do_start,
            "stop-btn": self._do_stop,
            "status-btn": self._show_status,
            "enable-btn": self._do_enable,
            "disable-btn": self._do_disable,
            "uninstall-btn": self._do_uninstall,
            "back-btn": lambda: self.app.pop_screen(),
        }
        handler = handlers.get(event.button.id)
        if handler:
            handler()

    def _show_status(self) -> None:
        if is_linux():
            self._show_systemd_status()
        elif is_windows():
            self._show_windows_status()
        else:
            self._log("Service management not supported on this platform.")

    def _show_systemd_status(self) -> None:
        from ..service.systemd import (
            service_status,
            is_service_active,
            is_service_enabled,
            ALL_SERVICE_NAMES,
        )
        for name in ALL_SERVICE_NAMES:
            status = service_status(name)
            load_state = status.get("LoadState", "unknown")
            if load_state == "not-found":
                self._log(f"{name}: [dim]not installed[/]")
                continue
            active = is_service_active(name)
            enabled = is_service_enabled(name)
            self._log(
                f"{name}: "
                f"{'[green]active[/]' if active else '[red]inactive[/]'} | "
                f"{'enabled' if enabled else 'disabled'} | "
                f"{status.get('SubState', '?')}"
            )

    def _show_windows_status(self) -> None:
        from ..service.windows import is_task_running, ALL_TASK_NAMES
        for name in ALL_TASK_NAMES:
            running = is_task_running(name)
            self._log(
                f"{name}: {'[green]running[/]' if running else '[red]stopped[/]'}"
            )

    def _do_install(self) -> None:
        try:
            if is_linux():
                from ..service.systemd import install_services
                paths = install_services(self._config)
                for p in paths:
                    self._log(f"Installed: {p}")
                if not paths:
                    self._log("No services to install (add folders first).")
                else:
                    self._log("Services installed. Use 'Enable on login' + 'Start'.")
            elif is_windows():
                from ..service.windows import install_tasks
                paths = install_tasks(self._config)
                for p in paths:
                    self._log(f"Task script: {p}")
                self._log("Tasks installed.")
        except Exception as exc:
            self._log(f"Install failed: {exc}")

    def _do_start(self) -> None:
        try:
            if is_linux():
                from ..service.systemd import start_services
                ok = start_services(self._config)
                self._log("Services started." if ok else "Start failed \u2014 check logs.")
            elif is_windows():
                from ..service.windows import start_task, ALL_TASK_NAMES
                for name in ALL_TASK_NAMES:
                    start_task(name)
                self._log("Tasks started.")
        except Exception as exc:
            self._log(f"Start failed: {exc}")
        self._show_status()

    def _do_stop(self) -> None:
        try:
            if is_linux():
                from ..service.systemd import stop_services
                stop_services(self._config)
                self._log("Services stopped.")
            elif is_windows():
                from ..service.windows import stop_task, ALL_TASK_NAMES
                for name in ALL_TASK_NAMES:
                    stop_task(name)
                self._log("Tasks stopped.")
        except Exception as exc:
            self._log(f"Stop failed: {exc}")
        self._show_status()

    def _do_enable(self) -> None:
        if is_linux():
            from ..service.systemd import enable_services
            ok = enable_services(self._config)
            self._log("Enabled on login." if ok else "Enable failed.")
        self._show_status()

    def _do_disable(self) -> None:
        if is_linux():
            from ..service.systemd import disable_services
            disable_services(self._config)
            self._log("Disabled.")
        self._show_status()

    def _do_uninstall(self) -> None:
        if is_linux():
            from ..service.systemd import stop_services, disable_services, _user_unit_dir
            from ..service.systemd import ALL_SERVICE_NAMES
            stop_services(self._config)
            disable_services(self._config)
            unit_dir = _user_unit_dir()
            for name in ALL_SERVICE_NAMES:
                path = unit_dir / f"{name}.service"
                if path.exists():
                    path.unlink()
                    self._log(f"Removed: {path}")
            self._log("Services uninstalled.")
        elif is_windows():
            from ..service.windows import remove_tasks
            remove_tasks()
            self._log("Tasks removed.")

    def action_cancel(self) -> None:
        self.app.pop_screen()
