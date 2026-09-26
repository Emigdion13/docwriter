"""Key file generation, validation, and loading for VaultNotes."""

from __future__ import annotations

import base64
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vaultnotes.storage.atomic import atomic_write


class KeyFileError(ValueError):
    """Raised when a key file is invalid, damaged, or cannot be loaded."""
    pass


@dataclass
class VaultKey:
    """Represents an encryption key for a VaultNotes vault."""

    key: bytes
    vault_id: str
    vault_name: str
    created: str
    protection: str = "none"

    def wipe(self) -> None:
        """Best-effort clearing of key material in memory."""
        if isinstance(self.key, bytearray):
            for i in range(len(self.key)):
                self.key[i] = 0
        self.key = b"\x00" * len(self.key)

    def to_dict(self) -> dict[str, Any]:
        """Convert key to JSON-serializable dictionary matching Section 4.2."""
        return {
            "format": "vaultnotes-key",
            "version": 1,
            "vault_id": self.vault_id,
            "vault_name": self.vault_name,
            "protection": self.protection,
            "key_b64": base64.b64encode(self.key).decode("ascii"),
            "created": self.created,
        }


def generate_key_file(
    path: Path | str,
    vault_id: str | uuid.UUID,
    vault_name: str,
) -> VaultKey:
    """Generate a random 256-bit key and save it to path as a *.vnkey file.

    Follows Section 4.2 specification.
    """
    dest = Path(path).resolve()
    v_id = str(vault_id)
    key_bytes = secrets.token_bytes(32)
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    vault_key = VaultKey(
        key=key_bytes,
        vault_id=v_id,
        vault_name=vault_name,
        created=now_iso,
        protection="none",
    )

    save_key_file(dest, vault_key)
    return vault_key


def save_key_file(path: Path | str, vault_key: VaultKey) -> None:
    """Atomically write a VaultKey to disk as UTF-8 JSON."""
    dest = Path(path).resolve()
    data = vault_key.to_dict()
    content = json.dumps(data, indent=2) + "\n"
    atomic_write(dest, content.encode("utf-8"))


def load_key_file(path: Path | str) -> VaultKey:
    """Load and validate a key file from disk.

    Raises FileNotFoundError if the file does not exist.
    Raises KeyFileError if the file is damaged, missing fields, or has an unsupported version.
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

    key_b64 = data.get("key_b64")
    if not key_b64 or not isinstance(key_b64, str):
        raise KeyFileError("Damaged key file: missing or invalid 'key_b64'")

    try:
        key_bytes = base64.b64decode(key_b64, validate=True)
    except Exception as err:
        raise KeyFileError(f"Damaged key file: invalid base64 in 'key_b64' ({err})") from err

    if len(key_bytes) != 32:
        raise KeyFileError(f"Damaged key file: expected 32-byte key, got {len(key_bytes)} bytes")

    return VaultKey(
        key=key_bytes,
        vault_id=vault_id,
        vault_name=vault_name,
        created=created,
        protection=protection,
    )
