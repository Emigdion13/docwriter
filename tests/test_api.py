"""Tests for the Bridge API (Milestone M2)."""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any
import pytest
from vaultnotes.api import Api
from vaultnotes.backup.gdrive_auth import TokenStore
from vaultnotes.crypto.keyfile import key_file_needs_passphrase
from vaultnotes.config import Config


@pytest.fixture
def api(tmp_path: Path) -> Api:
    settings_file = tmp_path / "settings.json"
    cfg = Config(settings_path=settings_file)
    cfg.data["notes_root"] = str(tmp_path / "notes")
    cfg.save()
    cfg.ensure_folders()
    return Api(config=cfg, app_dir=tmp_path / "appdata", drive_store=TokenStore(memory=True))


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
    unlocked = Api(config=cfg, app_dir=tmp_path / "appdata", drive_store=TokenStore(memory=True))
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
    # M10 extras: opening a note from a clicked link, and the graph view data.
    "open_note_by_title",
    "get_graph",
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
    # M10 extras: opening a note from a clicked link, and the graph view data.
    "open_note_by_title",
    "get_graph",
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
    # M10 extra: the opt-in "remove deleted files from Drive" cleanup.  Every
    # other backup action goes through a name section 4.8 already documents,
    # and Drive state is reported inside get_state() rather than by inventing
    # another status endpoint.
    "prune_drive_backup",
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

# ======================================================================
# Google Drive backup through the Bridge API (M8, section 8)
# ======================================================================
class BridgeFakeDrive:
    """The slice of Drive the Bridge API needs, kept in memory."""

    FOLDER = "application/vnd.google-apps.folder"

    def __init__(self) -> None:
        self.nodes: dict[str, dict] = {
            "root": {"id": "root", "name": "root", "mimeType": self.FOLDER, "children": []}
        }
        self._next = 0
        self.names: list[str] = []

    def _add(self, parent_id: str, name: str, mime: str, data: bytes = b"") -> str:
        self._next += 1
        node_id = f"drive-{self._next}"
        self.nodes[node_id] = {
            "id": node_id,
            "name": name,
            "mimeType": mime,
            "children": [],
            "data": data,
            "parent": parent_id,
        }
        self.nodes[parent_id]["children"].append(node_id)
        return node_id

    def list_children(self, folder_id: str) -> list[dict]:
        return [self.nodes[child] for child in self.nodes[folder_id]["children"]]

    def find_child(self, name: str, folder_id: str) -> dict | None:
        for child in self.list_children(folder_id):
            if child["name"] == name:
                return child
        return None

    def create_folder(self, name: str, parent_id: str) -> str:
        self.names.append(f"mkdir:{name}")
        return self._add(parent_id, name, self.FOLDER)

    def upload(self, name: str, parent_id: str, path: Path) -> str:
        self.names.append(f"create:{name}")
        return self._add(parent_id, name, "", Path(path).read_bytes())

    def replace(self, file_id: str, path: Path) -> None:
        self.names.append(f"update:{file_id}")
        self.nodes[file_id]["data"] = Path(path).read_bytes()

    def download(self, file_id: str, dest: Path) -> None:
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(self.nodes[file_id]["data"])

    def delete(self, file_id: str) -> None:
        self.names.append(f"delete:{file_id}")


def test_backup_endpoints_say_not_connected_before_signin(api: Api) -> None:
    """No silent no-op: the UI is told what is missing (section 4.8)."""
    state = api.get_state()
    assert state["backup"]["connected"] is False
    assert state["backup"]["enabled"] is False
    assert state["backup"]["interval_minutes"] == 60
    assert state["backup"]["folder_name"] == "VaultNotes Backup"

    refused = api.backup_now()
    assert refused["error"] == "not_connected"
    assert "Settings" in refused["message"]
    assert api.prune_drive_backup()["error"] == "not_connected"


def test_connect_drive_reports_a_missing_client_secret(api: Api) -> None:
    """The app-settings folder holds no client_secret.json, so say what to do."""
    result = api.connect_drive()
    assert result["error"] == "client_secret_missing"
    assert "client_secret.json" in result["message"]
    assert api.get_state()["backup"]["connected"] is False


