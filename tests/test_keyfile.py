"""Tests for keyfile generation and loading (Milestone M3)."""

from __future__ import annotations

import base64
import json
from pathlib import Path
import pytest

from vaultnotes.crypto.keyfile import (
    KeyFileError,
    PassphraseRequired,
    VaultKey,
    WrongPassphrase,
    generate_key_file,
    key_file_needs_passphrase,
    load_key_file,
    save_key_file,
)


def test_generate_and_load_key_file(tmp_path: Path) -> None:
    key_path = tmp_path / "personal.vnkey"
    vault_id = "0b6f2c1e-9a4d-4c55-8f0e-2d7c1b9a3e44"
    vault_name = "Personal"

    gen_key = generate_key_file(key_path, vault_id, vault_name)
    assert key_path.is_file()
    assert isinstance(gen_key, VaultKey)
    assert len(gen_key.key) == 32
    assert gen_key.vault_id == vault_id
    assert gen_key.vault_name == vault_name
    assert gen_key.protection == "none"
    assert gen_key.created

    # Read raw JSON to check format specification
    raw = json.loads(key_path.read_text(encoding="utf-8"))
    assert raw["format"] == "vaultnotes-key"
    assert raw["version"] == 1
    assert raw["vault_id"] == vault_id
    assert raw["vault_name"] == vault_name
    assert raw["protection"] == "none"
    assert "key_b64" in raw

    # Load back
    loaded = load_key_file(key_path)
    assert loaded.key == gen_key.key
    assert loaded.vault_id == gen_key.vault_id
    assert loaded.vault_name == gen_key.vault_name
    assert loaded.created == gen_key.created


def test_load_nonexistent_key_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_key_file(tmp_path / "missing.vnkey")


def test_load_corrupted_json(tmp_path: Path) -> None:
    bad_file = tmp_path / "bad.vnkey"
    bad_file.write_text("not json content", encoding="utf-8")
    with pytest.raises(ValueError):
        load_key_file(bad_file)


def test_load_invalid_format_or_version(tmp_path: Path) -> None:
    key_file = tmp_path / "test.vnkey"
    data = {
        "format": "wrong-format",
        "version": 1,
        "vault_id": "vid",
        "vault_name": "Name",
        "key_b64": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    }
    key_file.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        load_key_file(key_file)

    # Wrong version
    data["format"] = "vaultnotes-key"
    data["version"] = 2
    key_file.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        load_key_file(key_file)


def test_load_invalid_key_length(tmp_path: Path) -> None:
    key_file = tmp_path / "short_key.vnkey"
    data = {
        "format": "vaultnotes-key",
        "version": 1,
        "vault_id": "vid",
        "vault_name": "Name",
        "key_b64": "AAAA",  # only 3 bytes
    }
    key_file.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        load_key_file(key_file)


def test_key_wipe() -> None:
    key = VaultKey(
        key=b"\x42" * 32,
        vault_id="vid",
        vault_name="Name",
        created="now",
    )
    key.wipe()
    assert key.key == b"\x00" * 32


# ======================================================================
# M10: passphrase-protected key files (section 4.5)
#
# scrypt at n=2**17 costs real time and memory, so the wrapped file is
# generated once for this module and the tests read copies of it.
# ======================================================================
VAULT_ID = "7c1f9a52-4d0e-4f60-9d1b-0a5e8c3f2b11"
PASSPHRASE = "correct horse battery staple"


@pytest.fixture(scope="module")
def wrapped(tmp_path_factory) -> dict:
    """One scrypt-protected key file plus what it holds."""
    folder = tmp_path_factory.mktemp("wrapped-key")
    path = folder / "personal.vnkey"
    key = generate_key_file(path, VAULT_ID, "Personal", passphrase=PASSPHRASE)
    return {
        "path": path,
        "bytes": key.key,
        "data": json.loads(path.read_text(encoding="utf-8")),
    }


def test_wrapped_key_file_matches_section_45(wrapped: dict) -> None:
    data = wrapped["data"]

    assert data["format"] == "vaultnotes-key"
    assert data["version"] == 1
    assert data["vault_id"] == VAULT_ID
    assert data["vault_name"] == "Personal"
    assert data["protection"] == "scrypt"
    assert data["kdf"]["n"] == 131072 and data["kdf"]["r"] == 8 and data["kdf"]["p"] == 1
    assert len(base64.b64decode(data["kdf"]["salt_b64"])) == 16
    assert len(base64.b64decode(data["nonce_b64"])) == 12
    assert data["wrapped_key_b64"]
    # The plain key must not be in the file at all.
    assert "key_b64" not in data
    assert base64.b64encode(bytes(wrapped["bytes"])).decode("ascii") not in json.dumps(data)


