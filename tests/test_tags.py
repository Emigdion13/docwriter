"""Tags: the ``tags: [...]`` line of a note's front matter, and nothing else."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from vaultnotes import notes_cli
from vaultnotes.api import Api
from vaultnotes.backup.gdrive_auth import TokenStore
from vaultnotes.config import Config
from vaultnotes.frontmatter import front_matter_tags, set_important, set_tags
from vaultnotes.links import LinkIndex
from vaultnotes.render import render_markdown
from vaultnotes.tags import clean_tag, count_tags, extract_tags, has_tags, split_query


# ----------------------------------------------------------------------
# Reading tags
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "body",
    [
        "---\ntags: [work, home]\n---\n# Note\n",
        "---\ntags: work, home\n---\n",
        "---\ntags: work home\n---\n",
        "---\ntags: ['#work', \"home\"]\n---\n",
        "---\ntags:\n  - work\n  - home\n---\n",
        "---\ntags:\n- work\n- home\ntitle: x\n---\n",
        "---\r\nTags: [work, home]  # sorted later\r\n---\r\n",
        "﻿---\ntags: [work, home, Work]\n---\n",
    ],
)
def test_front_matter_tags_in_every_form(body: str) -> None:
    assert extract_tags(body) == ["work", "home"]


@pytest.mark.parametrize(
    "body",
    [
        "",
        "# Shopping list\n\n#errands #home\n",  # a # in the text is never a tag
        "Text #work\n",
        "---\ntitle: x\n---\n#work\n",
        "# Title\n---\ntags: [work]\n---\n",  # not at the top
        "---\ntags: [work]\n",  # never closed
    ],
)
def test_the_text_never_holds_tags(body: str) -> None:
    assert extract_tags(body) == []


def test_only_real_tags_are_kept() -> None:
    assert extract_tags("---\ntags: [2026, q3-2026, a b, c/d, x/, ok_1, bad!]\n---\n") == [
        "q3-2026",
        "a",
        "b",
        "c/d",
        "x",
        "ok_1",
    ]
    assert clean_tag("#café") == "café"
    assert clean_tag("12") == clean_tag("a//b") == clean_tag("x" * 65) == clean_tag(None) == ""


def test_headings_render_as_headings() -> None:
    html = render_markdown("---\ntags: [shopping]\n---\n# Shopping list\n\n#errands\n")
    assert "<h1>Shopping list</h1>" in html
    assert "tags" not in html and "shopping" not in html


# ----------------------------------------------------------------------
# Writing tags
# ----------------------------------------------------------------------
def test_set_tags_round_trips_to_the_original_text() -> None:
    body = "# Note\n\nText.\n"
    tagged = set_tags(body, ["work", "home"])
    assert tagged == "---\ntags: [work, home]\n---\n# Note\n\nText.\n"
    assert set_tags(tagged, []) == body


def test_set_tags_keeps_the_other_keys_and_the_mark() -> None:
    body = "---\r\ntitle: Plan\r\ntags:\r\n  - old\r\n  - older\r\nimportant: true\r\n---\r\n# Plan\r\n"
    assert set_tags(body, ["new"]) == "---\r\ntitle: Plan\r\ntags: [new]\r\nimportant: true\r\n---\r\n# Plan\r\n"
    assert set_tags(body, []) == "---\r\ntitle: Plan\r\nimportant: true\r\n---\r\n# Plan\r\n"
    # Clearing the mark leaves the tags alone, and the other way round.
    both = set_important(set_tags("# N\n", ["a"]), True)
    assert set_important(both, False) == "---\ntags: [a]\n---\n# N\n"
    assert set_tags(both, []) == "---\nimportant: true\n---\n# N\n"


def test_a_byte_order_mark_stays_first() -> None:
    assert set_tags("﻿# N\n", ["a"]) == "﻿---\ntags: [a]\n---\n# N\n"
    assert set_tags("﻿---\ntags: [a]\n---\n# N\n", []) == "﻿# N\n"


def test_front_matter_tags_are_read_back() -> None:
    assert front_matter_tags(set_tags("", ["x", "y/z"])) == ["x", "y/z"]


# ----------------------------------------------------------------------
# Filtering and counting
# ----------------------------------------------------------------------
def test_filters_match_nested_tags_and_ignore_case() -> None:
    assert has_tags(["Project/Alpha", "work"], ["project"])
    assert has_tags(["Project/Alpha", "work"], ["WORK", "project/alpha"])
    assert not has_tags(["projects"], ["project"])
    assert not has_tags(["work"], ["work", "home"])


def test_search_words_starting_with_hash_are_tags() -> None:
    assert split_query("#work budget  #Q3-plan") == ("budget", ["work", "Q3-plan"])
    assert split_query("C# # #12") == ("C# # #12", [])


def test_counts_use_the_most_common_spelling() -> None:
    assert count_tags([["Work", "home"], ["work"], ["work", "Home"]]) == [
        {"tag": "work", "count": 3},
        {"tag": "home", "count": 2},
    ]


def test_the_index_forgets_tags_when_cleared() -> None:
    index = LinkIndex("personal")
    index.build([{"id": "a", "title": "A", "body": "---\ntags: [secret]\n---\n"}])
    assert index.tags_of("a") == ["secret"]
    assert index.tag_counts() == [{"tag": "secret", "count": 1}]
    index.clear()
    assert index.tag_counts() == [] and index.tags_of("a") == []


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
def test_set_tags_then_filter_the_list(api: Api, space: str) -> None:
    ids = {title: api.create_note(space, title)["id"] for title in ("Alpha", "Bravo", "Charlie")}

    tagged = api.set_tags(space, ids["Alpha"], ["work", "#Q3", "work"])
    assert tagged["tags"] == ["work", "Q3"]
    assert tagged["body"] == "---\ntags: [work, Q3]\n---\n# Alpha\n\n"
    api.set_tags(space, ids["Bravo"], ["work/client"])
    assert api.open_note(space, ids["Alpha"])["tags"] == ["work", "Q3"]

    rows = {row["title"]: row["tags"] for row in api.list_notes(space) if row["id"] in ids.values()}
    assert rows == {"Alpha": ["work", "Q3"], "Bravo": ["work/client"], "Charlie": []}
    assert [row["title"] for row in api.list_notes(space, "#work", sort="title")] == ["Alpha", "Bravo"]
    assert [row["title"] for row in api.list_notes(space, "#work #q3")] == ["Alpha"]
    assert api.list_notes(space, "#work charlie") == []
    assert {"tag": "work", "count": 1} in api.list_tags(space)

    cleared = api.set_tags(space, ids["Alpha"], [])
    assert cleared["body"] == "# Alpha\n\n" and cleared["tags"] == []


def test_tags_typed_by_hand_count(api: Api) -> None:
    api.create_note("plain", "Typed")
    saved = api.save_note("plain", "Typed", "---\ntags: [hand]\n---\n# Typed #not-a-tag\n")
    assert saved["tags"] == ["hand"]
    assert {"tag": "hand", "count": 1} in api.list_tags("plain")


def test_the_plain_file_holds_the_tags(api: Api) -> None:
    api.create_note("plain", "On disk")
    api.set_tags("plain", "On disk", ["kept"])
    path = Path(api.config.notes_root) / "plain" / "On disk.md"
    assert path.read_text(encoding="utf-8").startswith("---\ntags: [kept]\n---\n")


def test_tags_move_with_the_note(api: Api) -> None:
    api.create_note("plain", "Travels")
    api.set_tags("plain", "Travels", ["trip"])
    moved = api.move_note("plain", "Travels", "personal")
    assert api.open_note("personal", moved["new_id"])["tags"] == ["trip"]


def test_a_locked_vault_reveals_no_tags(api: Api) -> None:
    note = api.create_note("personal", "Secret")
    api.set_tags("personal", note["id"], ["hidden"])
    assert {"tag": "hidden", "count": 1} in api.list_tags("personal")
    api.lock_vault("personal")
    assert api.list_tags("personal") == []
    assert api.list_tags("encrypted") == []


def test_set_tags_refuses_bad_input_and_locked_vaults(api: Api) -> None:
    api.create_note("plain", "Note")
    before = api.open_note("plain", "Note")["body"]
    assert api.set_tags("plain", "Note", "work")["error"] == "invalid_input"
    assert api.set_tags("plain", "Note", ["ok", "not ok!"])["error"] == "invalid_input"
    assert api.set_tags("plain", "Note", [f"t{i}" for i in range(51)])["error"] == "too_large"
    assert api.set_tags("plain", "Missing", ["a"])["error"] == "not_found"
    assert api.set_tags("nowhere", "Note", ["a"])["error"] == "invalid_space"
    assert api.set_tags("encrypted", "0" * 32, ["a"])["error"] == "locked"
    assert api.open_note("plain", "Note")["body"] == before


# ----------------------------------------------------------------------
# notes.py
# ----------------------------------------------------------------------
def test_notes_cli_tags_and_filters(api: Api) -> None:
    root = str(api.config.notes_root)
    api.create_note("ai", "PR review")
    api.create_note("plain", "Mine")
    api.set_tags("plain", "Mine", ["home"])

    def cli(*args: str) -> tuple[int, str]:
        out = io.StringIO()
        return notes_cli.run(["--root", root, *args], out=out), out.getvalue()

    code, out = cli("tag", "ai", "pr review", "#review", "backend")
    assert code == 0 and "#review #backend" in out
    assert api.open_note("ai", "PR review")["tags"] == ["review", "backend"]
    assert cli("tag", "ai", "PR review", "review")[1].endswith("nothing changed.\n")

    code, out = cli("tag", "ai", "PR review", "backend", "--remove")
    assert code == 0 and api.open_note("ai", "PR review")["tags"] == ["review"]

    code, out = cli("list", "ai", "--tag", "review")
    assert code == 0 and [line.split("\t")[0] for line in out.splitlines()] == ["PR review"]
    code, out = cli("tags", "plain")
    assert code == 0 and "#home\t1" in out.splitlines()

    assert cli("tag", "ai", "PR review", "bad!")[0] == notes_cli.EXIT_ERROR
    assert cli("tag", "plain", "Mine", "x")[0] == notes_cli.EXIT_REFUSED
    assert cli("tag", "encrypted", "x", "y")[0] == notes_cli.EXIT_REFUSED
    assert api.open_note("plain", "Mine")["tags"] == ["home"]
