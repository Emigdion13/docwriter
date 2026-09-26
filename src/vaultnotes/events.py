"""Event dispatcher from Python engine to frontend pywebview window."""

from __future__ import annotations

import json
from typing import Any


def emit_event(window: Any, event_name: str, data: Any = None) -> None:
    """Evaluate JS in pywebview window to dispatch an event to window.vn.emit.

    Guarantees proper JSON escaping by using json.dumps.
    """
    if window is None:
        return

    try:
        payload = json.dumps(data)
        event_json = json.dumps(event_name)
        js = f"window.vn && window.vn.emit({event_json}, {payload});"
        window.evaluate_js(js)
    except Exception:
        # Ignore errors if window is closed or webview runtime is destroyed
        pass
