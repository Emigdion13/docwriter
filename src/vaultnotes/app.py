"""VaultNotes app shell.

Creates the single pywebview window. In M1 there is no Python Bridge API
yet (the frontend fakes it in bridge.js); milestone M2 wires api.Api() in.
"""

from __future__ import annotations

import sys
from pathlib import Path

import webview

# Directory that "npm run build" fills (frontend/vite.config.js -> build.outDir).
WEB_DIR = Path(__file__).resolve().parent / "web"

# Where the Vite dev server listens (frontend: "npm run dev").
DEV_URL = "http://localhost:5173"


def _frontend_target(dev: bool) -> str:
    """Return what the window should load.

    With --dev: the Vite dev server URL (hot reload for CSS/JS).
    Otherwise: the built index.html, served by pywebview's own local
    server, which only serves the static files in web/.
    """
    if dev:
        return DEV_URL

    index = WEB_DIR / "index.html"
    if not index.is_file():
        sys.stderr.write(
            "VaultNotes: the built frontend is missing.\n"
            f"Expected: {index}\n"
            "Build it once with:\n"
            "    cd frontend\n"
            "    npm install\n"
            "    npm run build\n"
            "then start the app again with:  python run.py\n"
        )
        raise SystemExit(1)
    return str(index)


def main(dev: bool = False) -> None:
    """Open the VaultNotes window and block until it is closed."""
    webview.create_window(
        "VaultNotes",
        url=_frontend_target(dev),
        width=1280,
        height=800,
        min_size=(1000, 640),
        # Match --bg of the Nebula theme so the window never flashes white
        # while the page loads.
        background_color="#06070d",
        # Security rule 12d: no persistent storage in the webview.
        private_mode=True,
        # Developer tools only in --dev mode (security rule 12g).
        debug=dev,
        # M2 will pass js_api=api.Api() here; M1's UI talks to a fake bridge.
    )
    webview.start()
