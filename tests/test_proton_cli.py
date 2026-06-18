"""Tests for the Proton Drive CLI backend (core/proton_cli.py).

These mock subprocess.run so the real argument-building, JSON parsing, error
classification, per-item success handling, staging, mtime restoration and
eventual-consistency retry logic are all exercised without a live account.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from protondrive_sync.core import proton_cli
from protondrive_sync.core.config import AppConfig
from protondrive_sync.core.proton_cli import (
    ProtonAuthError,
    ProtonConflict,
    ProtonDriveCLI,
    ProtonNotFound,
    RemoteNode,
    _iso_to_epoch,
    _node_from_json,
    _transfer_timeout,
    sanitize,
    sha1_matches,
    to_cli_path,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _file_node(
    name: str, *, size: int, sha1: str, modtime: str, uid: str = "U"
) -> dict:
    return {
        "uid": uid,
        "type": "file",
        "name": {"ok": True, "value": name},
        "activeRevision": {
            "ok": True,
            "value": {
                "claimedSize": size,
                "claimedModificationTime": modtime,
                "claimedDigests": {"sha1": sha1, "sha1Verified": False},
            },
        },
    }


def _folder_node(name: str, uid: str = "D") -> dict:
    return {"uid": uid, "type": "folder", "name": {"ok": True, "value": name}}


class FakeCli:
    """Programmable stand-in for subprocess.run([binary, 'filesystem', ...])."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.handlers = {}  # (subcommand) -> callable(args) -> CompletedProcess

    def __call__(self, cmd, capture_output=True, text=True, timeout=None):
        # version probe
        if cmd[1:] == ["--version"]:
            return subprocess.CompletedProcess(
                cmd, 0, "Proton Drive CLI cli-drive@0.4.3\n", ""
            )
        assert cmd[1] == "filesystem", cmd
        args = cmd[2:]
        self.calls.append(args)
        sub = args[0]
        handler = self.handlers.get(sub)
        if handler is None:
            return subprocess.CompletedProcess(cmd, 0, "[]", "")
        return handler(cmd, args)

    def on(self, subcommand, fn):
        self.handlers[subcommand] = fn


@pytest.fixture
def fake_binary(tmp_path):
    binary = tmp_path / "proton-drive"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    return str(binary)


@pytest.fixture
def cli(fake_binary, monkeypatch):
    config = AppConfig(proton_cli_path=fake_binary, proton_cli_concurrency=2)
    backend = ProtonDriveCLI(config)
    fake = FakeCli()
    monkeypatch.setattr(proton_cli.subprocess, "run", fake)
    # Make retry sleeps instant.
    monkeypatch.setattr(proton_cli.time, "sleep", lambda *_: None)
    return backend, fake


def _ok(cmd, stdout="[]"):
    return subprocess.CompletedProcess(cmd, 0, stdout, "")


def _err(cmd, stderr, code=1):
    return subprocess.CompletedProcess(cmd, code, "", stderr)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestPureHelpers:
    def test_to_cli_path(self):
        assert to_cli_path("") == "/my-files"
        assert to_cli_path("a/b") == "/my-files/a/b"
        assert to_cli_path("/a/b/") == "/my-files/a/b"

    def test_sanitize_redacts_email_and_uid(self):
        uid = "mTVaAtzZVepcEPGxvgnoRxxWNdUdf_KAB1Y_cEkDrnxUTzo_n4wf==~HSVBfG8gFR0ToHdtU2P6SnDv3PBU87Dg_QanGXjr7xe=="
        out = sanitize(f"user a.b+c@example.com node {uid}")
        assert "example.com" not in out
        assert "mTVaAtz" not in out
        assert "[email]" in out and "[uid]" in out

    def test_node_from_json_file(self):
        node = _node_from_json(
            "w/a.txt",
            _file_node("a.txt", size=14, sha1="ABCDEF", modtime="2026-01-01T00:00:00Z"),
        )
        assert node.size == 14
        assert node.sha1 == "abcdef"
        assert node.is_dir is False
        assert node.modtime == "2026-01-01T00:00:00Z"

    def test_node_from_json_folder(self):
        node = _node_from_json("w", _folder_node("w"))
        assert node.is_dir is True
        assert node.size == 0

    def test_transfer_timeout_scales(self):
        assert _transfer_timeout(0) == 120
        assert _transfer_timeout(512 * 1024) == 121
        assert _transfer_timeout(10 * 1024**4) == 7200  # capped

    def test_iso_to_epoch_roundtrip(self):
        assert _iso_to_epoch("2026-06-12T07:00:36.072Z") == pytest.approx(
            1781247636.072, abs=1
        )
        assert _iso_to_epoch("garbage") is None

    def test_sha1_matches(self):
        node = RemoteNode(path="x", sha1="abc")
        assert sha1_matches(node, "ABC")
        assert not sha1_matches(node, "def")
        assert not sha1_matches(None, "abc")
        assert not sha1_matches(node, None)


