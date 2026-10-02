"""One SQL result the user chose to share with an AI helper.

The SQL grid's "Share with AI" box writes the ticked result here and nowhere
else, so a helper such as Claude can read it with ``notes.py results``.  The
file lives in ``%LOCALAPPDATA%\\VaultNotes\\ai-share``: outside the notes folder,
so the Google Drive backup never sees it.

It holds at most one result, only its first :data:`MAX_SHARE_ROWS` rows, and
only for :data:`SHARE_EXPIRY_SECONDS`.  The SQL space removes it as soon as the
tab runs another query, closes, or turns off, and the app removes any leftover
at start-up.  :func:`load_snapshot` also ignores an expired file, so a crash
cannot leave a result readable.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

#: The folder (inside the per-PC data folder) and file of the shared result.
SHARE_FOLDER = "ai-share"
SHARE_FILE = "result.json"
#: Rows a shared result may carry; the rest stay in the app.
MAX_SHARE_ROWS = 1000
#: Seconds a shared result stays readable.
SHARE_EXPIRY_SECONDS = 30 * 60
#: What one cell or the query text may carry into the shared file.
MAX_SHARE_CELL_CHARS = 2000
MAX_SHARE_QUERY_CHARS = 20_000

SNAPSHOT_FORMAT = "vaultnotes-shared-result"


class AiShare:
    """Writes and removes the shared result file (the app's side)."""

    def __init__(self, folder: Path | str) -> None:
        self.folder = Path(folder)

    @property
    def path(self) -> Path:
        return self.folder / SHARE_FILE

    def write(self, snapshot: dict[str, Any], expires_in: float) -> None:
        """Replace the shared result; the new file appears whole or not at all."""
        now = time.time()
        data = {
            **snapshot,
            "format": SNAPSHOT_FORMAT,
            "version": 1,
            "shared_at": now,
            "expires_at": now + expires_in,
        }
        self.folder.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(temp, self.path)

    def clear(self) -> None:
        """Remove the shared result, and a half-written one; never creates anything."""
        for path in (self.path, self.path.with_suffix(".tmp")):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                # Cannot delete it (locked?): empty it so nothing stays readable.
                try:
                    path.write_text("", encoding="utf-8")
                except OSError:
                    pass


def load_snapshot(folder: Path | str, now: float | None = None) -> dict[str, Any] | None:
    """The shared result, or None when nothing is shared, it expired or it is damaged."""
    path = Path(folder) / SHARE_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("format") != SNAPSHOT_FORMAT:
        return None
    expires = data.get("expires_at")
    if not isinstance(expires, (int, float)) or (time.time() if now is None else now) >= expires:
        return None
    return data
