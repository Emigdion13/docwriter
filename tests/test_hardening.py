"""Tests for Milestone M7: hardening.

The bar set by the build plan: *"you can't make the app lose a note by killing
it while typing, pulling out the USB key, or giving it a broken file."*

Everything here is about the unhappy paths - damaged files, unwritable disks,
vanished key files, huge notes and 500-note vaults - plus the promise that the
Bridge API answers with an error object instead of crashing.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

import vaultnotes.storage.vault_store as vault_store_module
from vaultnotes.api import Api
from vaultnotes.backup.gdrive_auth import TokenStore
from vaultnotes.config import Config
from vaultnotes.crypto.keyfile import generate_key_file, load_key_file
from vaultnotes.storage.vault_store import DamagedVaultError, VaultStore

# A note body just over 1 MB, the largest single note M7 asks us to survive.
ONE_MEGABYTE = 1_100_000


def big_body(chunk: str, minimum: int = ONE_MEGABYTE) -> str:
    """Repeat ``chunk`` until the body is comfortably over ``minimum`` bytes."""
    repeats = (minimum // max(1, len(chunk))) + 2
    return chunk * repeats

needs_posix_permissions = pytest.mark.skipif(
    os.name != "posix" or hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="needs POSIX file permissions and a non-root user",
)


def make_api(tmp_path: Path, with_vaults: bool = True) -> Api:
    """An Api over an empty notes folder, with both vaults ready to unlock."""
    cfg = Config(settings_path=tmp_path / "settings.json")
    cfg.data["notes_root"] = str(tmp_path / "notes")
    cfg.save()
    cfg.ensure_folders()
    for sample in cfg.plain_dir.glob("*.md"):
        sample.unlink()  # Config seeds sample notes on first run
    api = Api(config=cfg, app_dir=tmp_path / "appdata", drive_store=TokenStore(memory=True))
    if with_vaults:
        created = api.initialize_vaults(
            key_paths={
                "encrypted": tmp_path / "keys" / "encrypted.vnkey",
                "personal": tmp_path / "keys" / "personal.vnkey",
            }
        )
        assert created.get("ok") is True
    return api


@pytest.fixture
def api(tmp_path: Path) -> Api:
    return make_api(tmp_path, with_vaults=False)


@pytest.fixture
def unlocked(tmp_path: Path) -> Api:
    api = make_api(tmp_path)
    assert api.unlock_vault("encrypted")["ok"] is True
    return api


def tamper(path: Path) -> bytes:
    """Flip a byte in an encrypted file and return the new contents."""
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0xFF
    path.write_bytes(bytes(data))
    return bytes(data)


# ----------------------------------------------------------------------
# Damaged and missing files are skipped, warned about, never overwritten
# ----------------------------------------------------------------------
def test_damaged_vault_note_is_skipped_and_the_rest_still_unlock(unlocked: Api) -> None:
    api = unlocked
    good = api.create_note("encrypted", "Good note")
    api.save_note("encrypted", good["id"], "still readable")
    bad = api.create_note("encrypted", "Damaged note")
    api.save_note("encrypted", bad["id"], "this file will be corrupted")

    bad_path = api.vault_stores["encrypted"].root / f"{bad['id']}.vnote"
    corrupted = tamper(bad_path)

    api.lock_vault("encrypted")
    result = api.unlock_vault("encrypted")

    assert result["ok"] is True
    assert result["count"] == 1  # the good note is still there
    assert result["damaged"] == 1
    assert result["warnings"], "the user must be told a file was skipped"
    assert api.open_note("encrypted", good["id"])["body"] == "still readable"
    assert api.open_note("encrypted", bad["id"]).get("error") == "not_found"

    # Security rule 8: the damaged file is reported, never rewritten.
    assert bad_path.read_bytes() == corrupted


def test_damaged_warning_never_leaks_a_title(unlocked: Api) -> None:
    api = unlocked
    secret = api.create_note("encrypted", "Swiss account numbers")
    api.save_note("encrypted", secret["id"], "The passphrase is hunter2")
    tamper(api.vault_stores["encrypted"].root / f"{secret['id']}.vnote")

    api.lock_vault("encrypted")
    result = api.unlock_vault("encrypted")

    # A damaged file was never decrypted, so nothing about it may be guessed:
    # only the opaque file name and a count are allowed to surface.
    for message in result["warnings"]:
        assert "Swiss account numbers" not in message
        assert "hunter2" not in message
    assert f"{secret['id']}.vnote" in json.dumps(api.vault_stores["encrypted"].damaged_files)


def test_strict_unlock_still_reports_a_damaged_file_as_an_error(tmp_path: Path) -> None:
    vault_id = "1" * 32
    key_path = tmp_path / "keys" / "strict.vnkey"
    key = generate_key_file(key_path, vault_id, "Encrypted")
    store = VaultStore(tmp_path / "vault")
    store.create(key, vault_id=vault_id, name="Encrypted")
    store.unlock(key)
    note = store.create_note("Damaged", "body")
    tamper(store.root / f"{note.id}.vnote")

    store.lock()
    with pytest.raises(DamagedVaultError):
        store.unlock(load_key_file(key_path), skip_damaged=False)

    # The default is to survive it.
    assert store.unlock(load_key_file(key_path)) == 0
    assert store.damaged_files == [f"{note.id}.vnote"]


def test_a_file_with_an_invalid_name_is_skipped(unlocked: Api) -> None:
    api = unlocked
    api.create_note("encrypted", "Good note")
    (api.vault_stores["encrypted"].root / "not-a-note-id.vnote").write_bytes(b"junk")

    api.lock_vault("encrypted")
    result = api.unlock_vault("encrypted")

    assert result["ok"] is True
    assert result["count"] == 1
    assert result["damaged"] == 1


def test_a_vanished_vault_file_does_not_break_the_unlock(unlocked: Api) -> None:
    api = unlocked
    kept = api.create_note("encrypted", "Kept")
    gone = api.create_note("encrypted", "Gone")
    (api.vault_stores["encrypted"].root / f"{gone['id']}.vnote").unlink()

    api.lock_vault("encrypted")
    result = api.unlock_vault("encrypted")

    assert result["ok"] is True and result["count"] == 1
    assert api.open_note("encrypted", kept["id"])["title"] == "Kept"


@needs_posix_permissions
def test_unreadable_plain_note_is_skipped_with_a_warning(api: Api) -> None:
    api.create_note("plain", "Readable")
    locked_down = api.config.plain_dir / "Locked.md"
    locked_down.write_text("# Locked\n\nnobody can read me", encoding="utf-8")
    os.chmod(locked_down, 0o000)
    try:
        notes = api.list_notes("plain")
        assert [note["title"] for note in notes] == ["Readable"]

        warnings = api.get_state()["warnings"]
        assert any("Locked.md" in warning for warning in warnings)
    finally:
        os.chmod(locked_down, 0o644)


def test_a_damaged_plain_file_cannot_hide_the_others(api: Api) -> None:
    api.create_note("plain", "Fine")
    (api.config.plain_dir / "Binary.md").write_bytes(b"\x00\x01\x02 not utf-8 \xff\xfe")

    titles = [note["title"] for note in api.list_notes("plain")]
    assert "Fine" in titles and "Binary" in titles
    # Invalid bytes are replaced rather than raising, so the note still opens.
    assert "error" not in api.open_note("plain", "Binary")


# ----------------------------------------------------------------------
# Disk full / no permission while saving
# ----------------------------------------------------------------------
@needs_posix_permissions
def test_plain_save_failure_keeps_the_text_and_says_so(api: Api) -> None:
    api.create_note("plain", "Important")
    api.save_note("plain", "Important", "original text")

    os.chmod(api.config.plain_dir, 0o500)  # read + execute, no write
    try:
        result = api.save_note("plain", "Important", "the edit that cannot be written")
        assert result.get("error") == "io_error"
        assert "still in the editor" in result["message"]
    finally:
        os.chmod(api.config.plain_dir, 0o700)

    # Nothing was lost and nothing half-written.
    assert api.open_note("plain", "Important")["body"] == "original text"
    assert not list(api.config.plain_dir.glob("*.tmp"))


@needs_posix_permissions
def test_vault_save_failure_keeps_the_previous_note(unlocked: Api) -> None:
    api = unlocked
    note = api.create_note("encrypted", "Important")
    api.save_note("encrypted", note["id"], "original text")

    os.chmod(api.vault_stores["encrypted"].root, 0o500)
    try:
        result = api.save_note("encrypted", note["id"], "the edit that cannot be written")
        assert result.get("error") == "io_error"
    finally:
        os.chmod(api.vault_stores["encrypted"].root, 0o700)

    # The encrypted file on disk still holds the last good version.
    assert api.open_note("encrypted", note["id"])["body"] == "original text"
    api.lock_vault("encrypted")
    assert api.unlock_vault("encrypted")["count"] == 1
    assert api.open_note("encrypted", note["id"])["body"] == "original text"


def test_a_write_that_does_not_survive_read_back_is_reported(unlocked: Api, monkeypatch) -> None:
    """M7: after saving an encrypted note, read it back before saying "Saved"."""
    api = unlocked
    note = api.create_note("encrypted", "Important")
    real_write = vault_store_module.atomic_write

    def write_corrupt_bytes(path, data, encoding="utf-8"):
        real_write(path, b"truncated ciphertext")

    monkeypatch.setattr(vault_store_module, "atomic_write", write_corrupt_bytes)
    result = api.save_note("encrypted", note["id"], "text that will not verify")
    monkeypatch.undo()

    assert result.get("error") == "save_failed"
    # The newest text is kept in memory (and stays in the editor) rather than
    # being thrown away with the bad file.
    assert api.open_note("encrypted", note["id"])["body"] == "text that will not verify"
    assert api.vault_stores["encrypted"].damaged_files

    # A later unlock refuses to serve the file it cannot decrypt.
    api.lock_vault("encrypted")
    reopened = api.unlock_vault("encrypted")
    assert reopened["count"] == 0 and reopened["damaged"] == 1


def test_verify_after_write_is_part_of_every_vault_save(unlocked: Api) -> None:
    api = unlocked
    store = api.vault_stores["encrypted"]
    assert store.verify_writes is True

    note = api.create_note("encrypted", "Verified")
    api.save_note("encrypted", note["id"], "body")
    path = store.root / f"{note['id']}.vnote"
    # The file on disk decrypts to exactly what the API returned.
    assert store.read_note(note["id"]).body == "body"
    assert path.is_file() and store.damaged_files == []


# ----------------------------------------------------------------------
# The USB key disappears
# ----------------------------------------------------------------------
def test_key_file_removed_after_unlock_keeps_working_until_lock(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    key_path = tmp_path / "keys" / "encrypted.vnkey"
    assert api.unlock_vault("encrypted")["ok"] is True

    note = api.create_note("encrypted", "Bank stuff")
    api.save_note("encrypted", note["id"], "private")

    # Pulling the stick out must not break the session that is already open.
    key_path.unlink()
    assert api.open_note("encrypted", note["id"])["body"] == "private"
    assert api.save_note("encrypted", note["id"], "edited without the key")["modified"]
    assert api.create_note("encrypted", "Another")["title"] == "Another"
    assert api.open_note("encrypted", note["id"])["body"] == "edited without the key"

    # Once it locks, the key really is needed again.
    api.lock_vault("encrypted")
    assert api.unlock_vault("encrypted").get("error") == "key_not_found"
    assert api.list_notes("encrypted") == []


# ----------------------------------------------------------------------
# Scale: one very large note, and a vault with 500 notes
# ----------------------------------------------------------------------
def test_one_megabyte_note_round_trips(api: Api) -> None:
    body = "# Big note\n\n" + big_body("A long paragraph of plain prose that goes on.\n\n")
    assert len(body) > ONE_MEGABYTE

    started = time.perf_counter()
    created = api.create_note("plain", "Big note")
    assert api.save_note("plain", created["id"], body)["modified"]
    opened = api.open_note("plain", created["id"])
    assert opened["body"] == body
    assert len(api.render_preview("plain", body)) > ONE_MEGABYTE
    assert time.perf_counter() - started < 30

    # A megabyte of text must not produce a megabyte of snippet.
    assert len(opened["snippet"]) <= 120
    assert len(api.list_notes("plain")) == 1


def test_one_megabyte_note_round_trips_through_a_vault(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    assert api.unlock_vault("encrypted")["ok"] is True
    body = "# Secret\n\n" + big_body("Encrypted prose line with a [[Other]] link.\n")
    assert len(body) > ONE_MEGABYTE

    note = api.create_note("encrypted", "Secret")
    assert "error" not in api.save_note("encrypted", note["id"], body)
    assert api.open_note("encrypted", note["id"])["body"] == body

    api.lock_vault("encrypted")
    assert api.unlock_vault("encrypted")["count"] == 1
    assert api.open_note("encrypted", note["id"])["body"] == body
    # Only ciphertext is on disk.
    encrypted = list(api.vault_stores["encrypted"].root.glob("*.vnote"))
    assert len(encrypted) == 1
    assert b"Encrypted prose" not in encrypted[0].read_bytes()


def test_vault_with_500_notes_stays_usable(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    assert api.unlock_vault("encrypted")["ok"] is True

    started = time.perf_counter()
    ids: list[str] = []
    for number in range(500):
        created = api.create_note("encrypted", f"Note {number:03d}")
        api.save_note(
            "encrypted",
            created["id"],
            f"# Note {number:03d}\n\nLinks to [[Note {number - 1:03d}]] and [[Missing]].\n",
        )
        ids.append(created["id"])
    write_seconds = time.perf_counter() - started
    assert len(ids) == 500

    # Every note links to the one before it, so backlinks must line up.
    listing = api.list_notes("encrypted")
    assert len(listing) == 500
    assert all(item["link_count"] == 2 for item in listing)

    started = time.perf_counter()
    for note_id in ids[:50]:
        opened = api.open_note("encrypted", note_id)
        assert opened["backlinks"]
    assert api.count_links_to("encrypted", ids[10]) == {"count": 1}
    browse_seconds = time.perf_counter() - started

    # Locking and unlocking 500 encrypted notes must not stall the app.
    api.lock_vault("encrypted")
    started = time.perf_counter()
    assert api.unlock_vault("encrypted")["count"] == 500
    relock_seconds = time.perf_counter() - started

    assert api.list_titles("encrypted")[0] == "Note 000"
    assert api.rename_note("encrypted", ids[10], "Renamed 010")["links_updated"] == 1

    # Generous budgets: the point is to catch an O(n^2) regression, not to
    # benchmark the machine running the tests.
    assert write_seconds < 60, write_seconds
    assert browse_seconds < 10, browse_seconds
    assert relock_seconds < 10, relock_seconds


def test_plain_space_with_500_notes_stays_usable(api: Api) -> None:
    for number in range(500):
        (api.config.plain_dir / f"Plain {number:03d}.md").write_text(
            f"# Plain {number:03d}\n\n" + ("prose " * 200) + f"\n[[Plain {number - 1:03d}]]\n",
            encoding="utf-8",
        )

    started = time.perf_counter()
    assert api.get_state()["spaces"][0]["note_count"] == 500
    assert len(api.list_notes("plain")) == 500
    assert api.open_note("plain", "Plain 250")["backlinks"] == [
        {"id": "Plain 251", "title": "Plain 251"}
    ]
    first_pass = time.perf_counter() - started

    # Editing one note must not re-read all 500 of them.
    started = time.perf_counter()
    api.save_note("plain", "Plain 250", "edited\n[[Plain 001]]")
    api.open_note("plain", "Plain 001")
    api.get_state()
    incremental = time.perf_counter() - started

    assert first_pass < 20, first_pass
    assert incremental < 5, incremental


# ----------------------------------------------------------------------
# Interrupted writes leave no debris
# ----------------------------------------------------------------------
def test_an_interrupted_write_leaves_no_temp_file(api: Api, monkeypatch) -> None:
    api.create_note("plain", "Important")
    api.save_note("plain", "Important", "original")

    def explode(*args, **kwargs):
        raise KeyboardInterrupt("power cut")

    monkeypatch.setattr(os, "replace", explode)
    with pytest.raises(KeyboardInterrupt):
        api.plain_store.save_note("Important", "never lands")

    assert (api.config.plain_dir / "Important.md").read_text(encoding="utf-8") == "original"
    assert not list(api.config.plain_dir.glob("*.tmp"))


# ----------------------------------------------------------------------
# Path-like input cannot escape the notes folder
# ----------------------------------------------------------------------
def test_path_like_note_ids_cannot_escape_the_notes_folder(api: Api, tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n\nnot yours", encoding="utf-8")

    for hostile in ("../../outside", "..\\..\\outside", "/etc/passwd", "C:\\outside", "../outside.md"):
        assert api.open_note("plain", hostile).get("error") in {"not_found", "invalid_note", "invalid_input"}
        assert api.save_note("plain", hostile, "hacked").get("error") in {
            "not_found",
            "invalid_note",
            "invalid_input",
        }
        assert api.delete_note("plain", hostile).get("error")

    assert outside.read_text(encoding="utf-8") == "# Outside\n\nnot yours"


def test_path_like_titles_are_cleaned_into_the_notes_folder(api: Api, tmp_path: Path) -> None:
    created = api.create_note("plain", "../../evil")
    assert "/" not in created["title"] and "\\" not in created["title"]
    assert ".." not in created["title"]
    assert not (tmp_path / "evil.md").exists()
    assert (api.config.plain_dir / "evil.md").is_file()

    # A blank title is refused instead of silently becoming "Untitled".
    assert api.create_note("plain", "   ").get("error") == "invalid_input"
    assert api.create_note("plain", "").get("error") == "invalid_input"
    assert api.create_note("plain", "\x00hidden")["title"] == "hidden"
    # A 120-character title is fine and lands inside the notes folder.
    long_title = api.create_note("plain", "x" * 120)["title"]
    assert len(long_title) == 120
    assert (api.config.plain_dir / f"{long_title}.md").is_file()
    # Anything longer is refused with a clear message instead of being
    # truncated or turned into a path Windows cannot open.
    too_long = api.create_note("plain", "x" * 5000)
    assert too_long.get("error") == "too_large"
    assert "limit" in too_long["message"]


def test_vault_note_ids_must_be_opaque_hex(unlocked: Api) -> None:
    api = unlocked
    note = api.create_note("encrypted", "Secret")

    for hostile in ("nope", "../x", "0" * 31, "0" * 33, "z" * 32, "", None, 123, [], {}):
        result = api.open_note("encrypted", hostile)
        assert result.get("error") in {"invalid_note", "invalid_input", "not_found"}, result
        assert api.save_note("encrypted", hostile, "x").get("error")

    assert api.open_note("encrypted", note["id"])["title"] == "Secret"
