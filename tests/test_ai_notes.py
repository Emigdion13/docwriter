"""AI-Notes: an always-open encrypted space of its own, so AI helpers never write into Plain."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from vaultnotes import notes_cli
from vaultnotes.api import Api
from vaultnotes.backup.gdrive_auth import TokenStore
from vaultnotes.backup.gdrive_backup import iter_backup_files
from vaultnotes.config import AI_GUIDE_TITLE, Config


def _api(tmp_path: Path, notes: str = "notes", ai_key: Path | None = None) -> Api:
    cfg = Config(settings_path=tmp_path / "settings.json")
    cfg.data["notes_root"] = str(tmp_path / notes)
    cfg.save()
    cfg.ensure_folders()
    return Api(
        config=cfg,
        app_dir=tmp_path / "appdata",
        drive_store=TokenStore(memory=True),
        ai_key_path=ai_key or tmp_path / "local" / "ai-notes.vnkey",
    )


@pytest.fixture
def api(tmp_path: Path) -> Api:
    return _api(tmp_path)


def write(api: Api, space: str, title: str, body: str) -> str:
    note_id = api.create_note(space, title)["id"]
    assert "modified" in api.save_note(space, note_id, body)
    return note_id


def ai_id(api: Api, title: str) -> str:
    return api.open_note_by_title("ai", title)["note_id"]


def test_the_vault_is_made_once_with_an_encrypted_guide(api: Api, tmp_path: Path) -> None:
    folder = api.config.ai_dir
    assert (folder / "vault.json").is_file() and (folder / ".trash").is_dir()
    assert list(folder.glob("*.md")) == []
    assert (tmp_path / "local" / "ai-notes.vnkey").is_file()
    # The key never sits in the notes folder.
    assert not list(api.config.notes_root.rglob("*.vnkey"))

    guide = api.open_note("ai", ai_id(api, AI_GUIDE_TITLE))["body"]
    assert "encrypted" in guide.lower() and "ai-notes.vnkey" in guide
    # Nothing readable on disk: the guide's text is not in any file.
    for path in folder.rglob("*"):
        if path.is_file():
            assert b"AI helpers" not in path.read_bytes()

    # Deleting the guide is for good: opening again does not bring it back.
    api.delete_note("ai", ai_id(api, AI_GUIDE_TITLE))
    api.config.ensure_folders()
    api._build_plain_stores()  # noqa: SLF001 - what a restart does
    assert AI_GUIDE_TITLE not in api.list_titles("ai")


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
    assert not (api.config.ai_dir / "Plan.md").exists()
    assert "the helper's" in api.open_note("ai", ai_id(api, "Plan"))["body"]
    assert "mine" in api.open_note("plain", "Plan")["body"]
    assert AI_GUIDE_TITLE not in api.list_titles("plain")
    assert AI_GUIDE_TITLE in api.list_titles("ai")


def test_links_stay_in_ai_notes_and_may_name_plain(api: Api) -> None:
    write(api, "plain", "Home lab", "# Home lab")
    findings = write(api, "ai", "Findings", "# Findings")
    session = write(api, "ai", "Session", "See [[Findings]], [[Plain:Home lab]] and [[Encrypted:Bank]].")

    html = api.render_preview("ai", api.open_note("ai", session)["body"])
    # Links name titles (resolved through open_note_by_title), as in every space.
    assert 'href="#vn-open/Findings"' in html
    assert 'href="#vn-open/plain/Home%20lab"' in html
    assert "#vn-open/encrypted" not in html and "[[Encrypted:Bank]]" in html
    assert [link["title"] for link in api.open_note("ai", findings)["backlinks"]] == ["Session"]
    # Plain cannot reach into AI-Notes: the same title is a note still to write.
    assert 'href="#vn-new/Findings"' in api.render_preview("plain", "[[Findings]]")
    assert api.open_note_by_title("plain", "Findings").get("error") == "not_found"
    assert api.open_note_by_title("ai", "findings")["note_id"] == findings


def test_renaming_a_plain_note_rewrites_its_links_in_ai_notes_and_open_vaults(api: Api, tmp_path: Path) -> None:
    keys = {space: tmp_path / "keys" / f"{space}.vnkey" for space in ("encrypted", "personal")}
    assert api.initialize_vaults(key_paths=keys)["ok"] is True
    write(api, "plain", "Target", "# Target")
    assert api.unlock_vault("personal")["ok"] is True
    in_vault = write(api, "personal", "Vault note", "see [[Plain:Target]]")
    finding = write(api, "ai", "Finding", "see [[Plain:Target]]")

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
    assert api.open_note("ai", finding)["body"] == "see [[Plain:Renamed]]"


def test_a_plain_rename_leaves_ai_notes_own_links_alone(api: Api) -> None:
    write(api, "plain", "Target", "# Target")
    target = write(api, "ai", "Target", "# Target\n\nThe helper's own note.")
    finding = write(api, "ai", "Finding", "[[Target]] is ours, [[Plain:target#Plan|theirs]] is the user's.")

    assert api.rename_note("plain", "Target", "Renamed", True)["links_updated"] == 1
    assert api.open_note("ai", finding)["body"] == "[[Target]] is ours, [[Plain:Renamed#Plan|theirs]] is the user's."
    assert [link["title"] for link in api.open_note("ai", target)["backlinks"]] == ["Finding"]


def test_renaming_an_ai_note_updates_its_links_and_refuses_a_taken_title(api: Api) -> None:
    write(api, "plain", "Target", "# Target")
    target = write(api, "ai", "Target", "# Target")
    finding = write(api, "ai", "Finding", "[[Target]] is ours, [[Plain:Target]] is the user's.")
    write(api, "ai", "Taken", "# Taken")

    assert api.count_links_to("ai", target) == {"count": 1}
    assert api.rename_note("ai", target, "Taken", True).get("error") == "collision"
    result = api.rename_note("ai", target, "Ours", True)
    assert result["links_updated"] == 1 and result["id"] == target  # the id survives a rename
    assert api.open_note("ai", finding)["body"] == "[[Ours]] is ours, [[Plain:Target]] is the user's."


def test_a_note_another_process_writes_shows_up_at_once(api: Api) -> None:
    findings = write(api, "ai", "Findings", "# Findings")
    assert "Findings" in api.list_titles("ai")

    # notes.py running beside the app, with the same notes folder and key.
    out = io.StringIO()
    args = ["--root", str(api.config.notes_root), "--ai-key", str(api.ai_key_path)]
    assert notes_cli.run([*args, "write", "ai", "Written by Claude", "--text", "Adds to [[Findings]]."], out=out) == 0

    assert "Written by Claude" in [note["title"] for note in api.list_notes("ai")]
    assert [link["title"] for link in api.open_note("ai", findings)["backlinks"]] == ["Written by Claude"]
    assert api.get_state()["spaces"][-1]["note_count"] == 3


def test_a_stray_plain_file_is_left_alone_and_reported(api: Api) -> None:
    (api.config.ai_dir / "Dropped.md").write_text("plain text", encoding="utf-8")
    assert "Dropped" not in api.list_titles("ai")
    assert any("Dropped.md" in warning for warning in api.get_state()["warnings"])


def test_notes_move_between_plain_and_ai_notes(api: Api) -> None:
    write(api, "plain", "Draft", "# Draft\n\nfor the helper")
    moved = api.move_note("plain", "Draft", "ai")
    assert moved["title"] == "Draft" and moved["broken_links"] == 0
    assert not (api.config.plain_dir / "Draft.md").exists()
    assert api.open_note("ai", moved["new_id"])["body"] == "# Draft\n\nfor the helper"
    assert list(api.config.ai_dir.glob("*.md")) == []

    back = api.move_note("ai", moved["new_id"], "plain")
    assert back["new_id"] == "Draft" and (api.config.plain_dir / "Draft.md").is_file()
    assert "Draft" not in api.list_titles("ai")
    assert api.list_trash("ai") == []  # a move is not a delete


def test_trash_rename_import_and_graph_work_as_in_plain(api: Api, tmp_path: Path) -> None:
    old = write(api, "ai", "Old name", "# Old name")
    index = write(api, "ai", "Index", "[[Old name]]")
    assert api.rename_note("ai", old, "New name", update_links=True)["links_updated"] == 1
    assert "[[New name]]" in api.open_note("ai", index)["body"]

    assert api.delete_note("ai", old)["ok"] is True
    assert [entry["title"] for entry in api.list_trash("ai")] == ["New name"]
    assert api.list_trash("plain") == []
    assert api.restore_note("ai", old)["ok"] is True

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "Imported.md").write_text("# Imported", encoding="utf-8")
    imported = api.import_notes("ai", [inbox / "Imported.md"])["imported"]
    assert [entry["title"] for entry in imported] == ["Imported"]

    graph = api.get_graph("ai")
    assert graph["locked"] is False
    assert {"from": index, "to": old, "title": "New name", "resolved": True} in graph["edges"]


def test_ai_notes_has_no_key_to_choose_and_nothing_to_lock(api: Api) -> None:
    assert api.unlock_vault("ai")["message"] == "AI-Notes is always open and cannot be unlocked"
    assert "this PC's own key" in api.choose_key_file("ai")["message"]
    assert api.lock_vault("ai").get("error") == "invalid_space"
    assert api.count_links_to("ai", ai_id(api, AI_GUIDE_TITLE)) == {"count": 0}


def test_the_backup_holds_only_ciphertext_and_never_the_key(api: Api) -> None:
    write(api, "ai", "Findings", "# Findings\n\nsecret finding")
    keys = [key for key, _ in iter_backup_files(api.config.notes_root)]
    ai_keys = [key for key in keys if key.startswith("ai-notes/")]
    assert "ai-notes/vault.json" in ai_keys
    assert len([key for key in ai_keys if key.endswith(".vnote")]) == 2  # the guide and Findings
    assert not [key for key in keys if key.endswith((".md", ".vnkey")) and key.startswith("ai-notes/")]
    for path in api.config.ai_dir.rglob("*"):
        if path.is_file():
            assert b"secret finding" not in path.read_bytes()


def test_another_notes_folder_gets_its_own_ai_notes_with_the_same_key(api: Api, tmp_path: Path) -> None:
    write(api, "ai", "Findings", "# Findings")
    other = tmp_path / "elsewhere"
    assert api.choose_notes_folder(str(other))["ok"] is True

    assert api.config.ai_dir == (other / "ai-notes").resolve()
    assert [note["title"] for note in api.list_notes("ai")] == [AI_GUIDE_TITLE]
    assert api.open_note_by_title("ai", "Findings").get("error") == "not_found"
    # One key per PC: the new folder's vault opens with the same file.
    assert len(list((tmp_path / "local").glob("*.vnkey"))) == 1


def test_a_missing_key_leaves_the_app_running_with_a_warning(tmp_path: Path) -> None:
    first = _api(tmp_path)
    write(first, "ai", "Findings", "# Findings")
    first.close()
    (tmp_path / "local" / "ai-notes.vnkey").unlink()

    again = _api(tmp_path)
    assert again.list_notes("ai") == []
    assert any("key file is missing" in warning for warning in again.get_state()["warnings"])
    assert again.create_note("ai", "New").get("error")
    # The encrypted notes were left alone, ready for the key to come back.
    assert len(list(again.config.ai_dir.glob("*.vnote"))) == 2


def test_an_unmigrated_folder_is_not_opened(tmp_path: Path) -> None:
    folder = tmp_path / "notes" / "ai-notes"
    folder.mkdir(parents=True)
    (folder / "Old note.md").write_text("# Old note", encoding="utf-8")

    api = _api(tmp_path)
    assert api.list_notes("ai") == []
    assert any("migrate_ai" in warning for warning in api.get_state()["warnings"])
    assert (folder / "Old note.md").is_file()
    assert not (folder / "vault.json").exists()