def test_signed_in_api_backs_up_in_the_background(api: Api, tmp_path: Path) -> None:
    """After sign-in, "Back up now" uploads changed notes and reports by event."""
    api.drive_store.save("refresh-token-for-test")
    events: list[tuple[str, dict]] = []
    api._emit = lambda name, data: events.append((name, data))  # type: ignore[method-assign]

    drive = BridgeFakeDrive()
    api._drive_service = lambda: drive  # type: ignore[method-assign]

    created = api.create_note("plain", "Bank stuff")
    api.save_note("plain", created["id"], "# Bank stuff\n- details")
    (tmp_path / "notes" / "plain" / "stray.vnkey").write_text('{"key_b64": "no"}', encoding="utf-8")

    assert api.backup_now()["ok"] is True
    assert api.backup.wait(timeout=20) is True

    done = [data for name, data in events if name == "backup_done"]
    assert done, "the outcome must reach the status bar and a toast"
    assert done[-1]["ok"] is True
    assert "create:Bank stuff.md" in drive.names
    assert not any("stray.vnkey" in call for call in drive.names), "key files never upload"

    settings = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert settings["backup"]["last_backup"], "the status bar reads this back"
    assert settings["backup"]["drive_folder_id"], "the Drive folder id is remembered"
    assert (tmp_path / "appdata" / "backup_manifest.json").is_file()

    # A second run sends nothing at all: the manifest already knows the hash.
    drive.names.clear()
    assert api.backup_now()["ok"] is True
    assert api.backup.wait(timeout=20) is True
    assert drive.names == []


def test_backup_is_refused_while_one_is_running(api: Api) -> None:
    api.drive_store.save("refresh-token-for-test")
    gate = __import__("threading").Event()
    inside = __import__("threading").Event()
    drive = BridgeFakeDrive()
    real_upload = drive.upload

    def slow_upload(name: str, parent_id: str, path: Path) -> str:
        inside.set()
        gate.wait(timeout=10)
        return real_upload(name, parent_id, path)

    drive.upload = slow_upload  # type: ignore[method-assign]
    api._drive_service = lambda: drive  # type: ignore[method-assign]
    api.create_note("plain", "Slow note")

    assert api.backup_now()["ok"] is True
    assert inside.wait(timeout=5) is True
    busy = api.backup_now()
    assert busy["error"] == "busy"
    gate.set()
    assert api.backup.wait(timeout=20) is True


def test_backup_settings_are_validated_and_the_timer_follows(api: Api) -> None:
    saved = api.update_settings({"backup": {"enabled": True, "interval_minutes": 12}})
    assert saved["backup"]["enabled"] is True and saved["backup"]["interval_minutes"] == 12
    assert api.backup.auto.running is True, "auto-backup arms when it is enabled"
    assert api.backup.auto.minutes == 12

    api.update_settings({"backup": {"enabled": False}})
    assert api.backup.auto.running is False

    assert api.update_settings({"backup": {"interval_minutes": 1}})["error"] == "invalid_settings"
    assert api.update_settings({"backup": {"interval_minutes": "soon"}})["error"] == "invalid_settings"
    # The app owns those two, so a bridge call cannot rewrite the history.
    untouched = api.update_settings({"backup": {"drive_folder_id": "fake", "last_backup": "yesterday"}})
    assert untouched["backup"]["last_backup"] is None
    assert untouched["backup"]["drive_folder_id"] is None
    assert api.update_settings({"backup": {"tunnel_to": "evil.example"}})["error"] == "invalid_settings"
    assert api.update_settings({"backup": "yes"})["error"] == "invalid_settings"


def test_restore_needs_a_folder_and_only_an_empty_one(api: Api, tmp_path: Path) -> None:
    assert api.restore_from_drive()["error"] == "not_connected"
    api.drive_store.save("refresh-token-for-test")
    assert api.restore_from_drive()["error"] == "cancelled", "no window means no folder dialog"

    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "keep.md").write_text("mine\n", encoding="utf-8")
    refused = api.restore_from_drive(busy)
    assert refused["error"] == "target_not_empty"
    assert (busy / "keep.md").read_text(encoding="utf-8") == "mine\n"

    assert api.restore_from_drive("relative/path")["error"] == "invalid_folder"
    assert api.restore_from_drive(123)["error"] == "invalid_folder"


def test_restore_downloads_a_backup_the_ui_can_then_use(api: Api, tmp_path: Path) -> None:
    api.drive_store.save("refresh-token-for-test")
    drive = BridgeFakeDrive()
    api._drive_service = lambda: drive  # type: ignore[method-assign]
    events: list[tuple[str, dict]] = []
    api._emit = lambda name, data: events.append((name, data))  # type: ignore[method-assign]

    note = api.create_note("plain", "Trip 2026")
    api.save_note("plain", note["id"], "# Trip 2026\n- [[Home lab]]")
    assert api.backup_now()["ok"] is True
    assert api.backup.wait(timeout=20) is True

    target = tmp_path / "restored"
    assert api.restore_from_drive(target)["ok"] is True
    assert api.backup.wait(timeout=20) is True
    assert (target / "plain" / "Trip 2026.md").is_file()
    kind = [data.get("kind") for name, data in events if name == "backup_done"]
    assert kind == ["backup", "restore"], "the UI shows the right words for each job"


