"""VaultNotes app shell.

Creates the single pywebview window and wires the Python Bridge API.
"""

from __future__ import annotations

import sys
from pathlib import Path

import webview
from filelock import FileLock, Timeout

from vaultnotes.api import Api
from vaultnotes.config import get_app_dir, get_config

# Directory that "npm run build" fills (frontend/vite.config.js -> build.outDir).
WEB_DIR = Path(__file__).resolve().parent / "web"

# Where the Vite dev server listens (frontend: "npm run dev").
DEV_URL = "http://localhost:5173"


def _frontend_target(dev: bool) -> str:
    """Return what the window should load.

    With --dev: the Vite dev server URL (hot reload for CSS/JS).
    Otherwise: the built index.html, served by pywebview's local server.
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
    app_dir = get_app_dir()
    lock_file = app_dir / "app.lock"
    lock = FileLock(lock_file, timeout=0.1)

    try:
        lock.acquire()
    except Timeout:
        sys.stderr.write("VaultNotes: another copy of the app is already running.\n")
        raise SystemExit(0)

    try:
        config = get_config()
        api = Api(config=config)

        window = webview.create_window(
            "VaultNotes",
            url=_frontend_target(dev),
            width=1280,
            height=800,
            min_size=(1000, 640),
            background_color="#06070d",
            js_api=api,
        )
        api.set_window(window)
        try:
            webview.start(debug=dev, private_mode=True)
        except Exception as e:
            sys.stderr.write(
                f"VaultNotes: could not start GUI window: {e}\n"
                "On Windows, Microsoft Edge WebView2 is used automatically.\n"
                "On Linux, install python3-gi / GTK or PyQt.\n"
            )
    finally:
        try:
            lock.release()
        except Exception:
            pass
