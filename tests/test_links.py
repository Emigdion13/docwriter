"""Tests for Obsidian-style links (Milestone M6).

Covers the pure link logic (``links.py``) and the way the Bridge API wires it
into the three spaces: suggestions, backlinks, rename-rewriting, the
move warning and the rule that links never cross a space boundary.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vaultnotes.api import Api
from vaultnotes.config import Config
from vaultnotes.links import (
    Link,
    LinkIndex,
    count_links,
    find_links_in_body,
    parse_links,
    rename_links,
    render_links_for_preview,
    resolve,
)
from vaultnotes.models import Note


def note(note_id: str, title: str, body: str = "") -> Note:
    """Small helper so index tests read like the notes they describe."""
    return Note(id=note_id, title=title, body=body, modified="now", created="now")


# ----------------------------------------------------------------------
# parse_links: every form from section 4.7
# ----------------------------------------------------------------------
def test_parse_wikilink_forms() -> None:
    body = (
        "Plain [[Travel 2026]], alias [[Travel 2026|my trip]], "
        "heading [[Travel 2026#Hotels]], suffix [[Travel 2026.md]]."
    )
    links = parse_links(body)

    assert [link.target for link in links] == ["Travel 2026"] * 4
    assert [link.display for link in links] == [
        "Travel 2026",
        "my trip",
        "Travel 2026#Hotels",
        "Travel 2026",
    ]
    assert links[2].heading == "Hotels"
    assert all(link.kind == "wikilink" for link in links)
    assert links[1].explicit_display is True
    assert links[0].explicit_display is False


def test_parse_escaped_pipe_inside_table() -> None:
    body = "| Trip | Notes |\n|---|---|\n| [[Travel 2026\\|my trip]] | [[Home lab]] |\n"
    links = parse_links(body)

    assert [(link.target, link.display) for link in links] == [
        ("Travel 2026", "my trip"),
        ("Home lab", "Home lab"),
    ]
    # The escape is remembered so a rename can write it back the same way.
    assert links[0].escaped_pipe is True
    assert links[1].escaped_pipe is False


def test_parse_markdown_md_link_form() -> None:
    body = (
        "See [the trip](Travel%202026.md), [lab](<Home lab.md>) "
        "and [web](https://example.com/a.md)."
    )
    links = parse_links(body)

    assert [link.target for link in links] == ["Travel 2026", "Home lab"]
    assert [link.display for link in links] == ["the trip", "lab"]
    assert all(link.kind == "markdown" for link in links)


def test_two_markdown_links_on_one_line_both_parse() -> None:
    body = "Read [one](Alpha.md) then [two](Beta.md)."
    assert [link.target for link in parse_links(body)] == ["Alpha", "Beta"]


def test_parse_ignores_images_and_external_links() -> None:
    body = "![pic](Photo.md) [mail](mailto:a@b.md) [anchor](#Heading) [file](file:///x.md)"
    assert parse_links(body) == []


def test_parse_reports_exact_positions() -> None:
    body = "See [[Travel 2026]] now."
    (link,) = parse_links(body)

    assert body[link.start : link.end] == "[[Travel 2026]]"
    assert link.raw == "[[Travel 2026]]"
    assert (link.start, link.end) == (4, 19)


def test_parse_skips_same_note_heading_links() -> None:
    # [[#Heading]] points inside the current note, so it is not a note link.
    assert parse_links("Jump to [[#Hotels]] and [[Travel 2026]].")[0].target == "Travel 2026"
    assert len(parse_links("Jump to [[#Hotels]].")) == 0


# ----------------------------------------------------------------------
# Code is never linked
# ----------------------------------------------------------------------
def test_links_inside_inline_code_are_ignored() -> None:
    body = "Write `[[Travel 2026]]` to link, and ``[[Travel 2026]]`` too."
    assert parse_links(body) == []
    assert find_links_in_body(body) == []
    assert count_links(body) == 0


def test_links_inside_fenced_code_are_ignored() -> None:
    body = "Before\n\n```markdown\n[[Travel 2026]]\n```\n\nAfter [[Travel 2026]]\n"
    links = parse_links(body)

    assert len(links) == 1
    assert body[links[0].start : links[0].end] == "[[Travel 2026]]"
    assert links[0].start > body.index("```")


def test_unclosed_fence_and_tilde_fence_are_code() -> None:
    assert parse_links("~~~\n[[A]]\n~~~") == []
    # An unclosed fence swallows the rest of the document, as CommonMark does.
    assert parse_links("```\n[[A]]") == []


def test_unbalanced_backticks_are_literal_text() -> None:
    # A single stray backtick does not start a code span, so this is a link.
    assert [link.target for link in parse_links("a ` b [[Travel 2026]]")] == ["Travel 2026"]


def test_render_preview_leaves_code_alone() -> None:
    body = "Inline `[[Not A Link]]` and block:\n```\n[[Also Not A Link]]\n```"
    html = render_links_for_preview(body, ["Not A Link", "Also Not A Link"])

    assert html == body
    assert "#vn-open" not in html and "#vn-new" not in html


# ----------------------------------------------------------------------
# resolve()
# ----------------------------------------------------------------------
def test_resolve_ignores_case_padding_and_md_suffix() -> None:
    titles = ["Travel 2026", "Home lab"]

    assert resolve("Travel 2026", titles) == "Travel 2026"
    assert resolve("travel 2026", titles) == "Travel 2026"
    assert resolve("  TRAVEL 2026  ", titles) == "Travel 2026"
    assert resolve("Travel 2026.md", titles) == "Travel 2026"
    assert resolve("TRAVEL 2026.MD", titles) == "Travel 2026"
    assert resolve("Travel 2026#Hotels", titles) == "Travel 2026"


def test_resolve_returns_none_for_missing_notes() -> None:
    assert resolve("Not written yet", ["Travel 2026"]) is None
    assert resolve("", ["Travel 2026"]) is None
    assert resolve("Travel 202", ["Travel 2026"]) is None
    assert resolve(None, ["Travel 2026"]) is None  # type: ignore[arg-type]


def test_resolve_maps_titles_to_note_ids() -> None:
    # A vault keeps opaque ids, so callers pass a title -> id mapping.
    assert resolve("secret", {"Secret": "0f" * 16}) == "0f" * 16
    assert resolve("secret", [note("0f" * 16, "Secret")]) == "0f" * 16


# ----------------------------------------------------------------------
# rename_links()
# ----------------------------------------------------------------------
def test_rename_rewrites_every_form_and_keeps_display_and_heading() -> None:
    body = (
        "[[Travel 2026]] [[Travel 2026|my trip]] [[Travel 2026#Hotels]] "
        "[[travel 2026.MD]] [text](Travel%202026.md)"
    )
    new_body, count = rename_links(body, "Travel 2026", "Trip 2026")

    assert count == 5
    assert "[[Trip 2026]]" in new_body
    assert "[[Trip 2026|my trip]]" in new_body
    assert "[[Trip 2026#Hotels]]" in new_body
    assert "[[Trip 2026]]" in new_body  # the .md form becomes canonical
    assert "[text](Trip%202026.md)" in new_body
    assert "Travel 2026" not in new_body


def test_rename_keeps_the_escaped_pipe_form_used_in_tables() -> None:
    body = "| Trip |\n|---|\n| [[Travel 2026\\|my trip]] |\n"
    new_body, count = rename_links(body, "Travel 2026", "Trip 2026")

    assert count == 1
    assert "[[Trip 2026\\|my trip]]" in new_body
    # The table must still be a table: one unescaped pipe per column separator.
    assert new_body.count("|") == body.count("|")


def test_rename_leaves_other_notes_and_code_alone() -> None:
    body = "[[Home lab]] and `[[Travel 2026]]` and [[Travel 20260]]"
    new_body, count = rename_links(body, "Travel 2026", "Trip 2026")

    assert count == 0
    assert new_body == body


def test_rename_matches_ignoring_case_and_returns_untouched_body_when_no_link() -> None:
    body = "See [[travel 2026]]."
    new_body, count = rename_links(body, "TRAVEL 2026", "Trip 2026")
    assert count == 1 and "[[Trip 2026]]" in new_body

    assert rename_links("no links here", "Travel 2026", "Trip 2026") == ("no links here", 0)
    assert rename_links("", "Travel 2026", "Trip 2026") == ("", 0)


# ----------------------------------------------------------------------
# render_links_for_preview()
# ----------------------------------------------------------------------
def test_render_preview_marks_existing_and_missing_notes() -> None:
    body = "Go to [[Travel 2026]], [[Home lab|the lab]] or [[Not written yet]]."
    html = render_links_for_preview(body, ["Travel 2026", "Home lab"])

    assert "[Travel 2026](#vn-open/Travel%202026)" in html
    assert "[the lab](#vn-open/Home%20lab)" in html
    assert "[Not written yet](#vn-new/Not%20written%20yet)" in html


def test_render_preview_address_uses_the_canonical_title() -> None:
    # The chip shows what the author typed; the address must be the real title,
    # or the click handler would not find the note.
    html = render_links_for_preview("[[travel 2026.md]]", ["Travel 2026"])
    assert html == "[travel 2026](#vn-open/Travel%202026)"


def test_render_preview_escapes_pipes_in_the_alias() -> None:
    # An unescaped pipe would split a table cell, because the preview source is
    # built before markdown-it parses the table.
    html = render_links_for_preview("| [[Home lab|a | label]] |\n|---|", ["Home lab"])
    assert r"[a \| label](#vn-open/Home%20lab)" in html


def test_render_preview_keeps_text_without_links_byte_identical() -> None:
    body = "# Title\n\nJust text with a [web link](https://example.com).\n"
    assert render_links_for_preview(body, ["Title"]) == body
    assert render_links_for_preview("", ["Title"]) == ""


# ----------------------------------------------------------------------
# LinkIndex
# ----------------------------------------------------------------------
def test_index_builds_backlinks_and_outgoing_links() -> None:
    index = LinkIndex("plain")
    index.build(
        [
            note("trip", "Travel 2026", "See [[Home lab]] and [[Home lab]] again."),
            note("lab", "Home lab", "Parts go on the [[Travel 2026]]."),
            note("list", "Shopping list", "Nothing here."),
        ]
    )

    assert index.backlinks("lab") == [{"id": "trip", "title": "Travel 2026"}]
    assert index.backlinks("trip") == [{"id": "lab", "title": "Home lab"}]
    assert index.backlinks("list") == []
    # Two links to the same note are one outgoing entry but two link hits.
    assert index.outgoing("trip") == [{"id": "lab", "title": "Home lab", "resolved": True}]
    assert index.link_count("trip") == 2
    assert index.titles() == ["Home lab", "Shopping list", "Travel 2026"]


def test_index_reports_missing_targets() -> None:
    index = LinkIndex("plain")
    index.build([note("trip", "Travel 2026", "See [[Not written yet]].")])

    assert index.missing_targets("trip") == ["Not written yet"]
    assert index.outgoing("trip") == [
        {"id": None, "title": "Not written yet", "resolved": False}
    ]


def test_index_promotes_a_missing_link_when_the_note_appears() -> None:
    index = LinkIndex("plain")
    index.build([note("trip", "Travel 2026", "See [[Home lab]].")])
    assert index.missing_targets("trip") == ["Home lab"]

    index.update(note("lab", "Home lab", ""))

    assert index.missing_targets("trip") == []
    assert index.backlinks("lab") == [{"id": "trip", "title": "Travel 2026"}]


def test_index_update_tracks_edits() -> None:
    index = LinkIndex("plain")
    index.build([note("lab", "Home lab", ""), note("trip", "Travel 2026", "")])

    index.update(note("trip", "Travel 2026", "Now linking [[Home lab]]."))
    assert index.backlinks("lab") == [{"id": "trip", "title": "Travel 2026"}]

    index.update(note("trip", "Travel 2026", "Link removed."))
    assert index.backlinks("lab") == []


def test_index_remove_turns_backlinks_into_missing_links() -> None:
    index = LinkIndex("plain")
    index.build(
        [
            note("lab", "Home lab", ""),
            note("trip", "Travel 2026", "See [[Home lab]]."),
        ]
    )

    index.remove("lab")

    assert index.backlinks("lab") == []
    assert index.has("lab") is False
    assert index.missing_targets("trip") == ["Home lab"]
    assert "Home lab" not in index.titles()
    assert resolve("Home lab", index.titles()) is None


def test_index_rename_moves_backlinks_to_the_new_title() -> None:
    index = LinkIndex("plain")
    index.build(
        [
            note("trip", "Travel 2026", "See [[Home lab]]."),
            note("lab", "Home lab", ""),
        ]
    )

    # A Plain rename changes the id, so the old entry goes and a new one comes.
    index.remove("trip")
    index.update(note("Trip 2026", "Trip 2026", "See [[Home lab]]."))

    assert index.backlinks("lab") == [{"id": "Trip 2026", "title": "Trip 2026"}]


def test_index_ignores_self_links() -> None:
    index = LinkIndex("plain")
    index.build([note("trip", "Travel 2026", "About [[Travel 2026]] itself.")])

    assert index.backlinks("trip") == []
    assert index.outgoing("trip") == []
    assert index.link_count("trip") == 1


def test_index_clear_forgets_everything() -> None:
    index = LinkIndex("encrypted")
    index.build([note("a" * 32, "Secret", "See [[Other]]"), note("b" * 32, "Other", "")])
    assert len(index) == 2

    index.clear()

    assert len(index) == 0
    assert index.titles() == []
    assert index.backlinks("b" * 32) == []


# ----------------------------------------------------------------------
# Bridge API wiring
# ----------------------------------------------------------------------
def make_api(tmp_path: Path) -> Api:
    """An Api over an empty notes folder.

    ``Config.ensure_folders`` seeds three sample Plain notes on first run; the
    link tests want a space whose contents they control completely.
    """
    settings_file = tmp_path / "settings.json"
    cfg = Config(settings_path=settings_file)
    cfg.data["notes_root"] = str(tmp_path / "notes")
    cfg.save()
    cfg.ensure_folders()
    for sample in cfg.plain_dir.glob("*.md"):
        sample.unlink()
    return Api(config=cfg)


@pytest.fixture
def api(tmp_path: Path) -> Api:
    return make_api(tmp_path)


@pytest.fixture
def api_with_vaults(tmp_path: Path) -> Api:
    api = make_api(tmp_path)
    created = api.initialize_vaults(
        key_paths={
            "encrypted": tmp_path / "keys" / "encrypted.vnkey",
            "personal": tmp_path / "keys" / "personal.vnkey",
        }
    )
    assert created.get("ok") is True
    return api


def write(api: Api, space_id: str, title: str, body: str) -> dict:
    """Create (or overwrite) a note and return the Bridge API's note dict."""
    existing = {item["id"]: item for item in api.list_notes(space_id)}
    target = existing.get(title)
    if target is None:
        target = api.create_note(space_id, title)
        assert "error" not in target, target
    saved = api.save_note(space_id, target["id"], body)
    assert "error" not in saved, saved
    return api.open_note(space_id, target["id"])