# ---------------------------------------------------------------------------
# stat
# ---------------------------------------------------------------------------


class TestStat:
    def test_stat_success(self, cli):
        backend, fake = cli
        fake.on(
            "info",
            lambda cmd, a: _ok(
                cmd,
                json.dumps(
                    _file_node(
                        "a.txt", size=9, sha1="aa", modtime="2026-01-01T00:00:00Z"
                    )
                ),
            ),
        )
        node = backend.stat("w/a.txt")
        assert node.size == 9 and node.sha1 == "aa"
        assert fake.calls[0] == ["info", "-j", "/my-files/w/a.txt"]

    def test_stat_not_found_raises(self, cli):
        backend, fake = cli
        fake.on("info", lambda cmd, a: _err(cmd, "Node not found: a.txt"))
        with pytest.raises(ProtonNotFound):
            backend.stat("w/a.txt")

    def test_stat_or_none(self, cli):
        backend, fake = cli
        fake.on("info", lambda cmd, a: _err(cmd, "Node not found"))
        assert backend.stat_or_none("missing") is None

    def test_auth_error_classified(self, cli):
        backend, fake = cli
        fake.on(
            "info",
            lambda cmd, a: _err(cmd, "error ERR_SECRETS_PLATFORM_ERROR autolaunch"),
        )
        with pytest.raises(ProtonAuthError):
            backend.stat("x")

    def test_error_message_is_sanitized(self, cli):
        backend, fake = cli
        fake.on("info", lambda cmd, a: _err(cmd, "boom owner secret@example.com"))
        with pytest.raises(Exception) as exc:
            backend.stat("x")
        assert "example.com" not in str(exc.value)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


class TestList:
    def test_list_dir_builds_child_paths(self, cli):
        backend, fake = cli
        payload = [
            _folder_node("sub"),
            _file_node("a.txt", size=1, sha1="aa", modtime="2026-01-01T00:00:00Z"),
        ]
        fake.on("list", lambda cmd, a: _ok(cmd, json.dumps(payload)))
        nodes = backend.list_dir("w")
        by_path = {n.path: n for n in nodes}
        assert "w/sub" in by_path and by_path["w/sub"].is_dir
        assert "w/a.txt" in by_path and by_path["w/a.txt"].size == 1

    def test_list_dir_missing_returns_empty(self, cli):
        backend, fake = cli
        fake.on("list", lambda cmd, a: _err(cmd, "Node not found"))
        assert backend.list_dir("nope") == []

    def test_list_recursive_walks_tree(self, cli):
        backend, fake = cli
        tree = {
            "/my-files/w": [
                _folder_node("sub"),
                _file_node("top.txt", size=1, sha1="a", modtime="t"),
            ],
            "/my-files/w/sub": [_file_node("deep.txt", size=2, sha1="b", modtime="t")],
        }

        def list_handler(cmd, a):
            path = a[2]
            return _ok(cmd, json.dumps(tree.get(path, [])))

        fake.on("list", list_handler)
        files = backend.list_recursive("w")
        paths = sorted(n.path for n in files)
        assert paths == ["w/sub/deep.txt", "w/top.txt"]


# ---------------------------------------------------------------------------
# ensure_dir
# ---------------------------------------------------------------------------


