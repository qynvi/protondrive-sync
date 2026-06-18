"""Tests for P3 targeted sync engine."""

import pytest

from protondrive_sync.core.config import AppConfig, FolderMapping
from protondrive_sync.core.inventory import (
    InventoryEntry,
    OutboxEntry,
    enqueue_journal_outbox,
    get_inventory_entry,
    journal_entry_seen,
    scan_local_inventory,
    upsert_inventory_entry,
)
from protondrive_sync.core.journal import JournalChange, make_journal_entry
from protondrive_sync.core.proton_cli import RemoteNode
from protondrive_sync.core.sync_engine import (
    apply_local_batch,
    apply_journal_change,
    apply_local_upload,
    classify_delta,
    list_remote_infos_for_paths,
    run_targeted_sync_cycle,
    TargetedSyncResult,
    remote_storage_path,
)
from protondrive_sync.core.state import folder_id, get_folder_state


class FakeBackend:
    """Minimal in-memory ProtonDriveCLI stand-in for engine tests.

    ``nodes`` maps app-relative remote paths to RemoteNode. Tests preload it to
    model remote state and assert on recorded operations.
    """

    def __init__(self, nodes=None):
        self.nodes = dict(nodes or {})
        self.uploaded: list[tuple[str, bool]] = []
        self.uploaded_many: list[tuple[tuple[str, ...], str, bool]] = []
        self.trashed: list[str] = []
        self.downloaded: list[str] = []
        self.text_blobs: dict[str, str] = {}

    # metadata
    def stat_or_none(self, rel):
        return self.nodes.get(rel.strip("/"))

    def wait_until_present(self, rel):
        return self.nodes.get(rel.strip("/"))

    def wait_until_absent(self, rel):
        return rel.strip("/") not in self.nodes

    def exists(self, rel):
        return rel.strip("/") in self.nodes

    def list_dir(self, rel):
        rel = rel.strip("/")
        prefix = rel + "/" if rel else ""
        out = []
        for path, node in self.nodes.items():
            parent = path.rsplit("/", 1)[0] if "/" in path else ""
            if parent == rel:
                out.append(node)
        return out

    def list_recursive(self, rel):
        rel = rel.strip("/")
        prefix = rel + "/" if rel else ""
        return [
            n for p, n in self.nodes.items() if p.startswith(prefix) and not n.is_dir
        ]

    # mutations
    def ensure_dir(self, rel):
        pass

    def upload(self, local_path, dst_rel, *, replace=False, size_hint=None):
        self.uploaded.append((dst_rel.strip("/"), replace))

    def upload_many(self, local_paths, parent_rel, *, replace=False, total_size_hint=0):
        self.uploaded_many.append((tuple(local_paths), parent_rel.strip("/"), replace))

    def upload_text(self, content, dst_rel, *, replace=True):
        self.text_blobs[dst_rel.strip("/")] = content

    def download(
        self, src_rel, dst_local_path, *, claimed_modtime=None, size_hint=None
    ):
        self.downloaded.append(src_rel.strip("/"))
        __import__("pathlib").Path(dst_local_path).write_text("new", encoding="utf-8")

    def download_text(self, src_rel):
        return self.text_blobs.get(src_rel.strip("/"), "")

    def trash(self, rel):
        rel = rel.strip("/")
        self.trashed.append(rel)
        self.nodes.pop(rel, None)
        return True


@pytest.fixture
def config(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setattr(
        "protondrive_sync.core.config.get_config_dir", lambda: config_dir
    )
    monkeypatch.setattr("protondrive_sync.core.journal.machine_id", lambda: "machine-a")
    return AppConfig()


@pytest.fixture(autouse=True)
def default_backend(monkeypatch):
    """Never construct a real CLI backend in engine tests.

    Honors an explicitly passed backend; otherwise hands back a fresh fake so
    cycle-level tests that mock the apply_* functions never touch the binary.
    """
    monkeypatch.setattr(
        "protondrive_sync.core.sync_engine.make_backend",
        lambda config, backend=None: backend if backend is not None else FakeBackend(),
    )


def _seed_inventory(
    config: AppConfig, folder: FolderMapping, path: str = ".baseline"
) -> None:
    upsert_inventory_entry(
        config,
        InventoryEntry(
            folder_id=folder_id(folder.local_path, folder.remote_subpath),
            path=path,
            kind="file",
            local_size=1,
            remote_size=1,
            last_source="setup",
        ),
    )


def test_classify_same_path_both_modified():
    previous = InventoryEntry(
        folder_id="f", path="a.txt", kind="file", local_size=1, remote_size=1
    )
    local = InventoryEntry(folder_id="f", path="a.txt", kind="file", local_size=2)
    remote = RemoteNode(path="a.txt", size=3)

    assert classify_delta(previous, local, remote) == "same_path_both_modified"


def test_apply_local_upload_exact_path_flow(config, tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    local_file = root / "a.txt"
    local_file.write_text("hello", encoding="utf-8")
    folder = FolderMapping(local_path=str(root), remote_subpath="proj")
    local = scan_local_inventory(folder, config, hash_paths={"a.txt"})["a.txt"]
    # Remote is empty before upload; after upload it carries the local sha1/size.
    backend = FakeBackend()

    original_upload = backend.upload

    def upload(local_path, dst_rel, *, replace=False, size_hint=None):
        original_upload(local_path, dst_rel, replace=replace, size_hint=size_hint)
        backend.nodes[dst_rel.strip("/")] = RemoteNode(
            path=dst_rel.strip("/"), size=local.local_size, sha1=local.local_sha1
        )

    backend.upload = upload
    monkeypatch.setattr(
        "protondrive_sync.core.sync_engine.write_journal_entry",
        lambda *_args, **_kwargs: True,
    )

    result = apply_local_upload(
        folder, config, "a.txt", local, operation_id="op-1", backend=backend
    )

    assert result.status == "healthy"
    assert backend.uploaded == [("proj/a.txt", False)]  # new file: no replace
    entry = get_inventory_entry(config, folder, "a.txt")
    assert entry is not None
    assert entry.remote_size == local.local_size
    assert entry.last_source == "local"


def test_apply_local_upload_blocks_remote_conflict(config, tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.txt").write_text("hello", encoding="utf-8")
    folder = FolderMapping(local_path=str(root), remote_subpath="proj")
    upsert_inventory_entry(
        config,
        InventoryEntry(
            folder_id=folder_id(folder.local_path, folder.remote_subpath),
            path="a.txt",
            kind="file",
            local_size=1,
            remote_size=1,
        ),
    )
    local = scan_local_inventory(folder, config, hash_paths={"a.txt"})["a.txt"]
    backend = FakeBackend({"proj/a.txt": RemoteNode(path="proj/a.txt", size=2)})

    result = apply_local_upload(folder, config, "a.txt", local, backend=backend)

    assert result.status == "pending_review"
    assert result.review_paths == ["a.txt"]


def test_outbox_blocks_new_targeted_sync(config, tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    folder = FolderMapping(local_path=str(root), remote_subpath="proj")
    _seed_inventory(config, folder)
    enqueue_journal_outbox(
        config,
        OutboxEntry(
            id="entry-1",
            folder_id=folder_id(folder.local_path, folder.remote_subpath),
            remote_path="journal/entry-1.json",
            entry_json="{}",
        ),
    )
    monkeypatch.setattr(
        "protondrive_sync.core.sync_engine.retry_journal_outbox",
        lambda *_args, **_kwargs: 0,
    )

    result = run_targeted_sync_cycle(folder, config)

    assert result.status == "journal_pending"
    assert get_folder_state(config, folder).status == "journal_pending"


def test_apply_journal_download_replaces_unchanged_local(config, tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    target = root / "a.txt"
    target.write_text("old", encoding="utf-8")
    folder = FolderMapping(local_path=str(root), remote_subpath="proj")
    fid = folder_id(folder.local_path, folder.remote_subpath)
    upsert_inventory_entry(
        config,
        InventoryEntry(
            folder_id=fid, path="a.txt", kind="file", local_size=3, remote_size=3
        ),
    )
    entry = make_journal_entry(
        folder,
        config,
        [
            JournalChange(
                path="a.txt", action="upload", before={"size": 3}, after={"size": 3}
            )
        ],
        operation_id="op-1",
    )
    entry.machine_id = "machine-b"
    # Remote carries the new content (size 3, no sha1 claim to verify against).
    backend = FakeBackend({"proj/a.txt": RemoteNode(path="proj/a.txt", size=3)})

    result = apply_journal_change(
        folder, config, entry, entry.changes[0], backend=backend
    )

    assert result.status == "healthy"
    assert target.read_text(encoding="utf-8") == "new"
    assert backend.downloaded == ["proj/a.txt"]
    updated = get_inventory_entry(config, folder, "a.txt")
    assert updated is not None
    assert updated.last_source == "journal"


def test_targeted_cycle_marks_external_journal_seen_after_success(
    config, tmp_path, monkeypatch
):
    root = tmp_path / "proj"
    root.mkdir()
    folder = FolderMapping(local_path=str(root), remote_subpath="proj")
    _seed_inventory(config, folder)
    entry = make_journal_entry(
        folder, config, [JournalChange(path="a.txt", action="upload")]
    )
    entry.machine_id = "machine-b"

    monkeypatch.setattr(
        "protondrive_sync.core.sync_engine.retry_journal_outbox",
        lambda *_args, **_kwargs: 0,
    )
    monkeypatch.setattr(
        "protondrive_sync.core.sync_engine.poll_journal",
        lambda *_args, **_kwargs: [entry],
    )
    monkeypatch.setattr(
        "protondrive_sync.core.sync_engine.apply_journal_change",
        lambda *_args, **_kwargs: TargetedSyncResult(
            status="healthy", downloaded_paths=["a.txt"]
        ),
    )
    monkeypatch.setattr(
        "protondrive_sync.core.sync_engine.changed_local_paths",
        lambda *_args, **_kwargs: [],
    )

    result = run_targeted_sync_cycle(folder, config)

    assert result.status == "healthy"
    assert journal_entry_seen(config, entry.folder_id, entry.entry_id)


def test_targeted_cycle_byte_limit_counts_only_selected_paths(
    config, tmp_path, monkeypatch
):
    root = tmp_path / "proj"
    root.mkdir()
    folder = FolderMapping(local_path=str(root), remote_subpath="proj")
    fid = folder_id(folder.local_path, folder.remote_subpath)
    _seed_inventory(config, folder)
    config.targeted_max_bytes_per_cycle = 10
    small = InventoryEntry(folder_id=fid, path="small.bin", kind="file", local_size=1)
    huge = InventoryEntry(folder_id=fid, path="huge.bin", kind="file", local_size=100)

    monkeypatch.setattr(
        "protondrive_sync.core.sync_engine.retry_journal_outbox",
        lambda *_args, **_kwargs: 0,
    )
    monkeypatch.setattr(
        "protondrive_sync.core.sync_engine.poll_journal", lambda *_args, **_kwargs: []
    )
    monkeypatch.setattr(
        "protondrive_sync.core.sync_engine.changed_local_paths",
        lambda *_args, **_kwargs: ["small.bin"],
    )
    monkeypatch.setattr(
        "protondrive_sync.core.sync_engine.scan_local_inventory",
        lambda *_args, **_kwargs: {"small.bin": small, "huge.bin": huge},
    )
    monkeypatch.setattr(
        "protondrive_sync.core.sync_engine.apply_local_upload",
        lambda *_args, **_kwargs: TargetedSyncResult(
            status="healthy", synced_paths=["small.bin"]
        ),
    )

    result = run_targeted_sync_cycle(folder, config)

    assert result.status == "healthy"
    assert result.synced_paths == ["small.bin"]


def test_remote_storage_path_uses_rclonelink_for_preserved_symlink(tmp_path):
    folder = FolderMapping(
        local_path=str(tmp_path / "proj"),
        remote_subpath="remote/proj",
        symlink_mode="preserve",
    )

    assert (
        remote_storage_path(folder, "dir/link", kind="symlink")
        == "remote/proj/dir/link.rclonelink"
    )
    assert (
        remote_storage_path(folder, "dir/file", kind="file") == "remote/proj/dir/file"
    )


def test_list_remote_infos_for_paths_lists_unique_parent_dirs(
    config, tmp_path, monkeypatch
):
    folder = FolderMapping(
        local_path=str(tmp_path / "proj"), remote_subpath="remote/proj"
    )
    backend = FakeBackend(
        {
            "remote/proj/a/one.txt": RemoteNode(
                path="remote/proj/a/one.txt", size=1, sha1="sha-one", name="one.txt"
            ),
            "remote/proj/b/two.txt": RemoteNode(
                path="remote/proj/b/two.txt", size=2, sha1="sha-two", name="two.txt"
            ),
        }
    )

    infos = list_remote_infos_for_paths(
        folder, config, ["a/one.txt", "b/two.txt"], backend=backend
    )

    assert infos["a/one.txt"].sha1 == "sha-one"
    assert infos["b/two.txt"].size == 2


def test_apply_local_batch_uploads_verifies_journals_and_updates_inventory(
    config, tmp_path, monkeypatch
):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.txt").write_text("a", encoding="utf-8")
    (root / "b.txt").write_text("b", encoding="utf-8")
    folder = FolderMapping(local_path=str(root), remote_subpath="proj")
    local_by_path = scan_local_inventory(folder, config, hash_paths={"a.txt", "b.txt"})
    journals: list = []

    backend = FakeBackend()

    def upload_many(local_paths, parent_rel, *, replace=False, total_size_hint=0):
        # Model the post-upload remote state so verification passes.
        for src in local_paths:
            name = __import__("pathlib").Path(src).name
            rel = f"{parent_rel.strip('/')}/{name}".strip("/")
            entry = local_by_path[name]
            backend.nodes[rel] = RemoteNode(
                path=rel, size=entry.local_size, sha1=entry.local_sha1, name=name
            )
        backend.uploaded_many.append(
            (tuple(local_paths), parent_rel.strip("/"), replace)
        )

    backend.upload_many = upload_many
    monkeypatch.setattr(
        "protondrive_sync.core.sync_engine.write_journal_entry",
        lambda _config, entry, **_kwargs: journals.append(entry) or True,
    )

    result = apply_local_batch(
        folder,
        config,
        ["a.txt", "b.txt"],
        local_by_path,
        operation_id="op-1",
        backend=backend,
    )

    assert result.status == "healthy"
    assert result.synced_paths == ["a.txt", "b.txt"]
    assert len(backend.uploaded_many) == 1  # one invocation for the shared parent
    assert backend.uploaded_many[0][1] == "proj"  # uploaded into the proj parent
    assert backend.uploaded_many[0][2] is False  # both new -> no replace
    assert len(journals) == 1
    assert len(journals[0].changes) == 2
    assert get_inventory_entry(config, folder, "a.txt").last_source == "batch-local"


def test_apply_local_batch_blocks_remote_conflict(config, tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.txt").write_text("new", encoding="utf-8")
    folder = FolderMapping(local_path=str(root), remote_subpath="proj")
    fid = folder_id(folder.local_path, folder.remote_subpath)
    upsert_inventory_entry(
        config,
        InventoryEntry(
            folder_id=fid, path="a.txt", kind="file", local_size=1, remote_size=1
        ),
    )
    local_by_path = scan_local_inventory(folder, config, hash_paths={"a.txt"})
    backend = FakeBackend(
        {"proj/a.txt": RemoteNode(path="proj/a.txt", size=2, name="a.txt")}
    )

    result = apply_local_batch(
        folder, config, ["a.txt"], local_by_path, backend=backend
    )

    assert result.status == "pending_review"
    assert result.review_paths == ["a.txt"]


def test_apply_local_batch_allows_protected_delete_recreated_in_same_batch(
    config, tmp_path, monkeypatch
):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "new.pdf").write_text("same", encoding="utf-8")
    folder = FolderMapping(local_path=str(root), remote_subpath="proj")
    fid = folder_id(folder.local_path, folder.remote_subpath)
    local_by_path = scan_local_inventory(folder, config, hash_paths={"new.pdf"})
    new_sha1 = local_by_path["new.pdf"].local_sha1
    upsert_inventory_entry(
        config,
        InventoryEntry(
            folder_id=fid,
            path="old.pdf",
            kind="file",
            local_size=4,
            local_sha1=new_sha1,
            remote_size=4,
            remote_sha1=new_sha1,
        ),
    )
    journals: list = []
    # Remote starts with old.pdf present; new.pdf uploads land via upload_many.
    backend = FakeBackend(
        {
            "proj/old.pdf": RemoteNode(
                path="proj/old.pdf", size=4, sha1=new_sha1, name="old.pdf"
            )
        }
    )

    def upload_many(local_paths, parent_rel, *, replace=False, total_size_hint=0):
        for src in local_paths:
            name = __import__("pathlib").Path(src).name
            rel = f"{parent_rel.strip('/')}/{name}".strip("/")
            backend.nodes[rel] = RemoteNode(path=rel, size=4, sha1=new_sha1, name=name)
        backend.uploaded_many.append(
            (tuple(local_paths), parent_rel.strip("/"), replace)
        )

    backend.upload_many = upload_many
    monkeypatch.setattr(
        "protondrive_sync.core.sync_engine.write_journal_entry",
        lambda _config, entry, **_kwargs: journals.append(entry) or True,
    )

    result = apply_local_batch(
        folder, config, ["old.pdf", "new.pdf"], local_by_path, backend=backend
    )

    assert result.status == "healthy"
    assert result.synced_paths == ["new.pdf"]
    assert result.deleted_paths == ["old.pdf"]
    assert backend.trashed == ["proj/old.pdf"]


def test_targeted_cycle_uses_batch_for_multi_path_local_changes(
    config, tmp_path, monkeypatch
):
    root = tmp_path / "proj"
    root.mkdir()
    folder = FolderMapping(local_path=str(root), remote_subpath="proj")
    fid = folder_id(folder.local_path, folder.remote_subpath)
    _seed_inventory(config, folder)
    first = InventoryEntry(folder_id=fid, path="a.txt", kind="file", local_size=1)
    second = InventoryEntry(folder_id=fid, path="b.txt", kind="file", local_size=1)

    monkeypatch.setattr(
        "protondrive_sync.core.sync_engine.retry_journal_outbox",
        lambda *_args, **_kwargs: 0,
    )
    monkeypatch.setattr(
        "protondrive_sync.core.sync_engine.poll_journal", lambda *_args, **_kwargs: []
    )
    monkeypatch.setattr(
        "protondrive_sync.core.sync_engine.changed_local_paths",
        lambda *_args, **_kwargs: ["a.txt", "b.txt"],
    )
    monkeypatch.setattr(
        "protondrive_sync.core.sync_engine._scan_selected_local",
        lambda *_args, **_kwargs: {"a.txt": first, "b.txt": second},
    )
    monkeypatch.setattr(
        "protondrive_sync.core.sync_engine.apply_local_batch",
        lambda *_args, **_kwargs: TargetedSyncResult(
            status="healthy", synced_paths=["a.txt", "b.txt"]
        ),
    )
    monkeypatch.setattr(
        "protondrive_sync.core.sync_engine.apply_local_upload",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("per-file upload called")
        ),
    )

    result = run_targeted_sync_cycle(folder, config)

    assert result.status == "healthy"
    assert result.synced_paths == ["a.txt", "b.txt"]


def test_targeted_cycle_can_skip_remote_journal_poll_for_local_changes(
    config, tmp_path, monkeypatch
):
    root = tmp_path / "proj"
    root.mkdir()
    folder = FolderMapping(local_path=str(root), remote_subpath="proj")
    fid = folder_id(folder.local_path, folder.remote_subpath)
    _seed_inventory(config, folder)
    local = InventoryEntry(folder_id=fid, path="a.txt", kind="file", local_size=1)

    monkeypatch.setattr(
        "protondrive_sync.core.sync_engine.retry_journal_outbox",
        lambda *_args, **_kwargs: 0,
    )
    monkeypatch.setattr(
        "protondrive_sync.core.sync_engine.poll_journal",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("journal polled")
        ),
    )
    monkeypatch.setattr(
        "protondrive_sync.core.sync_engine.changed_local_paths",
        lambda *_args, **_kwargs: ["a.txt"],
    )
    monkeypatch.setattr(
        "protondrive_sync.core.sync_engine._scan_selected_local",
        lambda *_args, **_kwargs: {"a.txt": local},
    )
    monkeypatch.setattr(
        "protondrive_sync.core.sync_engine.apply_local_upload",
        lambda *_args, **_kwargs: TargetedSyncResult(
            status="healthy", synced_paths=["a.txt"]
        ),
    )

    result = run_targeted_sync_cycle(folder, config, poll_remote_journal=False)

    assert result.status == "healthy"
    assert result.synced_paths == ["a.txt"]
