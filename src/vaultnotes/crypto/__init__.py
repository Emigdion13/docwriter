"""Cryptographic subpackage for VaultNotes."""

from vaultnotes.crypto.keyfile import (
    KeyFileError,
    PassphraseRequired,
    VaultKey,
    WrongPassphrase,
    generate_key_file,
    key_file_needs_passphrase,
    load_key_file,
    save_key_file,
    unwrap_key,
    wrap_key,
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
    "PassphraseRequired",
    "VaultKey",
    "WrongPassphrase",
    "check_verifier",
    "decrypt_note",
    "encrypt_note",
    "generate_key_file",
    "key_file_needs_passphrase",
    "load_key_file",
    "make_verifier",
    "save_key_file",
    "unwrap_key",
    "wrap_key",
]