def test_suggestions_type_ahead_and_completion(api: Api) -> None:
    write(api, "plain", "Travel 2026", "# Travel 2026\n")
    write(api, "plain", "Travel log", "# Travel log\n")
    write(api, "plain", "Home lab", "# Home lab\n")

    titles = api.list_titles("plain")
    assert [title for title in titles if title.lower().startswith("tra")] == ["Travel 2026", "Travel log"]


def test_preview_link_opens_the_note_and_offers_to_create_missing_ones(api: Api) -> None:
    write(api, "plain", "Travel 2026", "# Travel 2026\n")
    html = api.render_preview("plain", "Go to [[Travel 2026]] or [[Not written yet]].")

    assert 'href="#vn-open/Travel%202026"' in html
    assert 'href="#vn-new/Not%20written%20yet"' in html

    # The frontend turns #vn-new/ into a "Create note?" prompt and then this:
    created = api.create_note("plain", "Not written yet")
    assert created["title"] == "Not written yet"
    assert 'href="#vn-open/Not%20written%20yet"' in api.render_preview(
        "plain", "Go to [[Not written yet]]."
    )


def test_backlinks_follow_adding_editing_and_deleting_notes(api: Api) -> None:
    write(api, "plain", "Travel 2026", "# Travel 2026\n")
    assert api.open_note("plain", "Travel 2026")["backlinks"] == []

    write(api, "plain", "Shopping list", "Batteries for the [[Travel 2026]] sensors.")
    write(api, "plain", "Home lab", "Plans in [[travel 2026]].")
    backlinks = api.open_note("plain", "Travel 2026")["backlinks"]
    assert [item["title"] for item in backlinks] == ["Home lab", "Shopping list"]

    # Editing a note so the link disappears removes the backlink at once.
    write(api, "plain", "Home lab", "No link any more.")
    assert [item["title"] for item in api.open_note("plain", "Travel 2026")["backlinks"]] == [
        "Shopping list"
    ]

    # Deleting a note removes it from "Linked from" too.
    api.delete_note("plain", "Shopping list")
    assert api.open_note("plain", "Travel 2026")["backlinks"] == []

    # Restoring brings the link back.
    api.restore_note("plain", "Shopping list")
    assert [item["title"] for item in api.open_note("plain", "Travel 2026")["backlinks"]] == [
        "Shopping list"
    ]