class TestEnsureDir:
    def test_recursive_create_and_cache(self, cli):
        backend, fake = cli
        fake.on(
            "create-folder", lambda cmd, a: _ok(cmd, json.dumps(_folder_node(a[-1])))
        )
        backend.ensure_dir("a/b/c")
        created = [a for a in fake.calls if a[0] == "create-folder"]
        # three levels: a, b, c
        assert len(created) == 3
        assert created[0] == ["create-folder", "-j", "/my-files", "a"]
        assert created[2] == ["create-folder", "-j", "/my-files/a/b", "c"]
        # second call is fully cached -> no new create-folder calls
        fake.calls.clear()
        backend.ensure_dir("a/b/c")
        assert fake.calls == []

    def test_already_exists_is_ignored(self, cli):
        backend, fake = cli
        fake.on(
            "create-folder",
            lambda cmd, a: _err(cmd, 'Name conflict on "a" (folder) already exists'),
        )
        backend.ensure_dir("a")  # must not raise


# ---------------------------------------------------------------------------
# upload
# ---------------------------------------------------------------------------


class TestUpload:
    def test_new_upload_no_replace_flag(self, cli, tmp_path):
        backend, fake = cli
        fake.on(
            "create-folder", lambda cmd, a: _ok(cmd, json.dumps(_folder_node(a[-1])))
        )
        fake.on("upload", lambda cmd, a: _ok(cmd, ""))
        local = tmp_path / "a.txt"
        local.write_text("hi")
        backend.upload(str(local), "w/a.txt")
        up = [a for a in fake.calls if a[0] == "upload"][0]
        assert "-f" not in up  # default hard-fail safety net for new files
        assert up[:2] == ["upload", "-t"]
        assert up[-1] == "/my-files/w"  # uploaded into parent
        assert up[-2] == str(local)

    def test_replace_upload_sets_flag(self, cli, tmp_path):
        backend, fake = cli
        fake.on(
            "create-folder", lambda cmd, a: _ok(cmd, json.dumps(_folder_node(a[-1])))
        )
        fake.on("upload", lambda cmd, a: _ok(cmd, ""))
        local = tmp_path / "a.txt"
        local.write_text("hi")
        backend.upload(str(local), "w/a.txt", replace=True)
        up = [a for a in fake.calls if a[0] == "upload"][0]
        assert "-f" in up and up[up.index("-f") + 1] == "replace"

    def test_staging_uses_remote_basename(self, cli, tmp_path):
        backend, fake = cli
        fake.on(
            "create-folder", lambda cmd, a: _ok(cmd, json.dumps(_folder_node(a[-1])))
        )
        seen = {}

        def upload_handler(cmd, a):
            src = Path(a[-2])
            seen["name"] = src.name
            seen["exists"] = src.exists()
            return _ok(cmd, "")

        fake.on("upload", upload_handler)
        local = tmp_path / "source-name.txt"
        local.write_text("data")
        backend.upload(str(local), "w/link.rclonelink")
        assert seen["name"] == "link.rclonelink"
        assert seen["exists"] is True

    def test_upload_conflict_raises(self, cli, tmp_path):
        backend, fake = cli
        fake.on(
            "create-folder", lambda cmd, a: _ok(cmd, json.dumps(_folder_node(a[-1])))
        )
        fake.on(
            "upload",
            lambda cmd, a: _err(
                cmd, 'ValidationError: Name conflict on "a.txt" (file) already exists'
            ),
        )
        local = tmp_path / "a.txt"
        local.write_text("hi")
        with pytest.raises(ProtonConflict):
            backend.upload(str(local), "w/a.txt")

    def test_upload_text_blob(self, cli):
        backend, fake = cli
        fake.on(
            "create-folder", lambda cmd, a: _ok(cmd, json.dumps(_folder_node(a[-1])))
        )
        captured = {}

        def upload_handler(cmd, a):
            src = Path(a[-2])
            captured["name"] = src.name
            captured["content"] = src.read_text()
            return _ok(cmd, "")

        fake.on("upload", upload_handler)
        backend.upload_text('{"k": 1}', ".journal/f/2026-01-01/entry.json")
        assert captured["name"] == "entry.json"
        assert captured["content"] == '{"k": 1}'


# ---------------------------------------------------------------------------
# download
# ---------------------------------------------------------------------------


