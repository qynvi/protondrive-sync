"""Manual verify/repair screen for P3 safety operations."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Label, Static

from ..core.config import AppConfig, FolderMapping


class VerifyScreen(Screen):
    """Expose manual targeted audit/verify/retention actions."""

    BINDINGS = [Binding("escape", "cancel", "Back")]

    def __init__(self, config: AppConfig, folder: FolderMapping) -> None:
        super().__init__()
        self._config = config
        self._folder = folder
        self._status = "Ready. Manual deep verify may take a long time on Proton."

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical(id="verify-form"):
            yield Label(" Verify / Repair", classes="section-header")
            yield Label(f" Folder: {self._folder.local_path}")
            yield Label(f" Remote: Proton Drive:{self._folder.remote_subpath}")
            yield Label("")
            yield Static(self._status, id="verify-status")
            yield Label("")
            with Horizontal(classes="button-row"):
                yield Button("Run partitioned audit", id="audit-btn", variant="primary")
                yield Button("Run deep verify", id="deep-btn", variant="warning")
                yield Button("Retry journal", id="journal-btn")
                yield Button("Cleanup old backups", id="cleanup-btn")
                yield Button("Back", id="back-btn")
        yield Footer()

    def _set_status(self, message: str) -> None:
        self._status = message
        try:
            self.query_one("#verify-status", Static).update(message)
        except Exception:
            pass

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "back-btn":
            self.app.pop_screen()
        elif event.button.id == "audit-btn":
            self._run_audit()
        elif event.button.id == "deep-btn":
            self._run_deep_verify()
        elif event.button.id == "journal-btn":
            self._retry_journal()
        elif event.button.id == "cleanup-btn":
            self._cleanup()

    def _run_audit(self) -> None:
        from ..core.audit import run_partitioned_audit

        result = run_partitioned_audit(self._config, self._folder)
        if result.completed:
            self._set_status(
                f"Partitioned audit complete: {len(result.audited)} partition(s)."
            )
        elif result.failed:
            self._set_status(f"Audit failed at {result.failed[0]}: {result.message}")
        else:
            self._set_status(
                f"Audit incomplete: {result.message or 'time budget reached'}"
            )

    def _run_deep_verify(self) -> None:
        from ..core.audit import run_deep_verify

        report = run_deep_verify(self._config, self._folder)
        if report.ok:
            self._set_status(f"Deep verify clean: {report.matches} matching file(s).")
        else:
            self._set_status(
                "Deep verify found differences: "
                f"missing_on_dst={len(report.missing_on_dst)}, "
                f"missing_on_src={len(report.missing_on_src)}, "
                f"different={len(report.different)}, errors={len(report.errors)}"
            )

    def _retry_journal(self) -> None:
        from ..core.journal import retry_journal_outbox
        from ..core.state import folder_id

        sent = retry_journal_outbox(
            self._config,
            folder_id(self._folder.local_path, self._folder.remote_subpath),
        )
        self._set_status(f"Retried journal outbox: {sent} sent.")

    def _cleanup(self) -> None:
        from ..core.retention import (
            cleanup_folder_local_backups,
            cleanup_remote_journal,
        )

        local = cleanup_folder_local_backups(self._config, self._folder)
        remote_journal = cleanup_remote_journal(self._config, self._folder)
        self._set_status(
            f"Cleanup complete: {len(local.deleted_local)} local backup file(s), "
            f"{len(remote_journal.deleted_remote)} journal file(s)."
        )

    def action_cancel(self) -> None:
        self.app.pop_screen()
