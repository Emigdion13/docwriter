"""Tests for the Bridge API (Milestone M2)."""

from __future__ import annotations

from pathlib import Path
import pytest
from vaultnotes.api import Api
from vaultnotes.config import Config


@pytest.fixture
def api(tmp_path: Path) -> Api:
    settings_file = tmp_path / "settings.json"
    cfg = Config(settings_path=settings_file)
    cfg.data["notes_root"] = str(tmp_path / "notes")
    cfg.save()
    cfg.ensure_folders()
    return Api(config=cfg)


def test_api_get_state(api: Api) -> None:
    state = api.get_state()
    assert "spaces" in state
    assert len(state["spaces"]) == 3

    plain_space = next(s for s in state["spaces"] if s["id"] == "plain")
    assert plain_space["locked"] is False
    assert plain_space["kind"] == "plain"

    enc_space = next(s for s in state["spaces"] if s["id"] == "encrypted")
    assert enc_space["locked"] is True

    pers_space = next(s for s in state["spaces"] if s["id"] == "personal")
    assert pers_space["locked"] is True


def test_api_plain_crud(api: Api) -> None:
    # Create note
    created = api.create_note("plain", "Test Note")
    assert created["title"] == "Test Note"
    assert created["id"] == "Test Note"

    # Save note
    save_res = api.save_note("plain", "Test Note", "# Test Note\n\nBody content")
    assert "modified" in save_res

    # Open note
    opened = api.open_note("plain", "Test Note")
    assert opened["title"] == "Test Note"
    assert "Body content" in opened["body"]

    # List notes
    notes = api.list_notes("plain")
    assert any(n["id"] == "Test Note" for n in notes)

    # Rename note
    renamed = api.rename_note("plain", "Test Note", "Renamed Note")
    assert renamed["title"] == "Renamed Note"

    # Delete note to trash
    del_res = api.delete_note("plain", "Renamed Note")
    assert del_res["ok"] is True
    assert del_res["deleted"]["title"] == "Renamed Note"

    # List trash
    trash = api.list_trash("plain")
    assert any(t["title"] == "Renamed Note" for t in trash)

    # Restore note
    restore_res = api.restore_note("plain", "Renamed Note")
    assert restore_res["ok"] is True
    assert restore_res["note"]["title"] == "Renamed Note"


def test_api_rename_updates_links(api: Api) -> None:
    # Create Note 1 and Note 2 linking to Note 1
    api.create_note("plain", "Target Note")
    api.save_note("plain", "Target Note", "# Target Note\n\nTarget body")

    api.create_note("plain", "Source Note")
    api.save_note("plain", "Source Note", "Check out [[Target Note]] and [[Target Note|click here]]")

    # Count links to Target Note
    count_res = api.count_links_to("plain", "Target Note")
    assert count_res["count"] == 1

    # Rename Target Note -> New Target
    rename_res = api.rename_note("plain", "Target Note", "New Target", update_links=True)
    assert rename_res["title"] == "New Target"
    assert rename_res["links_updated"] == 2

    # Verify Source Note was updated
    src = api.open_note("plain", "Source Note")
    assert "[[New Target]]" in src["body"]
    assert "[[New Target|click here]]" in src["body"]


def test_api_render_preview(api: Api) -> None:
    api.create_note("plain", "PreviewTarget")
    html = api.render_preview("plain", "Link to [[PreviewTarget]]\n\n# Title\n\n- [x] Done")
    assert 'href="#vn-open/PreviewTarget"' in html
    assert "<h1>Title</h1>" in html


def test_api_open_external_security(api: Api) -> None:
    # Safe schemes
    assert api.open_external("https://example.com") == {"ok": True}
    assert api.open_external("http://example.com") == {"ok": True}
    assert api.open_external("mailto:user@example.com") == {"ok": True}

    # Unsafe schemes must be blocked
    res_file = api.open_external("file:///etc/passwd")
    assert "error" in res_file
    res_js = api.open_external("javascript:alert(1)")
    assert "error" in res_js


def test_api_settings(api: Api) -> None:
    curr = api.get_settings()
    assert "look" in curr

    updated = api.update_settings({"look": {"theme": "synthwave"}})
    assert updated["look"]["theme"] == "synthwave"
    assert api.get_settings()["look"]["theme"] == "synthwave"
