"""Add / Edit folder mapping screen with migration (mount) or bisync setup flow."""

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
    RadioButton,
    RadioSet,
    Switch,
)

from ..core.config import (
    AppConfig,
    FolderMapping,
    add_folder,
    save_config,
    ConfigError,
)
from ..core.migration import (
    plan_migration,
    execute_migration,
    plan_bisync_setup,
    execute_bisync_setup,
    _format_size,
    MigrationError,
    MigrationPlan,
    BisyncPlan,
)
from ..core.rclone import is_mounted
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
        self._mount_plan: Optional[MigrationPlan] = None
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

            yield Label(" Sync mode:")
            with RadioSet(id="sync-mode"):
                yield RadioButton(
                    "Bisync (default \u2014 local dir stays as-is, periodic sync)",
                    value=(
                        (self._editing.sync_mode == "bisync") if self._editing
                        else True
                    ),
                    id="mode-bisync",
                )
                yield RadioButton(
                    "Mount (symlink to FUSE mount, near-real-time sync)",
                    value=(
                        (self._editing.sync_mode == "mount") if self._editing
                        else False
                    ),
                    id="mode-mount",
                )

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

            yield Label(" Remote subpath (parent folder on Proton Drive \u2014 source dir name auto-appended):")
            with Horizontal(classes="path-input-row"):
                yield Input(
                    value=self._editing.remote_subpath if self._editing else "",
                    placeholder="workspace/_projects",
                    id="remote-subpath",
                    suggester=RemotePathSuggester(self._config.remote_name),
                )
                yield Button("Browse", id="browse-remote-btn", classes="browse-btn")

            yield Label("")
            with Horizontal(id="keep-offline-row"):
                yield Label(" Keep offline (mount mode only): ")
                yield Switch(
                    value=(self._editing.pin_mode == "keep_offline") if self._editing else False,
                    id="keep-offline",
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
            RemoteBrowserScreen(
                remote_name=self._config.remote_name,
                start_path=current,
            ),
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

    def _get_sync_mode(self) -> str:
        radio_set = self.query_one("#sync-mode", RadioSet)
        return "bisync" if radio_set.pressed_index == 0 else "mount"

    def _get_inputs(self) -> tuple[str, str, str, str]:
        local = self.query_one("#local-path", Input).value.strip()
        remote = self.query_one("#remote-subpath", Input).value.strip().rstrip("/")
        pin = "keep_offline" if self.query_one("#keep-offline", Switch).value else "on_demand"
        sync_mode = self._get_sync_mode()

        # Auto-append source directory name if not already present
        if local and remote:
            src_name = Path(local).expanduser().absolute().name
            if not remote.endswith(src_name):
                remote = f"{remote}/{src_name}"

        return local, remote, pin, sync_mode

    def _do_preview(self) -> None:
        local, remote, pin, sync_mode = self._get_inputs()
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

        local_is_empty = not local_path.exists() or not any(local_path.iterdir())

        try:
            if sync_mode == "bisync":
                # plan_bisync_setup handles empty/nonexistent local,
                # queries remote, and computes divergence
                self._bisync_plan = plan_bisync_setup(str(local_path), remote, self._config)
                self._mount_plan = None
                self._pull_mode = self._bisync_plan.local_is_empty
                self._show_bisync_preview(self._bisync_plan)
            elif local_is_empty:
                # Mount mode pull: local doesn't exist or is empty,
                # skip plan_migration (nothing to upload), show pull preview
                self._mount_plan = None
                self._bisync_plan = None
                self._pull_mode = True
                self._show_mount_pull_preview(local_path, remote, pin)
            else:
                # Mount mode push: standard upload migration
                self._mount_plan = plan_migration(str(local_path), remote, self._config)
                self._bisync_plan = None
                self._show_mount_preview(self._mount_plan, pin)
        except MigrationError as exc:
            self._log(f"Error: {exc}")
            return

        save_btn = self.query_one("#save-btn", Button)
        save_btn.disabled = False

    def _show_bisync_preview(self, plan: BisyncPlan) -> None:
        if plan.local_is_empty and plan.remote_file_count > 0:
            # Pull from remote scenario
            self._log("Bisync Setup Preview (Pull from Remote):")
            self._log(f"  Local:       {plan.local_path}  (empty)")
            self._log(f"  Remote:      {self._config.remote_name}:{plan.remote_subpath}")
            self._log(f"  Remote:      {plan.remote_file_count} files ({plan.remote_size_human})")
            self._log(f"  Mode:        bisync (download from remote, then sync)")
            self._log("")
            self._log("Press 'Setup & Save' to download from remote and initialize sync.")
        elif plan.local_is_empty:
            # Both empty
            self._log("Bisync Setup Preview:")
            self._log(f"  Local:       {plan.local_path}  (empty)")
            self._log(f"  Remote:      {self._config.remote_name}:{plan.remote_subpath}  (empty)")
            self._log(f"  Mode:        bisync")
            self._log("")
            self._log("Both local and remote are empty. Sync will be initialized")
            self._log("and the bisync daemon will sync new files automatically.")
            self._log("")
            self._log("Press 'Setup & Save' to initialize sync.")
        else:
            # Standard local-has-files scenario
            self._log("Bisync Setup Preview:")
            self._log(f"  Local:       {plan.local_path}")
            self._log(f"  Remote:      {self._config.remote_name}:{plan.remote_subpath}")
            self._log(f"  Local:       {plan.file_count} files ({plan.total_size_human})")
            if plan.remote_file_count > 0:
                self._log(f"  Remote:      {plan.remote_file_count} files ({plan.remote_size_human})")
            else:
                self._log(f"  Remote:      (empty)")
            self._log(f"  Mode:        bisync (local dir stays as-is)")
            self._log("")

            if plan.filtered_items:
                self._log(f"  Filtered ({len(plan.filtered_items)}) \u2014 excluded from sync:")
                for item in plan.filtered_items:
                    self._log(f"    {item}/")
                self._log("")

            if plan.env_warnings:
                self._log("WARNING: The following .env files will be synced to Proton Drive:")
                for env_file in plan.env_warnings:
                    self._log(f"    {env_file}")
                self._log("  These may contain secrets. Add '- .env*'")
                self._log("  to filter rules in Settings (press 's' from main screen).")
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
                self._log("  bisync --resync will merge both sides (newer versions win).")
                self._log("  Confirmation will be required before proceeding.")
                self._log("")
            elif plan.divergence:
                div = plan.divergence
                diff_total = div.local_only_count + div.remote_only_count + div.size_mismatch_count
                if diff_total > 0:
                    self._log(f"  Note: minor differences detected ({diff_total} files differ).")
                    self._log("  bisync --resync will merge both sides.")
                    self._log("")

            self._log("Press 'Setup & Save' to run initial bisync (--resync).")

    def _show_mount_preview(self, plan: MigrationPlan, pin: str) -> None:
        self._log("Mount Migration Preview:")
        self._log(f"  Source:      {plan.local_path}")
        self._log(f"  Destination: {self._config.remote_name}:{plan.remote_subpath}")
        self._log(f"  Mount link:  {plan.mount_target}")
        self._log(f"  Backup:      {plan.backup_path}")
        self._log(f"  Files:       {plan.file_count}")
        self._log(f"  Size:        {plan.total_size_human}")
        self._log(f"  Pin mode:    {pin}")
        self._log("")

        if plan.filtered_items:
            self._log(f"  Filtered items ({len(plan.filtered_items)}) \u2014 excluded from sync, preserved locally:")
            for item in plan.filtered_items:
                self._log(f"    {item}/")
            self._log("")

        if plan.env_warnings:
            self._log("WARNING: The following .env files will be synced to Proton Drive:")
            for env_file in plan.env_warnings:
                self._log(f"    {env_file}")
            self._log("  These may contain secrets. Add '- .env*'")
            self._log("  to filter rules in Settings (press 's' from main screen).")
            self._log("")

        if not is_mounted(self._config.mount_point):
            self._log("WARNING: Mount is not active. Migration will upload files")
            self._log("         but the symlink won't resolve until mount starts.")
            self._log("")

        self._log("Press 'Setup & Save' to proceed with migration.")

    def _show_mount_pull_preview(
        self, local_path: Path, remote_subpath: str, pin: str,
    ) -> None:
        """Preview for mount-mode pull: local is empty, creating symlink to mount."""
        mount_target = Path(self._config.mount_point) / remote_subpath

        # Query remote to show what files are there
        remote_file_count = 0
        remote_size_bytes = 0
        try:
            from ..core.rclone import rclone_lsjson
            remote_files = rclone_lsjson(
                self._config.remote_name, remote_subpath, recursive=True,
            )
            remote_file_count = len(remote_files)
            remote_size_bytes = sum(rf.size for rf in remote_files)
        except Exception:
            pass

        self._log("Mount Setup Preview (Link to Remote):")
        self._log(f"  Local:       {local_path}  (will be created as symlink)")
        self._log(f"  Remote:      {self._config.remote_name}:{remote_subpath}")
        self._log(f"  Mount link:  {mount_target}")
        if remote_file_count > 0:
            self._log(f"  Remote:      {remote_file_count} files ({_format_size(remote_size_bytes)})")
        else:
            self._log(f"  Remote:      (empty)")
        self._log(f"  Pin mode:    {pin}")
        self._log("")

        if not is_mounted(self._config.mount_point):
            self._log("WARNING: Mount is not active. The symlink won't resolve")
            self._log("         until the mount service starts.")
            self._log("")

        self._log("No upload needed \u2014 files are already on Proton Drive.")
        self._log("A symlink will be created from the local path to the mount.")
        self._log("")
        self._log("Press 'Setup & Save' to create the link.")

    def _do_setup(self) -> None:
        local, remote, pin, sync_mode = self._get_inputs()

        self._log("")

        # Check if divergence requires confirmation before proceeding
        if (
            sync_mode == "bisync"
            and self._bisync_plan is not None
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
                f"bisync --resync will merge both sides.\n"
                f"Newer versions win on conflicts.\n\n"
                f"Proceed with setup?"
            )
            self.app.push_screen(
                ConfirmScreen(msg),
                callback=lambda confirmed: self._do_setup_confirmed() if confirmed else None,
            )
            return

        self._do_setup_confirmed()

    def _do_setup_confirmed(self) -> None:
        """Execute setup after any confirmation dialogs."""
        local, remote, pin, sync_mode = self._get_inputs()

        # Set up cancellation and button states for the long-running operation
        self._cancel_event = threading.Event()
        self._running = True
        self.query_one("#save-btn", Button).disabled = True
        self.query_one("#preview-btn", Button).disabled = True
        self.query_one("#abort-btn", Button).disabled = False
        self.query_one("#cancel-btn", Button).disabled = True

        if sync_mode == "bisync" and self._bisync_plan is not None:
            # Run in background thread so TUI stays responsive
            self.run_worker(
                lambda: self._worker_bisync_setup(local, remote),
                thread=True,
                exit_on_error=False,
            )
        elif sync_mode == "mount" and self._pull_mode:
            # Mount-pull: no upload, just create symlink
            self.run_worker(
                lambda: self._worker_mount_pull(local, remote, pin),
                thread=True,
                exit_on_error=False,
            )
        elif sync_mode == "mount" and self._mount_plan is not None:
            self.run_worker(
                lambda: self._worker_mount_migrate(local, remote, pin),
                thread=True,
                exit_on_error=False,
            )
        else:
            self._log("Run Preview first.")
            self._reset_buttons()

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
        log("Starting bisync setup ...")

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
                    log(f"Found {len(repos)} git repo(s) — writing metadata for rehydration ...")
                    write_metadata(local_path, repos)
            except Exception as exc:
                log(f"Note: git metadata scan skipped ({exc})")

        # Switch back to UI thread for config/widget updates
        self.app.call_from_thread(
            self._finish_bisync_setup, result, local, remote,
        )

    def _finish_bisync_setup(self, result: object, local: str, remote: str) -> None:
        """UI thread: handle bisync setup result."""
        from ..core.migration import MigrationResult
        assert isinstance(result, MigrationResult)

        if result.success:
            mapping = FolderMapping(
                local_path=local,
                remote_subpath=remote,
                sync_mode="bisync",
                bisync_initialized=True,
            )
            try:
                add_folder(self._config, mapping)
                save_config(self._config)
                self._log("")
                self._log("Folder added and config saved.")
                self._log("The bisync daemon will handle ongoing sync automatically.")
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
                self._log("The upload stalled (no progress for 5 minutes).")
                self._log("This may indicate a network issue or Proton API outage.")
            else:
                self._log("Already-uploaded files are preserved on remote.")
            self._log("Press 'Setup & Save' to retry (completed files are skipped).")
            self._reset_buttons()

    def _worker_mount_migrate(self, local: str, remote: str, pin: str) -> None:
        """Background worker: run mount migration. Runs in a thread."""
        log = self._log_threadsafe
        log("Starting mount migration ...")

        result = execute_migration(
            self._mount_plan,
            self._config,
            progress=log,
            cancel_event=self._cancel_event,
        )

        self.app.call_from_thread(
            self._finish_mount_migrate, result, local, remote, pin,
        )

    def _finish_mount_migrate(
        self, result: object, local: str, remote: str, pin: str,
    ) -> None:
        """UI thread: handle mount migration result."""
        from ..core.migration import MigrationResult
        assert isinstance(result, MigrationResult)

        if result.success:
            mapping = FolderMapping(
                local_path=local,
                remote_subpath=remote,
                sync_mode="mount",
                pin_mode=pin,
            )
            try:
                add_folder(self._config, mapping)
                save_config(self._config)
                self._log("")
                self._log("Folder added and config saved.")
                self._log(f"Backup retained at: {result.backup_path}")
                self._log("")
                self._log("Press Escape to return to the main screen.")
                self._notify_main()
            except ConfigError as exc:
                self._log(f"Config error: {exc}")
            self._reset_buttons()
        elif result.cancelled:
            self._log("")
            self._log("Migration was cancelled. No changes were saved.")
            self._log("Local files are untouched. You can retry or go back.")
            self._reset_buttons()
        else:
            self._log("")
            self._log(f"Migration failed: {result.message}")
            self._log("")
            if "stalled" in result.message:
                self._log("The upload stalled (no progress for 5 minutes).")
                self._log("This may indicate a network issue or Proton API outage.")
            else:
                self._log("Already-uploaded files are preserved on remote.")
            self._log("Press 'Setup & Save' to retry (completed files are skipped).")
            self._reset_buttons()

    def _worker_mount_pull(self, local: str, remote: str, pin: str) -> None:
        """Background worker: mount-pull setup. Creates symlink without uploading."""
        log = self._log_threadsafe
        log("Setting up mount link ...")

        local_path = Path(local).expanduser().absolute()
        mount_target = Path(self._config.mount_point) / remote

        try:
            # Ensure local parent exists
            local_path.parent.mkdir(parents=True, exist_ok=True)

            # Remove existing empty dir if present (can't symlink over a dir)
            if local_path.is_dir() and not any(local_path.iterdir()):
                local_path.rmdir()
                log(f"Removed empty directory: {local_path}")

            # Create symlink
            from ..core.symlinks import create_link
            log(f"Creating link {local_path} -> {mount_target} ...")
            create_link(local_path, mount_target)
            log("Link created successfully.")

            self.app.call_from_thread(
                self._finish_mount_pull, True, local, remote, pin, "",
            )
        except Exception as exc:
            self.app.call_from_thread(
                self._finish_mount_pull, False, local, remote, pin, str(exc),
            )

    def _finish_mount_pull(
        self, success: bool, local: str, remote: str, pin: str, error: str,
    ) -> None:
        """UI thread: handle mount-pull result."""
        if success:
            mapping = FolderMapping(
                local_path=local,
                remote_subpath=remote,
                sync_mode="mount",
                pin_mode=pin,
            )
            try:
                add_folder(self._config, mapping)
                save_config(self._config)
                self._log("")
                self._log("Folder linked and config saved.")
                self._log("Files are accessible via the mount.")
                self._log("")
                self._log("Press Escape to return to the main screen.")
                self._notify_main()
            except ConfigError as exc:
                self._log(f"Config error: {exc}")
            self._reset_buttons()
        else:
            self._log("")
            self._log(f"Mount link setup failed: {error}")
            self._reset_buttons()

    def _do_save_edit(self) -> None:
        """Save changes to an existing folder mapping."""
        local, remote, pin, sync_mode = self._get_inputs()

        if self._editing is None:
            return

        self._editing.remote_subpath = remote
        self._editing.sync_mode = sync_mode
        self._editing.pin_mode = pin
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
