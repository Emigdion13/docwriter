"""Tests for note encryption, decryption, and vault verification (Milestone M3)."""

from __future__ import annotations

import os
import uuid
import pytest
from cryptography.exceptions import InvalidTag

from vaultnotes.crypto.notecrypt import (
    DecryptionError,
    FormatError,
    check_verifier,
    decrypt_note,
    encrypt_note,
    make_verifier,
)


@pytest.fixture
def sample_note() -> dict:
    return {
        "title": "Bank stuff",
        "created": "2026-09-25T10:00:00Z",
        "modified": "2026-09-25T10:05:00Z",
        "tags": ["finance", "personal"],
        "body": "# Bank stuff\n\nAccount: 1234-5678",
    }


def test_encrypt_decrypt_roundtrip(sample_note: dict) -> None:
    key = os.urandom(32)
    vault_id = uuid.uuid4()
    note_id = uuid.uuid4().hex

    data = encrypt_note(key, vault_id, note_id, sample_note)
    assert isinstance(data, bytes)
    assert data[:4] == b"VNT1"
    assert data[4] == 0x01

    recovered = decrypt_note(key, vault_id, note_id, data)
    assert recovered == sample_note


def test_wrong_key_fails(sample_note: dict) -> None:
    key1 = os.urandom(32)
    key2 = os.urandom(32)
    vault_id = uuid.uuid4()
    note_id = uuid.uuid4().hex

    data = encrypt_note(key1, vault_id, note_id, sample_note)
    with pytest.raises((InvalidTag, DecryptionError, ValueError)):
        decrypt_note(key2, vault_id, note_id, data)


def test_tampered_byte_fails(sample_note: dict) -> None:
    key = os.urandom(32)
    vault_id = uuid.uuid4()
    note_id = uuid.uuid4().hex

    data = bytearray(encrypt_note(key, vault_id, note_id, sample_note))
    # Flip one byte in the ciphertext payload
    data[-5] ^= 0x01

    with pytest.raises((InvalidTag, DecryptionError, ValueError)):
        decrypt_note(key, vault_id, note_id, bytes(data))


def test_different_note_id_fails(sample_note: dict) -> None:
    key = os.urandom(32)
    vault_id = uuid.uuid4()
    note_id_orig = uuid.uuid4().hex
    note_id_other = uuid.uuid4().hex

    data = encrypt_note(key, vault_id, note_id_orig, sample_note)

    # Decrypting with a different note_id (e.g. if file was renamed on disk) must fail
    with pytest.raises((InvalidTag, DecryptionError, ValueError)):
        decrypt_note(key, vault_id, note_id_other, data)


def test_different_vault_id_fails(sample_note: dict) -> None:
    key = os.urandom(32)
    vault_id1 = uuid.uuid4()
    vault_id2 = uuid.uuid4()
    note_id = uuid.uuid4().hex

    data = encrypt_note(key, vault_id1, note_id, sample_note)

    # Decrypting with a different vault_id (e.g. copied to another vault) must fail
    with pytest.raises((InvalidTag, DecryptionError, ValueError)):
        decrypt_note(key, vault_id2, note_id, data)


def test_unique_nonce_per_encryption(sample_note: dict) -> None:
    key = os.urandom(32)
    vault_id = uuid.uuid4()
    note_id = uuid.uuid4().hex

    data1 = encrypt_note(key, vault_id, note_id, sample_note)
    data2 = encrypt_note(key, vault_id, note_id, sample_note)

    # Different bytes due to random 12-byte nonce on each save
    assert data1 != data2
    nonce1 = data1[5:17]
    nonce2 = data2[5:17]
    assert nonce1 != nonce2

    # Both decrypt to the same content
    assert decrypt_note(key, vault_id, note_id, data1) == sample_note
    assert decrypt_note(key, vault_id, note_id, data2) == sample_note


def test_bad_magic_bytes_fails(sample_note: dict) -> None:
    key = os.urandom(32)
    vault_id = uuid.uuid4()
    note_id = uuid.uuid4().hex

    data = bytearray(encrypt_note(key, vault_id, note_id, sample_note))
    data[:4] = b"BAD!"

    with pytest.raises((FormatError, ValueError)):
        decrypt_note(key, vault_id, note_id, bytes(data))


def test_bad_version_fails(sample_note: dict) -> None:
    key = os.urandom(32)
    vault_id = uuid.uuid4()
    note_id = uuid.uuid4().hex

    data = bytearray(encrypt_note(key, vault_id, note_id, sample_note))
    data[4] = 0x99

    with pytest.raises((FormatError, ValueError)):
        decrypt_note(key, vault_id, note_id, bytes(data))


def test_truncated_data_fails() -> None:
    key = os.urandom(32)
    vault_id = uuid.uuid4()
    note_id = uuid.uuid4().hex

    with pytest.raises((FormatError, ValueError)):
        decrypt_note(key, vault_id, note_id, b"too short")


def test_verifier_success_and_failures() -> None:
    key = os.urandom(32)
    wrong_key = os.urandom(32)
    vault_id = uuid.uuid4()
    wrong_vault_id = uuid.uuid4()

    verifier = make_verifier(key, vault_id)
    assert isinstance(verifier, str)

    # Correct key and vault_id succeeds
    assert check_verifier(key, vault_id, verifier) is True

    # Wrong key fails
    assert check_verifier(wrong_key, vault_id, verifier) is False

    # Wrong vault_id fails
    assert check_verifier(key, wrong_vault_id, verifier) is False

    # Corrupted verifier fails
    assert check_verifier(key, vault_id, "corrupted_verifier_data") is False
