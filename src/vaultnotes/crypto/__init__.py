"""Cryptographic subpackage for VaultNotes."""

from vaultnotes.crypto.keyfile import (
    KeyFileError,
    VaultKey,
    generate_key_file,
    load_key_file,
    save_key_file,
)
from vaultnotes.crypto.notecrypt import (
    CryptoError,
    DecryptionError,
    FormatError,
    check_verifier,
    decrypt_note,
    encrypt_note,
    make_verifier,
)

__all__ = [
    "CryptoError",
    "DecryptionError",
    "FormatError",
    "KeyFileError",
    "VaultKey",
    "check_verifier",
    "decrypt_note",
    "encrypt_note",
    "generate_key_file",
    "load_key_file",
    "make_verifier",
    "save_key_file",
]