def test_rename_updates_links_in_other_notes(api: Api) -> None:
    write(api, "plain", "Travel 2026", "# Travel 2026\n")
    write(
        api,
        "plain",
        "Shopping list",
        "See [[Travel 2026]], [[Travel 2026|my trip]] and [[Travel 2026#Hotels]].",
    )
    assert api.count_links_to("plain", "Travel 2026") == {"count": 1}

    result = api.rename_note("plain", "Travel 2026", "Trip 2026", update_links=True)
    assert result["title"] == "Trip 2026"
    assert result["links_updated"] == 3

    body = api.open_note("plain", "Shopping list")["body"]
    assert "[[Trip 2026]]" in body
    assert "[[Trip 2026|my trip]]" in body
    assert "[[Trip 2026#Hotels]]" in body
    assert "Travel 2026" not in body

    # Backlinks followed the rename.
    assert [item["title"] for item in api.open_note("plain", "Trip 2026")["backlinks"]] == [
        "Shopping list"
    ]


def test_rename_can_leave_links_alone(api: Api) -> None:
    write(api, "plain", "Travel 2026", "# Travel 2026\n")
    write(api, "plain", "Shopping list", "See [[Travel 2026]].")

    result = api.rename_note("plain", "Travel 2026", "Trip 2026", update_links=False)
    assert result["links_updated"] == 0
    assert api.open_note("plain", "Shopping list")["body"] == "See [[Travel 2026]]."
    # The old link is now a "missing note" chip rather than a dead end.
    assert 'href="#vn-new/Travel%202026"' in api.render_preview(
        "plain", api.open_note("plain", "Shopping list")["body"]
    )


