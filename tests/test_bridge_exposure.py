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


def test_the_page_cannot_pass_file_paths(api: Api, tmp_path: Path) -> None:
    """Rule 12f: paths come from Python's own dialogs, never from the page."""
    window = FakeWindow()
    expose_bridge(window, api)
    note_id = api.create_note("plain", "Secret-ish")["id"]
    outside = tmp_path / "anywhere" / "copy.md"

    calls = [
        ("export_note", ("plain", note_id, str(outside))),
        ("import_notes", ("plain", [str(tmp_path / "x.md")])),
        ("choose_notes_folder", (str(tmp_path / "elsewhere"),)),
        ("create_vault", ("encrypted", str(tmp_path / "k.vnkey"))),
        ("initialize_vaults", ({"encrypted": str(tmp_path / "k.vnkey")},)),
        ("restore_from_drive", (str(tmp_path / "restore"),)),
        ("choose_client_secret", (str(tmp_path / "client_secret.json"),)),
    ]
    for name, args in calls:
        result = window.exposed[name](*args)
        assert result.get("error") == "invalid_input", (name, result)
    assert not outside.exists() and not (tmp_path / "k.vnkey").exists()
    assert not (tmp_path / "elsewhere").exists()
    # Leaving the path out is what the UI does, and still works.
    assert window.exposed["export_note"]("plain", note_id).get("error") == "cancelled"


def test_a_page_the_window_navigated_to_gets_nothing(api: Api) -> None:
    """pywebview injects the bridge into any page; the calls check the origin."""

    class NavigatingWindow(FakeWindow):
        real_url = "http://127.0.0.1:51234/index.html"
        current = "http://127.0.0.1:51234/index.html"

        def get_current_url(self) -> str:
            return self.current

    window = NavigatingWindow()
    expose_bridge(window, api)
    assert "spaces" in window.exposed["get_state"]()

    window.current = "https://evil.example/drop.html"  # e.g. a dropped link
    assert window.exposed["list_notes"]("plain") == {
        "error": "forbidden",
        "message": "Only the VaultNotes page can use VaultNotes.",
    }
    window.current = "file:///C:/Users/me/Downloads/page.html"
    assert window.exposed["get_state"]().get("error") == "forbidden"


def test_closing_the_window_waits_for_the_page_to_save(api: Api) -> None:
    import threading as _threading

    from vaultnotes.app import hold_close_until_saved

    class FakeEvent(list):
        """pywebview events take handlers with +=."""

        def __iadd__(self, handler):  # type: ignore[override]
            self.append(handler)
            return self

    class ClosingWindow:
        def __init__(self) -> None:
            class Events:
                def __init__(self) -> None:
                    self.closing = FakeEvent()

            self.events = Events()
            self.destroyed = _threading.Event()
            self.scripts: list[str] = []

        def evaluate_js(self, script: str) -> None:
            self.scripts.append(script)
            api.ready_to_close()  # what the page does after flushSave()

        def destroy(self) -> None:
            self.destroyed.set()

    window = ClosingWindow()
    hold_close_until_saved(window, api)
    (on_closing,) = window.events.closing

    assert on_closing() is False, "the first close waits for the page"
    assert window.destroyed.wait(timeout=5), "then the window closes"
    assert "flushBeforeClose" in window.scripts[0]
    assert on_closing() is None, "the second close goes through"
