"""Event dispatcher from Python engine to frontend pywebview window."""

from __future__ import annotations

import json
import threading
from typing import Any

#: Set while the window has no live page (its WebView2 browser died and
#: ``webview_guard`` is bringing it back).  pywebview's ``evaluate_js`` can wait
#: forever on a dead browser, so nothing is sent until the page loads again;
#: the page asks for what it needs (state, open pop-ups) when it starts.
PAGE_DOWN = threading.Event()


def emit_event(window: Any, event_name: str, data: Any = None) -> None:
    """Evaluate JS in pywebview window to dispatch an event to window.vn.emit.

    Guarantees proper JSON escaping by using json.dumps.
    """
    if window is None or PAGE_DOWN.is_set():
        return

    try:
        payload = json.dumps(data)
        event_json = json.dumps(event_name)
        js = f"window.vn && window.vn.emit({event_json}, {payload});"
        window.evaluate_js(js)
    except Exception:
        # Ignore errors if window is closed or webview runtime is destroyed
        pass
