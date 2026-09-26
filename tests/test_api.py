"""Tests for the Bridge API (Milestone M2)."""

from __future__ import annotations

import inspect
import json
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


# ----------------------------------------------------------------------
# Milestone M7: bad inputs to the Bridge API return errors and never crash
# ----------------------------------------------------------------------

# Values a broken or hostile frontend could hand us.  pywebview passes whatever
# JavaScript sent, so none of these may reach the stores unchecked.
HOSTILE_VALUES = [
    None,
    0,
    -1,
    3.5,
    True,
    "",
    "   ",
    [],
    {},
    (),
    "../../etc/passwd",
    "..\\..\\windows\\system32",
    "/absolute/path",
    "\x00nul",
    "a" * 5000,
    "😀🚀",
    {"error": "not a space"},
]

BRIDGE_METHODS_TAKING_A_SPACE = [
    "list_notes",
    "open_note",
    "create_note",
    "save_note",
    "rename_note",
    "count_links_to",
    "delete_note",
    "restore_note",
    "list_trash",
    "purge_note",
    "empty_trash",
    "move_note",
    "import_notes",
    "export_note",
    "render_preview",
    "list_titles",
    "unlock_vault",
    "lock_vault",
    "choose_key_file",
    "create_vault",
    "note_links",
]


def assert_bridge_shape(result: object, allow_text: bool = False) -> None:
    """Every Bridge API answer is JSON data, and an error carries a message.

    ``render_preview`` is the one endpoint that returns an HTML string instead
    of a structure (section 4.8), hence ``allow_text``.
    """
    allowed = (dict, list, str) if allow_text else (dict, list)
    assert isinstance(result, allowed), result
    json.dumps(result)  # must be serializable back to JavaScript
    if isinstance(result, dict) and "error" in result:
        assert isinstance(result["error"], str) and result["error"]
        assert isinstance(result.get("message"), str) and result.get("message")


@pytest.mark.parametrize("hostile", HOSTILE_VALUES)
def test_unknown_spaces_are_refused(api: Api, hostile: object) -> None:
    for name in BRIDGE_METHODS_TAKING_A_SPACE:
        method = getattr(api, name)
        assert_bridge_shape(method(hostile))
        assert_bridge_shape(method(hostile, hostile))
        assert_bridge_shape(method(hostile, hostile, hostile))
        assert_bridge_shape(method(hostile, hostile, hostile, hostile))


@pytest.mark.parametrize("hostile", HOSTILE_VALUES)
def test_bad_note_ids_and_bodies_are_refused(api: Api, hostile: object) -> None:
    api.create_note("plain", "Real note")
    notes_before = len(api.list_notes("plain"))
    files_before = len(list(api.config.plain_dir.glob("*.md")))

    assert_bridge_shape(api.open_note("plain", hostile))
    assert_bridge_shape(api.save_note("plain", hostile, hostile))
    assert_bridge_shape(api.save_note("plain", "Real note", hostile))
    assert_bridge_shape(api.rename_note("plain", hostile, hostile))
    assert_bridge_shape(api.rename_note("plain", "Real note", hostile))
    assert_bridge_shape(api.delete_note("plain", hostile))
    assert_bridge_shape(api.count_links_to("plain", hostile))
    assert_bridge_shape(api.move_note("plain", hostile, "encrypted"))
    assert_bridge_shape(api.render_preview("plain", hostile), allow_text=True)
    assert_bridge_shape(api.note_links("plain", hostile))

    # Nothing was lost, duplicated or written outside the space.  A value like
    # 0 is a *valid* title, so the sequence above may legitimately rename the
    # note and then delete it - but a delete only ever moves it to the trash.
    listing = api.list_notes("plain")
    assert isinstance(listing, list)
    assert len(listing) + len(api.list_trash("plain")) == notes_before
    on_disk = len(list(api.config.plain_dir.glob("*.md"))) + len(
        list((api.config.plain_dir / ".trash").glob("*.md"))
    )
    assert on_disk == files_before
    assert not list(api.config.notes_root.parent.glob("*.md"))


@pytest.mark.parametrize("hostile", HOSTILE_VALUES)
def test_settings_reject_hostile_changes(api: Api, hostile: object) -> None:
    before = api.get_settings()
    result = api.update_settings(hostile)  # type: ignore[arg-type]
    assert_bridge_shape(result)
    if hostile == {}:
        # An empty change is a harmless no-op rather than an error.
        assert result == before
        return
    assert result.get("error") == "invalid_settings"
    # The stored settings were not touched.
    assert api.get_settings() == before


def test_settings_reject_unknown_keys(api: Api) -> None:
    """Only documented settings may be written (section 4.6)."""
    assert api.update_settings({"error": "injected"}).get("error") == "invalid_settings"
    assert api.update_settings({"look": {"theme": "arctic"}, "junk": 1}).get("error") == "invalid_settings"
    # A valid change still goes through.
    assert api.update_settings({"look": {"theme": "arctic"}})["look"]["theme"] == "arctic"


