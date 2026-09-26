"""Encrypted vault storage for VaultNotes.

A :class:`VaultStore` keeps the decrypted contents of one vault in memory only
while the vault is unlocked.  The files on disk are the format documented in
``VaultNotes-Build-Plan.md``: a public ``vault.json`` header and one encrypted
``.vnote`` file per note.  Trash entries remain encrypted as well.

This module deliberately does not know anything about pywebview or the Bridge
API.  It can therefore be exercised independently in unit tests.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from vaultnotes.crypto.keyfile import VaultKey
from vaultnotes.crypto.notecrypt import (
    check_verifier,
    decrypt_note,
    encrypt_note,
    make_verifier,
)
from vaultnotes.models import Note
from vaultnotes.storage.atomic import atomic_write
from vaultnotes.storage.plain_store import sanitize_title

VAULT_FORMAT = "vaultnotes-vault"
VAULT_VERSION = 1
NOTE_SUFFIX = ".vnote"
NOTE_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class VaultStoreError(ValueError):
    """Base error for an invalid, locked, or damaged encrypted vault."""


class VaultNotInitializedError(VaultStoreError):
    """Raised when a vault folder has no valid ``vault.json`` header."""


class VaultLockedError(VaultStoreError):
    """Raised when an operation needs a vault that is not unlocked."""


class WrongVaultError(VaultStoreError):
    """Raised when a key file belongs to another vault."""


class WrongKeyError(VaultStoreError):
    """Raised when a key cannot decrypt the vault verifier."""


class DamagedVaultError(VaultStoreError):
    """Raised when a vault header or encrypted note is damaged."""


def _now_iso() -> str:
    """Return a UTC timestamp in the format used by note JSON."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _canonical_uuid(value: str | uuid.UUID) -> str:
    """Return a UUID string in the canonical dashed representation."""
    try:
        return str(value if isinstance(value, uuid.UUID) else uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"Invalid vault id: {value!r}") from exc


def _note_id(value: str | uuid.UUID) -> str:
    """Validate and normalize a note id used as a filename."""
    if isinstance(value, uuid.UUID):
        result = value.hex
    else:
        result = str(value).strip().lower()
        # UUID objects are the public convenience form; filenames are not.
        if len(result) == 36:
            try:
                result = uuid.UUID(result).hex
            except ValueError:
                pass
    if not NOTE_ID_RE.fullmatch(result):
        raise ValueError("Invalid vault note id")
    return result


def _key_bytes(key: bytes | bytearray | VaultKey) -> bytes:
    """Extract and validate raw AES-256 key bytes."""
    raw = key.key if isinstance(key, VaultKey) else key
    raw_bytes = bytes(raw)
    if len(raw_bytes) != 32:
        raise ValueError(f"AES-256 requires a 32-byte key, got {len(raw_bytes)} bytes")
    return raw_bytes


def _copy_note(note: Note) -> Note:
    """Return a note copy so callers cannot mutate the in-memory store."""
    return Note(
        id=note.id,
        title=note.title,
        body=note.body,
        modified=note.modified,
        created=note.created,
        tags=list(note.tags),
    )


