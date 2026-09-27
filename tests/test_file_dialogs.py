"""The native file dialogs, driven the way the desktop app drives them.

Every path the UI uses goes through ``window.create_file_dialog``.  The fake
window below checks its arguments exactly as pywebview does before a dialog
opens; a filter pywebview rejects once made every dialog fail while the app
reported "cancelled".
"""

from __future__ import annotations

from pathlib import Path

import pytest
import webview
from webview.util import parse_file_type

from vaultnotes.api import Api
from vaultnotes.backup.gdrive_auth import TokenStore
from vaultnotes.config import Config


class DialogWindow:
    """Answers each dialog with the next queued path, after pywebview's checks."""

    def __init__(self, answers: list[object]) -> None:
        self.answers = list(answers)
        self.dialogs: list[tuple[object, dict]] = []

    def create_file_dialog(self, dialog_type: object, **kwargs: object) -> object:
        # What pywebview.Window.create_file_dialog does before showing anything.
        assert dialog_type in tuple(webview.FileDialog), dialog_type
        for file_type in kwargs.get("file_types", ()):  # type: ignore[union-attr]
            parse_file_type(file_type)  # raises ValueError for a bad filter
        self.dialogs.append((dialog_type, kwargs))
        return self.answers.pop(0)

    def evaluate_js(self, script: str) -> None:  # events
        pass


@pytest.fixture
def api(tmp_path: Path) -> Api:
    cfg = Config(settings_path=tmp_path / "settings.json")
    cfg.data["notes_root"] = str(tmp_path / "notes")
    cfg.save()
    cfg.ensure_folders()
    return Api(config=cfg, app_dir=tmp_path / "appdata", drive_store=TokenStore(memory=True))


def test_create_vaults_through_the_save_dialogs(api: Api, tmp_path: Path) -> None:
    keys = tmp_path / "usb"
    window = DialogWindow([(str(keys / "encrypted.vnkey"),), (str(keys / "personal.vnkey"),)])
    api.set_window(window)

    result = api.initialize_vaults(None, None)  # exactly what the UI sends

    assert result == {"ok": True, "created": ["encrypted", "personal"]}, result
    assert (keys / "encrypted.vnkey").is_file() and (keys / "personal.vnkey").is_file()
    assert [kind for kind, _ in window.dialogs] == [webview.FileDialog.SAVE] * 2
    assert window.dialogs[0][1]["save_filename"] == "encrypted.vnkey"


def test_browse_for_a_key_file_to_unlock(api: Api, tmp_path: Path) -> None:
    keys = tmp_path / "usb"
    api.set_window(DialogWindow([(str(keys / "encrypted.vnkey"),), (str(keys / "personal.vnkey"),)]))
    assert api.initialize_vaults(None, None).get("ok") is True
    api.lock_all()
    api.config.set_vault_key_path("Encrypted", tmp_path / "moved-away.vnkey")

    api.set_window(DialogWindow([(str(keys / "encrypted.vnkey"),)]))
    chosen = api.choose_key_file("encrypted")
    assert "error" not in chosen, chosen
    assert api.unlock_vault("encrypted").get("ok") is True


def test_import_and_export_through_the_dialogs(api: Api, tmp_path: Path) -> None:
    source = tmp_path / "outside" / "Recipe.md"
    source.parent.mkdir()
    source.write_text("# Recipe\n\nflour\n", encoding="utf-8")
    exported = tmp_path / "outside" / "Recipe copy.md"
    api.set_window(DialogWindow([(str(source),), str(exported)]))

    imported = api.import_notes("plain")
    assert "error" not in imported, imported
    note_id = next(n["id"] for n in api.list_notes("plain") if n["title"] == "Recipe")
    result = api.export_note("plain", note_id)
    assert result.get("ok") is True, result
    assert exported.read_text(encoding="utf-8") == "# Recipe\n\nflour\n"


def test_closing_a_dialog_is_cancelled(api: Api) -> None:
    api.set_window(DialogWindow([None]))
    assert api.initialize_vaults(None, None)["error"] == "cancelled"


def test_a_dialog_that_fails_is_an_error_not_a_cancel(api: Api) -> None:
    class BrokenWindow(DialogWindow):
        def create_file_dialog(self, dialog_type: object, **kwargs: object) -> object:
            raise RuntimeError("WebView2 is gone")

    api.set_window(BrokenWindow([]))
    result = api.initialize_vaults(None, None)
    assert result["error"] == "dialog_failed", result
    assert "could not be opened" in result["message"]
