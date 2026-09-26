"""Bridge API connecting the pywebview frontend to VaultNotes' engine.

Every public method here is a Bridge API endpoint (section 4.8 of the build
plan).  Two rules shape this module:

* **Errors are data.**  A method returns ``{"error": code, "message": text}``
  instead of raising, so JavaScript always gets something it can show.  The
  :func:`bridge_method` wrapper is the safety net that guarantees it.
* **Inputs are hostile.**  pywebview hands us whatever JavaScript sent, so
  space ids, note ids, titles and bodies are validated before they reach the
  stores (security rule 12f).
"""

from __future__ import annotations

import functools
import sys
import time
import urllib.parse
import uuid
import webbrowser
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Callable

from vaultnotes.autolock import AutoLock
from vaultnotes.config import Config, get_config
from vaultnotes.crypto.keyfile import KeyFileError, VaultKey, generate_key_file, load_key_file
from vaultnotes.events import emit_event
from vaultnotes.links import LinkIndex, count_links, parse_links, rename_links
from vaultnotes.models import Note
from vaultnotes.render import render_preview
from vaultnotes.storage.atomic import atomic_write
from vaultnotes.storage.plain_store import MAX_TITLE_LENGTH, PlainStore, make_snippet, sanitize_title
from vaultnotes.storage.vault_store import (
    DamagedVaultError,
    VaultLockedError,
    VaultStore,
    VaultStoreError,
    WrongKeyError,
    WrongVaultError,
)
from vaultnotes.storage.vault_store import _note_id as _vault_note_id

SPACE_DEFINITIONS = {
    "encrypted": {"name": "Encrypted", "colorVar": "--encrypted"},
    "personal": {"name": "Personal", "colorVar": "--personal"},
}

#: Longest note body the editor may send (about 20 MB of text).
MAX_BODY_LENGTH = 20_000_000

#: The only keys ``settings.json`` may hold (section 4.6).
SETTING_KEYS = frozenset({"notes_root", "vaults", "autolock_minutes", "look", "backup"})


class BridgeError(Exception):
    """An input or state problem with a user-facing code and message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _error(code: str, message: str) -> dict[str, str]:
    """The Bridge API's error shape (section 4.8)."""
    return {"error": code, "message": message}


def bridge_method(method: Callable[..., Any]) -> Callable[..., Any]:
    """Guarantee a Bridge API method returns JSON data and never raises.

    An exception escaping into pywebview reaches JavaScript as an opaque
    rejected promise, which the UI cannot turn into a message.  Specific
    exceptions keep their established codes; anything unexpected is reported as
    ``internal_error`` and written to stderr without note contents
    (security rule 5).
    """

    @functools.wraps(method)
    def wrapper(self: "Api", *args: Any, **kwargs: Any) -> Any:
        try:
            return method(self, *args, **kwargs)
        except BridgeError as exc:
            return _error(exc.code, exc.message)
        except VaultLockedError as exc:
            return _error("locked", str(exc) or "Vault is locked")
        except WrongVaultError:
            return _error("wrong_vault", "This key belongs to a different vault.")
        except WrongKeyError:
            return _error("wrong_key", "Wrong or damaged key.")
        except DamagedVaultError as exc:
            return _error("damaged", str(exc) or "That file could not be read.")
        except FileNotFoundError as exc:
            return _error("not_found", str(exc) or "Not found")
        except FileExistsError as exc:
            return _error("collision", str(exc) or "That already exists")
        except OSError as exc:
            # Full disk, no permission, unplugged drive: the note stays in the
            # editor, the user gets told (M7).
            detail = exc.strerror or exc.__class__.__name__
            return _error("io_error", f"VaultNotes could not write to disk ({detail}). Your text is still in the editor.")
        except VaultStoreError as exc:
            return _error("invalid_note", str(exc) or "Invalid vault data")
        except (ValueError, TypeError, AttributeError, KeyError, IndexError) as exc:
            return _error("invalid_input", f"That input was not accepted: {exc}")
        except Exception as exc:  # noqa: BLE001 - the bridge must never crash
            sys.stderr.write(f"VaultNotes: {method.__name__} failed: {type(exc).__name__}\n")
            return _error("internal_error", "Something unexpected happened. Your notes were not changed.")

    return wrapper


