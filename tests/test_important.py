"""The important mark: an ``important: true`` line in a note's front matter."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from vaultnotes import notes_cli
from vaultnotes.api import Api
from vaultnotes.backup.gdrive_auth import TokenStore
from vaultnotes.config import Config
from vaultnotes.frontmatter import is_important, set_important, strip_front_matter
from vaultnotes.render import render_markdown
from vaultnotes.storage.plain_store import make_snippet

MARKED = "---\nimportant: true\n---\n# Call the bank\n\nAbout the loan.\n"


# ----------------------------------------------------------------------
# The front matter itself
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "body",
    [
        MARKED,
        "---\nimportant: yes\n---\nText",
        "---\nImportant:   TRUE   # top of the list\n---\n",
        "---\ntags: [work]\nimportant: true\n...\nText",
        "﻿---\r\nimportant: true\r\n---\r\nText",
    ],
)
def test_marked_notes_are_important(body: str) -> None:
    assert is_important(body)


@pytest.mark.parametrize(
    "body",
    [
        "",
        "# Just a note\n",
        "---\nimportant: false\n---\n",
        "---\nimportant: true\n",  # never closed
        "---\nThis is a rule, then prose.\n---\n",  # not YAML
        "# Title\n---\nimportant: true\n---\n",  # not at the top
        "Text\n\nimportant: true\n",
    ],
)
def test_other_notes_are_not(body: str) -> None:
    assert not is_important(body)


@pytest.mark.parametrize(
    "body",
    [
        "# Call the bank\n\nAbout the loan.\n",
        "",
        "---\ntags: [work]\n---\n# Tagged\n",
        "﻿# With a byte order mark\n",
        "# Windows\r\n\r\nline endings\r\n",
        "---\nNot front matter\n---\n",
    ],
)
def test_marking_and_unmarking_gives_the_original_text_back(body: str) -> None:
    marked = set_important(body, True)
    assert is_important(marked)
    assert set_important(marked, True) == marked
    assert set_important(marked, False) == body


def test_marking_keeps_other_properties_and_line_endings() -> None:
    body = "---\r\ntags: [work]\r\nimportant: false\r\n---\r\n# Tagged\r\n"
    marked = set_important(body, True)
    assert marked == "---\r\ntags: [work]\r\nimportant: true\r\n---\r\n# Tagged\r\n"
    assert set_important(marked, False) == "---\r\ntags: [work]\r\n---\r\n# Tagged\r\n"


def test_a_new_block_goes_after_a_byte_order_mark() -> None:
    assert set_important("﻿# Note\n", True) == "﻿---\nimportant: true\n---\n# Note\n"


def test_the_mark_stays_out_of_the_preview_and_the_snippet() -> None:
    assert strip_front_matter(MARKED) == "# Call the bank\n\nAbout the loan.\n"
    html = render_markdown(MARKED)
    assert "important" not in html and "<hr" not in html
    assert "<h1" in html and "About the loan." in html
    assert make_snippet(MARKED) == "About the loan."


def test_a_leading_rule_is_still_drawn() -> None:
    assert "<hr" in render_markdown("---\n\nJust a rule, then text.\n")


# ----------------------------------------------------------------------
# Through the Bridge API
# ----------------------------------------------------------------------
@pytest.fixture
def api(tmp_path: Path) -> Api:
    cfg = Config(settings_path=tmp_path / "settings.json")
    cfg.data["notes_root"] = str(tmp_path / "notes")
    cfg.save()
    cfg.ensure_folders()
    api = Api(config=cfg, app_dir=tmp_path / "appdata", drive_store=TokenStore(memory=True))
    created = api.initialize_vaults(
        key_paths={
            "encrypted": tmp_path / "keys" / "encrypted.vnkey",
            "personal": tmp_path / "keys" / "personal.vnkey",
        }
    )
    assert created.get("ok") is True
    assert api.unlock_vault("personal").get("ok") is True
    return api


@pytest.mark.parametrize("space", ["plain", "ai", "personal"])
def test_marked_notes_come_first_in_every_sort(api: Api, space: str) -> None:
    ids = {title: api.create_note(space, title)["id"] for title in ("Alpha", "Bravo", "Charlie")}

    marked = api.set_important(space, ids["Charlie"], True)
    assert marked["important"] is True
    assert marked["body"].startswith("---\nimportant: true\n---\n# Charlie")
    assert api.open_note(space, ids["Charlie"])["important"] is True

    def ours(sort: str) -> list[dict]:
        # A fresh notes folder has sample notes of its own.
        return [row for row in api.list_notes(space, sort=sort) if row["id"] in ids.values()]

    for sort in ("title", "modified"):
        assert ours(sort)[0]["title"] == "Charlie"
    assert [row["title"] for row in ours("title")] == ["Charlie", "Alpha", "Bravo"]
    assert [row["important"] for row in ours("title")] == [True, False, False]
    assert api.list_notes(space)[0]["title"] == "Charlie"
    assert ours("title")[0]["snippet"] == ""  # the mark is not the note's text

    cleared = api.set_important(space, ids["Charlie"], False)
    assert cleared["important"] is False
    assert cleared["body"] == "# Charlie\n\n"
    assert [row["title"] for row in ours("title")] == ["Alpha", "Bravo", "Charlie"]


def test_a_mark_typed_by_hand_counts(api: Api) -> None:
    api.create_note("plain", "Typed")
    saved = api.save_note("plain", "Typed", "---\nimportant: true\n---\n# Typed\n")
    assert saved["important"] is True
    assert api.list_notes("plain")[0]["important"] is True


def test_the_plain_file_holds_the_mark(api: Api) -> None:
    api.create_note("plain", "On disk")
    api.set_important("plain", "On disk", True)
    path = Path(api.config.notes_root) / "plain" / "On disk.md"
    assert path.read_text(encoding="utf-8").startswith("---\nimportant: true\n---\n")


def test_the_mark_moves_with_the_note(api: Api) -> None:
    api.create_note("plain", "Travels")
    api.set_important("plain", "Travels", True)
    moved = api.move_note("plain", "Travels", "personal")
    assert api.open_note("personal", moved["new_id"])["important"] is True


def test_set_important_refuses_bad_input_and_locked_vaults(api: Api) -> None:
    api.create_note("plain", "Note")
    assert api.set_important("plain", "Note", "yes")["error"] == "invalid_input"
    assert api.set_important("plain", "Missing", True)["error"] == "not_found"
    assert api.set_important("nowhere", "Note", True)["error"] == "invalid_space"
    assert api.set_important("encrypted", "0" * 32, True)["error"] == "locked"


# ----------------------------------------------------------------------
# notes.py
# ----------------------------------------------------------------------
def test_notes_cli_lists_important_notes_first(api: Api) -> None:
    for title in ("Alpha", "Bravo"):
        api.create_note("plain", title)
    api.set_important("plain", "Bravo", True)

    out = io.StringIO()
    code = notes_cli.run(["--root", str(api.config.notes_root), "list", "plain"], out=out)
    rows = [line.split("\t") for line in out.getvalue().splitlines()]
    assert code == 0
    assert rows[0] == [rows[0][0], rows[0][1], "Bravo", "important"]
    assert all(len(row) == 3 for row in rows[1:])
    assert "Alpha" in [row[0] for row in rows[1:]]
