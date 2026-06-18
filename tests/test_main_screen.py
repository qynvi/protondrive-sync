"""Tests for main-screen status text."""

from types import SimpleNamespace

from protondrive_sync.core.config import AppConfig, FolderMapping
from protondrive_sync.screens.main import MainScreen


def test_pending_review_hint_uses_plain_key_text(monkeypatch):
    config = AppConfig()
    folder = FolderMapping(local_path="/tmp/project", remote_subpath="project")
    screen = MainScreen(config)
    monkeypatch.setattr("protondrive_sync.screens.main.has_pending_review", lambda _config, _path: True)

    status, info = screen._bisync_folder_status(folder)

    assert status == "[red][review required][/]"
    assert info == "press v to review"
    assert "[v]" not in info


def test_runtime_pending_review_hint_uses_plain_key_text(monkeypatch):
    config = AppConfig()
    folder = FolderMapping(local_path="/tmp/project", remote_subpath="project")
    screen = MainScreen(config)
    monkeypatch.setattr("protondrive_sync.screens.main.has_pending_review", lambda _config, _path: False)
    monkeypatch.setattr(
        "protondrive_sync.screens.main.get_folder_state",
        lambda _config, _folder: SimpleNamespace(status="pending_review", last_error=None),
    )

    status, info = screen._bisync_folder_status(folder)

    assert status == "[red][review required][/]"
    assert info == "press v to review"
    assert "[v]" not in info


def test_runtime_journal_and_audit_statuses(monkeypatch):
    config = AppConfig()
    folder = FolderMapping(local_path="/tmp/project", remote_subpath="project", bisync_initialized=True)
    screen = MainScreen(config)
    monkeypatch.setattr("protondrive_sync.screens.main.has_pending_review", lambda _config, _path: False)

    monkeypatch.setattr(
        "protondrive_sync.screens.main.get_folder_state",
        lambda _config, _folder: SimpleNamespace(status="journal_pending", last_error="outbox pending"),
    )
    status, info = screen._bisync_folder_status(folder)
    assert status == "[red]journal pending[/]"
    assert "outbox pending" in info

    monkeypatch.setattr(
        "protondrive_sync.screens.main.get_folder_state",
        lambda _config, _folder: SimpleNamespace(status="journal_stale", last_error=None),
    )
    status, info = screen._bisync_folder_status(folder)
    assert status == "[yellow]journal stale[/]"
    assert "exact local sync" in info

    monkeypatch.setattr(
        "protondrive_sync.screens.main.get_folder_state",
        lambda _config, _folder: SimpleNamespace(status="audit_due", last_error=None),
    )
    status, info = screen._bisync_folder_status(folder)
    assert status == "[yellow]audit due[/]"
    assert "audit" in info
