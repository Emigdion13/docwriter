"""The page gets exactly the Bridge API and nothing reachable beyond it (security rule 12f).

pywebview exposes, and dispatches calls to, anything reachable from a
``js_api`` object.  app.py therefore registers plain functions with
``window.expose`` instead; these tests pin that down.
"""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

import pytest

from vaultnotes.api import Api, bridge_function_names, expose_bridge
from vaultnotes.backup.gdrive_auth import TokenStore
from vaultnotes.config import Config

REPO = Path(__file__).resolve().parent.parent


class FakeWindow:
    """Records what app.py hands to pywebview's ``window.expose``."""

    def __init__(self) -> None:
        self.exposed: dict[str, object] = {}

    def expose(self, *functions: object) -> None:
        for function in functions:
            self.exposed[function.__name__] = function  # type: ignore[attr-defined]


@pytest.fixture
def api(tmp_path: Path) -> Api:
    cfg = Config(settings_path=tmp_path / "settings.json")
    cfg.data["notes_root"] = str(tmp_path / "notes")
    cfg.save()
    cfg.ensure_folders()
    return Api(config=cfg, app_dir=tmp_path / "appdata", drive_store=TokenStore(memory=True))


def test_exposes_every_bridge_method_and_nothing_else(api: Api) -> None:
    window = FakeWindow()
    expose_bridge(window, api)

    assert sorted(window.exposed) == list(bridge_function_names())
    assert {"set_window", "close"}.isdisjoint(window.exposed)
    for name, function in window.exposed.items():
        # Plain functions are dispatched by exact name; pywebview never walks into them.
        assert inspect.isfunction(function), name
        assert not hasattr(function, "__self__"), name
        assert "." not in name and not name.startswith("_"), name


def test_exposed_functions_forward_to_the_api(api: Api) -> None:
    window = FakeWindow()
    expose_bridge(window, api)

    assert window.exposed["get_state"]() == api.get_state()
    window.exposed["create_note"]("plain", "Written from the page")
    titles = [note["title"] for note in api.list_notes("plain")]
    assert "Written from the page" in titles


def test_every_function_the_frontend_calls_is_exposed() -> None:
    called: set[str] = set()
    for path in (REPO / "frontend" / "src").rglob("*.js"):
        called |= set(re.findall(r"\bapi\.([A-Za-z_]+)\(", path.read_text(encoding="utf-8")))

    assert called, "found no Bridge API calls in frontend/src"
    missing = called - set(bridge_function_names())
    assert not missing, f"the frontend calls functions that are not exposed: {sorted(missing)}"


def test_app_never_hands_pywebview_the_api_object() -> None:
    tree = ast.parse((REPO / "src" / "vaultnotes" / "app.py").read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "create_window"
    ]
    assert calls, "app.py no longer calls webview.create_window"
    for call in calls:
        assert all(keyword.arg != "js_api" for keyword in call.keywords)