def test_key_paths_must_stay_outside_the_notes_folder(api: Api) -> None:
    inside = api.config.notes_root / "keys" / "encrypted.vnkey"
    result = api.create_vault("encrypted", inside)
    assert result.get("error") == "key_inside_notes"

    outside = api.config.settings_file.parent / "keys" / "encrypted.vnkey"
    assert api.create_vault("encrypted", outside).get("ok") is True
    assert api.create_vault("encrypted", outside).get("error") == "already_exists"


def test_settings_cannot_move_the_notes_folder(api: Api, tmp_path: Path, monkeypatch) -> None:
    """The notes folder is chosen with a dialog, never by a settings write.

    A relative or junk value would otherwise be resolved against the current
    working directory and created there (security rule 12f).
    """
    root_before = api.config.notes_root
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)

    for hostile in HOSTILE_VALUES + [".", "notes", "~/somewhere", str(root_before)]:
        result = api.update_settings({"notes_root": hostile})
        assert result.get("error") == "invalid_settings", hostile
        assert api.config.notes_root == root_before

    # Nothing at all appeared in the working directory while that was tried.
    assert list(cwd.iterdir()) == []
    assert api.update_settings({"notes_root": str(tmp_path / "elsewhere")}).get("error") == "invalid_settings"


def test_headless_folder_choice_rejects_junk_paths(api: Api, tmp_path: Path, monkeypatch) -> None:
    """Without a window a caller may name a folder - but only a real one."""
    root_before = api.config.notes_root
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)

    for hostile in HOSTILE_VALUES:
        result = api.choose_notes_folder(hostile)
        assert_bridge_shape(result)
        if hostile is None:
            assert result.get("error") == "cancelled"
            continue
        absolute = isinstance(hostile, (str, Path)) and Path(str(hostile)).expanduser().is_absolute()
        if not absolute:
            # Not a path at all: refused before anything touches the disk.
            assert result.get("error") == "invalid_folder", hostile
        else:
            # An absolute path is a real request; the OS may still say no
            # (permission denied), which comes back as a clean io_error.
            assert result.get("error") in {"io_error", "key_inside_notes", None}, hostile

    # No stray folders, and the notes root did not move.
    assert list(cwd.iterdir()) == []
    assert api.config.notes_root == root_before

    # A real absolute folder still works for headless callers.
    target = tmp_path / "notes-elsewhere"
    assert api.choose_notes_folder(target).get("ok") is True
    assert api.config.notes_root == target.resolve()


def test_native_pickers_report_a_clean_error_without_a_window(api: Api) -> None:
    # No pywebview window (tests, or a headless start): the user is told what
    # happened instead of getting a crash or a silent no-op.
    assert api.choose_notes_folder().get("error") == "cancelled"
    assert api.import_notes("plain").get("error") == "cancelled"
    api.create_note("plain", "Exportable")
    assert api.export_note("plain", "Exportable").get("error") == "cancelled"


# The Bridge API surface (section 4.8) plus the endpoints this app adds.
BRIDGE_SURFACE = [
    "get_state",
    "list_notes",
    "open_note",
    "create_note",
    "save_note",
    "rename_note",
    "count_links_to",
    "delete_note",
    "restore_note",
    "list_trash",
    "move_note",
    "render_preview",
    "list_titles",
    "unlock_vault",
    "lock_vault",
    "lock_all",
    "touch",
    "open_external",
    "get_settings",
    "update_settings",
    "backup_now",
    "connect_drive",
    "disconnect_drive",
    "restore_from_drive",
    "purge_note",
    "empty_trash",
    "import_notes",
    "export_note",
    "note_links",
    "choose_key_file",
]


def test_no_bridge_endpoint_raises(api_with_vaults: Api) -> None:
    """The safety net: no endpoint may let an exception reach pywebview.

    ``choose_notes_folder``, ``create_vault`` and ``initialize_vaults`` take a
    path for headless callers, so they are covered by their own tests instead of
    being swept with hostile values here.
    """
    api = api_with_vaults
    checked = 0

    for name in BRIDGE_SURFACE:
        method = getattr(api, name, None)
        assert callable(method), f"{name} is missing from the Bridge API"
        arity = len(inspect.signature(method).parameters)
        for hostile in (None, 0, "", [], {}, "../../x", "a" * 5000):
            assert_bridge_shape(method(*((hostile,) * arity)), allow_text=True)
            checked += 1

    assert checked == len(BRIDGE_SURFACE) * 7


def test_bridge_surface_matches_the_documented_api() -> None:
    """Every documented endpoint exists; nothing public is left untested."""
    public = {
        name
        for name in vars(Api)
        if not name.startswith("_") and callable(getattr(Api, name))
    }
    documented = set(BRIDGE_SURFACE) | {
        # path-taking setup endpoints, covered by their own tests
        "choose_notes_folder",
        "create_vault",
        "initialize_vaults",
        # lifecycle helpers used by run.py / app.py rather than by JavaScript
        "set_window",
        "close",
    }
    assert public - documented == set(), f"untested public methods: {sorted(public - documented)}"
