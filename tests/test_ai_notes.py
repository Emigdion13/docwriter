"""AI-Notes: a Plain-style space of its own, so AI helpers never write into Plain."""

from __future__ import annotations

from pathlib import Path

import pytest

from vaultnotes.api import Api
from vaultnotes.backup.gdrive_auth import TokenStore
from vaultnotes.backup.gdrive_backup import iter_backup_files
from vaultnotes.config import AI_GUIDE_TITLE, Config


def _api(tmp_path: Path, notes: str = "notes") -> Api:
    cfg = Config(settings_path=tmp_path / "settings.json")
    cfg.data["notes_root"] = str(tmp_path / notes)
    cfg.save()
    cfg.ensure_folders()
    return Api(config=cfg, app_dir=tmp_path / "appdata", drive_store=TokenStore(memory=True))


@pytest.fixture
def api(tmp_path: Path) -> Api:
    return _api(tmp_path)


def write(api: Api, space: str, title: str, body: str) -> str:
    note_id = api.create_note(space, title)["id"]
    assert "modified" in api.save_note(space, note_id, body)
    return note_id


def test_the_folder_is_made_once_with_a_guide(api: Api) -> None:
    guide = api.config.ai_dir / f"{AI_GUIDE_TITLE}.md"
    assert (api.config.ai_dir / ".trash").is_dir()
    assert "PHI" in guide.read_text(encoding="utf-8")

    guide.unlink()  # deleting the guide is for good
    api.config.ensure_folders()
    assert not guide.exists()


def test_ai_notes_is_listed_last_and_always_open(api: Api) -> None:
    spaces = api.get_state()["spaces"]
    assert [space["id"] for space in spaces] == ["plain", "encrypted", "personal", "ai"]
    ai = spaces[-1]
    assert ai["name"] == "AI-Notes" and ai["kind"] == "plain" and ai["locked"] is False
    assert ai["colorVar"] == "--ai"
    assert ai["note_count"] == 1  # the guide


def test_notes_never_mix_with_plain(api: Api) -> None:
    write(api, "plain", "Plan", "# Plan\n\nmine")
    write(api, "ai", "Plan", "# Plan\n\nthe helper's")

    assert (api.config.plain_dir / "Plan.md").read_text(encoding="utf-8").endswith("mine")
    assert (api.config.ai_dir / "Plan.md").read_text(encoding="utf-8").endswith("the helper's")
    assert "the helper's" in api.open_note("ai", "Plan")["body"]
    assert "mine" in api.open_note("plain", "Plan")["body"]
    assert AI_GUIDE_TITLE not in api.list_titles("plain")
    assert AI_GUIDE_TITLE in api.list_titles("ai")


def test_links_stay_in_ai_notes_and_may_name_plain(api: Api) -> None:
    write(api, "plain", "Home lab", "# Home lab")
    write(api, "ai", "Findings", "# Findings")
    write(api, "ai", "Session", "See [[Findings]], [[Plain:Home lab]] and [[Encrypted:Bank]].")

    html = api.render_preview("ai", api.open_note("ai", "Session")["body"])
    assert 'href="#vn-open/Findings"' in html
    assert 'href="#vn-open/plain/Home%20lab"' in html
    assert "#vn-open/encrypted" not in html and "[[Encrypted:Bank]]" in html
    assert [link["title"] for link in api.open_note("ai", "Findings")["backlinks"]] == ["Session"]
    # Plain cannot reach into AI-Notes: the same title is a note still to write.
    assert 'href="#vn-new/Findings"' in api.render_preview("plain", "[[Findings]]")
    assert api.open_note_by_title("plain", "Findings").get("error") == "not_found"
    assert api.open_note_by_title("ai", "findings")["note_id"] == "Findings"


def test_renaming_a_plain_note_rewrites_its_links_in_ai_notes_and_open_vaults(api: Api, tmp_path: Path) -> None:
    keys = {space: tmp_path / "keys" / f"{space}.vnkey" for space in ("encrypted", "personal")}
    assert api.initialize_vaults(key_paths=keys)["ok"] is True
    write(api, "plain", "Target", "# Target")
    assert api.unlock_vault("personal")["ok"] is True
    in_vault = write(api, "personal", "Vault note", "see [[Plain:Target]]")
    write(api, "ai", "Finding", "see [[Plain:Target]]")

    # The rename prompt counts them, and says Encrypted was not looked in.
    assert api.count_links_to("plain", "Target") == {
        "count": 2,
        "spaces": [
            {"space_id": "personal", "name": "Personal", "count": 1},
            {"space_id": "ai", "name": "AI-Notes", "count": 1},
        ],
        "locked": [{"space_id": "encrypted", "name": "Encrypted"}],
    }
    assert api.rename_note("plain", "Target", "Renamed", True)["links_updated"] == 2
    assert api.open_note("personal", in_vault)["body"] == "see [[Plain:Renamed]]"
    assert api.open_note("ai", "Finding")["body"] == "see [[Plain:Renamed]]"
    assert 'href="#vn-open/plain/Renamed"' in api.render_preview("ai", "see [[Plain:Renamed]]")