def test_wrapped_key_loads_with_the_passphrase(wrapped: dict) -> None:
    loaded = load_key_file(wrapped["path"], passphrase=PASSPHRASE)

    assert loaded.key == wrapped["bytes"]
    assert loaded.vault_id == VAULT_ID
    assert loaded.protection == "scrypt"
    assert loaded.kdf["n"] == 131072


def test_wrapped_key_file_asks_for_the_passphrase(wrapped: dict) -> None:
    assert key_file_needs_passphrase(wrapped["path"]) is True

    with pytest.raises(PassphraseRequired):
        load_key_file(wrapped["path"])
    with pytest.raises(KeyFileError):
        load_key_file(wrapped["path"], passphrase="")


def test_wrong_passphrase_is_refused(wrapped: dict) -> None:
    with pytest.raises(WrongPassphrase):
        load_key_file(wrapped["path"], passphrase="wrong passphrase")


def test_a_plain_key_file_never_asks(tmp_path: Path) -> None:
    path = tmp_path / "open.vnkey"
    generate_key_file(path, VAULT_ID, "Encrypted")

    assert key_file_needs_passphrase(path) is False
    assert load_key_file(path).protection == "none"
    # Offering a passphrase for an unwrapped file is a mistake worth reporting
    # rather than silently ignoring.
    with pytest.raises(KeyFileError):
        load_key_file(path, passphrase="unused")
    # A key file that is simply not there is still reported as missing, and a
    # missing file is never treated as "wrapped" (so no passphrase box shows).
    with pytest.raises(FileNotFoundError):
        load_key_file(tmp_path / "missing.vnkey")
    assert key_file_needs_passphrase(tmp_path / "missing.vnkey") is False


def test_wrapped_key_is_bound_to_its_vault(tmp_path: Path, wrapped: dict) -> None:
    """The vault id is authenticated data: re-labelling the file breaks it."""
    data = dict(wrapped["data"])
    data["vault_id"] = "00000000-0000-4000-8000-000000000000"
    forged = tmp_path / "forged.vnkey"
    forged.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(WrongPassphrase):
        load_key_file(forged, passphrase=PASSPHRASE)


def test_damaged_wrap_fields_are_rejected(tmp_path: Path, wrapped: dict) -> None:
    for field in ("kdf", "nonce_b64", "wrapped_key_b64"):
        data = dict(wrapped["data"])
        data.pop(field)
        path = tmp_path / f"no-{field}.vnkey"
        path.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(KeyFileError):
            load_key_file(path, passphrase=PASSPHRASE)

    data = dict(wrapped["data"])
    data["nonce_b64"] = base64.b64encode(b"short").decode("ascii")
    path = tmp_path / "short-nonce.vnkey"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(KeyFileError):
        load_key_file(path, passphrase=PASSPHRASE)


def test_rewrapping_a_key_keeps_the_vault_working(tmp_path: Path) -> None:
    """Section 4.5 as a user flow: add a passphrase, then unlock again."""
    path = tmp_path / "rewrapped.vnkey"
    first = generate_key_file(path, VAULT_ID, "Personal")
    assert key_file_needs_passphrase(path) is False

    save_key_file(path, first, passphrase=PASSPHRASE)
    assert key_file_needs_passphrase(path) is True
    with pytest.raises(PassphraseRequired):
        load_key_file(path)

    again = load_key_file(path, passphrase=PASSPHRASE)
    assert again.key == first.key


def test_a_loaded_key_can_really_be_wiped(tmp_path: Path) -> None:
    """Rule 7: keys live in a mutable buffer that lock can zero."""
    path = tmp_path / "wipe.vnkey"
    generate_key_file(path, "0b6f2c1e-9a4d-4c55-8f0e-2d7c1b9a3e44", "Personal")
    loaded = load_key_file(path)
    buffer = loaded.key
    assert isinstance(buffer, bytearray)
    loaded.wipe()
    assert buffer == bytearray(32)
