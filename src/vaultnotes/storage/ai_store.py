"""Encrypted storage for the AI-Notes space.

AI-Notes used to be a folder of plain ``.md`` files, so the Google Drive backup
carried AI helpers' notes in clear text.  :class:`EncryptedAIStore` keeps the
same folder (``<notes root>/ai-notes``) but stores it as an encrypted vault:
a ``vault.json`` header and one ``.vnote`` file per note, exactly the format of
the Personal and Encrypted vaults.  The backup therefore only ever sees
ciphertext.

Unlike those vaults, AI-Notes is **always open**: AI helpers write to it from
outside the app (``notes.py``), and nobody is there to type a passphrase.  The
key file therefore has ``"protection": "none"`` and lives on this PC, outside
the notes folder (``%LOCALAPPDATA%\\VaultNotes\\ai-notes.vnkey`` by default), so
it is never backed up with the notes it opens.

To the rest of the app the store looks like a :class:`PlainStore`: the same
public methods, display timestamps, ``fingerprint()`` and ``skipped_files``.
The API keeps treating AI-Notes as a plain space (no lock, no auto-lock); only
the bytes on disk change.

Because :class:`VaultStore` decrypts everything into memory when it unlocks, a
note written by another process (``notes.py`` while the app runs) would never
show up.  Every public call therefore first compares a cheap stamp of the
folder with the one taken after this store's own last read or write, and
decrypts the folder again when someone else changed it.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from vaultnotes.crypto.keyfile import (
    KeyFileError,
    PassphraseRequired,
    VaultKey,
    generate_key_file,
    load_key_file,
)
from vaultnotes.models import Note
from vaultnotes.storage.plain_store import format_timestamp, sanitize_title
from vaultnotes.storage.vault_store import (
    NOTE_SUFFIX,
    VaultStore,
    VaultStoreError,
    _note_id,
)

#: Name written into the AI-Notes ``vault.json`` and its key file.
AI_VAULT_NAME = "AI-Notes"

_HEADER_NAME = "vault.json"


class AiNotesError(ValueError):
    """AI-Notes cannot be opened as it stands; the message says why and what to do."""


class AiNotesNeedsMigration(AiNotesError):
    """The folder still holds plain ``.md`` notes and has no encrypted vault yet."""


def plaintext_note_files(folder: Path | str) -> list[Path]:
    """The plain ``.md`` notes in an AI-Notes folder and its trash, sorted.

    Hidden files are skipped in the folder itself, the way :class:`PlainStore`
    lists notes.
    """
    root = Path(folder)
    found: list[Path] = []
    for directory, skip_hidden in ((root, True), (root / ".trash", False)):
        if not directory.is_dir():
            continue
        for path in directory.iterdir():
            if not path.is_file() or path.suffix.lower() != ".md":
                continue
            if skip_hidden and path.name.startswith("."):
                continue
            found.append(path)
    return sorted(found, key=lambda path: (path.parent != root, path.name.lower()))


def _display(value: str) -> str:
    """An ISO note timestamp as the short text PlainStore shows ("Today 14:02")."""
    try:
        moment = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return str(value)
    return format_timestamp(moment.timestamp())


def _shown(note: Note) -> Note:
    return Note(
        id=note.id,
        title=note.title,
        body=note.body,
        modified=_display(note.modified),
        created=_display(note.created),
        tags=list(note.tags),
    )


def _iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class EncryptedAIStore:
    """AI-Notes as an always-open encrypted vault with a PlainStore-like API.

    ``create=True`` sets up a new vault when the folder has none, reusing this
    PC's AI-Notes key or creating it.  It never makes a second key for an
    existing vault, never overwrites a key file, and refuses a folder that
    still holds plain notes: those go through :mod:`vaultnotes.migrate_ai` first.
    """

    def __init__(
        self,
        root: Path | str,
        key_path: Path | str,
        *,
        create: bool = False,
        guide: tuple[str, str] | None = None,
        allow_plaintext: bool = False,
    ) -> None:
        self.root = Path(root).resolve()
        self.trash_dir = self.root / ".trash"
        self.key_path = Path(key_path).expanduser()
        self._key: VaultKey | None = None
        #: Whether this store generated the key file (a new PC), not reused one.
        self.created_key = False

        header = self.root / _HEADER_NAME
        if not header.is_file():
            if not allow_plaintext and plaintext_note_files(self.root):
                raise AiNotesNeedsMigration(
                    f"{self.root} still holds plain .md notes. Encrypt them first with: "
                    "python -m vaultnotes.migrate_ai --root <notes folder>"
                )
            if not create:
                raise AiNotesError(f"AI-Notes has no encrypted vault in {self.root} yet.")
            self._vault = self._create_vault(guide)
        else:
            self._vault = VaultStore(self.root)
            self._key = self._load_key()
            try:
                self._vault.unlock(self._key)
            except VaultStoreError as exc:
                self._forget_key()
                raise AiNotesError(f"AI-Notes did not open with {self.key_path} ({exc}).") from exc
        self._stamp = self._disk_stamp()

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------
    def _create_vault(self, guide: tuple[str, str] | None) -> VaultStore:
        """Set up a vault in an empty folder.

        An existing key file is reused, never replaced: one key per PC opens
        AI-Notes in every notes folder used here (the vault id comes from the
        key, so each folder's header matches it).
        """
        self.created_key = False
        if self.key_path.exists():
            key = self._read_key_file()
        else:
            self.key_path.parent.mkdir(parents=True, exist_ok=True)
            key = generate_key_file(self.key_path, uuid.uuid4(), AI_VAULT_NAME)
            self.created_key = True
        vault = VaultStore(self.root)
        vault.create(key, name=AI_VAULT_NAME)
        vault.unlock(key)
        self._key = key
        if guide is not None:
            title, body = guide
            vault.create_note(title=title, body=body)
        return vault

    def _public_key_fields(self) -> dict:
        """The key file's public JSON fields, read without decoding the key."""
        try:
            with open(self.key_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError) as exc:
            raise AiNotesError(f"The AI-Notes key file could not be read ({type(exc).__name__}).") from exc
        if not isinstance(data, dict):
            raise AiNotesError("The AI-Notes key file is not a VaultNotes key file.")
        return data

    def _load_key(self) -> VaultKey:
        """The key for an existing vault, refused unread if it belongs to another vault."""
        if not self.key_path.is_file():
            raise AiNotesError(
                f"The AI-Notes key file is missing: {self.key_path}. Copy your saved "
                "ai-notes.vnkey back there; without it these notes cannot be read."
            )
        key_vault_id = self._public_key_fields().get("vault_id")
        if str(key_vault_id).casefold() != str(self._vault.vault_id).casefold():
            raise AiNotesError(f"{self.key_path} does not belong to the AI-Notes vault in {self.root}.")
        return self._read_key_file()

    def _read_key_file(self) -> VaultKey:
        """Decode the key file.  Only an AI-Notes key is ever used.

        A new vault in another notes folder reuses this PC's AI-Notes key; the
        name check stops another vault's key (Personal's, say) being reused
        for AI-Notes by mistake.
        """
        if self._public_key_fields().get("vault_name") != AI_VAULT_NAME:
            raise AiNotesError(f"{self.key_path} is not an AI-Notes key file, so it was not used.")
        try:
            return load_key_file(self.key_path)
        except PassphraseRequired as exc:
            raise AiNotesError("The AI-Notes key file must not have a passphrase.") from exc
        except (KeyFileError, OSError) as exc:
            raise AiNotesError(f"The AI-Notes key file could not be read ({exc}).") from exc

    def _forget_key(self) -> None:
        if self._key is not None:
            self._key.wipe()
            self._key = None

    def close(self) -> None:
        """Forget the key and every decrypted note."""
        self._vault.lock()
        self._forget_key()

    # ------------------------------------------------------------------
    # Staying current with writes from other processes
    # ------------------------------------------------------------------
    def _disk_stamp(self) -> tuple:
        entries: list[tuple[str, int, int]] = []
        for folder, tag in ((self.root, ""), (self.trash_dir, ".trash/")):
            try:
                with os.scandir(folder) as items:
                    for item in items:
                        try:
                            if not item.is_file() or not item.name.lower().endswith(NOTE_SUFFIX):
                                continue
                            stat = item.stat()
                        except OSError:
                            continue
                        entries.append((tag + item.name, int(stat.st_mtime_ns), stat.st_size))
            except OSError:
                entries.append((tag, -1, -1))
        return tuple(sorted(entries))

    def _refresh(self) -> None:
        stamp = self._disk_stamp()
        if stamp == self._stamp:
            return
        if self._key is None:
            raise AiNotesError("AI-Notes is closed.")
        self._vault.unlock(self._key)
        self._stamp = stamp

    def _wrote(self) -> None:
        self._stamp = self._disk_stamp()

    # ------------------------------------------------------------------
    # PlainStore-compatible surface used by the API and notes.py
    # ------------------------------------------------------------------
    @property
    def vault_id(self) -> str | None:
        return self._vault.vault_id

    @property
    def skipped_files(self) -> list[str]:
        """Files that could not be used: undecryptable notes and stray plain files.

        Encrypted entries are opaque ``<id>.vnote`` names (their titles were
        never decrypted).  A ``.md`` file in the folder was written by
        something that does not know AI-Notes is encrypted; it is left alone
        and shown so it is not silently ignored.
        """
        damaged = [*self._vault.damaged_files, *(f".trash/{name}" for name in self._vault.damaged_trash_files)]
        stray = [f"{path.name} (a plain file; AI-Notes only reads encrypted notes)" for path in plaintext_note_files(self.root)]
        return damaged + stray

    def fingerprint(self) -> tuple[int, int, int]:
        """(note count, newest mtime_ns, total size) of the active encrypted notes.

        Same meaning as :meth:`PlainStore.fingerprint`, so the API's index and
        note count work unchanged.
        """
        count = newest = total = 0
        try:
            with os.scandir(self.root) as entries:
                for entry in entries:
                    try:
                        if not entry.is_file() or not entry.name.lower().endswith(NOTE_SUFFIX):
                            continue
                        stat = entry.stat()
                    except OSError:
                        continue
                    count += 1
                    total += stat.st_size
                    newest = max(newest, int(stat.st_mtime_ns))
        except OSError:
            return (-1, -1, -1)
        return (count, newest, total)

    def find_by_title(self, title: str) -> Note | None:
        """The active note titled ``title`` (any case, as typed or as it would be saved)."""
        if not isinstance(title, str) or not title.strip():
            return None
        self._refresh()
        wanted = {title.strip().casefold(), sanitize_title(title).casefold()}
        for note in self._vault.iter_notes():
            if note.title.casefold() in wanted:
                return _shown(note)
        return None

    def create_note(self, title: str = "Untitled", body: str = "") -> Note:
        self._refresh()
        try:
            note = self._vault.create_note(title=title, body=body)
        finally:
            self._wrote()
        return _shown(note)

    def read_note(self, note_id: str) -> Note:
        self._refresh()
        return _shown(self._vault.read_note(note_id))

    def save_note(self, note_id: str, body: str) -> Note:
        self._refresh()
        try:
            note = self._vault.save_note(note_id, body)
        finally:
            self._wrote()
        return _shown(note)

    def rename_note(self, note_id: str, new_title: str) -> tuple[Note, str]:
        """Rename a note; a title another note already has raises ``FileExistsError``.

        That is what :meth:`PlainStore.rename_note` does, and the API turns it
        into a "collision" message instead of quietly saving "Title (2)".
        """
        self._refresh()
        clean_id = _note_id(note_id)
        clean = sanitize_title(new_title) if isinstance(new_title, str) else ""
        if not clean:
            raise ValueError("Title cannot be empty")
        for note in self._vault.iter_notes():
            if note.id != clean_id and note.title.casefold() == clean.casefold():
                raise FileExistsError(f"A note with title '{clean}' already exists")
        try:
            note, old_title = self._vault.rename_note(clean_id, clean)
        finally:
            self._wrote()
        return _shown(note), old_title

    def delete_note(self, note_id: str) -> Note:
        self._refresh()
        try:
            note = self._vault.delete_note(note_id)
        finally:
            self._wrote()
        return _shown(note)

    def restore_note(self, note_id: str) -> Note:
        self._refresh()
        try:
            note = self._vault.restore_note(note_id)
        finally:
            self._wrote()
        return _shown(note)

    def purge_note(self, note_id: str) -> str:
        self._refresh()
        try:
            return self._vault.purge_note(note_id)
        finally:
            self._wrote()

    def empty_trash(self) -> int:
        self._refresh()
        try:
            return self._vault.empty_trash()
        finally:
            self._wrote()

    def detach_note(self, note_id: str) -> None:
        """Remove an active note without putting it in the trash (a move, not a delete)."""
        self._refresh()
        clean_id = _note_id(note_id)
        self._vault._require_unlocked()  # noqa: SLF001 - same package
        path = self._vault._note_path(clean_id)  # noqa: SLF001 - same package
        if clean_id not in self._vault._notes or not path.is_file():  # noqa: SLF001
            raise FileNotFoundError(f"Note not found: {note_id}")
        try:
            path.unlink()
            self._vault._notes.pop(clean_id, None)  # noqa: SLF001 - same package
        finally:
            self._wrote()

    def list_notes(self, query: str = "", sort: str = "modified") -> list[Note]:
        self._refresh()
        return [_shown(note) for note in self._vault.list_notes(query, sort=sort)]

    def list_trash(self) -> list[Note]:
        self._refresh()
        return [_shown(note) for note in self._vault.list_trash()]

    def import_note(self, title: str, body: str, modified: float, *, trash: bool = False) -> Note:
        """Store a note exactly as given, keeping its title and modified time.

        Used by the one-time migration from plain files.  An active note whose
        title is taken gets the next free "Title (2)"; a trashed one keeps its
        title, as trash entries may share names.
        """
        self._refresh()
        vault = self._vault
        vault._require_unlocked()  # noqa: SLF001 - same package
        clean = vault._clean_title(title)  # noqa: SLF001 - same package
        if not trash:
            clean = vault._unique_title(clean)  # noqa: SLF001 - same package
        note_id = uuid.uuid4().hex
        while note_id in vault._notes or note_id in vault._trash_notes:  # noqa: SLF001
            note_id = uuid.uuid4().hex
        stamp = _iso(modified)
        note = Note(id=note_id, title=clean, body=body, modified=stamp, created=stamp)
        try:
            vault._write_note(note, vault._note_path(note_id, trash=trash))  # noqa: SLF001
            (vault._trash_notes if trash else vault._notes)[note_id] = note  # noqa: SLF001
        finally:
            self._wrote()
        return _shown(note)

    def iter_all(self) -> Iterable[tuple[bool, Note]]:
        """Every note with its exact stored text, as ``(in_trash, note)`` pairs."""
        self._refresh()
        for note in self._vault.list_notes(sort="title"):
            yield False, note
        for note in self._vault.list_trash(sort="title"):
            yield True, note

    # Aliases kept in step with PlainStore.
    create = create_note
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


class UnavailableAIStore:
    """Stands in for AI-Notes when it cannot be opened (missing key, not migrated).

    The app must still start: the space shows no notes, every change is refused
    with the reason, and :attr:`problem` becomes a visible warning.
    """

    def __init__(self, root: Path | str, problem: str) -> None:
        self.root = Path(root).resolve()
        self.trash_dir = self.root / ".trash"
        self.problem = problem
        self.skipped_files: list[str] = []

    def _refuse(self, *_args, **_kwargs):
        raise AiNotesError(f"AI-Notes is unavailable: {self.problem}")

    def fingerprint(self) -> tuple[int, int, int]:
        return (0, 0, 0)

    def list_notes(self, query: str = "", sort: str = "modified") -> list[Note]:
        return []

    def list_trash(self) -> list[Note]:
        return []

    def find_by_title(self, title: str) -> Note | None:
        return None

    def read_note(self, note_id: str) -> Note:
        raise FileNotFoundError(f"Note not found: {note_id}")

    def close(self) -> None:
        pass

    create_note = save_note = rename_note = delete_note = restore_note = _refuse
    purge_note = empty_trash = detach_note = import_note = _refuse
