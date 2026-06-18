#!/usr/bin/env python3
"""Live smoke test for the Proton Drive CLI backend.

Exercises the real ProtonDriveCLI against a unique scratch folder on the live
account, then trashes it. Run this once after installing/upgrading the CLI
binary to confirm the backend works end-to-end before flipping the daemon.

    .venv/bin/python scripts/smoke-proton-cli.py

It NEVER touches data outside its scratch folder and NEVER runs empty-trash.
"""

from __future__ import annotations

import sys
import time
import hashlib
from pathlib import Path

# Allow running from a source checkout without install.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from protondrive_sync.core.config import AppConfig
from protondrive_sync.core.proton_cli import ProtonDriveCLI, sha1_matches


def _sha1(path: Path) -> str:
    h = hashlib.sha1()
    h.update(path.read_bytes())
    return h.hexdigest()


def main() -> int:
    config = AppConfig()
    cli = ProtonDriveCLI(config)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    scratch = f"opencode-smoke-{stamp}"
    failures: list[str] = []

    def check(label: str, cond: bool, detail: str = "") -> None:
        status = "PASS" if cond else "FAIL"
        print(f"  [{status}] {label}{(' - ' + detail) if detail else ''}")
        if not cond:
            failures.append(label)

    print(f"Proton CLI version: {cli.version()}")
    print(f"Probe: {cli.probe()}")
    print(f"Scratch folder: {scratch}")

    import tempfile

    with tempfile.TemporaryDirectory(prefix="proton-smoke-") as tmp:
        tmpdir = Path(tmp)
        src = tmpdir / "hello.txt"
        src.write_text("hello proton cli backend\n" * 10)
        local_sha1 = _sha1(src)
        local_size = src.stat().st_size

        # upload (new)
        cli.ensure_dir(scratch)
        cli.upload(str(src), f"{scratch}/hello.txt")
        node = cli.wait_until_present(f"{scratch}/hello.txt")
        check("upload+stat round trip", node is not None)
        if node:
            check(
                "remote size matches",
                node.size == local_size,
                f"{node.size} vs {local_size}",
            )
            check(
                "remote sha1 matches local",
                sha1_matches(node, local_sha1),
                str(node.sha1),
            )
            check("remote exposes claimed mtime", bool(node.modtime), str(node.modtime))

        # list
        children = cli.list_dir(scratch)
        check(
            "list_dir finds file",
            any(c.path == f"{scratch}/hello.txt" for c in children),
        )

        # download + integrity + mtime restore
        dest = tmpdir / "roundtrip.txt"
        cli.download(
            f"{scratch}/hello.txt",
            str(dest),
            claimed_modtime=node.modtime if node else None,
            size_hint=local_size,
        )
        check("download integrity", dest.exists() and _sha1(dest) == local_sha1)

        # overwrite via replace
        src.write_text("CHANGED CONTENT v2\n")
        new_sha1 = _sha1(src)
        cli.upload(str(src), f"{scratch}/hello.txt", replace=True)
        node2 = cli.wait_until_present(f"{scratch}/hello.txt")
        # eventual consistency: poll until sha1 reflects the new content
        for _ in range(5):
            if node2 and sha1_matches(node2, new_sha1):
                break
            time.sleep(3)
            node2 = cli.stat_or_none(f"{scratch}/hello.txt")
        check(
            "replace upload updates sha1", bool(node2) and sha1_matches(node2, new_sha1)
        )

        # trash (cleanup)
        ok = cli.trash(scratch)
        check("trash scratch folder", ok)
        check("scratch gone after trash", cli.wait_until_absent(scratch))

    print()
    if failures:
        print(f"SMOKE TEST FAILED: {len(failures)} check(s): {failures}")
        return 1
    print("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