class Api:
    """Methods exposed to JavaScript through ``window.pywebview.api``.

    Vault stores are deliberately kept in this object rather than recreated on
    each request.  Their decrypted notes and keys therefore have one clear
    lifecycle: unlock, use, then lock or auto-lock.
    """

    def __init__(self, config: Config | None = None, window: Any = None) -> None:
        self.config = config or get_config()
        self.window = window
        self.plain_store = PlainStore(self.config.plain_dir)
        self.vault_stores: dict[str, VaultStore] = {}
        self._build_vault_stores()

        # One link graph per space, in memory only.  Plain is indexed at
        # startup; a vault's index is built on unlock and cleared on lock, so
        # titles from a locked vault can never leak (security rule 11).
        self.link_indexes: dict[str, LinkIndex] = {
            space_id: LinkIndex(space_id) for space_id in ("plain", *SPACE_DEFINITIONS)
        }
        self._plain_fingerprint: tuple[int, int, int] | None = None
        self._sync_plain_index()

        self._pending_key_paths: dict[str, Path] = {}
        self.autolock = AutoLock(
            self.config.get("autolock_minutes", 10),
            on_lock=self._auto_lock_expired,
        )

    def set_window(self, window: Any) -> None:
        """Store the pywebview window used for native file dialogs/events."""
        self.window = window

    def close(self) -> None:
        """Stop the timer and clear all decrypted vault state on shutdown."""
        self.autolock.stop()
        for store in self.vault_stores.values():
            store.lock()
        # Decrypted titles must not outlive the vault they came from.
        for index in self.link_indexes.values():
            index.clear()

    def _build_vault_stores(self) -> None:
        """Build providers for the configured vault folders without unlocking."""
        self.vault_stores = {}
        for space_id in SPACE_DEFINITIONS:
            # A damaged header should be reported by unlock_vault rather than
            # preventing the application shell from opening.
            try:
                root = self.config.vault_dir(space_id)
            except (KeyError, ValueError):
                root = self.config.notes_root / "vaults" / space_id
            self.vault_stores[space_id] = VaultStore(root, strict_header=False)

    # ------------------------------------------------------------------
    # Input validation (security rule 12f)
    # ------------------------------------------------------------------
    @staticmethod
    def _text(value: Any, field: str, *, allow_empty: bool = True, max_length: int | None = None) -> str:
        """Coerce a Bridge API argument to text or raise a coded BridgeError."""
        if value is None:
            if allow_empty:
                return ""
            raise BridgeError("invalid_input", f"{field} is required")
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise BridgeError("invalid_input", f"{field} must be text")
        text = value if isinstance(value, str) else str(value)
        if max_length is not None and len(text) > max_length:
            raise BridgeError(
                "too_large",
                f"{field} is too large ({len(text):,} characters). The limit is {max_length:,}.",
            )
        if not text.strip() and not allow_empty:
            raise BridgeError("invalid_input", f"{field} cannot be empty")
        return text

    def _known_space(self, space_id: Any) -> str:
        """Validate a space id, raising ``invalid_space`` for anything else."""
        text = self._text(space_id, "Space", allow_empty=False).strip()
        if text != "plain" and text not in self.vault_stores:
            raise BridgeError("invalid_space", f"Space not found: {text}")
        return text

    def _note_id_text(self, space_id: str, note_id: Any) -> str:
        """Validate a note id for a space.

        Vault ids are the opaque 32-hex file names; Plain ids are file names,
        so anything with a path separator in it is refused before it can reach
        the filesystem.
        """
        text = self._text(note_id, "Note", allow_empty=False).strip()
        if space_id == "plain":
            if any(character in text for character in ("/", "\\", ":", "\x00")) or text in {".", ".."}:
                raise BridgeError("invalid_note", "That note id is not valid")
            return text
        try:
            return _vault_note_id(text)
        except ValueError as exc:
            raise BridgeError("invalid_note", str(exc)) from exc

    def _title_text(self, title: Any) -> str:
        """Validate and clean a note title.

        The limit matches the file-name cap in ``sanitize_title``, so a title is
        either accepted as written or refused with a clear message - never
        silently truncated behind the user's back.
        """
        raw = self._text(title, "Title", allow_empty=False, max_length=MAX_TITLE_LENGTH)
        try:
            cleaned = sanitize_title(raw)
        except ValueError as exc:
            raise BridgeError("invalid_title", str(exc)) from exc
        if not cleaned:
            raise BridgeError("invalid_title", "Title cannot be empty")
        return cleaned

    def _body_text(self, body: Any) -> str:
        """Validate a note body coming from the editor."""
        return self._text(body, "Note body", allow_empty=True, max_length=MAX_BODY_LENGTH)

    # ------------------------------------------------------------------
    # Link indexes (M6)
    # ------------------------------------------------------------------
    def _index(self, space_id: str) -> LinkIndex | None:
        """The link index for a space, or ``None`` for an unknown space."""
        return self.link_indexes.get(space_id)

    def _sync_plain_index(self) -> LinkIndex:
        """Rebuild the Plain index only when the folder actually changed.

        Plain notes can be edited by other programs (the plan explicitly
        supports opening the folder in Obsidian), so the index is checked
        against a cheap directory fingerprint instead of being trusted blindly.
        """
        index = self.link_indexes["plain"]
        fingerprint = self.plain_store.fingerprint()
        if fingerprint != self._plain_fingerprint:
            # list_notes() also refreshes skipped-file warnings.
            index.build(self.plain_store.list_notes())
            self._plain_fingerprint = fingerprint
        return index

    def _sync_index(self, space_id: str) -> LinkIndex | None:
        """Return an up-to-date index for an open space.

        A locked vault always gets an empty index: its titles must not reach
        the ``[[`` suggestions, the palette or a "Linked from" bar.
        """
        index = self.link_indexes.get(space_id)
        if index is None:
            return None
        if space_id == "plain":
            return self._sync_plain_index()

        store = self.vault_stores.get(space_id)
        if store is None:
            return index
        if store.locked:
            index.clear()
            return index
        # Vault notes only change through this process, so a size mismatch is
        # the only drift possible; rebuilding then costs one pass.
        if len(index) != store.active_count:
            index.build(store.iter_notes())
        return index

    def _index_update(self, space_id: str, note: Any) -> None:
        """Add or refresh one note in its space's index."""
        index = self.link_indexes.get(space_id)
        if index is None:
            return
        index.update(note)
        if space_id == "plain":
            # We just wrote that file ourselves, so the index is current: move
            # the fingerprint forward instead of forcing a full rebuild on the
            # next call.  Only edits from *outside* the app should trigger one.
            self._plain_fingerprint = self.plain_store.fingerprint()

    def _index_remove(self, space_id: str, note_id: str) -> None:
        """Drop one note from its space's index."""
        index = self.link_indexes.get(space_id)
        if index is None:
            return
        index.remove(note_id)
        if space_id == "plain":
            self._plain_fingerprint = self.plain_store.fingerprint()

    def _backlinks_for(self, space_id: str, note: Any) -> list[dict[str, str]]:
        """The "Linked from" list for a note, from its space's index."""
        index = self._sync_index(space_id)
        if index is None:
            return []
        if not index.has(note.id):
            index.update(note)
        return index.backlinks(note.id)

    def _titles_for(self, space_id: str) -> list[str]:
        """Titles the space can link to; empty while a vault is locked."""
        index = self._sync_index(space_id)
        return index.titles() if index is not None else []

    def _warnings(self, space_id: str | None = None) -> list[str]:
        """Human-readable warnings about files that were skipped (M7).

        Plain file names are shown as-is.  Vault entries are opaque
        ``<id>.vnote`` names: a damaged encrypted file was never decrypted, so
        there is no title to show and none may be guessed (security rule 5).
        """
        warnings: list[str] = []
        for name in self.plain_store.skipped_files:
            warnings.append(f"Skipped a note that could not be read: {name}")
        for vault_space in SPACE_DEFINITIONS:
            if space_id is not None and vault_space != space_id:
                continue
            store = self.vault_stores[vault_space]
            damaged = len(store.damaged_files) + len(store.damaged_trash_files)
            if damaged:
                warnings.append(
                    f"{damaged} encrypted file(s) in {SPACE_DEFINITIONS[vault_space]['name']} "
                    "could not be decrypted and were skipped. They were left untouched."
                )
        return warnings

    # ------------------------------------------------------------------
    # General state and validation
    # ------------------------------------------------------------------
    @staticmethod
    def _invalid_space(space_id: str) -> dict[str, str]:
        return {"error": "invalid_space", "message": f"Space not found: {space_id}"}

    def _vault(self, space_id: str) -> VaultStore | None:
        return self.vault_stores.get(space_id)

    def _key_path_inside_notes_root(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.config.notes_root.resolve())
            return True
        except ValueError:
            return False

    def _validate_key_path(self, path: Path | str) -> Path:
        """Resolve a key path and reject any location inside the notes root."""
        candidate = Path(path).expanduser().resolve()
        if self._key_path_inside_notes_root(candidate):
            raise ValueError("Key files must be stored outside the VaultNotes notes folder")
        return candidate

    def _space_summary(self, space_id: str) -> dict[str, Any]:
        definition = SPACE_DEFINITIONS[space_id]
        store = self.vault_stores[space_id]
        key_path = self.config.get_vault_key_path(definition["name"])
        if store.locked:
            # Counting encrypted files reveals only file count, not titles or
            # contents, and lets the sealed UI show a useful status.
            count = store.disk_note_count() if store.has_header else 0
        else:
            # active_count reads no note bodies; get_state runs on every space
            # switch, so it must stay cheap even with 500 notes (M7).
            count = store.active_count
        result: dict[str, Any] = {
            "id": space_id,
            "name": definition["name"],
            "kind": "vault",
            "locked": store.locked,
            "colorVar": definition["colorVar"],
            "note_count": count,
            "key_path": key_path,
        }
        if store.vault_id:
            result["vault_id"] = store.vault_id
        return result

    @bridge_method
    def get_state(self) -> dict[str, Any]:
        """Return spaces, look settings, backup status, and setup state."""
        self._sync_plain_index()
        spaces = [
            {
                "id": "plain",
                "name": "Plain",
                "kind": "plain",
                "locked": False,
                "colorVar": "--plain",
                # The directory fingerprint counts files without reading them.
                "note_count": max(0, self.plain_store.fingerprint()[0]),
            },
            self._space_summary("encrypted"),
            self._space_summary("personal"),
        ]
        needs_setup = any(not self.vault_stores[space_id].has_header for space_id in SPACE_DEFINITIONS)
        backup = self.config.get("backup", {})
        return {
            "spaces": spaces,
            "look": self.config.get("look", {}),
            "autolock_minutes": self.config.get("autolock_minutes", 10),
            "last_backup": backup.get("last_backup") if isinstance(backup, Mapping) else None,
            "needs_setup": needs_setup,
            "locks_at": self.autolock.locks_at,
            "notes_root": str(self.config.notes_root),
            "warnings": self._warnings(),
        }

    def _link_count(self, space_id: str, note: Note) -> int:
        """How many ``[[links]]`` a note holds, from the index when possible."""
        index = self.link_indexes.get(space_id)
        if index is not None and index.has(note.id):
            return index.link_count(note.id)
        return count_links(note.body)

    def _note_summary(self, space_id: str, note: Note) -> dict[str, Any]:
        """One row of the note list (section 4.8)."""
        return {
            "id": note.id,
            "title": note.title,
            "snippet": make_snippet(note.body),
            "modified": note.modified,
            "link_count": self._link_count(space_id, note),
        }

    def _note_result(
        self,
        space_id: str,
        note: Note,
        backlinks: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """One full note as the Bridge API returns it (section 4.8)."""
        return {
            "id": note.id,
            "title": note.title,
            "body": note.body,
            "modified": note.modified,
            "created": note.created,
            "tags": list(note.tags),
            "snippet": make_snippet(note.body),
            "link_count": self._link_count(space_id, note),
            "backlinks": backlinks if backlinks is not None else [],
        }

    # ------------------------------------------------------------------
    # Note CRUD
    # ------------------------------------------------------------------
    @bridge_method
    def list_notes(
        self,
        space_id: str,
        query: str = "",
        sort: str = "modified",
    ) -> list[dict[str, Any]] | dict[str, str]:
        """List note summaries in one space; locked vaults return no titles."""
        space = self._known_space(space_id)
        text_query = self._text(query, "Search text", max_length=1024)
        text_sort = self._text(sort, "Sort") or "modified"

        # Keeps link counts honest and a locked vault's index empty.
        self._sync_index(space)
        if space == "plain":
            notes = self.plain_store.list_notes(query=text_query, sort=text_sort)
            return [self._note_summary(space, note) for note in notes]

        store = self.vault_stores[space]
        if store.locked:
            return []
        return [
            self._note_summary(space, note)
            for note in store.list_notes(query=text_query, sort=text_sort)
        ]

    @bridge_method
    def open_note(self, space_id: str, note_id: str) -> dict[str, Any]:
        """Retrieve a full note and its "Linked from" list from the same space.

        Backlinks come from the space's in-memory link index, so opening a note
        stays cheap no matter how many notes the space holds (M6/M7).  Links
        never cross spaces: a Plain note only ever sees Plain backlinks.
        """
        space = self._known_space(space_id)
        clean_id = self._note_id_text(space, note_id)

        if space == "plain":
            note = self._read_plain_note(clean_id)
        else:
            store = self.vault_stores[space]
            if store.locked:
                return _error("locked", "Vault is locked")
            try:
                note = store.read_note(clean_id)
            except FileNotFoundError:
                return _error("not_found", f"Note not found: {clean_id}")

        return self._note_result(space, note, self._backlinks_for(space, note))

    def _read_plain_note(self, note_id: str) -> Note:
        """Read one Plain note, turning a missing file into ``not_found``."""
        try:
            return self.plain_store.read_note(note_id)
        except FileNotFoundError:
            raise BridgeError("not_found", f"Note not found: {note_id}") from None
        except OSError as exc:
            detail = exc.strerror or exc.__class__.__name__
            raise BridgeError("io_error", f"That note could not be read ({detail}).") from None

    @bridge_method
    def create_note(self, space_id: str, title: str = "Untitled") -> dict[str, Any]:
        """Create a note in Plain or an unlocked encrypted vault.

        Titles are unique inside a space: a collision gets `` (2)``, `` (3)``
        and so on rather than an error or an overwritten note.
        """
        space = self._known_space(space_id)
        clean_title = self._title_text(title)

        if space == "plain":
            note = self.plain_store.create_note(title=clean_title)
        else:
            store = self.vault_stores[space]
            if store.locked:
                return _error("locked", "Unlock vault first")
            note = store.create_note(title=clean_title)

        self._index_update(space, note)
        return self._note_result(space, note, self._backlinks_for(space, note))

    @bridge_method
    def save_note(self, space_id: str, note_id: str, body: str) -> dict[str, Any]:
        """Atomically save a note body, encrypting vault notes first.

        A write that fails (full disk, no permission, vanished USB drive) or an
        encrypted file that does not survive its read-back check returns an
        error; the editor keeps the text so nothing is lost (M7).
        """
        space = self._known_space(space_id)
        clean_id = self._note_id_text(space, note_id)
        text_body = self._body_text(body)

        if space == "plain":
            note = self.plain_store.save_note(clean_id, text_body)
        else:
            store = self.vault_stores[space]
            if store.locked:
                return _error("locked", "Vault is locked")
            try:
                note = store.save_note(clean_id, text_body)
            except FileNotFoundError:
                return _error("not_found", f"Note not found: {clean_id}")
            except DamagedVaultError:
                # The read-back check failed: the bytes on disk are suspect,
                # but VaultStore kept the newest text in memory, so index that
                # and tell the user instead of pretending it saved (M7).
                try:
                    self._index_update(space, store.read_note(clean_id))
                except (FileNotFoundError, VaultStoreError):
                    pass
                return _error(
                    "save_failed",
                    "The note was written but could not be read back. Your text is still in the "
                    "editor; check the disk and save again.",
                )

        self._index_update(space, note)
        if self._any_vault_unlocked():
            self.autolock.touch()
        return {"modified": note.modified}

    @bridge_method
    def rename_note(
        self,
        space_id: str,
        note_id: str,
        new_title: str,
        update_links: bool = True,
    ) -> dict[str, Any]:
        """Rename a note and optionally update the links pointing at it.

        The notes to rewrite come from the link index, so a rename touches only
        the notes that actually link here instead of scanning the whole space.
        """
        space = self._known_space(space_id)
        clean_id = self._note_id_text(space, note_id)
        clean_title = self._title_text(new_title)
        rewrite_links = bool(update_links)

        index = self._sync_index(space)
        # Ask the index *before* the rename: a Plain note's id is its title, so
        # the old id stops existing as soon as the file is renamed.
        source_ids: list[str] = index.sources_linking_to(clean_id) if index is not None else []

        if space == "plain":
            try:
                note, old_title = self.plain_store.rename_note(clean_id, clean_title)
            except FileExistsError:
                return _error("collision", f"A note titled '{clean_title}' already exists")
            except FileNotFoundError:
                return _error("not_found", f"Note not found: {clean_id}")
        else:
            store = self.vault_stores[space]
            if store.locked:
                return _error("locked", "Vault is locked")
            # A vault keeps its opaque id; a title collision becomes " (2)".
            note, old_title = store.rename_note(clean_id, clean_title)

        self._index_remove(space, clean_id)
        self._index_update(space, note)

        links_updated = 0
        if rewrite_links:
            links_updated = self._rewrite_links_to(
                space, source_ids, old_title, note.title, exclude_id=note.id
            )
        return {"title": note.title, "id": note.id, "links_updated": links_updated}

    def _rewrite_links_to(
        self,
        space_id: str,
        source_ids: Iterable[str],
        old_title: str,
        new_title: str,
        exclude_id: str = "",
    ) -> int:
        """Point the links in ``source_ids`` at ``new_title``; return the count.

        A note that vanished or cannot be written is skipped rather than
        aborting the whole rename: the rename itself already succeeded and must
        not be reported as failed because of one unwritable neighbour (M7).
        """
        if not old_title or not new_title or old_title == new_title:
            return 0
        store = None if space_id == "plain" else self.vault_stores.get(space_id)
        if space_id != "plain" and (store is None or store.locked):
            return 0

        updated = 0
        for source_id in source_ids:
            if not source_id or source_id == exclude_id:
                continue
            try:
                source = (
                    self._read_plain_note(source_id)
                    if space_id == "plain"
                    else store.read_note(source_id)
                )
                new_body, count = rename_links(source.body, old_title, new_title)
                if not count:
                    continue
                saved = (
                    self.plain_store.save_note(source_id, new_body)
                    if space_id == "plain"
                    else store.save_note(source_id, new_body)
                )
            except (BridgeError, FileNotFoundError, OSError, ValueError, VaultStoreError):
                continue
            self._index_update(space_id, saved)
            updated += count
        return updated

    @bridge_method
    def count_links_to(self, space_id: str, note_id: str) -> dict[str, int]:
        """Count the notes linking here, without crossing space boundaries.

        Used by the "Update N links?" rename prompt and the move warning.  A
        locked vault reports 0 rather than admitting anything about its notes.
        """
        space = self._known_space(space_id)
        clean_id = self._note_id_text(space, note_id)
        index = self._sync_index(space)
        if index is None:
            return {"count": 0}
        if space != "plain":
            if self.vault_stores[space].locked:
                return {"count": 0}
            if not index.has(clean_id):
                return {"count": 0}
        return {"count": len(index.backlinks(clean_id))}

    @bridge_method
    def delete_note(self, space_id: str, note_id: str) -> dict[str, Any]:
        """Move a note into that space's trash, still encrypted for vaults."""
        space = self._known_space(space_id)
        clean_id = self._note_id_text(space, note_id)

        if space == "plain":
            note = self.plain_store.delete_note(clean_id)
        else:
            store = self.vault_stores[space]
            if store.locked:
                return _error("locked", "Vault is locked")
            note = store.delete_note(clean_id)

        # Trashed notes are not linkable, so they leave the index.  Links to
        # them turn into "missing" links, exactly as in Obsidian.
        self._index_remove(space, clean_id)
        return {
            "ok": True,
            "deleted": {"id": note.id, "title": note.title, "modified": note.modified},
        }

    @bridge_method
    def restore_note(self, space_id: str, note_id: str) -> dict[str, Any]:
        """Restore a note from that space's trash."""
        space = self._known_space(space_id)
        clean_id = self._note_id_text(space, note_id)

        if space == "plain":
            note = self.plain_store.restore_note(clean_id)
        else:
            store = self.vault_stores[space]
            if store.locked:
                return _error("locked", "Vault is locked")
            note = store.restore_note(clean_id)

        # Links written while the note was in the trash start working again.
        self._index_update(space, note)
        return {
            "ok": True,
            "note": {"id": note.id, "title": note.title, "modified": note.modified},
        }

    @bridge_method
    def list_trash(self, space_id: str) -> list[dict[str, str]] | dict[str, str]:
        """List trash entries without ever returning locked vault titles."""
        space = self._known_space(space_id)
        if space == "plain":
            return [
                {"id": note.id, "title": note.title, "modified": note.modified}
                for note in self.plain_store.list_trash()
            ]
        store = self.vault_stores[space]
        if store.locked:
            return []
        return [
            {"id": note.id, "title": note.title, "modified": note.modified}
            for note in store.list_trash()
        ]

    def _read_any_note(self, space_id: str, note_id: str) -> Any:
        """Read a note from Plain or an unlocked vault.

        Raises VaultLockedError, FileNotFoundError, or ValueError for a bad
        vault note id.
        """
        if space_id == "plain":
            return self.plain_store.read_note(note_id)
        store = self._vault(space_id)
        if store is None:  # pragma: no cover - callers validate the space first
            raise FileNotFoundError(f"Space not found: {space_id}")
        return store.read_note(note_id)

    def _count_broken_links_on_move(self, space_id: str, note: Note) -> int:
        """Count same-space links that break when a note leaves its space.

        Links never cross spaces, so moving a note breaks both the notes that
        link to it and the note's own links to its old neighbours.  A note
        linking to itself keeps working after the move and is not counted.
        """
        index = self._sync_index(space_id)
        if index is None:
            return 0
        if not index.has(note.id):
            index.update(note)
        incoming = len(index.backlinks(note.id))
        outgoing = 0
        for link in index.links_of(note.id):
            target = index.resolve(link.target)
            if target is not None and target != note.id:
                outgoing += 1
        return incoming + outgoing

    @bridge_method
    def move_note(self, space_id: str, note_id: str, target_space_id: str) -> dict[str, Any]:
        """Move a note between spaces, encrypting or decrypting as needed.

        The note is created in the target space first; the source copy is
        only removed afterwards, so a failure never loses the note. Returns
        ``{new_id, title, broken_links}`` where ``broken_links`` counts the
        same-space links that stop working because of the move.
        """
        space = self._known_space(space_id)
        target = self._known_space(target_space_id)
        clean_id = self._note_id_text(space, note_id)
        if space == target:
            return _error("same_space", "The note is already in that space")
        source_store = self._vault(space)
        if source_store is not None and source_store.locked:
            return _error("locked", "Unlock the source vault first")
        target_store = self._vault(target)
        if target_store is not None and target_store.locked:
            return _error("locked", f"Unlock {SPACE_DEFINITIONS[target]['name']} first")

        try:
            source = self._read_any_note(space, clean_id)
        except FileNotFoundError:
            return _error("not_found", f"Note not found: {clean_id}")
        except ValueError as exc:
            return _error("invalid_note", str(exc))
        except VaultLockedError:
            return {"error": "locked", "message": "Vault is locked"}

        broken_links = self._count_broken_links_on_move(space, source)

        # Create the target copy first. A second save preserves the exact
        # body even when the source body is empty (creation would otherwise
        # insert a default "# Title" heading).
        try:
            if target == "plain":
                created = self.plain_store.create_note(title=source.title, body=source.body)
                if created.body != source.body:
                    created = self.plain_store.save_note(created.id, source.body)
            else:
                assert target_store is not None  # narrowed by the checks above
                created = target_store.create_note(title=source.title, body=source.body)
                if created.body != source.body:
                    created = target_store.save_note(created.id, source.body)
        except (ValueError, OSError, VaultStoreError) as exc:
            return _error("move_failed", f"Could not move the note: {exc}")

        # The target copy exists; now remove the source without using trash,
        # because a move is not a delete.
        try:
            if space == "plain":
                source_path = self.plain_store._resolve_note_path(clean_id)  # noqa: SLF001 - same package
                if not source_path.is_file():
                    raise FileNotFoundError(f"Note not found: {clean_id}")
                source_path.unlink()
            else:
                assert source_store is not None  # narrowed by the checks above
                source_store._require_unlocked()  # noqa: SLF001 - same package
                source_store._notes.pop(source.id, None)  # noqa: SLF001 - same package
                source_file = source_store._note_path(source.id)  # noqa: SLF001 - same package
                if source_file.is_file():
                    source_file.unlink()
        except (FileNotFoundError, ValueError, OSError, VaultStoreError) as exc:
            # The note now lives in the target space; report the leftover
            # source copy instead of pretending the move was clean.
            self._index_update(target, created)
            return {
                "error": "move_partial",
                "message": f"Note copied to the new space, but the original could not be removed: {exc}",
                "new_id": created.id,
                "title": created.title,
                "broken_links": broken_links,
            }

        # Links follow the note into its new space and stop resolving in the
        # old one, which is exactly what "broken_links" warned about.
        self._index_remove(space, source.id)
        self._index_update(target, created)
        if self._any_vault_unlocked():
            self.autolock.touch()
        return {"new_id": created.id, "title": created.title, "broken_links": broken_links}

    @bridge_method
    def purge_note(self, space_id: str, note_id: str) -> dict[str, Any]:
        """Permanently delete a trashed note (cannot be undone)."""
        space = self._known_space(space_id)
        clean_id = self._note_id_text(space, note_id)
        if space == "plain":
            try:
                title = self.plain_store.purge_note(clean_id)
            except FileNotFoundError:
                return _error("not_found", f"Note not found in trash: {clean_id}")
        else:
            store = self.vault_stores[space]
            if store.locked:
                return _error("locked", "Vault is locked")
            try:
                title = store.purge_note(clean_id)
            except FileNotFoundError:
                return _error("not_found", f"Note not found in trash: {clean_id}")
        return {"ok": True, "title": title}

    @bridge_method
    def empty_trash(self, space_id: str) -> dict[str, Any]:
        """Permanently delete every trashed note in one space."""
        space = self._known_space(space_id)
        if space == "plain":
            return {"ok": True, "purged": self.plain_store.empty_trash()}
        store = self.vault_stores[space]
        if store.locked:
            return _error("locked", "Vault is locked")
        return {"ok": True, "purged": store.empty_trash()}

    # ------------------------------------------------------------------
    # Markdown import and export (.md files)
    # ------------------------------------------------------------------
    @bridge_method
    def import_notes(
        self,
        space_id: str,
        file_paths: list[Path | str] | None = None,
    ) -> dict[str, Any]:
        """Import ``.md`` files into Plain or an unlocked vault.

        The frontend calls this without paths, causing Python to open the
        native file picker itself (the frontend never sends file paths).
        ``file_paths`` exists only for headless callers and tests.
        """
        space = self._known_space(space_id)
        store = self._vault(space)
        if store is not None and store.locked:
            return _error("locked", "Unlock the vault first")

        if self.window is not None or file_paths is None:
            # In the desktop app the picker decides which files are imported.
            selected = self._choose_import_files()
            if not selected:
                return _error("cancelled", "No files were selected.")
            candidates = selected
        elif isinstance(file_paths, (str, Path)):
            candidates = [Path(file_paths).expanduser()]
        elif isinstance(file_paths, (list, tuple)):
            candidates = [Path(each).expanduser() for each in file_paths if isinstance(each, (str, Path))]
        else:
            return _error("invalid_input", "Import needs a list of file paths")
        if not candidates:
            return _error("cancelled", "No files were selected.")

        imported: list[dict[str, str]] = []
        skipped: list[dict[str, str]] = []
        for candidate in candidates:
            path = Path(candidate).expanduser()
            if path.suffix.lower() != ".md":
                skipped.append({"name": path.name, "reason": "not a .md file"})
                continue
            if not path.is_file():
                skipped.append({"name": path.name, "reason": "file not found"})
                continue
            try:
                if path.stat().st_size > 5 * 1024 * 1024:
                    skipped.append({"name": path.name, "reason": "larger than 5 MB"})
                    continue
                body = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                skipped.append({"name": path.name, "reason": str(exc)})
                continue
            try:
                title = sanitize_title(path.stem)
            except ValueError as exc:
                skipped.append({"name": path.name, "reason": str(exc)})
                continue
            title = title or path.stem
            if len(body) > MAX_BODY_LENGTH:
                skipped.append({"name": path.name, "reason": "larger than the note size limit"})
                continue
            try:
                if space == "plain":
                    note = self.plain_store.create_note(title=title, body=body)
                    if note.body != body:
                        note = self.plain_store.save_note(note.id, body)
                else:
                    assert store is not None  # narrowed by the checks above
                    note = store.create_note(title=title, body=body)
                    if note.body != body:
                        note = store.save_note(note.id, body)
            except (ValueError, OSError, VaultStoreError) as exc:
                skipped.append({"name": path.name, "reason": str(exc)})
                continue
            # Imported notes join the link graph immediately, so links written
            # to them by existing notes start working (M6).
            self._index_update(space, note)
            imported.append({"id": note.id, "title": note.title})

        if self._any_vault_unlocked():
            self.autolock.touch()
        return {"ok": True, "imported": imported, "skipped": skipped}

    @bridge_method
    def export_note(
        self,
        space_id: str,
        note_id: str,
        dest_path: Path | str | None = None,
    ) -> dict[str, Any]:
        """Export one note to a ``.md`` file chosen by a native Save dialog.

        The frontend warns before calling this for vault notes ("This saves
        an unencrypted copy."). ``dest_path`` exists only for headless
        callers and tests.
        """
        space = self._known_space(space_id)
        clean_id = self._note_id_text(space, note_id)
        store = self._vault(space)
        if store is not None and store.locked:
            return _error("locked", "Vault is locked")
        try:
            note = self._read_any_note(space, clean_id)
        except FileNotFoundError:
            return _error("not_found", f"Note not found: {clean_id}")
        except ValueError as exc:
            return _error("invalid_note", str(exc))
        except VaultLockedError:
            return _error("locked", "Vault is locked")

        suggested = f"{sanitize_title(note.title)}.md"
        if self.window is not None or dest_path is None:
            # In the desktop app the user picks the destination themselves.
            selected = self._choose_export_file(suggested)
            if selected is None:
                return _error("cancelled", "Export was cancelled.")
            destination = selected
        else:
            destination = Path(str(dest_path)).expanduser()
            if destination.is_dir():
                destination = destination / suggested
        if destination.suffix.lower() != ".md":
            destination = destination.with_name(destination.name + ".md")
        try:
            atomic_write(destination, note.body.encode("utf-8"))
        except OSError as exc:
            detail = exc.strerror or exc.__class__.__name__
            return _error("export_failed", f"Could not write the file: {detail}")
        return {"ok": True, "name": destination.name}

    # ------------------------------------------------------------------
    # Markdown links and preview
    # ------------------------------------------------------------------
    @bridge_method
    def render_preview(self, space_id: str, body: str) -> str:
        """Render Markdown, resolving links against same-space titles only.

        A locked vault contributes no titles, so its notes can never be
        discovered through a preview (security rule 11).
        """
        space = self._known_space(space_id)
        text_body = self._body_text(body)
        return render_preview(text_body, titles=self._titles_for(space))

    @bridge_method
    def list_titles(self, space_id: str) -> list[str]:
        """Return the ``[[`` suggestion titles for an open space only."""
        space = self._known_space(space_id)
        return self._titles_for(space)

    @bridge_method
    def note_links(self, space_id: str, note_id: str) -> dict[str, Any]:
        """Backlinks and outgoing links for one note (the "Linked from" bar).

        Not in the original Bridge API table: the frontend already gets
        backlinks from ``open_note``, and this endpoint lets it refresh just
        the link bar after a rename or a move without re-reading the note.
        """
        space = self._known_space(space_id)
        clean_id = self._note_id_text(space, note_id)
        index = self._sync_index(space)
        if index is None or not index.has(clean_id):
            return {"backlinks": [], "outgoing": [], "link_count": 0}
        return {
            "backlinks": index.backlinks(clean_id),
            "outgoing": index.outgoing(clean_id),
            "link_count": index.link_count(clean_id),
        }

    # ------------------------------------------------------------------
    # Vault creation, native key dialogs, unlock and lock
    # ------------------------------------------------------------------
    def _choose_file(self, save: bool, suggested_name: str) -> Path | None:
        """Open a native key-file dialog, never a browser file input."""
        if self.window is None:
            return None
        try:
            import webview  # type: ignore

            dialog_type = webview.SAVE_DIALOG if save else webview.OPEN_DIALOG
            kwargs: dict[str, Any] = {
                "allow_multiple": False,
                "file_types": ("VaultNotes key (*.vnkey)", "*.vnkey"),
            }
            if save:
                kwargs["save_filename"] = suggested_name
            selected = self.window.create_file_dialog(dialog_type, **kwargs)
            if isinstance(selected, (list, tuple)):
                selected = selected[0] if selected else None
            if not selected:
                return None
            return Path(str(selected)).expanduser().resolve()
        except Exception:
            # Native dialog failures are presented as a normal user-facing
            # error by the caller rather than crashing the bridge.
            return None

    def _choose_import_files(self) -> list[Path]:
        """Open a native multi-select picker for ``.md`` files to import."""
        if self.window is None:
            return []
        try:
            import webview  # type: ignore

            selected = self.window.create_file_dialog(
                webview.OPEN_DIALOG,
                allow_multiple=True,
                file_types=("Markdown (*.md)", "*.md"),
            )
            if not selected:
                return []
            if isinstance(selected, (str, Path)):
                selected = [selected]
            return [Path(str(each)).expanduser() for each in selected]
        except Exception:
            return []

    def _choose_export_file(self, suggested_name: str) -> Path | None:
        """Open a native Save dialog for exporting one ``.md`` file."""
        if self.window is None:
            return None
        try:
            import webview  # type: ignore

            selected = self.window.create_file_dialog(
                webview.SAVE_DIALOG,
                allow_multiple=False,
                file_types=("Markdown (*.md)", "*.md"),
                save_filename=suggested_name,
            )
            if isinstance(selected, (list, tuple)):
                selected = selected[0] if selected else None
            if not selected:
                return None
            return Path(str(selected)).expanduser()
        except Exception:
            return None

    def _choose_folder(self) -> Path | None:
        """Open a native folder picker and return its server-side path."""
        if self.window is None:
            return None
        try:
            import webview  # type: ignore

            dialog_type = getattr(webview, "FOLDER_DIALOG", None)
            if dialog_type is None:
                return None
            selected = self.window.create_file_dialog(dialog_type, allow_multiple=False)
            if isinstance(selected, (list, tuple)):
                selected = selected[0] if selected else None
            if not selected:
                return None
            return Path(str(selected)).expanduser().resolve()
        except Exception:
            return None

    @bridge_method
    def choose_notes_folder(self, folder_path: Path | str | None = None) -> dict[str, Any]:
        """Choose and persist the notes root through a native folder dialog.

        ``folder_path`` is honoured only when no window exists (tests and other
        headless callers).  In the desktop app the native picker decides, so a
        crafted bridge call cannot aim the notes folder at an arbitrary path
        (security rule 12f: the frontend never sends file paths).
        """
        if self.window is not None or folder_path is None:
            selected = self._choose_folder()
        else:
            # Headless callers (tests, scripts) may name a folder, but it has
            # to be a plain absolute path: a number, an empty string or a
            # relative path must never decide where the notes live.
            if not isinstance(folder_path, (str, Path)):
                return {"error": "invalid_folder", "message": "That is not a folder path."}
            candidate = Path(str(folder_path)).expanduser()
            if not str(folder_path).strip() or not candidate.is_absolute():
                return {
                    "error": "invalid_folder",
                    "message": "Choose the notes folder with the folder dialog.",
                }
            selected = candidate.resolve()
        if selected is None:
            return {"error": "cancelled", "message": "No notes folder was selected."}
        if selected == selected.parent:
            return {"error": "invalid_folder", "message": "Choose a folder below a drive or home directory."}

        # A remembered key must not become unsafe merely because the notes
        # root changed.
        for vault in self.config.get("vaults", []):
            raw_key_path = vault.get("key_path", "") if isinstance(vault, dict) else ""
            if raw_key_path:
                try:
                    Path(str(raw_key_path)).expanduser().resolve().relative_to(selected)
                except ValueError:
                    pass
                else:
                    return {
                        "error": "key_inside_notes",
                        "message": "The selected notes folder contains a key file. Choose another folder.",
                    }

        # Pre-flight: the folder has to be creatable before anything else
        # changes.  A removed USB key, a full disk or a permission error must
        # leave the current notes folder exactly as it was (M7).
        try:
            selected.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            reason = exc.strerror or exc.__class__.__name__
            return {
                "error": "io_error",
                "message": f"VaultNotes cannot use that folder ({reason}). "
                           "Your current notes folder was kept.",
            }

        self._reset_notes_root(str(selected))
        return {"ok": True, "name": selected.name or str(selected), "warnings": self._warnings()}

    def _reset_notes_root(self, notes_root: str) -> None:
        """Re-point every store at a new notes folder and forget all links.

        Locking the vaults wipes their decrypted notes, so their link indexes
        must go with them: an index outliving its vault would keep serving
        titles from a locked space (security rule 11).
        """
        for store in self.vault_stores.values():
            store.lock()
        self.autolock.clear()
        previous_root = self.config.data.get("notes_root")
        try:
            self.config.update({"notes_root": notes_root})
        except Exception:
            # Never leave the app pointing at a folder it could not create.
            self.config.data["notes_root"] = previous_root
            self.config.save()
            raise
        self.plain_store = PlainStore(self.config.plain_dir)
        self._pending_key_paths.clear()
        self._build_vault_stores()
        for index in self.link_indexes.values():
            index.clear()
        self._plain_fingerprint = None
        self._sync_plain_index()

    @bridge_method
    def choose_key_file(self, space_id: str) -> dict[str, Any]:
        """Let the native UI choose a key and keep its path server-side."""
        space = self._known_space(space_id)
        if space == "plain":
            return _error("invalid_space", "Plain is not encrypted and has no key file")
        selected = self._choose_file(False, f"{space}.vnkey")
        if selected is None:
            return _error("key_not_found", "No key file was selected.")
        try:
            selected = self._validate_key_path(selected)
        except ValueError as exc:
            return _error("key_inside_notes", str(exc))
        self._pending_key_paths[space] = selected
        return {"ok": True, "name": selected.name}

    @bridge_method
    def create_vault(self, space_id: str, key_path: Path | str | None = None) -> dict[str, Any]:
        """Create one configured vault and generate its external key file.

        ``key_path`` is optional for Python callers/tests.  The frontend calls
        this without a path, causing a native Save dialog.  In a headless
        environment a deterministic location next to ``settings.json`` is
        used; it is still outside the notes root.
        """
        definition = SPACE_DEFINITIONS.get(space_id)
        store = self._vault(space_id)
        if definition is None or store is None:
            return self._invalid_space(space_id)
        if store.has_header:
            return {"error": "already_exists", "message": f"{definition['name']} already exists"}
        if store.header_error is not None:
            return {"error": "damaged", "message": str(store.header_error)}

        if self.window is not None:
            # The desktop app always asks with a native Save dialog.
            selected = self._choose_file(True, f"{space_id}.vnkey")
            if selected is None:
                return _error("cancelled", "Vault setup was cancelled.")
        elif key_path is None:
            selected = self.config.settings_file.parent / "keys" / f"{space_id}.vnkey"
        else:
            selected = Path(str(key_path))
        try:
            selected = self._validate_key_path(selected)
        except ValueError as exc:
            return {"error": "key_inside_notes", "message": str(exc)}

        if selected.suffix.lower() != ".vnkey":
            selected = selected.with_name(selected.name + ".vnkey")
        if selected.exists():
            return {"error": "key_exists", "message": "That key file already exists. Choose a new file name."}
        key: VaultKey | None = None
        try:
            vault_id = str(uuid.uuid4())
            key = generate_key_file(selected, vault_id, definition["name"])
            header = store.create(key, vault_id=vault_id, name=definition["name"])
            self.config.set_vault_key_path(definition["name"], selected)
            # Make the in-memory object reflect the newly created header.
            store._header_error = None  # noqa: SLF001 - controlled lifecycle update
            return {
                "ok": True,
                "space_id": space_id,
                "vault_id": header["vault_id"],
                "key_path": str(selected),
                "key_name": selected.name,
            }
        except (OSError, KeyFileError, VaultStoreError, ValueError) as exc:
            return {"error": "damaged", "message": str(exc)}
        finally:
            if key is not None:
                key.wipe()

    @bridge_method
    def initialize_vaults(self, key_paths: Mapping[str, Path | str] | None = None) -> dict[str, Any]:
        """Create the built-in Encrypted and Personal vaults if needed."""
        paths = key_paths or {}
        created: list[str] = []
        for space_id in SPACE_DEFINITIONS:
            store = self.vault_stores[space_id]
            if store.has_header:
                continue
            result = self.create_vault(
                space_id,
                paths.get(space_id, paths.get(SPACE_DEFINITIONS[space_id]["name"])),
            )
            if result.get("error"):
                return {"error": result["error"], "message": result.get("message", ""), "created": created}
            created.append(space_id)
        return {"ok": True, "created": created}

    @bridge_method
    def unlock_vault(self, space_id: str) -> dict[str, Any]:
        """Load an external key, verify the vault, and decrypt notes in memory.

        Files that cannot be decrypted are skipped, counted and reported in
        ``warnings`` rather than locking the user out of the whole vault; they
        are never overwritten (M7, security rule 8).
        """
        space = self._known_space(space_id)
        if space == "plain":
            return _error("invalid_space", "Plain is always open and cannot be unlocked")
        store = self.vault_stores[space]
        if store.header_error is not None:
            return {"error": "damaged", "message": str(store.header_error)}
        if not store.has_header:
            return {"error": "not_initialized", "message": "Create this vault before unlocking it."}

        path = self._pending_key_paths.pop(space_id, None)
        if path is None:
            configured_name = SPACE_DEFINITIONS[space_id]["name"]
            configured_path = self.config.get_vault_key_path(configured_name)
            path = Path(configured_path).expanduser() if configured_path else None
        if path is None or not str(path):
            path = self._choose_file(False, f"{space_id}.vnkey")
        if path is None:
            return {"error": "key_not_found", "message": "Choose the key file for this vault."}
        try:
            path = self._validate_key_path(path)
        except ValueError as exc:
            return {"error": "key_inside_notes", "message": str(exc)}
        if not path.is_file():
            return {"error": "key_not_found", "message": "The selected key file was not found."}

        key: VaultKey | None = None
        try:
            key = load_key_file(path)
            count = store.unlock(key)
        except FileNotFoundError:
            return {"error": "key_not_found", "message": "The selected key file was not found."}
        except WrongVaultError:
            return {"error": "wrong_vault", "message": "This key belongs to a different vault."}
        except WrongKeyError:
            return {"error": "wrong_key", "message": "Wrong or damaged key."}
        except (KeyFileError, DamagedVaultError, VaultStoreError, ValueError) as exc:
            return {"error": "damaged", "message": str(exc)}
        finally:
            # VaultStore has copied the key into its own mutable buffer before
            # returning.  The temporary key-file object can now be cleared.
            if key is not None:
                key.wipe()

        # Remember only the location, never the key contents.  The setting is
        # written after a successful verifier/decryption check.
        self.config.set_vault_key_path(SPACE_DEFINITIONS[space]["name"], path)

        # The vault's link graph exists only while it is unlocked (M6).
        index = self.link_indexes[space]
        index.build(store.iter_notes())

        locks_at = self.autolock.touch()
        damaged = len(store.damaged_files) + len(store.damaged_trash_files)
        # The caller reports these; they name opaque ids only, so a damaged
        # encrypted file reveals nothing about the note that could not be read
        # (security rule 5).
        warnings = self._warnings(space)
        return {
            "ok": True,
            "count": count,
            "locks_at": locks_at,
            "damaged": damaged,
            "warnings": warnings,
        }

    @bridge_method
    def lock_vault(self, space_id: str) -> dict[str, Any]:
        """Lock one vault and clear its key, notes and link index."""
        space = self._known_space(space_id)
        store = self._vault(space)
        if store is None:
            return _error("invalid_space", f"Space not found: {space}")
        store.lock()
        # Titles from a locked vault must vanish from suggestions, the palette
        # and every "Linked from" bar (security rule 11).
        self.link_indexes[space].clear()
        if not self._any_vault_unlocked():
            self.autolock.clear()
        emit_event(self.window, "vault_locked", {"space_id": space})
        return {"ok": True, "space_id": space}

    def _lock_all(self, emit: bool = True) -> list[str]:
        locked: list[str] = []
        for space_id, store in self.vault_stores.items():
            if not store.locked:
                store.lock()
                locked.append(space_id)
            self.link_indexes[space_id].clear()
        self.autolock.clear()
        if emit and locked:
            emit_event(self.window, "vault_locked", {"space_ids": locked})
        return locked

    @bridge_method
    def lock_all(self) -> dict[str, Any]:
        """Lock every open vault."""
        return {"ok": True, "locked_spaces": self._lock_all(emit=True)}

    def _auto_lock_expired(self) -> None:
        """AutoLock callback; never touches the frontend synchronously."""
        self._lock_all(emit=True)

    def _any_vault_unlocked(self) -> bool:
        return any(not store.locked for store in self.vault_stores.values())

    @bridge_method
    def touch(self) -> dict[str, int | None]:
        """Reset the Python timer after user activity."""
        if not self._any_vault_unlocked():
            self.autolock.clear()
            # Keep the Bridge API's historical integer shape even though no
            # vault is armed; the countdown UI ignores it while all vaults are
            # locked.
            deadline = int(time.time() * 1000) + int(float(self.config.get("autolock_minutes", 10)) * 60_000)
            return {"locks_at": deadline}
        return {"locks_at": self.autolock.touch()}

    # ------------------------------------------------------------------
    # Remaining Bridge API methods
    # ------------------------------------------------------------------
    @bridge_method
    def open_external(self, url: str) -> dict[str, Any]:
        """Open only http/https/mailto URLs in the user's browser.

        Checked again here even though the preview click handler already
        filters: a crafted bridge call must not reach ``file:`` or
        ``javascript:`` (section 4.7).
        """
        if not isinstance(url, str) or not url.strip():
            return _error("invalid_url", "Blocked unsafe URL scheme")
        candidate = url.strip()
        if len(candidate) > 2048 or any(ch in candidate for ch in "\x00\n\r"):
            return _error("invalid_url", "Blocked unsafe URL scheme")
        try:
            parsed = urllib.parse.urlparse(candidate)
            if parsed.scheme.lower() in ("http", "https", "mailto"):
                webbrowser.open(candidate)
                return {"ok": True}
        except ValueError:
            pass
        return _error("invalid_url", "Blocked unsafe URL scheme")

    @bridge_method
    def get_settings(self) -> dict[str, Any]:
        """Return a copy of persisted settings."""
        return self.config.data

    @bridge_method
    def update_settings(self, changes: dict[str, Any]) -> dict[str, Any]:
        """Update settings and keep the Python auto-lock timer in sync.

        Key paths are validated here as well as during unlock/setup so a
        crafted bridge call cannot place a key file inside the notes root.
        """
        if not isinstance(changes, dict):
            return {"error": "invalid_settings", "message": "Settings changes must be an object"}

        # Only the documented settings keys may be written (section 4.6), so a
        # crafted bridge call cannot persist arbitrary junk into settings.json.
        unknown = sorted(set(changes) - SETTING_KEYS)
        if unknown:
            return {
                "error": "invalid_settings",
                "message": f"Unknown setting(s): {', '.join(unknown)}",
            }

        # Validate look and auto-lock values before persisting anything, so
        # a crafted bridge call cannot corrupt the settings file.
        look = changes.get("look")
        if look is not None:
            if not isinstance(look, dict):
                return {"error": "invalid_settings", "message": "Look settings must be an object"}
            if "theme" in look and look["theme"] not in ("nebula", "synthwave", "arctic"):
                return {"error": "invalid_settings", "message": "Unknown theme"}
            if "effects" in look and look["effects"] not in ("full", "lite", "off"):
                return {"error": "invalid_settings", "message": "Unknown effects level"}
            if "view_mode" in look and look["view_mode"] not in ("edit", "split", "preview"):
                return {"error": "invalid_settings", "message": "Unknown view mode"}
            if "editor_font_size" in look:
                try:
                    font_size = float(look["editor_font_size"])
                except (TypeError, ValueError):
                    return {"error": "invalid_settings", "message": "Editor font size must be a number"}
                if not 10.0 <= font_size <= 20.0:
                    return {"error": "invalid_settings", "message": "Editor font size must be between 10 and 20"}
        if "autolock_minutes" in changes:
            try:
                minutes = float(changes["autolock_minutes"])
            except (TypeError, ValueError):
                return {"error": "invalid_settings", "message": "Auto-lock minutes must be a number"}
            if not 1.0 <= minutes <= 120.0:
                return {"error": "invalid_settings", "message": "Auto-lock must be between 1 and 120 minutes"}
        # The notes folder is moved with the native folder dialog
        # (choose_notes_folder), never by a settings write: rule 12f keeps the
        # frontend from naming paths, and a hostile value here could otherwise
        # re-point the whole app at an arbitrary folder and create it.
        if "notes_root" in changes:
            return {
                "error": "invalid_settings",
                "message": "Use the folder dialog to change the notes folder.",
            }

        candidate_root = self.config.notes_root.resolve()
        entries = changes.get("vaults", self.config.get("vaults", []))
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                raw_path = entry.get("key_path", "")
                if raw_path:
                    try:
                        key_candidate = Path(str(raw_path)).expanduser().resolve()
                        key_candidate.relative_to(candidate_root)
                    except ValueError:
                        pass
                    else:
                        return {
                            "error": "key_inside_notes",
                            "message": "Key files must be stored outside the VaultNotes notes folder",
                        }

        notes_root_changed = "notes_root" in changes
        try:
            updated = self.config.update(changes)
        except (TypeError, ValueError) as exc:
            return {"error": "invalid_settings", "message": str(exc)}
        if notes_root_changed:
            for store in self.vault_stores.values():
                store.lock()
            self.autolock.clear()
            self.plain_store = PlainStore(self.config.plain_dir)
            self._pending_key_paths.clear()
            self._build_vault_stores()
            for index in self.link_indexes.values():
                index.clear()
            self._plain_fingerprint = None
            self._sync_plain_index()
        if "autolock_minutes" in changes:
            try:
                self.autolock.set_minutes(float(changes["autolock_minutes"]))
            except (TypeError, ValueError):
                pass
        return updated

    def backup_now(self) -> dict[str, Any]:
        """Trigger immediate backup (implemented in M8)."""
        return {"ok": True, "last_backup": "just now"}

    def connect_drive(self) -> dict[str, Any]:
        """Connect Google Drive account (implemented in M8)."""
        return {"ok": True, "connected": True}

    def disconnect_drive(self) -> dict[str, Any]:
        """Disconnect Google Drive account (implemented in M8)."""
        return {"ok": True, "connected": False}

    def restore_from_drive(self, target_folder: str) -> dict[str, Any]:
        """Restore from Google Drive (implemented in M8)."""
        return {"ok": True, "restored_files": 0}