def test_moving_a_note_reports_the_links_that_break(api: Api) -> None:
    write(api, "plain", "Travel 2026", "# Travel 2026\n\nSee also [[Home lab]].")
    write(api, "plain", "Shopping list", "For [[Travel 2026]].")
    write(api, "plain", "Home lab", "# Home lab\n")

    links = api.count_links_to("plain", "Travel 2026")
    assert links == {"count": 1}
    # The note itself holds one resolved link, so a move breaks two in total.
    result = api.note_links("plain", "Travel 2026")
    assert [item["title"] for item in result["outgoing"]] == ["Home lab"]


def test_vault_links_only_resolve_inside_their_own_vault(api_with_vaults: Api) -> None:
    api = api_with_vaults
    write(api, "plain", "Home lab", "# Home lab\n")
    assert api.unlock_vault("encrypted")["ok"] is True
    assert api.unlock_vault("personal")["ok"] is True

    secret = api.create_note("encrypted", "Bank stuff")
    api.save_note("encrypted", secret["id"], "Private [[Home lab]] and [[Payday]].")
    payday = api.create_note("encrypted", "Payday")
    api.save_note("encrypted", payday["id"], "Money in [[Bank stuff]].")

    # Inside Encrypted the links resolve, and the Plain title is not one of them.
    assert api.list_titles("encrypted") == ["Bank stuff", "Payday"]
    html = api.render_preview("encrypted", "Private [[Home lab]] and [[Payday]].")
    assert 'href="#vn-new/Home%20lab"' in html  # Plain is a different space
    assert 'href="#vn-open/Payday"' in html

    # A Plain note can never see or open a vault note.
    assert "Bank stuff" not in api.list_titles("plain")
    assert "Payday" not in api.list_titles("plain")
    plain_html = api.render_preview("plain", "Secret? [[Bank stuff]]")
    assert 'href="#vn-new/Bank%20stuff"' in plain_html
    assert api.open_note("plain", "Bank stuff").get("error") == "not_found"

    # Backlinks stay inside the vault.
    opened = api.open_note("encrypted", payday["id"])
    assert [item["title"] for item in opened["backlinks"]] == ["Bank stuff"]


