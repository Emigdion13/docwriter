"""Atomic file operations for VaultNotes."""

from __future__ import annotations

import os
import tempfile
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

    # A unique temp name per write: two saves of one note at the same moment
    # must never share (or delete) each other's temp file.  It still ends in
    # ".tmp", which the note listing and the Drive backup both skip.
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, dest)
    except BaseException:
        # BaseException, not Exception: a KeyboardInterrupt or a kill while
        # saving must not leave a half-written .tmp file behind either (M7).
        # The target file is untouched in every case, so the previous version
        # of the note survives.
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise
