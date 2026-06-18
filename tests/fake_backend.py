"""Shared in-memory fake of ProtonDriveCLI for unit tests.

Models remote state as a dict of app-relative path -> RemoteNode plus a parallel
dict of text blob contents. Only the methods the rewired modules use are
implemented.
"""

from __future__ import annotations

from pathlib import Path

from protondrive_sync.core.proton_cli import ProtonError, RemoteNode


class FakeBackend:
    def __init__(self, nodes=None, blobs=None, *, fail_upload=False):
        self.nodes: dict[str, RemoteNode] = dict(nodes or {})
        self.blobs: dict[str, str] = dict(blobs or {})
        self.uploaded: list[str] = []
        self.trashed: list[str] = []
        self.fail_upload = fail_upload

    # -- uploads -------------------------------------------------------------
    def upload(self, local_path, dst_rel, *, replace=False, size_hint=None):
        if self.fail_upload:
            raise ProtonError("offline")
        rel = dst_rel.strip("/")
        self.uploaded.append(rel)
        try:
            self.blobs[rel] = Path(local_path).read_text(encoding="utf-8")
        except OSError:
            pass
        self.nodes[rel] = RemoteNode(path=rel, name=rel.rsplit("/", 1)[-1])

    def upload_text(self, content, dst_rel, *, replace=True):
        rel = dst_rel.strip("/")
        self.blobs[rel] = content
        self.uploaded.append(rel)
        self.nodes[rel] = RemoteNode(path=rel, name=rel.rsplit("/", 1)[-1])

    def upload_many(self, local_paths, parent_rel, *, replace=False, total_size_hint=0):
        if self.fail_upload:
            raise ProtonError("offline")
        parent = parent_rel.strip("/")
        for local_path in local_paths:
            source = Path(local_path)
            name = source.name
            rel = f"{parent}/{name}".strip("/")
            self.uploaded.append(rel)
            try:
                content = source.read_bytes()
                import hashlib

                sha1 = hashlib.sha1(content).hexdigest()
                size = len(content)
            except OSError:
                sha1 = None
                size = 0
            self.nodes[rel] = RemoteNode(path=rel, name=name, size=size, sha1=sha1)

    def ensure_dir(self, rel):
        rel = rel.strip("/")
        if rel:
            self.nodes.setdefault(
                rel, RemoteNode(path=rel, name=rel.rsplit("/", 1)[-1], is_dir=True)
            )

    # -- reads ---------------------------------------------------------------
    def download_text(self, src_rel):
        return self.blobs[src_rel.strip("/")]

    def download(
        self, src_rel, dst_local_path, *, claimed_modtime=None, size_hint=None
    ):
        rel = src_rel.strip("/")
        target = Path(dst_local_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        content = self.blobs.get(rel, b"")
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")

    def stat_or_none(self, rel):
        return self.nodes.get(rel.strip("/"))

    def exists(self, rel):
        return rel.strip("/") in self.nodes

    def list_dir(self, rel):
        rel = rel.strip("/")
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

    def probe(self):
        return "Proton Drive CLI test"

    # -- mutations -----------------------------------------------------------
    def trash(self, rel):
        rel = rel.strip("/")
        self.trashed.append(rel)
        self.nodes.pop(rel, None)
        self.blobs.pop(rel, None)
        return True