def test_backup_failure_is_data_not_a_crash(api: Api) -> None:
    """A refused sign-in during a run becomes a normal, JSON-safe answer."""
    from vaultnotes.backup.gdrive_auth import DriveAuthError

    api.drive_store.save("refresh-token-for-test")
    emitted: list[tuple[str, Any]] = []
    api._emit = lambda name, data: emitted.append((name, data))  # type: ignore[method-assign]

    def broken() -> BridgeFakeDrive:
        raise DriveAuthError("reconnect_required", "Google refused the saved sign-in.")

    api._drive_service = broken  # type: ignore[method-assign]
    assert api.backup_now()["ok"] is True
    assert api.backup.wait(timeout=20) is True
    name, payload = emitted[-1]
    assert name == "backup_done"
    json.dumps(payload)
    assert payload["ok"] is False and "refused" in payload["message"]
    assert api.get_state()["backup"]["running"] is False


def test_app_close_backs_up_only_when_the_user_asks(api: Api, tmp_path: Path) -> None:
    """Section 8.2: auto-backup runs "while the app is open, and when it closes"."""
    api.drive_store.save("refresh-token-for-test")
    drive = BridgeFakeDrive()
    api._drive_service = lambda: drive  # type: ignore[method-assign]
    api.create_note("plain", "Closing note")

    api.update_settings({"backup": {"enabled": False}})
    api.close()
    assert drive.names == [], "no schedule means no surprise network call at shutdown"

    api.update_settings({"backup": {"enabled": True, "interval_minutes": 5}})
    assert api.backup.finish_on_close(timeout=20) is True
    assert "create:Closing note.md" in drive.names


def test_auto_backup_timer_fires_through_the_bridge(api: Api) -> None:
    """The Python-side schedule starts a normal background backup."""
    api.drive_store.save("refresh-token-for-test")
    drive = BridgeFakeDrive()
    api._drive_service = lambda: drive  # type: ignore[method-assign]
    api.create_note("plain", "Timed note")

    api._auto_backup_due()
    assert api.backup.wait(timeout=20) is True
    assert "create:Timed note.md" in drive.names

    # Not connected: the timer stays quiet instead of queueing a doomed run.
    api.drive_store.clear()
    drive.names.clear()
    api._auto_backup_due()
    assert api.backup.wait(timeout=5) is True
    assert drive.names == []


# ======================================================================
# M10: clicking links, embeds, the graph view, passphrase-protected keys
# ======================================================================
def test_open_note_by_title_resolves_like_a_link(api: Api) -> None:
    api.create_note("plain", "Travel 2026")
    api.save_note("plain", "Travel 2026", "# Travel 2026\n\n## Hotels\n")

    found = api.open_note_by_title("plain", "travel 2026.md")
    assert found["ok"] is True
    assert found["note_id"] == "Travel 2026"
    assert found["title"] == "Travel 2026"

    with_heading = api.open_note_by_title("plain", "Travel 2026", "Hotels")
    assert with_heading["heading"] == "Hotels"


def test_open_note_by_title_reports_a_missing_note(api: Api) -> None:
    missing = api.open_note_by_title("plain", "Not written yet")

    assert missing["error"] == "not_found"
    assert "Plain" in missing["message"]


def test_open_note_by_title_refuses_a_locked_vault(api_with_vaults: Api) -> None:
    api_with_vaults.lock_vault("personal")

    locked = api_with_vaults.open_note_by_title("personal", "Any title")

    assert locked["error"] == "locked"
    # ... while the same space answers normally once it is open again.
    api_with_vaults.unlock_vault("personal")
    assert api_with_vaults.open_note_by_title("personal", "Any title")["error"] == "not_found"


def test_preview_inlines_an_embedded_plain_note(api: Api) -> None:
    api.create_note("plain", "Home lab")
    api.save_note("plain", "Home lab", "# Home lab\n\n- [x] Pi 5 online\n")

    html = api.render_preview("plain", "Today:\n\n![[Home lab]]\n")

    assert 'class="vn-embed"' in html
    assert "Home lab" in html
    assert "Pi 5 online" in html
    assert "\u2063" not in html


def test_preview_embeds_only_read_the_current_space(api: Api) -> None:
    """A Plain note's ``![[Vault note]]`` can inline nothing: no titles leak."""
    plain = api
    plain.create_note("plain", "Home lab")
    plain.save_note("plain", "Home lab", "secret router password\n")

    html = plain.render_preview("plain", "![[Home lab]] and [[Encrypted:Home lab]]")

    assert "secret router password" in html  # same-space embed is fine
    assert "[[Encrypted:Home lab]]" in html  # a vault link stays plain text
    assert "Encrypted" not in html.replace("[[Encrypted:Home lab]]", "")


