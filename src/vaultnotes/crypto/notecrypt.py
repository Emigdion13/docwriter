"""Note encryption, decryption, and vault verification for VaultNotes."""

from __future__ import annotations

import base64
import json
import os
import uuid
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from vaultnotes.crypto.keyfile import VaultKey

MAGIC = b"VNT1"
VERSION = 1
NONCE_LENGTH = 12
TAG_LENGTH = 16


class CryptoError(Exception):
    """Base exception for cryptographic operations."""
    pass


class DecryptionError(InvalidTag, ValueError):
    """Raised when note decryption fails (wrong key, tampering, tag mismatch)."""
    pass


class FormatError(ValueError):
    """Raised when note file has invalid magic bytes or unsupported version."""
    pass


def _extract_raw_key(key: bytes | bytearray | VaultKey) -> bytes:
    """Extract 32-byte key from VaultKey or raw bytes."""
    if isinstance(key, VaultKey):
        raw = key.key
    else:
        raw = bytes(key)

    if len(raw) != 32:
        raise ValueError(f"AES-256 requires a 32-byte key, got {len(raw)} bytes")
    return raw


def _to_uuid_bytes(val: str | uuid.UUID | bytes) -> bytes:
    """Convert string, UUID, or 16-byte buffer into 16 raw UUID bytes."""
    if isinstance(val, uuid.UUID):
        return val.bytes
    if isinstance(val, bytes):
        if len(val) == 16:
            return val
        val = val.decode("ascii", errors="replace")
    if isinstance(val, str):
        clean = val.replace("-", "").strip()
        if len(clean) == 32:
            return uuid.UUID(hex=clean).bytes
        return uuid.UUID(val).bytes
    raise ValueError(f"Cannot convert {val!r} to 16-byte UUID")


def make_verifier(key: bytes | bytearray | VaultKey, vault_id: str | uuid.UUID | bytes) -> str:
    """Generate base64 vault verifier for vault.json matching Section 4.3.

    base64 of: nonce(12 bytes) + AESGCM(key).encrypt(nonce, b'vaultnotes-verify', b'verifier:' + vault_id.bytes)
    """
    raw_key = _extract_raw_key(key)
    v_bytes = _to_uuid_bytes(vault_id)
    nonce = os.urandom(NONCE_LENGTH)
    aad = b"verifier:" + v_bytes

    aesgcm = AESGCM(raw_key)
    ciphertext = aesgcm.encrypt(nonce, b"vaultnotes-verify", aad)
    combined = nonce + ciphertext
    return base64.b64encode(combined).decode("ascii")


def check_verifier(
    key: bytes | bytearray | VaultKey,
    vault_id: str | uuid.UUID | bytes,
    verifier: str | bytes,
) -> bool:
    """Verify whether key matches vault_id using the verifier from vault.json.

    Returns True if key successfully decrypts the verifier to b'vaultnotes-verify',
    False otherwise. Never raises an exception.
    """
    try:
        raw_key = _extract_raw_key(key)
        v_bytes = _to_uuid_bytes(vault_id)

        if isinstance(verifier, str):
            verifier_bytes = base64.b64decode(verifier, validate=True)
        else:
            verifier_bytes = bytes(verifier)

        if len(verifier_bytes) < NONCE_LENGTH + TAG_LENGTH:
            return False

        nonce = verifier_bytes[:NONCE_LENGTH]
        ciphertext = verifier_bytes[NONCE_LENGTH:]
        aad = b"verifier:" + v_bytes

        aesgcm = AESGCM(raw_key)
        plaintext = aesgcm.decrypt(nonce, ciphertext, aad)
        return plaintext == b"vaultnotes-verify"
    except Exception:
        return False


def encrypt_note(
    key: bytes | bytearray | VaultKey,
    vault_id: str | uuid.UUID | bytes,
    note_id: str | uuid.UUID | bytes,
    note: dict[str, Any],
) -> bytes:
    """Encrypt note dictionary into binary format matching Section 4.4.

    Binary format:
      Bytes 0–3:   b"VNT1"
      Byte 4:      0x01
      Bytes 5–16:  nonce (12 random bytes, new on every save)
      Bytes 17+:   AES-256-GCM ciphertext + 16-byte tag

    AAD: b"VNT1" + b"\x01" + vault_id.bytes + note_id.bytes (37 bytes)
    """
    raw_key = _extract_raw_key(key)
    v_bytes = _to_uuid_bytes(vault_id)
    n_bytes = _to_uuid_bytes(note_id)

    plaintext = json.dumps(note, ensure_ascii=False).encode("utf-8")
    nonce = os.urandom(NONCE_LENGTH)

    aad = MAGIC + bytes([VERSION]) + v_bytes + n_bytes

    aesgcm = AESGCM(raw_key)
    ciphertext = aesgcm.encrypt(nonce, plaintext, aad)

    return MAGIC + bytes([VERSION]) + nonce + ciphertext


def decrypt_note(
    key: bytes | bytearray | VaultKey,
    vault_id: str | uuid.UUID | bytes,
    note_id: str | uuid.UUID | bytes,
    data: bytes,
) -> dict[str, Any]:
    """Decrypt binary note data matching Section 4.4 into note dictionary.

    Validates magic bytes, format version, and cryptographic authentication tag.
    Raises FormatError if magic bytes or version are invalid or data is truncated.
    Raises DecryptionError if decryption fails (wrong key, tampering, mismatched note_id).
    """
    if len(data) < len(MAGIC) + 1 + NONCE_LENGTH + TAG_LENGTH:
        raise FormatError("Damaged note file: truncated data")

    magic = data[:4]
    if magic != MAGIC:
        raise FormatError(f"Invalid note file: bad magic bytes (expected {MAGIC!r}, got {magic!r})")

    version = data[4]
    if version != VERSION:
        raise FormatError(f"Unsupported note format version: {version}")

    raw_key = _extract_raw_key(key)
    v_bytes = _to_uuid_bytes(vault_id)
    n_bytes = _to_uuid_bytes(note_id)

    nonce = data[5 : 5 + NONCE_LENGTH]
    ciphertext = data[5 + NONCE_LENGTH :]

    aad = MAGIC + bytes([VERSION]) + v_bytes + n_bytes

    aesgcm = AESGCM(raw_key)
    try:
        plaintext = aesgcm.decrypt(nonce, ciphertext, aad)
    except InvalidTag as err:
        raise DecryptionError("Failed to decrypt note: wrong key or damaged file") from err

    try:
        return json.loads(plaintext.decode("utf-8"))
    except Exception as err:
        raise DecryptionError(f"Damaged note file: decrypted plaintext is not valid JSON ({err})") from err
