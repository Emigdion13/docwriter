"""EncryptedAIStore: AI-Notes as an always-open encrypted vault with a PlainStore-like API."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from vaultnotes.crypto.keyfile import generate_key_file
from vaultnotes.storage.ai_store import (
    AI_VAULT_NAME,
    AiNotesError,
    AiNotesNeedsMigration,
    EncryptedAIStore,
)


@pytest.fixture
def paths(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "notes" / "ai-notes", tmp_path / "local" / "ai-notes.vnkey"


def test_create_makes_header_key_and_guide_and_nothing_readable(paths: tuple[Path, Path]) -> None:
    folder, key = paths
    store = EncryptedAIStore(folder, key, create=True, guide=("About", "# About\n\nhello guide"))
    assert store.created_key and key.is_file()
    assert json.loads(key.read_text(encoding="utf-8"))["vault_name"] == AI_VAULT_NAME
    assert (folder / "vault.json").is_file()
    assert [n.title for n in store.list_notes()] == ["About"]
    assert all(b"hello guide" not in p.read_bytes() for p in folder.rglob("*") if p.is_file())


def test_without_create_an_empty_folder_is_refused(paths: tuple[Path, Path]) -> None:
    folder, key = paths
    with pytest.raises(AiNotesError):
        EncryptedAIStore(folder, key)
    assert not key.exists()


def test_plain_notes_need_the_migration(paths: tuple[Path, Path]) -> None:
    folder, key = paths
    folder.mkdir(parents=True)
    (folder / "Old.md").write_text("# Old", encoding="utf-8")
    with pytest.raises(AiNotesNeedsMigration):
        EncryptedAIStore(folder, key, create=True)
    assert not key.exists() and not (folder / "vault.json").exists()


def test_reopening_reads_from_disk_and_a_missing_key_is_explained(paths: tuple[Path, Path]) -> None:
    folder, key = paths
    first = EncryptedAIStore(folder, key, create=True)
    note = first.create_note("Findings", "# Findings\n\nsecret")
    first.close()

    again = EncryptedAIStore(folder, key)
    assert again.read_note(note.id).body == "# Findings\n\nsecret"
    again.close()

    key.rename(key.with_suffix(".bak"))
    with pytest.raises(AiNotesError, match="key file is missing"):
        EncryptedAIStore(folder, key, create=True)  # never a second key for an existing vault
    assert not key.exists()


def test_another_vaults_key_is_refused(paths: tuple[Path, Path], tmp_path: Path) -> None:
    folder, key = paths
    EncryptedAIStore(folder, key, create=True).close()
    other = tmp_path / "other.vnkey"
    generate_key_file(other, uuid.uuid4(), AI_VAULT_NAME)
    with pytest.raises(AiNotesError, match="does not belong"):
        EncryptedAIStore(folder, other)


def test_a_passphrase_key_or_a_foreign_key_is_never_reused_for_a_new_vault(tmp_path: Path) -> None:
    wrapped = tmp_path / "wrapped.vnkey"
    generate_key_file(wrapped, uuid.uuid4(), AI_VAULT_NAME, passphrase="pw")
    with pytest.raises(AiNotesError, match="passphrase"):
        EncryptedAIStore(tmp_path / "a", wrapped, create=True)

    personal = tmp_path / "personal.vnkey"
    generate_key_file(personal, uuid.uuid4(), "Personal")
    with pytest.raises(AiNotesError, match="not an AI-Notes key"):
        EncryptedAIStore(tmp_path / "b", personal, create=True)


def test_one_key_opens_ai_notes_in_two_notes_folders(tmp_path: Path) -> None:
    key = tmp_path / "ai.vnkey"
    one = EncryptedAIStore(tmp_path / "one", key, create=True)
    two = EncryptedAIStore(tmp_path / "two", key, create=True)
    assert one.created_key and not two.created_key
    assert one.vault_id == two.vault_id


def test_writes_from_another_process_show_up(paths: tuple[Path, Path]) -> None:
    folder, key = paths
    app = EncryptedAIStore(folder, key, create=True)
    first = app.create_note("First")
    fingerprint = app.fingerprint()

    helper = EncryptedAIStore(folder, key)  # notes.py beside the app
    helper.create_note("From helper", "hi")
    helper.save_note(first.id, "changed by helper")

    assert app.fingerprint() != fingerprint
    assert sorted(n.title for n in app.list_notes()) == ["First", "From helper"]
    assert app.read_note(first.id).body == "changed by helper"
    assert app.find_by_title("from HELPER").body == "hi"


def test_rename_refuses_a_taken_title_and_keeps_the_id(paths: tuple[Path, Path]) -> None:
    folder, key = paths
    store = EncryptedAIStore(folder, key, create=True)
    a = store.create_note("A")
    store.create_note("B")
    with pytest.raises(FileExistsError):
        store.rename_note(a.id, "b")
    renamed, old = store.rename_note(a.id, "C")
    assert (renamed.id, renamed.title, old) == (a.id, "C", "A")
    assert renamed.body.startswith("# C")


def test_detach_removes_without_trash_and_trash_round_trips(paths: tuple[Path, Path]) -> None:
    folder, key = paths
    store = EncryptedAIStore(folder, key, create=True)
    moved = store.create_note("Moved")
    gone = store.create_note("Gone")
    store.detach_note(moved.id)
    assert store.list_trash() == [] and not (folder / f"{moved.id}.vnote").exists()
    with pytest.raises(FileNotFoundError):
        store.detach_note(moved.id)

    store.delete_note(gone.id)
    assert [n.title for n in store.list_trash()] == ["Gone"]
    store.restore_note(gone.id)
    store.delete_note(gone.id)
    assert store.empty_trash() == 1 and store.list_trash() == []


def test_timestamps_are_shown_like_plain_notes(paths: tuple[Path, Path]) -> None:
    folder, key = paths
    store = EncryptedAIStore(folder, key, create=True)
    note = store.create_note("Now")
    assert note.modified.startswith("Today ")
    assert store.list_notes()[0].modified.startswith("Today ")


def test_import_keeps_title_text_and_time(paths: tuple[Path, Path]) -> None:
    folder, key = paths
    store = EncryptedAIStore(folder, key, create=True)
    store.import_note("Old", "---\nimportant: true\n---\nbody", 1_600_000_000)
    store.import_note("Old", "another", 1_600_000_100)  # title taken: "(2)"
    store.import_note("Old", "trashed", 1_600_000_200, trash=True)  # trash may share names
    assert sorted(n.title for n in store.list_notes()) == ["Old", "Old (2)"]
    assert [n.title for n in store.list_trash()] == ["Old"]
    assert store.list_notes(sort="modified")[0].title == "Old (2)"
    assert store.find_by_title("Old").body == "---\nimportant: true\n---\nbody"
