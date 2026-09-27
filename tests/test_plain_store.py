"""Tests for PlainStore storage provider (Milestone M2)."""

from __future__ import annotations

import pytest
from pathlib import Path
from vaultnotes.storage.plain_store import PlainStore, sanitize_title


@pytest.fixture
def store(tmp_path: Path) -> PlainStore:
    return PlainStore(tmp_path / "plain")


def test_create_and_read_note(store: PlainStore) -> None:
    note = store.create_note("Shopping list", "# Shopping list\n\n- Milk\n- Bread")
    assert note.id == "Shopping list"
    assert note.title == "Shopping list"
    assert "Milk" in note.body

    # File on disk
    file_path = store.root / "Shopping list.md"
    assert file_path.is_file()
    assert file_path.read_text(encoding="utf-8") == note.body

    # Read back
    read_back = store.read_note("Shopping list")
    assert read_back.id == "Shopping list"
    assert read_back.title == "Shopping list"
    assert read_back.body == note.body
    assert read_back.modified


def test_update_note(store: PlainStore) -> None:
    store.create_note("Idea", "Initial draft")
    updated = store.save_note("Idea", "Revised draft with more details")
    assert updated.body == "Revised draft with more details"

    read_back = store.read_note("Idea")
    assert read_back.body == "Revised draft with more details"


def test_rename_note(store: PlainStore) -> None:
    store.create_note("Draft Plan", "# Draft Plan\n\nSome ideas.")
    updated, old_title = store.rename_note("Draft Plan", "Final Plan")

    assert old_title == "Draft Plan"
    assert updated.title == "Final Plan"
    assert updated.id == "Final Plan"
    assert "# Final Plan" in updated.body

    # Old file removed, new file exists
    assert not (store.root / "Draft Plan.md").exists()
    assert (store.root / "Final Plan.md").exists()

    # Read with new name works, read with old name fails
    assert store.read_note("Final Plan").title == "Final Plan"
    with pytest.raises(FileNotFoundError):
        store.read_note("Draft Plan")


def test_delete_and_restore(store: PlainStore) -> None:
    store.create_note("To Delete", "Temporary content")
    assert (store.root / "To Delete.md").exists()

    # Delete moves to .trash/
    deleted = store.delete_note("To Delete")
    assert deleted.title == "To Delete"
    assert not (store.root / "To Delete.md").exists()
    assert (store.trash_dir / "To Delete.md").exists()

    with pytest.raises(FileNotFoundError):
        store.read_note("To Delete")

    # Listed in trash
    trash_items = store.list_trash()
    assert any(t.title == "To Delete" for t in trash_items)

    # Restore moves back to root
    restored = store.restore_note("To Delete")
    assert restored.title == "To Delete"
    assert (store.root / "To Delete.md").exists()
    assert not (store.trash_dir / "To Delete.md").exists()

    read_back = store.read_note("To Delete")
    assert read_back.body == "Temporary content"


def test_filename_collisions_on_create(store: PlainStore) -> None:
    n1 = store.create_note("Meeting", "First")
    assert n1.title == "Meeting"
    assert (store.root / "Meeting.md").exists()

    n2 = store.create_note("Meeting", "Second")
    assert n2.title == "Meeting (2)"
    assert (store.root / "Meeting (2).md").exists()

    n3 = store.create_note("Meeting", "Third")
    assert n3.title == "Meeting (3)"
    assert (store.root / "Meeting (3).md").exists()


def test_filename_collision_on_rename(store: PlainStore) -> None:
    store.create_note("Note A", "Content A")
    store.create_note("Note B", "Content B")

    with pytest.raises(FileExistsError):
        store.rename_note("Note A", "Note B")


def test_title_sanitization(store: PlainStore) -> None:
    raw_title = 'Project: "Mars" / Moon? *Star* <Gate> | Test\\'
    cleaned = sanitize_title(raw_title)
    assert not any(c in cleaned for c in r'\/:*?"<>|')

    note = store.create_note(raw_title, "Space project")
    assert (store.root / f"{note.title}.md").is_file()
    read_back = store.read_note(note.id)
    assert read_back.body == "Space project"


def test_list_notes_search_and_sort(store: PlainStore) -> None:
    store.create_note("Alpha", "Apples and oranges")
    store.create_note("Beta", "Bananas and berries")
    store.create_note("Gamma", "Grapes and apples")

    # List all
    all_notes = store.list_notes()
    assert len(all_notes) == 3

    # Search title or body
    results = store.list_notes(query="apples")
    titles = [n.title for n in results]
    assert "Alpha" in titles
    assert "Gamma" in titles
    assert "Beta" not in titles

    # Sort by title
    sorted_by_title = store.list_notes(sort="title")
    assert [n.title for n in sorted_by_title] == ["Alpha", "Beta", "Gamma"]


def test_not_found_errors(store: PlainStore) -> None:
    with pytest.raises(FileNotFoundError):
        store.read_note("Nonexistent")
    with pytest.raises(FileNotFoundError):
        store.save_note("Nonexistent", "text")
    with pytest.raises(FileNotFoundError):
        store.rename_note("Nonexistent", "New")
    with pytest.raises(FileNotFoundError):
        store.delete_note("Nonexistent")
    with pytest.raises(FileNotFoundError):
        store.restore_note("Nonexistent")


# ----------------------------------------------------------------------
# Titles that once pointed at the wrong note or broke links
# ----------------------------------------------------------------------
def test_a_title_ending_in_md_never_saves_into_another_note(store: PlainStore) -> None:
    readme = store.create_note("README", "# README\n\nkeep me\n")
    scratch = store.create_note("Scratch", "scratch\n")

    # "README.md" is the title "README", which is taken.
    with pytest.raises(FileExistsError):
        store.rename_note(scratch.id, "README.md")
    copy = store.create_note("README.md", "a second one\n")
    assert copy.id == "README (2)"
    store.save_note(copy.id, "overwritten?\n")

    assert store.read_note(readme.id).body == "# README\n\nkeep me\n"
    assert sorted(p.name for p in store.root.glob("*.md")) == ["README (2).md", "README.md", "Scratch.md"]
    # An id is exactly a file stem: "README.md" is not another name for "README".
    with pytest.raises(FileNotFoundError):
        store.save_note("README.md", "wrong note\n")


def test_link_breaking_characters_are_not_allowed_in_titles() -> None:
    for title in ("C# basics", "Q&A [draft]", "x^y", "a|b"):
        cleaned = sanitize_title(title)
        assert not set(cleaned) & set("#[]^|"), (title, cleaned)
    assert sanitize_title("todo.MD.md") == "todo"


def test_a_case_only_rename_renames_the_file(store: PlainStore) -> None:
    note = store.create_note("meeting notes", "notes\n")
    renamed, _ = store.rename_note(note.id, "Meeting Notes")
    assert renamed.id == "Meeting Notes"
    assert [p.name for p in store.root.glob("*.md")] == ["Meeting Notes.md"]


def test_undo_restores_a_deleted_note_under_its_own_title(store: PlainStore) -> None:
    old = store.create_note("Plan", "old plan\n")
    store.delete_note(old.id)
    new = store.create_note("Plan", "new plan\n")
    trashed = store.delete_note(new.id)

    assert trashed.id == "Plan", "the newest deletion keeps its name"
    restored = store.restore_note(trashed.id)
    assert restored.id == "Plan"
    assert store.read_note("Plan").body == "new plan\n"