def test_plain_note_never_receives_a_vault_title(api_with_vaults: Api) -> None:
    api_with_vaults.unlock_vault("encrypted")
    created = api_with_vaults.create_note("encrypted", "Bank codes")
    api_with_vaults.save_note("encrypted", created["id"], "see [[Plain:Home lab]]\n")
    api_with_vaults.create_note("plain", "Home lab")
    api_with_vaults.save_note("plain", "Home lab", "# Home lab\n")

    vault_html = api_with_vaults.render_preview("encrypted", "Link out: [[Plain:Home lab]]")
    plain_html = api_with_vaults.render_preview("plain", "Link in: [[Bank codes]]")

    assert "#vn-open/plain/Home%20lab" in vault_html
    assert "#vn-new/Bank%20codes" in plain_html  # invisible from Plain, so "missing"
    assert "Home lab" not in plain_html


def test_get_graph_returns_nodes_and_edges_for_one_space(api: Api) -> None:
    api.create_note("plain", "Graph alpha")
    api.save_note("plain", "Graph alpha", "planning\n\n[[Graph beta]] and [[Wishlist]]\n")
    api.create_note("plain", "Graph beta")
    api.save_note("plain", "Graph beta", "gear\n\n[[Graph alpha]]\n")

    graph = api.get_graph("plain")

    assert graph["space_id"] == "plain"
    assert graph["locked"] is False
    nodes = {node["title"] for node in graph["nodes"]}
    assert {"Graph alpha", "Graph beta"} <= nodes
    assert all({"id", "title", "links"} <= set(node) for node in graph["nodes"])
    edges = {(edge["from"], edge["to"]) for edge in graph["edges"]}
    assert ("Graph alpha", "Graph beta") in edges
    assert ("Graph beta", "Graph alpha") in edges
    # A link to a note that does not exist is a ghost, never an id.
    ghost = [edge for edge in graph["edges"] if edge["title"] == "Wishlist"]
    assert ghost and ghost[0]["to"] is None and ghost[0]["resolved"] is False


def test_get_graph_of_a_locked_vault_shows_nothing(api_with_vaults: Api) -> None:
    api_with_vaults.unlock_vault("personal")
    created = api_with_vaults.create_note("personal", "Diary")
    api_with_vaults.save_note("personal", created["id"], "[[Diary two]]\n")
    api_with_vaults.lock_vault("personal")

    graph = api_with_vaults.get_graph("personal")

    assert graph["locked"] is True
    assert graph["nodes"] == [] and graph["edges"] == []


def test_key_file_can_be_protected_with_a_passphrase(tmp_path: Path) -> None:
    """The M10 unlock flow, end to end, on the Encrypted vault."""
    settings_file = tmp_path / "settings.json"
    cfg = Config(settings_path=settings_file)
    cfg.data["notes_root"] = str(tmp_path / "notes")
    cfg.save()
    cfg.ensure_folders()
    app = Api(config=cfg, app_dir=tmp_path / "appdata", drive_store=TokenStore(memory=True))
    key_path = tmp_path / "keys" / "encrypted.vnkey"
    passphrase = "only-i-know-this"

    created = app.create_vault("encrypted", key_path, passphrase=passphrase)
    assert created["ok"] is True and created["protected"] is True
    assert key_file_needs_passphrase(key_path) is True
    assert passphrase not in key_path.read_text(encoding="utf-8")
    # The passphrase is the only way in, so a bare unlock must fail.
    assert app.unlock_vault("encrypted")["error"] == "passphrase_required"
    assert app.unlock_vault("encrypted", passphrase=passphrase)["ok"] is True

    note = app.create_note("encrypted", "Router")
    app.save_note("encrypted", note["id"], "admin / 1234\n")
    assert app.lock_vault("encrypted")["ok"] is True

    # get_state tells the dialog to show the passphrase box.
    state = app.get_state()
    encrypted = next(item for item in state["spaces"] if item["id"] == "encrypted")
    assert encrypted["key_wrapped"] is True
    assert encrypted["locked"] is True

    no_pass = app.unlock_vault("encrypted")
    assert no_pass["error"] == "passphrase_required"
    # Nothing was decrypted by the failed attempt, and the message is generic.
    assert "passphrase" in no_pass["message"].lower()

    wrong = app.unlock_vault("encrypted", passphrase="guess")
    assert wrong["error"] == "wrong_passphrase"

    opened = app.unlock_vault("encrypted", passphrase=passphrase)
    assert opened["ok"] is True and opened["count"] == 1
    assert app.open_note("encrypted", note["id"])["title"] == "Router"
