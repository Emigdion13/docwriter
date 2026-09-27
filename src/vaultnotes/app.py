"""VaultNotes app shell.

Creates the single pywebview window and wires the Python Bridge API.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Any

import webview
from filelock import FileLock, Timeout

from vaultnotes.api import Api, expose_bridge
from vaultnotes.config import get_app_dir, get_config

# Directory that "npm run build" fills (frontend/vite.config.js -> build.outDir).
WEB_DIR = Path(__file__).resolve().parent / "web"

# Where the Vite dev server listens (frontend: "npm run dev").
DEV_URL = "http://127.0.0.1:5173"

# How long closing the window waits for the page to save its last edit.
CLOSE_SAVE_TIMEOUT = 5.0


def hold_close_until_saved(window: Any, api: Api) -> None:
    """Let the page save before the window closes.

    Autosave runs a second after typing stops, so closing the window right
    after typing used to lose that text.  The first close is held back while
    the page saves (at most ``CLOSE_SAVE_TIMEOUT`` seconds, so a broken page
    can never keep the window open), then the window closes for real.
    """
    may_close = threading.Event()
    started = threading.Event()

    def save_then_close() -> None:
        try:
            window.evaluate_js(
                "window.vn && window.vn.flushBeforeClose && (window.vn.flushBeforeClose(), true)"
            )
            api.page_saved_for_close.wait(CLOSE_SAVE_TIMEOUT)
        except Exception:  # noqa: BLE001 - closing must always go ahead
            pass
        finally:
            may_close.set()
            window.destroy()

    def on_closing() -> bool | None:
        # Runs on the window's UI thread, which the page needs in order to
        # answer: never wait here.
        if may_close.is_set():
            return None
        if not started.is_set():
            started.set()
            threading.Thread(target=save_then_close, name="VaultNotes-close", daemon=True).start()
        return False  # not yet

    window.events.closing += on_closing


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

        # No js_api here: expose_bridge() hands the page only the Bridge API
        # functions (see its docstring for why passing the Api object is unsafe).
        window = webview.create_window(
            "VaultNotes",
            url=_frontend_target(dev),
            width=1280,
            height=800,
            min_size=(1000, 640),
            background_color="#06070d",
        )
        expose_bridge(window, api)
        hold_close_until_saved(window, api)
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
            api.close()
    finally:
        try:
            lock.release()
        except Exception:
            pass