def test_locked_vault_titles_disappear_from_suggestions_and_backlinks(api_with_vaults: Api) -> None:
    api = api_with_vaults
    assert api.unlock_vault("encrypted")["ok"] is True
    secret = api.create_note("encrypted", "Bank stuff")
    api.save_note("encrypted", secret["id"], "Secret body")
    other = api.create_note("encrypted", "Payday")
    api.save_note("encrypted", other["id"], "Link to [[Bank stuff]].")

    assert "Bank stuff" in api.list_titles("encrypted")
    assert api.open_note("encrypted", secret["id"])["backlinks"] == [
        {"id": other["id"], "title": "Payday"}
    ]

    api.lock_vault("encrypted")

    assert api.list_titles("encrypted") == []
    assert api.list_notes("encrypted") == []
    assert api.list_trash("encrypted") == []
    assert api.count_links_to("encrypted", secret["id"]) == {"count": 0}
    assert api.open_note("encrypted", secret["id"]).get("error") == "locked"
    # The index itself is empty: nothing decrypted outlives the lock.
    assert len(api.link_indexes["encrypted"]) == 0

    # Unlocking rebuilds it from scratch.
    assert api.unlock_vault("encrypted")["ok"] is True
    assert api.list_titles("encrypted") == ["Bank stuff", "Payday"]
    assert api.open_note("encrypted", secret["id"])["backlinks"] == [
        {"id": other["id"], "title": "Payday"}
    ]