class TestDownload:
    def test_download_moves_and_restores_mtime(self, cli, tmp_path):
        backend, fake = cli

        def download_handler(cmd, a):
            localdir = Path(a[2])
            (localdir / "a.txt").write_text("payload")
            return _ok(cmd, "")

        fake.on("download", download_handler)
        dest = tmp_path / "out" / "a.txt"
        backend.download("w/a.txt", str(dest), claimed_modtime="2024-01-02T03:04:05Z")
        assert dest.read_text() == "payload"
        expected = _iso_to_epoch("2024-01-02T03:04:05Z")
        assert dest.stat().st_mtime == pytest.approx(expected, abs=2)

    def test_download_missing_output_raises(self, cli, tmp_path):
        backend, fake = cli
        fake.on("download", lambda cmd, a: _ok(cmd, ""))  # writes nothing
        with pytest.raises(Exception):
            backend.download("w/a.txt", str(tmp_path / "a.txt"))

    def test_download_not_found_raises(self, cli, tmp_path):
        backend, fake = cli
        fake.on("download", lambda cmd, a: _err(cmd, "Node not found"))
        with pytest.raises(ProtonNotFound):
            backend.download("w/a.txt", str(tmp_path / "a.txt"))

    def test_download_text(self, cli, tmp_path):
        backend, fake = cli

        def download_handler(cmd, a):
            localdir = Path(a[2])
            (localdir / "entry.json").write_text('{"ok": true}')
            return _ok(cmd, "")

        fake.on("download", download_handler)
        assert backend.download_text("j/entry.json") == '{"ok": true}'


# ---------------------------------------------------------------------------
# trash / move / rename
# ---------------------------------------------------------------------------


class TestMutations:
    def test_trash_per_item_ok_true(self, cli):
        backend, fake = cli
        fake.on(
            "trash", lambda cmd, a: _ok(cmd, json.dumps([{"uid": "X", "ok": True}]))
        )
        assert backend.trash("w/a.txt") is True

    def test_trash_per_item_ok_false_despite_exit_zero(self, cli):
        backend, fake = cli
        # The dangerous case: exit 0 but the item failed.
        fake.on(
            "trash", lambda cmd, a: _ok(cmd, json.dumps([{"uid": "X", "ok": False}]))
        )
        assert backend.trash("w/a.txt") is False

    def test_trash_not_found_is_success(self, cli):
        backend, fake = cli
        fake.on("trash", lambda cmd, a: _err(cmd, "Node not found"))
        assert backend.trash("w/gone.txt") is True

    def test_move_ensures_parent(self, cli):
        backend, fake = cli
        fake.on(
            "create-folder", lambda cmd, a: _ok(cmd, json.dumps(_folder_node(a[-1])))
        )
        fake.on("move", lambda cmd, a: _ok(cmd, ""))
        backend.move("w/a.txt", "backups/x")
        move = [a for a in fake.calls if a[0] == "move"][0]
        assert move == ["move", "/my-files/w/a.txt", "/my-files/backups/x"]

    def test_rename(self, cli):
        backend, fake = cli
        fake.on("rename", lambda cmd, a: _ok(cmd, ""))
        backend.rename("w/a.txt", "b.txt")
        assert fake.calls[0] == ["rename", "/my-files/w/a.txt", "b.txt"]


# ---------------------------------------------------------------------------
# eventual-consistency polling
# ---------------------------------------------------------------------------


class TestConsistency:
    def test_wait_until_absent_retries(self, cli):
        backend, fake = cli
        states = ["present", "present", "gone"]

        def info_handler(cmd, a):
            state = states.pop(0) if states else "gone"
            if state == "present":
                return _ok(
                    cmd, json.dumps(_file_node("a.txt", size=1, sha1="a", modtime="t"))
                )
            return _err(cmd, "Node not found")

        fake.on("info", info_handler)
        assert backend.wait_until_absent("w/a.txt") is True

    def test_wait_until_present_retries(self, cli):
        backend, fake = cli
        states = ["gone", "present"]

        def info_handler(cmd, a):
            state = states.pop(0) if states else "present"
            if state == "gone":
                return _err(cmd, "Node not found")
            return _ok(
                cmd, json.dumps(_file_node("a.txt", size=5, sha1="a", modtime="t"))
            )

        fake.on("info", info_handler)
        node = backend.wait_until_present("w/a.txt")
        assert node is not None and node.size == 5
