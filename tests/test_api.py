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


# ----------------------------------------------------------------------
# Milestone M5: move, import/export, trash purge, sort, settings validation
# ----------------------------------------------------------------------


@pytest.fixture
def api_with_vaults(tmp_path: Path) -> Api:
    settings_file = tmp_path / "settings.json"
    cfg = Config(settings_path=settings_file)
    cfg.data["notes_root"] = str(tmp_path / "notes")
    cfg.save()
    cfg.ensure_folders()
    unlocked = Api(config=cfg)
    created = unlocked.initialize_vaults(
        key_paths={
            "encrypted": tmp_path / "keys" / "encrypted.vnkey",
            "personal": tmp_path / "keys" / "personal.vnkey",
        }
    )
    assert created.get("ok") is True
    assert unlocked.unlock_vault("encrypted").get("ok") is True
    assert unlocked.unlock_vault("personal").get("ok") is True
    return unlocked


def test_api_move_plain_to_vault_and_back(api_with_vaults: Api) -> None:
    api = api_with_vaults
    api.save_note("plain", api.create_note("plain", "Travel 2026")["id"], "# Travel 2026\n\nBody [[Home lab]]")

    moved = api.move_note("plain", "Travel 2026", "encrypted")
    assert moved.get("new_id")
    assert len(moved["new_id"]) == 32  # opaque vault id, not the title
    assert moved["title"] == "Travel 2026"

    # Source is gone from Plain (a move is not a delete-to-trash).
    assert api.open_note("plain", "Travel 2026").get("error") == "not_found"
    assert all(t["title"] != "Travel 2026" for t in api.list_trash("plain"))

    # Target holds the exact body, encrypted on disk.
    opened = api.open_note("encrypted", moved["new_id"])
    assert opened["body"] == "# Travel 2026\n\nBody [[Home lab]]"

    back = api.move_note("encrypted", moved["new_id"], "plain")
    assert back["title"] == "Travel 2026"
    reopened = api.open_note("plain", back["new_id"])
    assert reopened["body"] == "# Travel 2026\n\nBody [[Home lab]]"
    assert api.open_note("encrypted", moved["new_id"]).get("error") == "not_found"


def test_api_move_between_vaults(api_with_vaults: Api) -> None:
    api = api_with_vaults
    created = api.create_note("encrypted", "Secret")
    api.save_note("encrypted", created["id"], "vault body")

    moved = api.move_note("encrypted", created["id"], "personal")
    assert moved.get("new_id")
    assert moved["new_id"] != created["id"]  # new opaque id in the new vault
    assert api.open_note("personal", moved["new_id"])["body"] == "vault body"
    assert api.open_note("encrypted", created["id"]).get("error") == "not_found"


def test_api_move_errors(api_with_vaults: Api) -> None:
    api = api_with_vaults
    api.create_note("plain", "Mover")

    assert api.move_note("plain", "Mover", "plain").get("error") == "same_space"
    assert api.move_note("nope", "Mover", "plain").get("error") == "invalid_space"
    assert api.move_note("plain", "Mover", "nope").get("error") == "invalid_space"
    assert api.move_note("plain", "Missing", "personal").get("error") == "not_found"

    api.lock_vault("personal")
    locked_target = api.move_note("plain", "Mover", "personal")
    assert locked_target.get("error") == "locked"

    secret = api.create_note("encrypted", "Locked source")
    api.lock_vault("encrypted")
    assert api.move_note("encrypted", secret["id"], "plain").get("error") == "locked"


def test_api_move_counts_broken_links(api_with_vaults: Api) -> None:
    api = api_with_vaults
    api.create_note("plain", "Target Note")
    api.save_note("plain", "Target Note", "# Target Note\n\nSee also [[Helper]]")
    api.create_note("plain", "Source Note")
    api.save_note("plain", "Source Note", "Links to [[Target Note]] here")
    api.create_note("plain", "Helper")

    # One backlink (Source Note) plus one outgoing link (Helper) break.
    moved = api.move_note("plain", "Target Note", "personal")
    assert moved["broken_links"] == 2


