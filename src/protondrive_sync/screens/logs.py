"""Daemon log viewer — live streaming of bisync service journal."""

from __future__ import annotations

import subprocess
import sys
import threading

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Footer, Header, Log


class DaemonLog(Log):
    """Log widget for daemon journal output."""
    pass


class LogScreen(Screen):
    """Live view of the bisync daemon journal logs."""

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("c", "clear_log", "Clear"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._proc: subprocess.Popen | None = None
        self._stop_event = threading.Event()

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="log-view"):
            yield DaemonLog(id="daemon-log", max_lines=2000)
        yield Footer()

    def on_mount(self) -> None:
        self._start_streaming()

    def _start_streaming(self) -> None:
        """Launch journalctl -f in background and stream output."""
        log_widget = self.query_one(DaemonLog)
        log_widget.write_line("[dim]Connecting to bisync daemon journal...[/]")
        log_widget.write_line("")

        self._stop_event.clear()
        self.run_worker(self._stream_worker, thread=True, exit_on_error=False)

    def _stream_worker(self) -> None:
        """Background thread: read journalctl output line by line."""
        cmd = [
            "journalctl", "--user",
            "-u", "protondrive-bisync.service",
            "--no-pager",
            "-n", "100",  # show last 100 lines of history
            "-f",         # then follow
        ]

        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,  # line-buffered
            )
        except FileNotFoundError:
            self.app.call_from_thread(
                self._log, "[red]journalctl not found — cannot stream logs[/]"
            )
            return

        try:
            for line in self._proc.stdout:
                if self._stop_event.is_set():
                    break
                stripped = line.rstrip("\n")
                self.app.call_from_thread(self._log, stripped)
        except Exception:
            pass
        finally:
            self._cleanup_proc()

    def _log(self, msg: str) -> None:
        try:
            self.query_one(DaemonLog).write_line(msg)
        except Exception:
            pass

    def _cleanup_proc(self) -> None:
        """Terminate the journalctl subprocess."""
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=3)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None

    def action_clear_log(self) -> None:
        try:
            self.query_one(DaemonLog).clear()
        except Exception:
            pass

    def action_back(self) -> None:
        self._stop_event.set()
        self._cleanup_proc()
        self.app.pop_screen()

    def on_unmount(self) -> None:
        """Ensure cleanup if screen is removed without action_back."""
        self._stop_event.set()
        self._cleanup_proc()