class VaultStore:
    """Storage provider for one encrypted VaultNotes vault.

    ``root`` is the vault directory containing ``vault.json`` and ``.vnote``
    files.  Constructing a store never decrypts anything.  Call :meth:`unlock`
    with a matching :class:`~vaultnotes.crypto.keyfile.VaultKey` before using
    note operations.
    """

    def __init__(
        self,
        root: Path | str,
        vault_id: str | uuid.UUID | None = None,
        name: str | None = None,
        key: bytes | bytearray | VaultKey | None = None,
        create: bool = False,
        strict_header: bool = True,
        vault_name: str | None = None,
    ) -> None:
        if vault_name is not None:
            name = vault_name
        self.root = Path(root).resolve()
        self.trash_dir = self.root / ".trash"
        self.header_path = self.root / "vault.json"
        self.root.mkdir(parents=True, exist_ok=True)
        self.trash_dir.mkdir(parents=True, exist_ok=True)

        self._header: dict[str, Any] | None = None
        self._header_error: VaultStoreError | None = None
        self._vault_id_hint = _canonical_uuid(vault_id) if vault_id is not None else None
        self._name_hint = str(name or "Vault")
        self._key: bytearray | None = None
        self._notes: dict[str, Note] = {}
        self._trash_notes: dict[str, Note] = {}

        if self.header_path.is_file():
            try:
                self._load_header()
            except VaultStoreError as exc:
                if strict_header:
                    raise
                self._header_error = exc
        elif create:
            if key is None:
                raise ValueError("A key is required to create a vault")
            self.create(key, vault_id=vault_id, name=name)

    # ------------------------------------------------------------------
    # Header and lifecycle
    # ------------------------------------------------------------------
    @property
    def has_header(self) -> bool:
        """Whether a ``vault.json`` file exists and loaded successfully."""
        return self._header is not None

    @property
    def header_error(self) -> VaultStoreError | None:
        """The header error captured by non-strict construction, if any."""
        return self._header_error

    @property
    def vault_id(self) -> str | None:
        """The vault UUID, or ``None`` before the vault is created."""
        if self._header:
            return str(self._header["vault_id"])
        return self._vault_id_hint

    @property
    def name(self) -> str:
        """The public vault name."""
        if self._header:
            return str(self._header.get("name", self._name_hint))
        return self._name_hint

    @property
    def locked(self) -> bool:
        """Whether the vault is currently locked."""
        return self._key is None

    @property
    def is_unlocked(self) -> bool:
        """The inverse of :attr:`locked`, provided for API readability."""
        return not self.locked

    def is_locked(self) -> bool:
        """Return the current locked state as a callable convenience API."""
        return self.locked

    @property
    def header(self) -> dict[str, Any] | None:
        """Return a copy of the public vault header."""
        return deepcopy(self._header) if self._header is not None else None

    def _load_header(self) -> dict[str, Any]:
        """Read and validate the public vault header."""
        try:
            with self.header_path.open("r", encoding="utf-8") as handle:
                header = json.load(handle)
        except FileNotFoundError:
            raise VaultNotInitializedError("Vault has not been created")
        except Exception as exc:
            raise DamagedVaultError("Damaged vault header: invalid JSON") from exc

        if not isinstance(header, dict):
            raise DamagedVaultError("Damaged vault header: expected a JSON object")
        if header.get("format") != VAULT_FORMAT:
            raise DamagedVaultError("Invalid vault header format")
        if header.get("version") != VAULT_VERSION:
            raise DamagedVaultError(
                f"Unsupported vault header version: {header.get('version')}"
            )

        try:
            header_id = _canonical_uuid(header["vault_id"])
        except (KeyError, ValueError) as exc:
            raise DamagedVaultError("Damaged vault header: invalid vault_id") from exc

        verifier = header.get("verifier_b64")
        if not isinstance(verifier, str) or not verifier:
            raise DamagedVaultError("Damaged vault header: missing verifier")
        vault_name = header.get("name")
        if not isinstance(vault_name, str) or not vault_name.strip():
            raise DamagedVaultError("Damaged vault header: missing vault name")

        if self._vault_id_hint and self._vault_id_hint != header_id:
            raise DamagedVaultError("Vault id does not match its header")

        self._header = {
            "format": VAULT_FORMAT,
            "version": VAULT_VERSION,
            "vault_id": header_id,
            "name": vault_name,
            "verifier_b64": verifier,
        }
        self._vault_id_hint = header_id
        self._name_hint = vault_name
        return deepcopy(self._header)

    def create(
        self,
        key: bytes | bytearray | VaultKey,
        vault_id: str | uuid.UUID | None = None,
        name: str | None = None,
        vault_name: str | None = None,
    ) -> dict[str, Any]:
        """Create ``vault.json`` for this folder.

        The key is used only to create the verifier.  It is not written to the
        vault folder.  A :class:`VaultKey` must carry the same ``vault_id`` as
        the header; this prevents accidentally pairing a key with another
        vault.
        """
        if vault_name is not None:
            name = vault_name
        if self.header_path.exists():
            if self._header is None:
                self._load_header()
            raise FileExistsError(f"Vault already exists: {self.root}")

        chosen_id: str
        if vault_id is not None:
            chosen_id = _canonical_uuid(vault_id)
        elif isinstance(key, VaultKey):
            chosen_id = _canonical_uuid(key.vault_id)
        elif self._vault_id_hint:
            chosen_id = self._vault_id_hint
        else:
            chosen_id = str(uuid.uuid4())

        if isinstance(key, VaultKey) and _canonical_uuid(key.vault_id) != chosen_id:
            raise WrongVaultError("The key belongs to a different vault")

        chosen_name = str(name or (key.vault_name if isinstance(key, VaultKey) else self._name_hint)).strip()
        if not chosen_name:
            raise ValueError("Vault name cannot be empty")

        raw = _key_bytes(key)
        header = {
            "format": VAULT_FORMAT,
            "version": VAULT_VERSION,
            "vault_id": chosen_id,
            "name": chosen_name,
            "verifier_b64": make_verifier(raw, chosen_id),
        }
        # atomic_write creates the parent and fsyncs the header.  The header
        # contains no key or note plaintext.
        atomic_write(self.header_path, (json.dumps(header, indent=2) + "\n").encode("utf-8"))
        self._header = header
        self._vault_id_hint = chosen_id
        self._name_hint = chosen_name
        return deepcopy(header)

    # Explicit aliases keep the intent clear for callers that call this step
    # "initialize" rather than "create".
    initialize = create
    create_header = create

    @classmethod
    def create_vault(
        cls,
        root: Path | str,
        key: bytes | bytearray | VaultKey,
        vault_id: str | uuid.UUID | None = None,
        name: str | None = None,
        vault_name: str | None = None,
    ) -> "VaultStore":
        """Create and return a :class:`VaultStore` in one call."""
        if vault_name is not None:
            name = vault_name
        store = cls(root, vault_id=vault_id, name=name)
        store.create(key, vault_id=vault_id, name=name)
        return store

    def unlock(self, key: bytes | bytearray | VaultKey) -> int:
        """Verify ``key`` and decrypt all active and trash notes into memory.

        No in-memory state is replaced until every encrypted file has been
        checked.  Therefore a wrong key or a damaged note leaves an already
        unlocked store usable and never overwrites any file.
        """
        if self._header is None:
            if self.header_path.is_file():
                self._load_header()
            else:
                raise VaultNotInitializedError("Vault has not been created")

        assert self._header is not None  # narrowed for type checkers
        raw = _key_bytes(key)
        key_vault_id = key.vault_id if isinstance(key, VaultKey) else self.vault_id
        if key_vault_id is None or _canonical_uuid(key_vault_id) != self.vault_id:
            raise WrongVaultError("This key belongs to a different vault.")

        if not check_verifier(raw, self.vault_id or "", self._header["verifier_b64"]):
            raise WrongKeyError("Wrong or damaged key.")

        active: dict[str, Note] = {}
        trash: dict[str, Note] = {}
        try:
            for note_path in self._iter_note_paths(self.root):
                note_id = self._id_from_path(note_path)
                active[note_id] = self._decrypt_file(note_path, note_id, raw)
            for note_path in self._iter_note_paths(self.trash_dir):
                note_id = self._id_from_path(note_path)
                trash[note_id] = self._decrypt_file(note_path, note_id, raw)
        except (DamagedVaultError, ValueError) as exc:
            # Do not expose whether a particular title was in a damaged file;
            # the file name is an opaque id and the error is safe to display.
            if isinstance(exc, DamagedVaultError):
                raise
            raise DamagedVaultError("Damaged encrypted note in vault") from exc

        self._wipe_key()
        self._key = bytearray(raw)
        self._notes = active
        self._trash_notes = trash
        return len(active)

    def lock(self) -> None:
        """Forget the key and all decrypted note content held by this store."""
        self._notes.clear()
        self._trash_notes.clear()
        self._wipe_key()

    def _wipe_key(self) -> None:
        """Best-effort zeroing of the mutable in-memory key buffer."""
        if self._key is not None:
            for index in range(len(self._key)):
                self._key[index] = 0
            self._key = None

    def _require_unlocked(self) -> bytearray:
        if self._key is None or self._header is None:
            raise VaultLockedError("Vault is locked")
        return self._key

    # ------------------------------------------------------------------
    # Encrypted file helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _iter_note_paths(folder: Path) -> Iterable[Path]:
        if not folder.is_dir():
            return ()
        return sorted(
            (path for path in folder.iterdir() if path.is_file() and path.suffix.lower() == NOTE_SUFFIX),
            key=lambda path: path.name.lower(),
        )

    @staticmethod
    def _id_from_path(path: Path) -> str:
        stem = path.name[: -len(NOTE_SUFFIX)]
        try:
            return _note_id(stem)
        except ValueError as exc:
            raise DamagedVaultError("Damaged vault: invalid encrypted note filename") from exc

    def _note_path(self, note_id: str | uuid.UUID, trash: bool = False) -> Path:
        clean_id = _note_id(note_id)
        return (self.trash_dir if trash else self.root) / f"{clean_id}{NOTE_SUFFIX}"

    @staticmethod
    def _payload_to_note(note_id: str, payload: Any) -> Note:
        if not isinstance(payload, dict):
            raise DamagedVaultError("Damaged note: plaintext is not a JSON object")
        title = payload.get("title")
        body = payload.get("body")
        created = payload.get("created")
        modified = payload.get("modified")
        tags = payload.get("tags", [])
        if not isinstance(title, str) or not title.strip():
            raise DamagedVaultError("Damaged note: invalid title")
        if not isinstance(body, str):
            raise DamagedVaultError("Damaged note: invalid body")
        if not isinstance(created, str) or not isinstance(modified, str):
            raise DamagedVaultError("Damaged note: invalid timestamps")
        if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
            raise DamagedVaultError("Damaged note: invalid tags")
        return Note(
            id=note_id,
            title=title,
            body=body,
            modified=modified,
            created=created,
            tags=list(tags),
        )

    def _decrypt_file(self, path: Path, note_id: str, raw_key: bytes) -> Note:
        try:
            data = path.read_bytes()
            payload = decrypt_note(raw_key, self.vault_id or "", note_id, data)
            return self._payload_to_note(note_id, payload)
        except DamagedVaultError:
            raise
        except Exception as exc:
            raise DamagedVaultError("Damaged encrypted note") from exc

    @staticmethod
    def _note_payload(note: Note) -> dict[str, Any]:
        return {
            "title": note.title,
            "created": note.created,
            "modified": note.modified,
            "tags": list(note.tags),
            "body": note.body,
        }

    def _write_note(self, note: Note, path: Path) -> None:
        key = self._require_unlocked()
        if self.vault_id is None:
            raise VaultNotInitializedError("Vault has not been created")
        data = encrypt_note(key, self.vault_id, note.id, self._note_payload(note))
        # The temporary file contains ciphertext only.
        atomic_write(path, data)

    # ------------------------------------------------------------------
    # Note CRUD
    # ------------------------------------------------------------------
    @staticmethod
    def _clean_title(title: str) -> str:
        if not isinstance(title, str):
            raise ValueError("Title must be text")
        cleaned = sanitize_title(title)
        if not cleaned:
            raise ValueError("Title cannot be empty")
        return cleaned

    def _unique_title(self, title: str, exclude_id: str | None = None) -> str:
        base = self._clean_title(title)
        used = {
            note.title.casefold()
            for note_id, note in self._notes.items()
            if note_id != exclude_id
        }
        candidate = base
        number = 2
        while candidate.casefold() in used:
            candidate = f"{base} ({number})"
            number += 1
        return candidate

    def create_note(self, title: str = "Untitled", body: str = "") -> Note:
        """Create, encrypt, and return a new note with an opaque UUID id."""
        self._require_unlocked()
        clean_title = self._unique_title(title)
        note_id = uuid.uuid4().hex
        while note_id in self._notes or note_id in self._trash_notes:
            note_id = uuid.uuid4().hex
        content = body if body else f"# {clean_title}\n\n"
        now = _now_iso()
        note = Note(
            id=note_id,
            title=clean_title,
            body=content,
            modified=now,
            created=now,
        )
        path = self._note_path(note_id)
        self._write_note(note, path)
        self._notes[note_id] = note
        return _copy_note(note)

    def read_note(self, note_id: str | uuid.UUID) -> Note:
        """Return one decrypted note from memory."""
        self._require_unlocked()
        clean_id = _note_id(note_id)
        try:
            return _copy_note(self._notes[clean_id])
        except KeyError as exc:
            raise FileNotFoundError(f"Note not found: {note_id}") from exc

    def save_note(self, note_id: str | uuid.UUID, body: str) -> Note:
        """Encrypt an updated body using a fresh nonce and atomically replace it."""
        self._require_unlocked()
        if not isinstance(body, str):
            raise ValueError("Note body must be text")
        clean_id = _note_id(note_id)
        try:
            old = self._notes[clean_id]
        except KeyError as exc:
            raise FileNotFoundError(f"Note not found: {note_id}") from exc
        updated = Note(
            id=old.id,
            title=old.title,
            body=body,
            modified=_now_iso(),
            created=old.created,
            tags=list(old.tags),
        )
        self._write_note(updated, self._note_path(clean_id))
        self._notes[clean_id] = updated
        return _copy_note(updated)

    def rename_note(
        self,
        note_id: str | uuid.UUID,
        new_title: str,
    ) -> tuple[Note, str]:
        """Rename a note without changing its opaque id or filename."""
        self._require_unlocked()
        clean_id = _note_id(note_id)
        try:
            old = self._notes[clean_id]
        except KeyError as exc:
            raise FileNotFoundError(f"Note not found: {note_id}") from exc

        clean_title = self._unique_title(new_title, exclude_id=clean_id)
        old_title = old.title
        body = re.sub(
            r"^#\s+" + re.escape(old_title) + r"\s*$",
            f"# {clean_title}",
            old.body,
            count=1,
            flags=re.MULTILINE,
        )
        updated = Note(
            id=old.id,
            title=clean_title,
            body=body,
            modified=_now_iso(),
            created=old.created,
            tags=list(old.tags),
        )
        self._write_note(updated, self._note_path(clean_id))
        self._notes[clean_id] = updated
        return _copy_note(updated), old_title

    def delete_note(self, note_id: str | uuid.UUID) -> Note:
        """Move an encrypted note to ``.trash`` without decrypting it on disk."""
        self._require_unlocked()
        clean_id = _note_id(note_id)
        try:
            note = self._notes[clean_id]
        except KeyError as exc:
            raise FileNotFoundError(f"Note not found: {note_id}") from exc
        source = self._note_path(clean_id)
        destination = self._note_path(clean_id, trash=True)
        if not source.is_file():
            raise FileNotFoundError(f"Note not found: {note_id}")
        if destination.exists():
            raise FileExistsError("A trash entry with that note id already exists")
        os.replace(source, destination)
        del self._notes[clean_id]
        self._trash_notes[clean_id] = note
        return _copy_note(note)

    def restore_note(self, note_id: str | uuid.UUID) -> Note:
        """Restore an encrypted trash entry, resolving title collisions."""
        self._require_unlocked()
        clean_id = _note_id(note_id)
        try:
            note = self._trash_notes[clean_id]
        except KeyError as exc:
            raise FileNotFoundError(f"Note not found in trash: {note_id}") from exc

        title = self._unique_title(note.title)
        source = self._note_path(clean_id, trash=True)
        destination = self._note_path(clean_id)
        if not source.is_file():
            raise FileNotFoundError(f"Note not found in trash: {note_id}")

        if title == note.title:
            # Moving ciphertext preserves the original authenticated bytes.
            os.replace(source, destination)
            restored = note
        else:
            restored = Note(
                id=note.id,
                title=title,
                body=note.body,
                modified=_now_iso(),
                created=note.created,
                tags=list(note.tags),
            )
            self._write_note(restored, destination)
            try:
                source.unlink()
            except Exception:
                # Leave the source untouched if cleanup fails; remove the new
                # active file so a retry cannot silently overwrite it.
                try:
                    destination.unlink()
                except OSError:
                    pass
                raise

        del self._trash_notes[clean_id]
        self._notes[clean_id] = restored
        return _copy_note(restored)

    def purge_note(self, note_id: str | uuid.UUID) -> str:
        """Permanently delete an encrypted trash entry (cannot be undone).

        Removes both the ``.vnote`` file and its decrypted in-memory copy.
        Returns the purged note's title. Raises FileNotFoundError when the
        note is not in trash.
        """
        self._require_unlocked()
        clean_id = _note_id(note_id)
        try:
            note = self._trash_notes[clean_id]
        except KeyError as exc:
            raise FileNotFoundError(f"Note not found in trash: {note_id}") from exc
        source = self._note_path(clean_id, trash=True)
        if source.is_file():
            source.unlink()
        del self._trash_notes[clean_id]
        return note.title

    def empty_trash(self) -> int:
        """Permanently delete every encrypted trash entry. Returns the count."""
        self._require_unlocked()
        count = 0
        for note_id in list(self._trash_notes.keys()):
            try:
                self.purge_note(note_id)
                count += 1
            except (FileNotFoundError, ValueError, OSError):
                pass
        # Also remove any orphaned .vnote files with no in-memory entry.
        for orphan in self._iter_note_paths(self.trash_dir):
            try:
                orphan.unlink()
                count += 1
            except OSError:
                pass
        return count

    def list_notes(self, query: str = "", sort: str = "modified") -> list[Note]:
        """List decrypted active notes, optionally filtering title and body."""
        self._require_unlocked()
        notes = list(self._notes.values())
        needle = (query or "").strip().casefold()
        if needle:
            notes = [
                note
                for note in notes
                if needle in note.title.casefold() or needle in note.body.casefold()
            ]
        if sort.casefold() in {"title", "name"}:
            notes.sort(key=lambda note: (note.title.casefold(), note.id))
        else:
            notes.sort(key=lambda note: (note.modified, note.id), reverse=True)
        return [_copy_note(note) for note in notes]

    def list_trash(self, sort: str = "modified") -> list[Note]:
        """List decrypted notes currently in encrypted trash."""
        self._require_unlocked()
        notes = list(self._trash_notes.values())
        if sort.casefold() in {"title", "name"}:
            notes.sort(key=lambda note: (note.title.casefold(), note.id))
        else:
            notes.sort(key=lambda note: (note.modified, note.id), reverse=True)
        return [_copy_note(note) for note in notes]

    # Useful aliases shared with PlainStore and convenient in tests.  The
    # name ``create`` is intentionally reserved for creating the vault header;
    # note creation is ``create_note`` (or ``new_note``).
    new_note = create_note
    read = read_note
    get = read_note
    get_note = read_note
    save = save_note
    update = save_note
    update_note = save_note
    rename = rename_note
    delete = delete_note
    restore = restore_note
    purge = purge_note
    list = list_notes

    # ------------------------------------------------------------------
    # Locked-state metadata (does not decrypt note contents)
    # ------------------------------------------------------------------
    def disk_note_count(self, trash: bool = False) -> int:
        """Count encrypted files on disk without loading their plaintext."""
        folder = self.trash_dir if trash else self.root
        return sum(1 for _ in self._iter_note_paths(folder))


def create_vault(
    root: Path | str,
    key: bytes | bytearray | VaultKey,
    vault_id: str | uuid.UUID | None = None,
    name: str | None = None,
    vault_name: str | None = None,
) -> VaultStore:
    """Module-level convenience wrapper for :meth:`VaultStore.create_vault`."""
    return VaultStore.create_vault(
        root,
        key,
        vault_id=vault_id,
        name=name,
        vault_name=vault_name,
    )


# A descriptive alias for callers that prefer the provider name.
EncryptedVaultStore = VaultStore
