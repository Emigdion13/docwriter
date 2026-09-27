"""Key file generation, validation, and loading for VaultNotes.

Two on-disk shapes exist (sections 4.2 and 4.5):

* ``"protection": "none"`` — the 32-byte vault key is stored in the clear, and
  the *file itself* is the secret (the usual case: it lives on a USB stick).
* ``"protection": "scrypt"`` — the vault key is wrapped with a key-encryption
  key derived from a passphrase (M10).  The file alone unlocks nothing.
"""

from __future__ import annotations

import base64
import json
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from vaultnotes.storage.atomic import atomic_write

#: scrypt work factors for a passphrase-protected key file (section 4.5).
SCRYPT_N = 131072  # 2 ** 17
SCRYPT_R = 8
SCRYPT_P = 1
KEY_LENGTH = 32
SALT_LENGTH = 16
NONCE_LENGTH = 12


class KeyFileError(ValueError):
    """Raised when a key file is invalid, damaged, or cannot be loaded."""
    pass


class PassphraseRequired(KeyFileError):
    """The key file is wrapped: it cannot be read without its passphrase."""

    def __init__(self, message: str = "This key file is passphrase protected.") -> None:
        super().__init__(message)


class WrongPassphrase(KeyFileError):
    """The passphrase did not unlock the key file (the wrap failed to verify)."""

    def __init__(self, message: str = "Wrong passphrase for this key file.") -> None:
        super().__init__(message)


@dataclass
class VaultKey:
    """Represents an encryption key for a VaultNotes vault."""

    key: bytes
    vault_id: str
    vault_name: str
    created: str
    protection: str = "none"
    #: scrypt parameters, kept so a wrapped file can be rewritten in place.
    kdf: dict[str, Any] = field(default_factory=dict)

    def wipe(self) -> None:
        """Best-effort clearing of key material in memory."""
        if isinstance(self.key, bytearray):
            for i in range(len(self.key)):
                self.key[i] = 0
        self.key = b"\x00" * len(self.key)

    def to_dict(self) -> dict[str, Any]:
        """Convert key to a JSON-serializable dictionary matching Section 4.2."""
        return {
            "format": "vaultnotes-key",
            "version": 1,
            "vault_id": self.vault_id,
            "vault_name": self.vault_name,
            "protection": self.protection,
            "key_b64": base64.b64encode(self.key).decode("ascii"),
            "created": self.created,
        }


def _aad_for(vault_id: str | uuid.UUID) -> bytes:
    """The 16 vault-id bytes used as additional authenticated data."""
    if isinstance(vault_id, uuid.UUID):
        return vault_id.bytes
    clean = str(vault_id).replace("-", "").strip()
    try:
        return uuid.UUID(hex=clean).bytes
    except ValueError:
        # A vault id that is not a UUID (an older or hand-written file) still
        # binds: use its UTF-8 bytes rather than silently dropping the check.
        return str(vault_id).encode("utf-8")


def derive_kek(passphrase: str, salt: bytes, kdf: dict[str, Any] | None = None) -> bytes:
    """Derive the key that unlocks a wrapped key (section 4.5)."""
    params = kdf or {}
    return Scrypt(
        salt=salt,
        length=KEY_LENGTH,
        n=int(params.get("n", SCRYPT_N)),
        r=int(params.get("r", SCRYPT_R)),
        p=int(params.get("p", SCRYPT_P)),
    ).derive(passphrase.encode("utf-8"))


def wrap_key(key: bytes, vault_id: str | uuid.UUID, passphrase: str) -> dict[str, Any]:
    """Encrypt one vault key under a passphrase, returning the JSON fields."""
    salt = secrets.token_bytes(SALT_LENGTH)
    nonce = secrets.token_bytes(NONCE_LENGTH)
    kek = derive_kek(passphrase, salt)
    wrapped = AESGCM(kek).encrypt(nonce, key, _aad_for(vault_id))
    return {
        "protection": "scrypt",
        "kdf": {
            "salt_b64": base64.b64encode(salt).decode("ascii"),
            "n": SCRYPT_N,
            "r": SCRYPT_R,
            "p": SCRYPT_P,
        },
        "nonce_b64": base64.b64encode(nonce).decode("ascii"),
        "wrapped_key_b64": base64.b64encode(wrapped).decode("ascii"),
    }


