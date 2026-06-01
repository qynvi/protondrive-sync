"""Git rehydration screen — review and restore git repos from metadata."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

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

from ..core.config import AppConfig, FolderMapping
from ..core.git_meta import (
    SyncMeta,
    read_metadata,
    rehydrate_all,
    check_rehydration_status,
    RehydrationResult,
)


class RehydrationLog(Log):
    """Log widget for rehydration progress."""
    pass


class RehydrateScreen(Screen):
    """Screen showing git repos found in metadata and offering rehydration."""

    BINDINGS = [
        Binding("escape", "cancel", "Back"),
    ]

    def __init__(
        self,
        config: AppConfig,
        folder: FolderMapping,
    ) -> None:
        super().__init__()
        self._config = config
        self._folder = folder
        self._meta: Optional[SyncMeta] = None
        self._running = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical(id="rehydrate-form"):
            yield Label(" Git Rehydration", classes="section-header")
            yield Label("")
            yield Label(f" Folder: {self._folder.local_path}", id="folder-label")
            yield Label("", id="status-label")
            yield Label("")
            with Horizontal(classes="button-row"):
                yield Button(
                    "Rehydrate All",
                    id="rehydrate-btn",
                    variant="primary",
                    disabled=True,
                )
                yield Button("Back", id="back-btn")
            yield Label("")
            yield RehydrationLog(id="rehydration-log", max_lines=200)
        yield Footer()

    def on_mount(self) -> None:
        self._load_metadata()

    def _load_metadata(self) -> None:
        """Read metadata and display repo list with rehydration status."""
        log = self.query_one(RehydrationLog)
        local_path = Path(self._folder.local_path)

        self._meta = read_metadata(local_path)
        if self._meta is None or not self._meta.git_repos:
            self._log("No git metadata found for this folder.")
            return

        status = check_rehydration_status(local_path, self._meta.git_repos)

        total = len(self._meta.git_repos)
        rehydrated = sum(1 for v in status.values() if v)
        needs = total - rehydrated

        status_label = self.query_one("#status-label", Label)

        if needs == 0:
            status_label.update(
                f" [green]All {total} repo(s) already rehydrated[/]"
            )
            self._log("All repositories already have .git/ directories.")
            self._log("Nothing to do.")
            return

        status_label.update(
            f" [yellow]{needs} of {total} repo(s) need rehydration[/]"
        )

        self._log(f"Metadata from: {self._meta.hostname} ({self._meta.generated_at})")
        self._log("")
        self._log("Repositories:")
        for repo in self._meta.git_repos:
            rel = repo.relative_path if repo.relative_path != "." else "(root)"
            is_done = status.get(repo.relative_path, False)

            if is_done:
                self._log(f"  [green]OK[/]  {rel}")
            else:
                primary = "origin" if "origin" in repo.remotes else next(iter(repo.remotes), "?")
                url = repo.remotes.get(primary, {}).get("fetch", "?")
                branch_info = f"branch: {repo.branch}" if repo.branch else f"commit: {repo.commit[:12]}"
                self._log(f"  [yellow]--[/]  {rel}")
                self._log(f"        remote: {url}")
                self._log(f"        {branch_info}")
                if repo.has_submodules:
                    self._log(f"        (has submodules)")

        self._log("")
        self._log("Rehydration will run: git init, git fetch, git reset")
        self._log("Your synced files will NOT be modified (working tree preserved).")
        self._log("")
        self._log("NOTE: git fetch requires network access. If repos are private,")
        self._log("you'll need credentials (SSH keys or tokens) configured on this machine.")
        self._log("")
        self._log("Press 'Rehydrate All' to proceed.")

        # Enable the button
        self.query_one("#rehydrate-btn", Button).disabled = False

    def _log(self, msg: str) -> None:
        try:
            self.query_one(RehydrationLog).write_line(msg)
        except Exception:
            pass

    def _log_threadsafe(self, msg: str) -> None:
        self.app.call_from_thread(self._log, msg)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "back-btn":
            self.app.pop_screen()
        elif event.button.id == "rehydrate-btn" and not self._running:
            self._do_rehydrate()

    def _do_rehydrate(self) -> None:
        """Start rehydration in a background thread."""
        if self._meta is None or not self._meta.git_repos:
            return

        self._running = True
        self.query_one("#rehydrate-btn", Button).disabled = True
        self._log("Starting rehydration ...")
        self._log("")

        self.run_worker(
            self._worker_rehydrate,
            thread=True,
            exit_on_error=False,
        )

    def _worker_rehydrate(self) -> None:
        """Background worker: run rehydration."""
        local_path = Path(self._folder.local_path)
        results = rehydrate_all(local_path, self._meta, log=self._log_threadsafe)
        self.app.call_from_thread(self._finish_rehydrate, results)

    def _finish_rehydrate(self, results: list[RehydrationResult]) -> None:
        """UI thread: display results."""
        self._running = False

        self._log("")
        self._log("--- Results ---")

        succeeded = 0
        failed = 0
        skipped = 0

        for r in results:
            rel = r.relative_path if r.relative_path != "." else "(root)"
            if r.skipped:
                skipped += 1
                self._log(f"  [dim]SKIP[/]  {rel}: {r.message}")
            elif r.success:
                succeeded += 1
                self._log(f"  [green]OK[/]    {rel}: {r.message}")
            else:
                failed += 1
                self._log(f"  [red]FAIL[/]  {rel}: {r.message}")

        self._log("")
        summary_parts = []
        if succeeded:
            summary_parts.append(f"[green]{succeeded} rehydrated[/]")
        if skipped:
            summary_parts.append(f"[dim]{skipped} skipped[/]")
        if failed:
            summary_parts.append(f"[red]{failed} failed[/]")
        self._log("Summary: " + ", ".join(summary_parts))

        if failed == 0:
            self._log("")
            self._log("Press Escape to return to the main screen.")
            status_label = self.query_one("#status-label", Label)
            total = len(results)
            status_label.update(f" [green]All {total} repo(s) rehydrated[/]")
        else:
            self._log("")
            self._log("Some repos failed to rehydrate. Check your network")
            self._log("connection and git credentials, then try again.")
            self.query_one("#rehydrate-btn", Button).disabled = False

        # Notify main screen to refresh
        for screen in self.app.screen_stack:
            if hasattr(screen, "reload_config"):
                screen.reload_config(self._config)

    def action_cancel(self) -> None:
        if not self._running:
            self.app.pop_screen()