def test_api_import_and_export(api_with_vaults: Api, tmp_path: Path) -> None:
    api = api_with_vaults
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "Imported.md").write_text("# Imported\n\nhello", encoding="utf-8")
    (inbox / "notes.txt").write_text("not markdown", encoding="utf-8")

    res = api.import_notes("plain", [inbox / "Imported.md", inbox / "notes.txt"])
    assert res["ok"] is True
    assert [n["title"] for n in res["imported"]] == ["Imported"]
    assert len(res["skipped"]) == 1
    assert api.open_note("plain", "Imported")["body"] == "# Imported\n\nhello"

    # Importing the same file again resolves the title collision.
    again = api.import_notes("plain", [inbox / "Imported.md"])
    assert again["imported"][0]["title"] == "Imported (2)"

    # Import into an unlocked vault encrypts the note.
    vaulted = api.import_notes("encrypted", [inbox / "Imported.md"])
    assert vaulted["imported"][0]["title"] == "Imported"
    assert api.open_note("encrypted", vaulted["imported"][0]["id"])["body"].startswith("# Imported")

    api.lock_vault("encrypted")
    assert api.import_notes("encrypted", [inbox / "Imported.md"]).get("error") == "locked"

    # Export writes a plain .md copy anywhere the caller chooses.
    out_file = tmp_path / "out" / "copy.md"
    exported = api.export_note("plain", "Imported", out_file)
    assert exported == {"ok": True, "name": "copy.md"}
    assert out_file.read_text(encoding="utf-8") == "# Imported\n\nhello"
    assert api.export_note("plain", "Missing", out_file).get("error") == "not_found"


def test_api_purge_and_empty_trash(api_with_vaults: Api) -> None:
    api = api_with_vaults
    api.create_note("plain", "Gone")
    api.delete_note("plain", "Gone")
    assert api.purge_note("plain", "Gone") == {"ok": True, "title": "Gone"}
    assert api.list_trash("plain") == []
    assert api.purge_note("plain", "Gone").get("error") == "not_found"

    api.create_note("plain", "A")
    api.create_note("plain", "B")
    api.delete_note("plain", "A")
    api.delete_note("plain", "B")
    assert api.empty_trash("plain") == {"ok": True, "purged": 2}
    assert api.empty_trash("plain") == {"ok": True, "purged": 0}

    secret = api.create_note("personal", "Vault trash")
    api.delete_note("personal", secret["id"])
    assert len(api.list_trash("personal")) == 1
    assert api.empty_trash("personal") == {"ok": True, "purged": 1}
    api.lock_vault("personal")
    assert api.empty_trash("personal").get("error") == "locked"
    assert api.purge_note("personal", secret["id"]).get("error") == "locked"


def test_api_sort_options(api: Api) -> None:
    titles = api.list_notes("plain", sort="title")
    assert [n["title"] for n in titles] == sorted(
        [n["title"] for n in titles], key=str.lower
    )
    # Unknown sort values fall back to modified order instead of crashing.
    assert isinstance(api.list_notes("plain", sort="bogus"), list)


def test_api_settings_validation(api: Api) -> None:
    assert api.update_settings({"look": {"theme": "nope"}}).get("error") == "invalid_settings"
    assert api.update_settings({"look": {"effects": "nope"}}).get("error") == "invalid_settings"
    assert api.update_settings({"look": {"view_mode": "nope"}}).get("error") == "invalid_settings"
    assert api.update_settings({"look": {"editor_font_size": 99}}).get("error") == "invalid_settings"
    assert api.update_settings({"look": {"editor_font_size": "big"}}).get("error") == "invalid_settings"
    assert api.update_settings({"autolock_minutes": 0}).get("error") == "invalid_settings"
    assert api.update_settings({"autolock_minutes": "soon"}).get("error") == "invalid_settings"
    assert api.update_settings({"notes_root": ""}).get("error") == "invalid_settings"

    ok = api.update_settings(
        {"look": {"editor_font_size": 15}, "autolock_minutes": 1}
    )
    assert ok["look"]["editor_font_size"] == 15
    assert ok["autolock_minutes"] == 1
