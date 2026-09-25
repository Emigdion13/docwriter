"""VaultNotes launcher.

Usage:
    python run.py          # loads the built frontend from src/vaultnotes/web/
    python run.py --dev    # loads the Vite dev server at http://localhost:5173

Run "npm install" and "npm run build" inside frontend/ once, so the built
files exist, before starting the app normally.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make "src/vaultnotes" importable when running straight from a checkout
# ("python run.py") without installing the package.
SRC_DIR = Path(__file__).resolve().parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from vaultnotes.app import main  # noqa: E402  (import after sys.path tweak)


def run() -> None:
    parser = argparse.ArgumentParser(description="Start the VaultNotes window.")
    parser.add_argument(
        "--dev",
        action="store_true",
        help="load the Vite dev server (http://localhost:5173) instead of the built files",
    )
    args = parser.parse_args()
    main(dev=args.dev)


if __name__ == "__main__":
    run()