def test_vault_titles_stay_unique_ignoring_case(api_with_vaults: Api) -> None:
    api = api_with_vaults
    assert api.unlock_vault("encrypted")["ok"] is True

    first = api.create_note("encrypted", "Bank stuff")
    second = api.create_note("encrypted", "bank stuff")
    third = api.create_note("encrypted", "Bank stuff")

    # Uniqueness ignores case, and the author's own spelling is kept.
    assert first["title"] == "Bank stuff"
    assert second["title"] == "bank stuff (2)"
    assert third["title"] == "Bank stuff (3)"

    # Renaming into an existing title also gets a suffix instead of colliding,
    # and the new spelling is kept exactly as typed.
    renamed = api.rename_note("encrypted", second["id"], "BANK STUFF")
    assert renamed["title"] == "BANK STUFF (2)"
    assert api.rename_note("encrypted", third["id"], "Totally new")["title"] == "Totally new"


def test_plain_note_edited_outside_the_app_is_picked_up(api: Api) -> None:
    write(api, "plain", "Travel 2026", "# Travel 2026\n")
    assert api.open_note("plain", "Travel 2026")["backlinks"] == []

    # Another program (Obsidian, say) adds a link on disk.
    (api.config.plain_dir / "From Obsidian.md").write_text(
        "Written elsewhere, linking [[Travel 2026]].", encoding="utf-8"
    )

    assert [item["title"] for item in api.open_note("plain", "Travel 2026")["backlinks"]] == [
        "From Obsidian"
    ]
    assert "From Obsidian" in api.list_titles("plain")


def test_link_counts_reach_the_note_list(api: Api) -> None:
    write(api, "plain", "Travel 2026", "See [[Home lab]] and [[Home lab]] again.")
    listing = {item["id"]: item for item in api.list_notes("plain")}

    assert listing["Travel 2026"]["link_count"] == 2
