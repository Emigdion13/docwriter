"""Atomic file operations for VaultNotes."""

from __future__ import annotations

import os
from pathlib import Path


def atomic_write(path: Path | str, data: bytes | str, encoding: str = "utf-8") -> None:
    """Write data to path atomically using a temporary file in the same directory.
    
    Flushes and syncs to disk before replacing the target file to guarantee
    data integrity across crashes or sudden power loss.
    """
    dest = Path(path).resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)

    if isinstance(data, str):
        payload = data.encode(encoding)
    else:
        payload = data

    tmp_path = dest.with_name(f"{dest.name}.tmp")
    try:
        with open(tmp_path, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, dest)
    except BaseException:
        # BaseException, not Exception: a KeyboardInterrupt or a kill while
        # saving must not leave a half-written .tmp file behind either (M7).
        # The target file is untouched in every case, so the previous version
        # of the note survives.
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise
