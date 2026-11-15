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

#: Small helpers Claude Code starts during a Claude-space reply.  In the built
#: app there is no python.exe and no .py file on disk, so Claude Code runs
#: VaultNotes.exe itself with one of these flags (see claude_chat.helper_command).
HELPERS = {
    "--claude-permit": "vaultnotes.claude_permit",
    "--claude-queue-hook": "vaultnotes.claude_queue",
}


def run_helper(argv: list[str]) -> int | None:
    """Run the helper ``argv[1]`` names and return its exit code, or None for the app."""
    if len(argv) < 2 or argv[1] not in HELPERS:
        return None
    import importlib

    # A windowed exe may start without Python-level stdio even though Claude
    # Code gave it pipes: attach to the inherited handles.
    if sys.stdin is None:
        sys.stdin = open(0, "r", encoding="utf-8", closefd=False)  # noqa: SIM115
    if sys.stdout is None:
        sys.stdout = open(1, "w", encoding="utf-8", closefd=False)  # noqa: SIM115
    return importlib.import_module(HELPERS[argv[1]]).main(argv[2:])


if __name__ == "__main__":
    # Before the window code is imported: a helper must start fast and quietly.
    _code = run_helper(sys.argv)
    if _code is not None:
        sys.exit(_code)

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
