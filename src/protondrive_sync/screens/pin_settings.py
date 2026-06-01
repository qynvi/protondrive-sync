"""Pin settings screen — select subdirectories to keep offline (mount mode only)."""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Label,
    OptionList,
    RadioButton,
    RadioSet,
)
from textual.widgets.option_list import Option

from ..core.config import AppConfig, FolderMapping, save_config


class PinSettingsScreen(Screen):
    """Configure per-folder pinning: keep-offline vs on-demand, with subdir selection."""

    BINDINGS = [
        Binding("escape", "cancel", "Back"),
    ]

    def __init__(self, config: AppConfig, folder: FolderMapping) -> None:
        super().__init__()
        self._config = config
        self._folder = folder

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical(id="pin-form"):
            yield Label(f" Pin Settings: {self._folder.remote_subpath}", classes="section-header")
            yield Label(f" Local: {self._folder.local_path}")
            yield Label(f" Mode: {self._folder.sync_mode}")
            yield Label("")

            # Bisync folders don't need pin settings
            if self._folder.sync_mode == "bisync":
                yield Label(
                    " [yellow]Pin settings only apply to mount-mode folders.[/]"
                )
                yield Label(
                    " Bisync keeps all files locally — no cache eviction occurs."
                )
                yield Label("")
                yield Button("Back", id="back-btn")
            else:
                yield Label(" Pin Mode:")
                with RadioSet(id="pin-mode"):
                    yield RadioButton(
                        "On-demand (download when accessed)",
                        value=self._folder.pin_mode == "on_demand",
                        id="mode-on-demand",
                    )
                    yield RadioButton(
                        "Keep offline (always cached locally)",
                        value=self._folder.pin_mode == "keep_offline",
                        id="mode-keep-offline",
                    )

                yield Label("")
                yield Label(" Pinned subdirectories (on-demand mode only):")
                yield Label(" [dim]Select dirs to keep cached even in on-demand mode[/]")
                yield Label("")

                yield OptionList(id="subdir-list")

                yield Label("")
                with Horizontal(classes="button-row"):
                    yield Button("Save", id="save-btn", variant="primary")
                    yield Button("Back", id="back-btn")

        yield Footer()

    def on_mount(self) -> None:
        if self._folder.sync_mode == "mount":
            self._populate_subdirs()

    def _populate_subdirs(self) -> None:
        option_list = self.query_one("#subdir-list", OptionList)
        option_list.clear_options()

        mount_dir = Path(self._config.mount_point) / self._folder.remote_subpath

        if not mount_dir.exists():
            option_list.add_option(Option("[dim]Mount not available — start service first[/]"))
            return

        try:
            subdirs = sorted(
                p.relative_to(mount_dir).as_posix()
                for p in mount_dir.iterdir()
                if p.is_dir()
            )
        except OSError:
            option_list.add_option(Option("[dim]Could not read directory[/]"))
            return

        if not subdirs:
            option_list.add_option(Option("[dim]No subdirectories found[/]"))
            return

        for subdir in subdirs:
            pinned = subdir in self._folder.pin_subdirs
            prefix = "[green][x][/]" if pinned else "[ ]"
            option_list.add_option(Option(f"{prefix} {subdir}"))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        prompt = str(event.option.prompt)
        parts = prompt.split(" ", 1)
        if len(parts) < 2:
            return
        subdir = parts[1].strip()

        if subdir.startswith("[dim]"):
            return

        if subdir in self._folder.pin_subdirs:
            self._folder.pin_subdirs.remove(subdir)
        else:
            self._folder.pin_subdirs.append(subdir)

        self._populate_subdirs()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "back-btn":
            self.app.pop_screen()
        elif event.button.id == "save-btn":
            self._save()

    def _save(self) -> None:
        radio_set = self.query_one("#pin-mode", RadioSet)
        if radio_set.pressed_index == 1:
            self._folder.pin_mode = "keep_offline"
        else:
            self._folder.pin_mode = "on_demand"

        save_config(self._config)
        self.notify("Pin settings saved.")

        for screen in self.app.screen_stack:
            if hasattr(screen, "reload_config"):
                screen.reload_config(self._config)

        self.app.pop_screen()

    def action_cancel(self) -> None:
        self.app.pop_screen()