def unwrap_key(data: dict[str, Any], vault_id: str, passphrase: str) -> bytes:
    """Reverse :func:`wrap_key`, raising :class:`WrongPassphrase` on a mismatch."""
    kdf = data.get("kdf")
    if not isinstance(kdf, dict):
        raise KeyFileError("Damaged key file: passphrase protection without 'kdf'")
    # Section 4.5 nests the scrypt salt in "kdf" and keeps the wrap's own two
    # fields next to "protection".
    fields = {"salt_b64": kdf.get("salt_b64"), "nonce_b64": data.get("nonce_b64"), "wrapped_key_b64": data.get("wrapped_key_b64")}
    for name, value in fields.items():
        if not isinstance(value, str) or not value:
            raise KeyFileError(f"Damaged key file: missing or invalid '{name}'")
    try:
        salt = base64.b64decode(fields["salt_b64"], validate=True)
        nonce = base64.b64decode(fields["nonce_b64"], validate=True)
        wrapped = base64.b64decode(fields["wrapped_key_b64"], validate=True)
    except Exception as err:
        raise KeyFileError(f"Damaged key file: invalid base64 ({err})") from err
    if len(salt) < 8 or len(nonce) != NONCE_LENGTH:
        raise KeyFileError("Damaged key file: scrypt salt or nonce has the wrong size")
    if not passphrase:
        raise PassphraseRequired()
    try:
        key = AESGCM(derive_kek(passphrase, salt, kdf)).decrypt(nonce, wrapped, _aad_for(vault_id))
    except InvalidTag as err:
        raise WrongPassphrase() from err
    if len(key) != KEY_LENGTH:
        raise KeyFileError(f"Damaged key file: expected 32-byte key, got {len(key)} bytes")
    return key


def key_file_needs_passphrase(path: Path | str) -> bool:
    """Whether a key file on disk is wrapped and must be unlocked by hand.

    Damage is reported as "no passphrase", so the caller's normal load error
    still explains what is wrong with the file.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return False
    return isinstance(data, dict) and data.get("protection") == "scrypt"


def generate_key_file(
    path: Path | str,
    vault_id: str | uuid.UUID,
    vault_name: str,
    passphrase: str = "",
) -> VaultKey:
    """Generate a random 256-bit key and save it to path as a *.vnkey file.

    Follows Section 4.2; a non-empty ``passphrase`` writes the Section 4.5
    wrapped form instead of the plain one.
    """
    dest = Path(path).resolve()
    v_id = str(vault_id)
    key_bytes = secrets.token_bytes(32)
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    vault_key = VaultKey(
        key=bytearray(key_bytes),
        vault_id=v_id,
        vault_name=vault_name,
        created=now_iso,
        protection="none",
    )

    save_key_file(dest, vault_key, passphrase=passphrase)
    return vault_key


def save_key_file(path: Path | str, vault_key: VaultKey, passphrase: str = "") -> None:
    """Atomically write a VaultKey to disk as UTF-8 JSON.

    ``passphrase`` wraps the key (M10): the file then holds no key material of
    its own, so a stolen stick is useless without the passphrase.
    """
    dest = Path(path).resolve()
    data = vault_key.to_dict()
    if passphrase:
        data.pop("key_b64", None)
        data.update(wrap_key(bytes(vault_key.key), vault_key.vault_id, passphrase))
    content = json.dumps(data, indent=2) + "\n"
    atomic_write(dest, content.encode("utf-8"))


def load_key_file(path: Path | str, passphrase: str = "") -> VaultKey:
    """Load and validate a key file from disk.

    Raises FileNotFoundError if the file does not exist.
    Raises KeyFileError if the file is damaged, missing fields, or has an
    unsupported version; a wrapped file raises PassphraseRequired without a
    passphrase and WrongPassphrase with the wrong one.
    """
    file_path = Path(path).resolve()
    if not file_path.is_file():
        raise FileNotFoundError(f"Key file not found: {file_path}")

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as err:
        raise KeyFileError(f"Damaged key file: invalid JSON ({err})") from err

    if not isinstance(data, dict):
        raise KeyFileError("Damaged key file: root element must be a JSON object")

    if data.get("format") != "vaultnotes-key":
        raise KeyFileError(f"Invalid key file format: expected 'vaultnotes-key', got {data.get('format')!r}")

    if data.get("version") != 1:
        raise KeyFileError(f"Unsupported key version: {data.get('version')}")

    vault_id = data.get("vault_id")
    if not vault_id or not isinstance(vault_id, str):
        raise KeyFileError("Damaged key file: missing or invalid 'vault_id'")

    vault_name = data.get("vault_name", "Vault")
    created = data.get("created", "")
    protection = data.get("protection", "none")

    if protection == "scrypt":
        return VaultKey(
            key=bytearray(unwrap_key(data, vault_id, passphrase)),
            vault_id=vault_id,
            vault_name=vault_name,
            created=created,
            protection="scrypt",
            kdf=dict(data.get("kdf") or {}),
        )

    key_b64 = data.get("key_b64")
    if passphrase and protection != "scrypt":
        raise KeyFileError(
            "This key file is not passphrase protected, so its passphrase is not needed"
        )
    if not key_b64 or not isinstance(key_b64, str):
        raise KeyFileError("Damaged key file: missing or invalid 'key_b64'")

    try:
        key_bytes = base64.b64decode(key_b64, validate=True)
    except Exception as err:
        raise KeyFileError(f"Damaged key file: invalid base64 in 'key_b64' ({err})") from err

    if len(key_bytes) != 32:
        raise KeyFileError(f"Damaged key file: expected 32-byte key, got {len(key_bytes)} bytes")

    return VaultKey(
        # Mutable, like the other two paths, so VaultKey.wipe() can zero it.
        key=bytearray(key_bytes),
        vault_id=vault_id,
        vault_name=vault_name,
        created=created,
        protection=protection,
    )
