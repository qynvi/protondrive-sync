"""Add / edit CLI-backed sync folder mappings."""

from __future__ import annotations

import threading
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
    Input,
    Label,
    Log,
    Select,
)

from ..core.config import (
    AppConfig,
    FolderMapping,
    add_folder,
    save_config,
    ConfigError,
    SYMLINK_MODE_LABELS,
    SYMLINK_MODES,
    normalize_symlink_mode,
)
from ..core.migration import (
    plan_bisync_setup,
    execute_bisync_setup,
    _format_size,
    MigrationError,
    BisyncPlan,
)
from ..core.suggesters import LocalPathSuggester, RemotePathSuggester
from .path_browser import LocalBrowserScreen, RemoteBrowserScreen


class MigrationLog(Log):
    """Log widget to display migration progress."""

    pass


class AddFolderScreen(Screen):
    """Screen for adding or editing a folder mapping."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(
        self,
        config: AppConfig,
        editing: Optional[FolderMapping] = None,
    ) -> None:
        super().__init__()
        self._config = config
        self._editing = editing
        self._bisync_plan: Optional[BisyncPlan] = None
        self._pull_mode: bool = False  # True when local is empty, pulling from remote
        self._cancel_event: Optional[threading.Event] = None
        self._running: bool = False

    def compose(self) -> ComposeResult:
        title = "Edit Folder" if self._editing else "Add Folder"
        yield Header(show_clock=False)
        with Vertical(id="add-form"):
            yield Label(f" {title}", classes="section-header")
            yield Label("")

            yield Label(" Sync mode: [bold]Bisync[/] (CLI-backed periodic sync)")

            yield Label("")
            yield Label(" Local path (Right arrow to accept suggestion):")
            with Horizontal(classes="path-input-row"):
                yield Input(
                    value=self._editing.local_path if self._editing else "",
                    placeholder="/path/to/local/folder",
                    id="local-path",
                    suggester=LocalPathSuggester(),
                )
                yield Button("Browse", id="browse-local-btn", classes="browse-btn")

            yield Label(
                " Remote subpath (parent folder on Proton Drive \u2014 source dir name auto-appended):"
            )
            with Horizontal(classes="path-input-row"):
                yield Input(
                    value=self._editing.remote_subpath if self._editing else "",
                    placeholder="workspace/_projects",
                    id="remote-subpath",
                    suggester=RemotePathSuggester(),
                )
                yield Button("Browse", id="browse-remote-btn", classes="browse-btn")

            yield Label("")
            with Horizontal(id="keep-offline-row"):
                yield Label(" Symlink mode: ")
                yield Select(
                    [(SYMLINK_MODE_LABELS[mode], mode) for mode in SYMLINK_MODES],
                    value=(
                        self._editing.symlink_mode
                        if self._editing
                        else self._config.symlink_mode
                    ),
                    allow_blank=False,
                    id="symlink-mode",
                )

            yield Label("")
            with Horizontal(classes="button-row"):
                yield Button("Preview", id="preview-btn", variant="default")
                yield Button(
                    "Save" if self._editing else "Setup & Save",
                    id="save-btn",
                    variant="primary",
                    disabled=self._editing is None,
                )
                yield Button("Abort", id="abort-btn", variant="error", disabled=True)
                yield Button("Cancel", id="cancel-btn")

            yield Label("")
            yield MigrationLog(id="migration-log", max_lines=100)

        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel-btn":
            if not self._running:
                self.app.pop_screen()
        elif event.button.id == "preview-btn":
            self._do_preview()
        elif event.button.id == "save-btn":
            if self._editing:
                self._do_save_edit()
            else:
                self._do_setup()
        elif event.button.id == "abort-btn":
            self._do_abort()
        elif event.button.id == "browse-local-btn":
            self._browse_local()
        elif event.button.id == "browse-remote-btn":
            self._browse_remote()

    def _do_abort(self) -> None:
        """Signal the running setup to abort."""
        if self._cancel_event is not None and self._running:
            self._log("")
            self._log("Cancelling setup ...")
            self._cancel_event.set()
            self.query_one("#abort-btn", Button).disabled = True

    def _browse_local(self) -> None:
        """Open local directory browser modal."""
        current = self.query_one("#local-path", Input).value.strip()
        self.app.push_screen(
            LocalBrowserScreen(start_path=current),
            callback=self._on_local_browser_dismiss,
        )

    def _on_local_browser_dismiss(self, path: str) -> None:
        """Handle result from local browser modal."""
        if path:
            self.query_one("#local-path", Input).value = path

    def _browse_remote(self) -> None:
        """Open remote directory browser modal."""
        current = self.query_one("#remote-subpath", Input).value.strip()
        self.app.push_screen(
            RemoteBrowserScreen(start_path=current),
            callback=self._on_remote_browser_dismiss,
        )

    def _on_remote_browser_dismiss(self, path: str) -> None:
        """Handle result from remote browser modal."""
        if path:
            self.query_one("#remote-subpath", Input).value = path

    def _log(self, msg: str) -> None:
        """Log a message to the migration log widget (must be called from UI thread)."""
        try:
            log_widget = self.query_one(MigrationLog)
            log_widget.write_line(msg)
        except Exception:
            pass

    def _log_threadsafe(self, msg: str) -> None:
        """Log a message from a background worker thread."""
        self.app.call_from_thread(self._log, msg)

    def _get_inputs(self) -> tuple[str, str, str]:
        local = self.query_one("#local-path", Input).value.strip()
        remote = self.query_one("#remote-subpath", Input).value.strip().rstrip("/")
        symlink_value = self.query_one("#symlink-mode", Select).value
        symlink_mode = normalize_symlink_mode(str(symlink_value))

        # Auto-append source directory name if not already present
        if local and remote:
            src_name = Path(local).expanduser().absolute().name
            if not remote.endswith(src_name):
                remote = f"{remote}/{src_name}"

        return local, remote, symlink_mode

    def _do_preview(self) -> None:
        local, remote, symlink_mode = self._get_inputs()
        log = self.query_one(MigrationLog)
        log.clear()
        self._pull_mode = False

        if not local or not remote:
            self._log("Error: both local path and remote subpath are required.")
            return

        local_path = Path(local).expanduser().absolute()
        if local_path.exists() and not local_path.is_dir():
            self._log(f"Error: {local_path} exists but is not a directory.")
            return
        if not local_path.exists() and not local_path.parent.is_dir():
            self._log(f"Error: parent directory does not exist: {local_path.parent}")
            return

        try:
            # plan_bisync_setup handles empty/nonexistent local, queries remote,
            # and computes divergence.
            self._bisync_plan = plan_bisync_setup(
                str(local_path),
                remote,
                self._config,
                symlink_mode=symlink_mode,
            )
            self._pull_mode = self._bisync_plan.local_is_empty
            self._show_bisync_preview(self._bisync_plan)
            if (
                self._bisync_plan.local_is_empty
                and self._bisync_plan.remote_listing_error
            ):
                self._log("")
                self._log(
                    "Cannot safely initialize an empty local folder until the remote can be listed."
                )
                return
        except MigrationError as exc:
            self._log(f"Error: {exc}")
            return

        save_btn = self.query_one("#save-btn", Button)
        save_btn.disabled = False

    def _show_bisync_preview(self, plan: BisyncPlan) -> None:
        if plan.local_is_empty and plan.remote_listing_error:
            self._log("Sync Setup Preview:")
            self._log(f"  Local:       {plan.local_path}  (empty)")
            self._log(f"  Remote:      Proton Drive:{plan.remote_subpath}")
            self._log("  Remote:      unknown (listing failed)")
            self._log("")
            self._log(f"Remote listing error: {plan.remote_listing_error}")
        elif plan.local_is_empty and plan.remote_file_count > 0:
            # Pull from remote scenario
            self._log("Sync Setup Preview (Pull from Remote):")
            self._log(f"  Local:       {plan.local_path}  (empty)")
            self._log(f"  Remote:      Proton Drive:{plan.remote_subpath}")
            self._log(
                f"  Remote:      {plan.remote_file_count} files ({plan.remote_size_human})"
            )
            self._log(f"  Mode:        CLI sync (download from remote, then sync)")
            required = int(
                plan.remote_size_bytes
                * (1 + self._config.download_space_headroom_pct / 100)
            )
            self._log(
                f"  Disk needed: {_format_size(required)} including {self._config.download_space_headroom_pct}% staging headroom"
            )
            self._log("")
            self._log(
                "Press 'Setup & Save' to download from remote and initialize sync."
            )
        elif plan.local_is_empty:
            # Both empty
            self._log("Sync Setup Preview:")
            self._log(f"  Local:       {plan.local_path}  (empty)")
            self._log(f"  Remote:      Proton Drive:{plan.remote_subpath}  (empty)")
            self._log(f"  Mode:        CLI sync")
            self._log("")
            self._log("Both local and remote are empty. Sync will be initialized")
            self._log("and the sync daemon will sync new files automatically.")
            self._log("")
            self._log("Press 'Setup & Save' to initialize sync.")
        else:
            # Standard local-has-files scenario
            self._log("Sync Setup Preview:")
            self._log(f"  Local:       {plan.local_path}")
            self._log(f"  Remote:      Proton Drive:{plan.remote_subpath}")
            self._log(
                f"  Local:       {plan.file_count} files ({plan.total_size_human})"
            )
            if plan.remote_listing_error:
                self._log("  Remote:      unknown (listing failed)")
            elif plan.remote_file_count > 0:
                self._log(
                    f"  Remote:      {plan.remote_file_count} files ({plan.remote_size_human})"
                )
            else:
                self._log(f"  Remote:      (empty)")
            self._log(f"  Mode:        CLI sync (local dir stays as-is)")
            self._log(f"  Symlinks:    {SYMLINK_MODE_LABELS[plan.symlink_mode]}")
            self._log("")

            if plan.filtered_items:
                self._log(
                    f"  Filtered ({len(plan.filtered_items)}) \u2014 excluded from sync:"
                )
                for item in plan.filtered_items:
                    self._log(f"    {item}/")
                self._log("")

            if plan.env_warnings:
                self._log(
                    "WARNING: The following .env files will be synced to Proton Drive:"
                )
                for env_file in plan.env_warnings:
                    self._log(f"    {env_file}")
                self._log("  These may contain secrets. Add '- .env*'")
                self._log("  to filter rules in Settings (press 's' from main screen).")
                self._log("")

            if plan.symlink_count > 0:
                if plan.symlink_mode == "preserve":
                    self._log(
                        "Symlinks will be preserved as link metadata, not traversed."
                    )
                    self._log(
                        f"  Found {plan.symlink_count} symlink(s); {plan.external_symlink_count} point outside this folder."
                    )
                elif plan.symlink_mode == "copy":
                    self._log("WARNING: Copy symlink targets is enabled.")
                    self._log(
                        f"  Found {plan.symlink_count} symlink(s); {plan.external_symlink_count} point outside this folder."
                    )
                else:
                    self._log("Symlinks will be skipped.")
                    self._log(
                        f"  Found {plan.symlink_count} symlink(s); {plan.external_symlink_count} point outside this folder."
                    )
                if plan.symlink_samples:
                    self._log("  Examples:")
                    for sample in plan.symlink_samples:
                        self._log(f"    {sample}")
                if plan.symlink_mode == "copy":
                    self._log(
                        "  This can greatly expand large development-folder uploads."
                    )
                self._log("")

            if plan.remote_listing_error:
                self._log("WARNING: Remote listing failed during preview.")
                self._log(f"  {plan.remote_listing_error}")
                self._log(
                    "  Setup will resume with Proton CLI upload, then seed the metadata baseline."
                )
                self._log(
                    "  Completed remote files are skipped; matching changed files may be updated from local."
                )
                self._log("")
            elif plan.remote_file_count == 0:
                self._log("Remote is empty or not created yet.")
                self._log(
                    "  Setup will upload local files with Proton CLI, then seed the metadata baseline."
                )
                self._log("  This makes large initial uploads resumable.")
                self._log("")
            elif plan.remote_listing_limited:
                self._log(
                    "Note: remote is large, so detailed divergence comparison was skipped."
                )
                self._log(
                    "  Setup will compare both sides before seeding its metadata baseline."
                )
                self._log("")
            elif plan.remote_detail_error:
                self._log(
                    "Note: remote count succeeded, but detailed divergence comparison failed."
                )
                self._log(f"  {plan.remote_detail_error}")
                self._log(
                    "  Setup will compare both sides before seeding its metadata baseline."
                )
                self._log("")

            # Show divergence warning if both sides have content
            if plan.divergence and plan.divergence.is_significant:
                div = plan.divergence
                self._log("!! WARNING: Local and remote content differ significantly!")
                self._log("")
                self._log(f"  Only on local:   {div.local_only_count} files")
                self._log(f"  Only on remote:  {div.remote_only_count} files")
                self._log(f"  Size differs:    {div.size_mismatch_count} files")
                self._log("")
                self._log(
                    "  Setup requires confirmation before seeding a metadata baseline."
                )
                self._log("  Confirmation will be required before proceeding.")
                self._log("")
            elif plan.divergence:
                div = plan.divergence
                diff_total = (
                    div.local_only_count
                    + div.remote_only_count
                    + div.size_mismatch_count
                )
                if diff_total > 0:
                    self._log(
                        f"  Note: minor differences detected ({diff_total} files differ)."
                    )
                    self._log(
                        "  Setup requires confirmation before seeding a metadata baseline."
                    )
                    self._log("")

            if plan.remote_listing_error:
                self._log("Press 'Setup & Save' to resume upload and initialize sync.")
            elif plan.remote_file_count == 0:
                self._log("Press 'Setup & Save' to upload and initialize sync.")
            else:
                self._log("Press 'Setup & Save' to seed the metadata baseline.")

    def _do_setup(self) -> None:
        local, remote, symlink_mode = self._get_inputs()

        self._log("")

        # Check if divergence requires confirmation before proceeding
        if (
            self._bisync_plan is not None
            and self._bisync_plan.divergence
            and self._bisync_plan.divergence.is_significant
        ):
            from .confirm import ConfirmScreen

            div = self._bisync_plan.divergence
            msg = (
                f"Local and remote differ significantly!\n\n"
                f"  Only on local:  {div.local_only_count} files\n"
                f"  Only on remote: {div.remote_only_count} files\n"
                f"  Size differs:   {div.size_mismatch_count} files\n\n"
                f"Setup requires confirmation before seeding a metadata baseline.\n\n"
                f"Proceed with setup?"
            )
            self.app.push_screen(
                ConfirmScreen(msg),
                callback=lambda confirmed: (
                    self._do_setup_confirmed() if confirmed else None
                ),
            )
            return

        self._do_setup_confirmed()

    def _do_setup_confirmed(self) -> None:
        """Execute setup after any confirmation dialogs."""
        local, remote, symlink_mode = self._get_inputs()

        # Set up cancellation and button states for the long-running operation
        self._cancel_event = threading.Event()
        self._running = True
        self.query_one("#save-btn", Button).disabled = True
        self.query_one("#preview-btn", Button).disabled = True
        self.query_one("#abort-btn", Button).disabled = False
        self.query_one("#cancel-btn", Button).disabled = True

        if self._bisync_plan is None:
            self._log("Run Preview first.")
            self._reset_buttons()
            return

        # Run in background thread so TUI stays responsive
        self.run_worker(
            lambda: self._worker_bisync_setup(local, remote),
            thread=True,
            exit_on_error=False,
        )

    def _reset_buttons(self) -> None:
        """Restore buttons to pre-setup state."""
        self._running = False
        self._cancel_event = None
        self.query_one("#save-btn", Button).disabled = False
        self.query_one("#preview-btn", Button).disabled = False
        self.query_one("#abort-btn", Button).disabled = True
        self.query_one("#cancel-btn", Button).disabled = False

    def _worker_bisync_setup(self, local: str, remote: str) -> None:
        """Background worker: run bisync setup. Runs in a thread."""
        log = self._log_threadsafe
        log("Starting sync setup ...")

        result = execute_bisync_setup(
            self._bisync_plan,
            self._config,
            progress=log,
            cancel_event=self._cancel_event,
        )

        # Scan for git repos and write metadata after successful setup.
        # For bisync, the local directory is real — scanning works fine.
        # The metadata file syncs on the next bisync cycle.
        if result.success:
            try:
                from ..core.git_meta import scan_git_repos, write_metadata

                local_path = Path(local).expanduser().absolute()
                repos = scan_git_repos(local_path, self._config.filters)
                if repos:
                    log(
                        f"Found {len(repos)} git repo(s) — writing metadata for rehydration ..."
                    )
                    write_metadata(local_path, repos)
            except Exception as exc:
                log(f"Note: git metadata scan skipped ({exc})")

        # Switch back to UI thread for config/widget updates
        self.app.call_from_thread(
            self._finish_bisync_setup,
            result,
            local,
            remote,
        )

    def _finish_bisync_setup(self, result: object, local: str, remote: str) -> None:
        """UI thread: handle bisync setup result."""
        from ..core.migration import MigrationResult

        assert isinstance(result, MigrationResult)

        if result.success:
            mapping = FolderMapping(
                local_path=local,
                remote_subpath=remote,
                symlink_mode=result.mapping.symlink_mode,
                bisync_initialized=True,
            )
            try:
                add_folder(self._config, mapping)
                save_config(self._config)
                self._log("")
                self._log("Folder added and config saved.")
                self._log("The sync daemon will handle ongoing sync automatically.")
                self._log("")
                self._log("Press Escape to return to the main screen.")
                self._notify_main()
            except ConfigError as exc:
                self._log(f"Config error: {exc}")
            self._reset_buttons()
        elif result.cancelled:
            self._log("")
            self._log("Setup was cancelled. No changes were saved.")
            self._log("Local files are untouched. You can retry or go back.")
            self._reset_buttons()
        else:
            self._log("")
            self._log(f"Setup failed: {result.message}")
            self._log("")
            if "stalled" in result.message:
                self._log(
                    "The upload stalled (no transfer/check progress for 10 minutes)."
                )
                self._log("This may indicate a network issue or Proton API outage.")
            else:
                self._log("Already-uploaded files are preserved on remote.")
            self._log("Press 'Setup & Save' to retry (completed files are skipped).")
            self._reset_buttons()

    def _do_save_edit(self) -> None:
        """Save changes to an existing folder mapping."""
        local, remote, symlink_mode = self._get_inputs()

        if self._editing is None:
            return

        self._editing.remote_subpath = remote
        self._editing.symlink_mode = symlink_mode
        save_config(self._config)
        self._log("Changes saved.")
        self._notify_main()
        self.app.pop_screen()

    def _notify_main(self) -> None:
        """Tell the main screen to reload."""
        for screen in self.app.screen_stack:
            if hasattr(screen, "reload_config"):
                screen.reload_config(self._config)

    def action_cancel(self) -> None:
        # Only block Escape when there's genuinely an active, non-cancelled
        # operation.  Guards against _running getting stuck True due to a
        # missed _reset_buttons() call — if _cancel_event is None or
        # already set, the operation is not truly running.
        if (
            self._running
            and self._cancel_event is not None
            and not self._cancel_event.is_set()
        ):
            self.notify(
                "Setup is running. Press 'Abort' to cancel the operation first.",
                severity="warning",
            )
            return
        self.app.pop_screen()
