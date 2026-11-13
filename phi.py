"""PHI token filter: tokenise PHI in Claude's tool output, reveal tokens on request.

Usage:
    python phi.py hook                    # Claude Code hook (JSON on stdin)
    python phi.py reveal TOKEN [TOKEN..]  # verified Vertex session only
    python phi.py scan < file.txt         # tokenise a text
    python phi.py status

See vaultnotes/phi/ for the details.
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from vaultnotes.phi.cli import main  # noqa: E402  (import after sys.path tweak)

if __name__ == "__main__":
    main()
