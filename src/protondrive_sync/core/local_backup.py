"""Pure-Python local backup helpers (no remote backend dependency).

Local delete/overwrite protection moves the affected local file into a sibling
app backup directory before it is replaced or removed, so a bad sync decision is
always recoverable from disk. Remote-side protection is handled separately by
the backend's trash (recoverable via Proton Drive).
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Optional


LOCAL_BACKUP_DIR = ".protondrive-sync-backups"


def move_local_to_backup(
    local_path: str, *, run_id: str | None = None
) -> Optional[Path]:
    """Move a local path into a sibling app backup directory.

    Returns the backup destination, or None if the source does not exist.
    """
    src = Path(local_path)
    if not os.path.lexists(src):
        return None
    stamp = run_id or datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = src.parent / LOCAL_BACKUP_DIR / "targeted" / stamp / src.name
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.replace(dst)
    return dst
