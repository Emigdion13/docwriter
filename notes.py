"""Read Plain and Personal notes from the command line (the Encrypted vault is always refused).

Usage:
    python notes.py --root <notes folder> list plain
    python notes.py --root <notes folder> read plain "Shopping list"
    python notes.py --root <notes folder> --personal-key <file> search personal flights

See vaultnotes/notes_cli.py for the details.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make "src/vaultnotes" importable when running straight from a checkout,
# exactly like run.py.
SRC_DIR = Path(__file__).resolve().parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from vaultnotes.notes_cli import main  # noqa: E402  (import after sys.path tweak)

if __name__ == "__main__":
    main()
