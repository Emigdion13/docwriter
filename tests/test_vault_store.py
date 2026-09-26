"""Tests for encrypted vault storage (Milestone M4)."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from vaultnotes.config import Config
from vaultnotes.crypto.keyfile import generate_key_file, load_key_file
from vaultnotes.storage.vault_store import (
    DamagedVaultError,
    VaultLockedError,
    VaultStore,
    WrongKeyError,
    WrongVaultError,
)


def make_store(tmp_path: Path, name: str = "Encrypted") -> tuple[VaultStore, Path, object]:
    vault_id = uuid.uuid4()
    key_path = tmp_path / "keys" / f"{name.lower()}.vnkey"
    key = generate_key_file(key_path, vault_id, name)
    store = VaultStore(tmp_path / name.lower())
    store.create(key, vault_id=vault_id, name=name)
    return store, key_path, key


def test_vault_round_trip_and_opaque_filename(tmp_path: Path) -> None:
    store, key_path, key = make_store(tmp_path)
    assert store.unlock(load_key_file(key_path)) == 0

    note = store.create_note("Bank stuff", "# Bank stuff\n\nprivate phrase")
    assert len(note.id) == 32
    assert (store.root / f"{note.id}.vnote").is_file()
    assert b"Bank stuff" not in (store.root / f"{note.id}.vnote").read_bytes()

    store.lock()
    with pytest.raises(VaultLockedError):
        store.list_notes()
    assert store.unlock(key) == 1
    assert store.read_note(note.id).body == "# Bank stuff\n\nprivate phrase"


def test_wrong_key_and_wrong_vault_do_not_replace_open_state(tmp_path: Path) -> None:
    store, key_path, key = make_store(tmp_path)
    store.unlock(key)
    original = store.create_note("Keep me")

    wrong_key_path = tmp_path / "keys" / "wrong.vnkey"
    wrong_key = generate_key_file(wrong_key_path, uuid.uuid4(), "Other")
    with pytest.raises(WrongVaultError):
        store.unlock(wrong_key)
    assert store.read_note(original.id).title == "Keep me"

    # A key with the right vault id but different material fails its verifier.
    from vaultnotes.crypto.keyfile import VaultKey

    bad_key = VaultKey(
        key=bytes(bytearray(key.key)),
        vault_id=key.vault_id,
        vault_name=key.vault_name,
        created=key.created,
    )
    bad_key.key = bytes([bad_key.key[0] ^ 1]) + bad_key.key[1:]
    with pytest.raises(WrongKeyError):
        store.unlock(bad_key)
    assert store.read_note(original.id).title == "Keep me"


def test_delete_restore_stays_encrypted_and_title_collisions_are_safe(tmp_path: Path) -> None:
    store, key_path, key = make_store(tmp_path)
    store.unlock(key)
    first = store.create_note("Same")
    store.delete_note(first.id)
    second = store.create_note("Same")
    assert not (store.root / f"{first.id}.vnote").exists()
    trash_file = store.trash_dir / f"{first.id}.vnote"
    assert trash_file.exists()
    assert b"Same" not in trash_file.read_bytes()

    restored = store.restore_note(first.id)
    assert restored.title == "Same (2)"
    assert len(store.list_notes()) == 2
    assert second.id != restored.id


def test_api_creates_two_external_key_files_and_rejects_inner_key(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    config = Config(settings_path=settings)
    config.data["notes_root"] = str(tmp_path / "notes")
    config.save()
    config.ensure_folders()

    from vaultnotes.api import Api
    from vaultnotes.backup.gdrive_auth import TokenStore

    api = Api(config=config, app_dir=tmp_path / "appdata", drive_store=TokenStore(memory=True))
    result = api.initialize_vaults()
    assert result["ok"] is True
    assert (tmp_path / "keys" / "encrypted.vnkey").is_file()
    assert (tmp_path / "keys" / "personal.vnkey").is_file()

    fresh_config = Config(settings_path=tmp_path / "fresh-settings.json")
    fresh_config.data["notes_root"] = str(tmp_path / "fresh-notes")
    fresh_config.save()
    fresh_config.ensure_folders()
    fresh_api = Api(config=fresh_config, app_dir=tmp_path / "appdata", drive_store=TokenStore(memory=True))
    bad = fresh_api.create_vault("encrypted", fresh_config.notes_root / "inside.vnkey")
    assert bad["error"] == "key_inside_notes"
    fresh_api.close()

    assert api.unlock_vault("encrypted")["ok"] is True
    created = api.create_note("encrypted", "Secret")
    assert len(created["id"]) == 32
    api.lock_all()
    assert api.get_state()["spaces"][1]["locked"] is True
    api.close()