def test_a_plain_rename_leaves_ai_notes_own_links_alone(api: Api) -> None:
    write(api, "plain", "Target", "# Target")
    write(api, "ai", "Target", "# Target\n\nThe helper's own note.")
    write(api, "ai", "Finding", "[[Target]] is ours, [[Plain:target#Plan|theirs]] is the user's.")

    assert api.rename_note("plain", "Target", "Renamed", True)["links_updated"] == 1
    assert api.open_note("ai", "Finding")["body"] == "[[Target]] is ours, [[Plain:Renamed#Plan|theirs]] is the user's."
    assert [link["title"] for link in api.open_note("ai", "Target")["backlinks"]] == ["Finding"]

    # "Rename only" leaves every link where it was.
    assert api.rename_note("plain", "Renamed", "Final", False)["links_updated"] == 0
    assert "[[Plain:Renamed#Plan|theirs]]" in api.open_note("ai", "Finding")["body"]


def test_a_note_a_helper_writes_on_disk_shows_up_at_once(api: Api) -> None:
    write(api, "ai", "Findings", "# Findings")
    (api.config.ai_dir / "Written by Claude.md").write_text("Adds to [[Findings]].", encoding="utf-8")

    assert "Written by Claude" in [note["title"] for note in api.list_notes("ai")]
    assert [link["title"] for link in api.open_note("ai", "Findings")["backlinks"]] == ["Written by Claude"]


def test_notes_move_between_plain_and_ai_notes(api: Api) -> None:
    write(api, "plain", "Draft", "# Draft\n\nfor the helper")
    moved = api.move_note("plain", "Draft", "ai")
    assert moved == {"new_id": "Draft", "title": "Draft", "broken_links": 0}
    assert not (api.config.plain_dir / "Draft.md").exists()
    assert (api.config.ai_dir / "Draft.md").read_text(encoding="utf-8") == "# Draft\n\nfor the helper"

    back = api.move_note("ai", "Draft", "plain")
    assert back["new_id"] == "Draft" and (api.config.plain_dir / "Draft.md").is_file()
    assert not (api.config.ai_dir / "Draft.md").exists()


def test_trash_rename_import_and_graph_work_as_in_plain(api: Api, tmp_path: Path) -> None:
    write(api, "ai", "Old name", "# Old name")
    write(api, "ai", "Index", "[[Old name]]")
    assert api.rename_note("ai", "Old name", "New name", update_links=True)["links_updated"] == 1
    assert "[[New name]]" in api.open_note("ai", "Index")["body"]

    assert api.delete_note("ai", "New name")["ok"] is True
    assert [entry["title"] for entry in api.list_trash("ai")] == ["New name"]
    assert api.list_trash("plain") == []
    assert api.restore_note("ai", "New name")["ok"] is True

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "Imported.md").write_text("# Imported", encoding="utf-8")
    assert api.import_notes("ai", [inbox / "Imported.md"])["imported"] == [{"id": "Imported", "title": "Imported"}]

    graph = api.get_graph("ai")
    assert graph["locked"] is False
    assert {"from": "Index", "to": "New name", "title": "New name", "resolved": True} in graph["edges"]


def test_ai_notes_has_no_key_and_nothing_to_lock(api: Api) -> None:
    assert api.unlock_vault("ai")["message"] == "AI-Notes is always open and cannot be unlocked"
    assert api.choose_key_file("ai")["message"] == "AI-Notes is not encrypted and has no key file"
    assert api.lock_vault("ai").get("error") == "invalid_space"
    assert api.count_links_to("ai", AI_GUIDE_TITLE) == {"count": 0}


def test_the_backup_includes_ai_notes(api: Api) -> None:
    write(api, "ai", "Findings", "# Findings")
    keys = [key for key, _ in iter_backup_files(api.config.notes_root)]
    assert "ai-notes/Findings.md" in keys
    assert f"ai-notes/{AI_GUIDE_TITLE}.md" in keys


def test_another_notes_folder_brings_its_own_ai_notes(api: Api, tmp_path: Path) -> None:
    write(api, "ai", "Findings", "# Findings")
    other = tmp_path / "elsewhere"
    assert api.choose_notes_folder(str(other))["ok"] is True

    assert api.config.ai_dir == (other / "ai-notes").resolve()
    assert [note["title"] for note in api.list_notes("ai")] == [AI_GUIDE_TITLE]
    assert api.open_note("ai", "Findings").get("error") == "not_found"
