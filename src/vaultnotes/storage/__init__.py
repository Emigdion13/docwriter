"""Storage providers for VaultNotes.

The heavier providers are imported lazily so ``crypto.keyfile`` can safely
import ``storage.atomic`` without a package-level circular import.
"""

from vaultnotes.storage.atomic import atomic_write

__all__ = ["atomic_write", "PlainStore", "VaultStore", "create_vault"]


def __getattr__(name: str):
    if name == "PlainStore":
        from vaultnotes.storage.plain_store import PlainStore

        return PlainStore
    if name in {"VaultStore", "create_vault"}:
        from vaultnotes.storage.vault_store import VaultStore, create_vault

        return {"VaultStore": VaultStore, "create_vault": create_vault}[name]
    raise AttributeError(name)
