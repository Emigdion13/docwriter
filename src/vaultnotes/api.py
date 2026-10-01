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
import inspect
import sys
import threading
import time
import urllib.parse
import uuid
import webbrowser
from collections.abc import Collection, Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from vaultnotes.autolock import AutoLock
from vaultnotes.calllock import CallLock
from vaultnotes.backup.gdrive_auth import (
    DriveAuthError,
    TokenStore,
    install_client_secret,
    is_connected,
    sign_in,
)
from vaultnotes.backup.gdrive_auth import CLIENT_SECRET_FILE as CLIENT_SECRET_NAME
from vaultnotes.backup.gdrive_backup import (
    BACKUP_FOLDER_NAME,
    MANIFEST_NAME,
    BackupError,
    BackupManifest,
    BackupReport,
    BackupRunner,
    GoogleDriveClient,
)
from vaultnotes.config import Config, get_app_dir, get_config, get_local_app_dir
from vaultnotes.crypto.keyfile import (
    KeyFileError,
    PassphraseRequired,
    VaultKey,
    WrongPassphrase,
    generate_key_file,
    key_file_needs_passphrase,
    load_key_file,
)
from vaultnotes.events import emit_event
from vaultnotes.frontmatter import is_important, set_important, set_tags
from vaultnotes.links import LinkIndex, count_links, rename_links
from vaultnotes.models import Note
from vaultnotes.render import render_preview, toggle_task
from vaultnotes.tags import (
    MAX_TAGS_PER_NOTE,
    clean_tag,
    extract_tags,
    has_tags,
    split_query,
    unique_tags,
)
from vaultnotes.sql import (
    SqlError,
    SqlManager,
    SqlPasswords,
    SqlStore,
    clean_profile,
    clean_query_name,
    clean_query_text,
    mssql_driver,
    public_connection,
)
from vaultnotes.storage.atomic import atomic_write
from vaultnotes.vt import VT_CONNECTION_ID, VT_NAME, VtStore, clean_table_name
from vaultnotes.terminal import (
    MAX_FAVORITES,
    MAX_RECENT,
    TerminalError,
    TerminalManager,
    available_shells,
    clean_command,
    clean_command_list,
    remember,
)
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

#: The spaces kept as ordinary ``.md`` files, always open.  Plain is the
#: user's own; AI-Notes is where AI helpers write (``notes.py`` changes notes
#: nowhere else), so the two never mix.
PLAIN_SPACES = {
    "plain": {"name": "Plain", "colorVar": "--plain"},
    "ai": {"name": "AI-Notes", "colorVar": "--ai"},
}

#: Display name of every space, Plain included: user-facing messages must be
#: able to name the space without assuming it is a vault.
SPACE_NAMES = {
    **{key: value["name"] for key, value in PLAIN_SPACES.items()},
    **{key: value["name"] for key, value in SPACE_DEFINITIONS.items()},
}

#: The spaces whose notes may link out to a Plain note with ``[[Plain:Title]]``
#: (M10), in sidebar order.  Renaming a Plain note follows its links there.
LINKS_OUT_TO_PLAIN = (*SPACE_DEFINITIONS, *(space for space in PLAIN_SPACES if space != "plain"))

#: Longest note body the editor may send (about 20 MB of text).
MAX_BODY_LENGTH = 20_000_000

#: The only keys ``settings.json`` may hold (section 4.6).
SETTING_KEYS = frozenset({"notes_root", "vaults", "autolock_minutes", "look", "backup"})

#: Of the ``backup`` block, the two the frontend may write.  ``drive_folder_id``
#: and ``last_backup`` are app-owned (sections 4.6 and 8.2).
BACKUP_SETTING_KEYS = frozenset({"enabled", "interval_minutes"})


def _now_stamp() -> str:
    """UTC timestamp in the format ``settings.json`` and the manifest use."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


#: File filters for the native dialogs, in the only form pywebview accepts:
#: "Description (*.ext)".  A bare "*.vnkey" makes it raise before any dialog
#: opens.
KEY_FILE_TYPES = ("VaultNotes key (*.vnkey)",)
MARKDOWN_FILE_TYPES = ("Markdown (*.md)",)
CLIENT_SECRET_FILE_TYPES = ("Google OAuth client (*.json)",)
SQLITE_FILE_TYPES = ("SQLite database (*.db;*.sqlite;*.sqlite3;*.db3)", "All files (*.*)")


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
        # One call at a time: pywebview runs every call on its own thread.
        with self._calls:
            return guarded(self, *args, **kwargs)

    def guarded(self: "Api", *args: Any, **kwargs: Any) -> Any:
        try:
            return method(self, *args, **kwargs)
        except (BridgeError, TerminalError, SqlError) as exc:
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

    # Marks the method as part of the Bridge API; expose_bridge() gives the
    # page these methods and nothing else.
    wrapper._bridge_endpoint = True  # type: ignore[attr-defined]
    return wrapper


class Api:
    """Methods exposed to JavaScript through ``window.pywebview.api``.

    Vault stores are deliberately kept in this object rather than recreated on
    each request.  Their decrypted notes and keys therefore have one clear
    lifecycle: unlock, use, then lock or auto-lock.
    """

    def __init__(
        self,
        config: Config | None = None,
        window: Any = None,
        *,
        drive_store: TokenStore | None = None,
        app_dir: Path | str | None = None,
        sql_dir: Path | str | None = None,
        sql_passwords: SqlPasswords | None = None,
    ) -> None:
        self._calls = CallLock()
        #: Set by :meth:`ready_to_close` once the page saved its last edit.
        self.page_saved_for_close = threading.Event()
        self.config = config or get_config()
        self.window = window
        #: ``%APPDATA%\\VaultNotes``: settings, manifest, OAuth client file.
        #: Tests point it at a temporary folder.
        self.app_dir = Path(app_dir).resolve() if app_dir is not None else get_app_dir()
        self.plain_stores: dict[str, PlainStore] = {}
        self._build_plain_stores()
        self.vault_stores: dict[str, VaultStore] = {}
        self._build_vault_stores()

        # One link graph per space, in memory only.  Plain and AI-Notes are
        # indexed at startup; a vault's index is built on unlock and cleared on
        # lock, so titles from a locked vault can never leak (security rule 11).
        self.link_indexes: dict[str, LinkIndex] = {
            space_id: LinkIndex(space_id) for space_id in (*PLAIN_SPACES, *SPACE_DEFINITIONS)
        }
        self._plain_fingerprints: dict[str, tuple[int, int, int]] = {}
        self._sync_plain_indexes()

        self._pending_key_paths: dict[str, Path] = {}
        self.autolock = AutoLock(
            self.config.get("autolock_minutes", 10),
            on_lock=self._auto_lock_expired,
        )

        #: Where the Google refresh token lives.  Tests pass a memory store so
        #: no real credential manager is read or written.
        self.drive_store = drive_store if drive_store is not None else TokenStore()

        #: Memoised "is this key file wrapped?" answers, see
        #: :meth:`_key_file_is_wrapped`.  Never holds key material.
        self._key_wrapped_cache: dict[str, tuple[str, int, bool]] = {}

        # Drive backup runs in its own thread, so a slow or broken network can
        # never freeze the window (section 8.2).  It is built here but stays
        # silent until "Connect Google Drive" and "Back up now" are used.
        # Each hook is looked up when it fires, not when the runner is built:
        # the window, the stores and the schedule all change during a session.
        self.backup = BackupRunner(
            context_provider=lambda: self._backup_context(),
            service_factory=lambda: self._drive_service(),
            on_result=lambda report: self._backup_finished(report),
            emit=lambda name, data: self._emit(name, data),
            auto_callback=lambda: self._auto_backup_due(),
        )
        self.backup.configure(**self._backup_schedule_settings())

        #: The CMD space's shell.  Output goes to the page as events from the
        #: terminal's own threads, never through the call lock.
        self.terminal = TerminalManager(emit=lambda name, data: self._emit(name, data))

        #: The SQL space.  ``sql.db`` (saved connections) sits in
        #: ``%LOCALAPPDATA%\\VaultNotes``, never in the notes folder; passwords
        #: sit in the Credential Manager.  Tests point both somewhere harmless.
        self.sql_dir = Path(sql_dir).resolve() if sql_dir is not None else get_local_app_dir()
        self.sql_store = SqlStore(self.sql_dir / "sql.db")
        #: The SQL - VT space's tables: results kept after their connection closed.
        self.vt_store = VtStore(self.sql_dir / "vt.db")
        self.sql_passwords = sql_passwords if sql_passwords is not None else SqlPasswords()
        self.sql = SqlManager(emit=lambda name, data: self._emit(name, data))

    def set_window(self, window: Any) -> None:
        """Store the pywebview window used for native file dialogs/events."""
        self.window = window

    def close(self) -> None:
        """Stop the timers, make a final backup if asked to, and clear state."""
        # Outside the call lock: the backup's own callbacks need it.
        self.backup.finish_on_close()
        self.terminal.stop()
        self.sql.close()
        with self._calls:
            self.autolock.stop()
            for store in self.vault_stores.values():
                store.lock()
            # Decrypted titles must not outlive the vault they came from.
            for index in self.link_indexes.values():
                index.clear()

    def _build_plain_stores(self) -> None:
        """Open Plain and AI-Notes in the current notes folder."""
        self.plain_stores = {
            "plain": PlainStore(self.config.plain_dir),
            "ai": PlainStore(self.config.ai_dir),
        }

    @property
    def plain_store(self) -> PlainStore:
        """The user's own Plain space."""
        return self.plain_stores["plain"]

    def _plain(self, space_id: str) -> PlainStore | None:
        """The store of a Plain-kind space (Plain or AI-Notes), else ``None``."""
        return self.plain_stores.get(space_id)

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
        if text not in self.plain_stores and text not in self.vault_stores:
            raise BridgeError("invalid_space", f"Space not found: {text}")
        return text

    def _note_id_text(self, space_id: str, note_id: Any) -> str:
        """Validate a note id for a space.

        Vault ids are the opaque 32-hex file names; Plain and AI-Notes ids are
        file names, so anything with a path separator in it is refused before
        it can reach the filesystem.
        """
        text = self._text(note_id, "Note", allow_empty=False).strip()
        if space_id in self.plain_stores:
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

    def _sync_plain_index(self, space_id: str) -> LinkIndex:
        """Rebuild a Plain or AI-Notes index only when its folder changed.

        Plain notes can be edited by other programs (the plan explicitly
        supports opening the folder in Obsidian), and AI helpers write AI-Notes
        from outside the app, so the index is checked against a cheap
        directory fingerprint instead of being trusted blindly.
        """
        index = self.link_indexes[space_id]
        store = self.plain_stores[space_id]
        fingerprint = store.fingerprint()
        if fingerprint != self._plain_fingerprints.get(space_id):
            # list_notes() also refreshes skipped-file warnings.
            index.build(store.list_notes())
            self._plain_fingerprints[space_id] = fingerprint
        return index

    def _sync_plain_indexes(self) -> None:
        """Bring the Plain and AI-Notes indexes up to date."""
        for space_id in self.plain_stores:
            self._sync_plain_index(space_id)

    def _sync_index(self, space_id: str) -> LinkIndex | None:
        """Return an up-to-date index for an open space.

        A locked vault always gets an empty index: its titles must not reach
        the ``[[`` suggestions, the palette or a "Linked from" bar.
        """
        index = self.link_indexes.get(space_id)
        if index is None:
            return None
        if space_id in self.plain_stores:
            return self._sync_plain_index(space_id)

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
        store = self._plain(space_id)
        if store is not None:
            # We just wrote that file ourselves, so the index is current: move
            # the fingerprint forward instead of forcing a full rebuild on the
            # next call.  Only edits from *outside* the app should trigger one.
            self._plain_fingerprints[space_id] = store.fingerprint()

    def _index_remove(self, space_id: str, note_id: str) -> None:
        """Drop one note from its space's index."""
        index = self.link_indexes.get(space_id)
        if index is None:
            return
        index.remove(note_id)
        store = self._plain(space_id)
        if store is not None:
            self._plain_fingerprints[space_id] = store.fingerprint()

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

    def _links_out_to_plain(self, title: str) -> dict[str, list[str]]:
        """Notes outside Plain whose ``[[Plain:Title]]`` links name ``title``.

        Keyed by space, in sidebar order, spaces without such notes left out.
        Only AI-Notes and unlocked vaults are asked.  A locked vault is skipped
        before its index is even looked at: nothing in it is decrypted,
        counted or rewritten (security rule 11).
        """
        found: dict[str, list[str]] = {}
        for space_id in LINKS_OUT_TO_PLAIN:
            vault = self._vault(space_id)
            if vault is not None and vault.locked:
                continue
            index = self._sync_index(space_id)
            sources = index.sources_linking_out("plain", title) if index is not None else []
            if sources:
                found[space_id] = sources
        return found

    def _warnings(self, space_id: str | None = None) -> list[str]:
        """Human-readable warnings about files that were skipped (M7).

        Plain file names are shown as-is.  Vault entries are opaque
        ``<id>.vnote`` names: a damaged encrypted file was never decrypted, so
        there is no title to show and none may be guessed (security rule 5).
        """
        # Start-up problems (damaged settings.json, unreachable notes folder)
        # belong to the app as a whole, not to one vault.
        warnings: list[str] = list(self.config.warnings) if space_id is None else []
        for plain_space, store in self.plain_stores.items():
            if space_id is not None and plain_space != space_id:
                continue
            where = "" if plain_space == "plain" else f" in {SPACE_NAMES[plain_space]}"
            for name in store.skipped_files:
                warnings.append(f"Skipped a note{where} that could not be read: {name}")
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

    def _key_file_is_wrapped(self, space_id: str, key_path: str | None) -> bool:
        """Whether this space's key file is passphrase protected (M10).

        :func:`vaultnotes.crypto.keyfile.key_file_needs_passphrase` opens the
        file, and ``get_state`` runs on every space switch, so the answer is
        remembered per path and only re-read when the file actually changed (or
        the key file was swapped for another one).  A key file that cannot be
        read - a USB stick pulled out - is reported as unprotected, which is
        what the unlock flow needs: ask for no passphrase, then fail clearly.
        """
        if not key_path:
            return False
        try:
            stamp = Path(key_path).stat().st_mtime_ns
        except OSError:
            self._key_wrapped_cache.pop(space_id, None)
            return False
        seen = self._key_wrapped_cache.get(space_id)
        if seen is not None and seen[:2] == (str(key_path), stamp):
            return seen[2]
        wrapped = key_file_needs_passphrase(key_path)
        self._key_wrapped_cache[space_id] = (str(key_path), stamp, wrapped)
        return wrapped

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
            # False when this notes folder has no such vault yet (no vault.json):
            # the UI then says "not set up" instead of "locked", so a notes folder
            # switched by mistake is not mistaken for a vault that lost its key.
            "created": store.has_header,
            "colorVar": definition["colorVar"],
            "note_count": count,
            "key_path": key_path,
            # M10: the unlock dialog only shows a passphrase box for a wrapped
            # key file, so the app must know which kind it is pointing at.
            "key_wrapped": self._key_file_is_wrapped(space_id, key_path),
        }
        if store.vault_id:
            result["vault_id"] = store.vault_id
        return result

    def _plain_space_summary(self, space_id: str) -> dict[str, Any]:
        definition = PLAIN_SPACES[space_id]
        return {
            "id": space_id,
            "name": definition["name"],
            "kind": "plain",
            "locked": False,
            "colorVar": definition["colorVar"],
            # The directory fingerprint counts files without reading them.
            "note_count": max(0, self.plain_stores[space_id].fingerprint()[0]),
        }

    @bridge_method
    def get_state(self) -> dict[str, Any]:
        """Return spaces, look settings, backup status, and setup state."""
        self._sync_plain_indexes()
        spaces = [
            self._plain_space_summary("plain"),
            self._space_summary("encrypted"),
            self._space_summary("personal"),
            # Last, apart from the user's own spaces: AI helpers write here.
            self._plain_space_summary("ai"),
        ]
        needs_setup = any(not self.vault_stores[space_id].has_header for space_id in SPACE_DEFINITIONS)
        backup = self.config.get("backup", {})
        return {
            "spaces": spaces,
            "look": self.config.get("look", {}),
            "autolock_minutes": self.config.get("autolock_minutes", 10),
            "last_backup": backup.get("last_backup") if isinstance(backup, Mapping) else None,
            # Drive card / Settings read this (section 7 "Backup").  Reported
            # through get_state so the Bridge API keeps section 4.8's names.
            "backup": self._backup_state(),
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

    def _note_tags(self, space_id: str, note: Note) -> list[str]:
        """A note's tags, from the index when possible."""
        index = self.link_indexes.get(space_id)
        if index is not None and index.has(note.id):
            return index.tags_of(note.id)
        return extract_tags(note.body)

    def _note_summary(self, space_id: str, note: Note) -> dict[str, Any]:
        """One row of the note list (section 4.8)."""
        return {
            "id": note.id,
            "title": note.title,
            "snippet": make_snippet(note.body),
            "modified": note.modified,
            "link_count": self._link_count(space_id, note),
            "important": is_important(note.body),
            "tags": self._note_tags(space_id, note),
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
            # The front matter's tags: line (vaultnotes.tags), so a tag set in
            # the app, in Obsidian or by an AI helper is the same tag.
            "tags": self._note_tags(space_id, note),
            "snippet": make_snippet(note.body),
            "link_count": self._link_count(space_id, note),
            "important": is_important(note.body),
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
        """List note summaries in one space; locked vaults return no titles.

        Important notes come first, each group in the requested order, so the
        notes the user marked sit at the top of the list.  ``#tag`` words in
        the query keep only the notes with every one of those tags, and the
        rest of the query is searched for as usual.
        """
        space = self._known_space(space_id)
        text_query, wanted_tags = split_query(self._text(query, "Search text", max_length=1024))
        text_sort = self._text(sort, "Sort") or "modified"

        # Keeps link counts honest and a locked vault's index empty.
        self._sync_index(space)
        plain = self._plain(space)
        if plain is not None:
            notes = plain.list_notes(query=text_query, sort=text_sort)
        else:
            store = self.vault_stores[space]
            if store.locked:
                return []
            notes = store.list_notes(query=text_query, sort=text_sort)
        rows = [self._note_summary(space, note) for note in notes]
        if wanted_tags:
            rows = [row for row in rows if has_tags(row["tags"], wanted_tags)]
        # A stable sort: the store's order survives inside each group.
        rows.sort(key=lambda row: not row["important"])
        return rows

    @bridge_method
    def open_note(self, space_id: str, note_id: str) -> dict[str, Any]:
        """Retrieve a full note and its "Linked from" list from the same space.

        Backlinks come from the space's in-memory link index, so opening a note
        stays cheap no matter how many notes the space holds (M6/M7).  Links
        never cross spaces: a Plain note only ever sees Plain backlinks.
        """
        space = self._known_space(space_id)
        clean_id = self._note_id_text(space, note_id)

        if space in self.plain_stores:
            note = self._read_plain_note(space, clean_id)
        else:
            store = self.vault_stores[space]
            if store.locked:
                return _error("locked", "Vault is locked")
            try:
                note = store.read_note(clean_id)
            except FileNotFoundError:
                return _error("not_found", f"Note not found: {clean_id}")

        return self._note_result(space, note, self._backlinks_for(space, note))

    def _read_plain_note(self, space_id: str, note_id: str) -> Note:
        """Read one Plain or AI-Notes note, turning a missing file into ``not_found``."""
        try:
            return self.plain_stores[space_id].read_note(note_id)
        except FileNotFoundError:
            raise BridgeError("not_found", f"Note not found: {note_id}") from None
        except OSError as exc:
            detail = exc.strerror or exc.__class__.__name__
            raise BridgeError("io_error", f"That note could not be read ({detail}).") from None

    @bridge_method
    def create_note(self, space_id: str, title: str = "Untitled") -> dict[str, Any]:
        """Create a note in Plain, AI-Notes or an unlocked encrypted vault.

        Titles are unique inside a space: a collision gets `` (2)``, `` (3)``
        and so on rather than an error or an overwritten note.
        """
        space = self._known_space(space_id)
        clean_title = self._title_text(title)

        plain = self._plain(space)
        if plain is not None:
            note = plain.create_note(title=clean_title)
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

        plain = self._plain(space)
        if plain is not None:
            note = plain.save_note(clean_id, text_body)
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
        # The mark and the tags can be typed by hand too, so the header follows saves.
        return {
            "modified": note.modified,
            "important": is_important(note.body),
            "tags": self._note_tags(space, note),
        }

    @bridge_method
    def set_important(self, space_id: str, note_id: str, important: bool) -> dict[str, Any]:
        """Mark a note important, or clear the mark, and return the full note.

        The mark is an ``important: true`` line in the note's front matter
        (see :mod:`vaultnotes.frontmatter`), so this is an ordinary save of a
        new body: encrypted in a vault, a plain ``.md`` write elsewhere.
        """
        space = self._known_space(space_id)
        clean_id = self._note_id_text(space, note_id)
        if not isinstance(important, bool):
            raise BridgeError("invalid_input", "Important must be true or false.")

        plain = self._plain(space)
        if plain is not None:
            note = self._read_plain_note(space, clean_id)
        else:
            store = self.vault_stores[space]
            if store.locked:
                return _error("locked", "Vault is locked")
            try:
                note = store.read_note(clean_id)
            except FileNotFoundError:
                return _error("not_found", f"Note not found: {clean_id}")

        body = set_important(note.body, important)
        if body != note.body:
            note = plain.save_note(clean_id, body) if plain is not None else store.save_note(clean_id, body)
            self._index_update(space, note)
            if self._any_vault_unlocked():
                self.autolock.touch()
        return self._note_result(space, note, self._backlinks_for(space, note))

    @bridge_method
    def set_tags(self, space_id: str, note_id: str, tags: list[str]) -> dict[str, Any]:
        """Replace a note's tags and return the full note.

        The tags are the ``tags: [...]`` line of the note's front matter (see
        :mod:`vaultnotes.tags`), so like the important mark this is an ordinary
        save of a new body.  ``[]`` removes the line.
        """
        space = self._known_space(space_id)
        clean_id = self._note_id_text(space, note_id)
        if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
            raise BridgeError("invalid_input", "Tags must be a list of words.")
        for tag in tags:
            if tag.strip() and not clean_tag(tag):
                raise BridgeError(
                    "invalid_input",
                    f"“{tag[:40]}” is not a tag. Use letters, digits, - _ or /, with at least one letter.",
                )
        wanted = unique_tags(tags)
        if len(wanted) > MAX_TAGS_PER_NOTE:
            raise BridgeError("too_large", f"A note can have up to {MAX_TAGS_PER_NOTE} tags.")

        plain = self._plain(space)
        if plain is not None:
            note = self._read_plain_note(space, clean_id)
        else:
            store = self.vault_stores[space]
            if store.locked:
                return _error("locked", "Vault is locked")
            try:
                note = store.read_note(clean_id)
            except FileNotFoundError:
                return _error("not_found", f"Note not found: {clean_id}")

        if wanted != extract_tags(note.body):
            body = set_tags(note.body, wanted)
            note = plain.save_note(clean_id, body) if plain is not None else store.save_note(clean_id, body)
            self._index_update(space, note)
            if self._any_vault_unlocked():
                self.autolock.touch()
        return self._note_result(space, note, self._backlinks_for(space, note))

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
        A Plain rename also rewrites the ``[[Plain:Title]]`` links in AI-Notes
        and unlocked vaults; a locked vault is left as it is (security rule 11).
        ``links_updated`` counts every link rewritten, in every space.
        """
        space = self._known_space(space_id)
        clean_id = self._note_id_text(space, note_id)
        clean_title = self._title_text(new_title)
        rewrite_links = bool(update_links)

        index = self._sync_index(space)
        # Ask the index *before* the rename: a Plain note's id is its title, so
        # the old id stops existing as soon as the file is renamed.
        source_ids: list[str] = index.sources_linking_to(clean_id) if index is not None else []

        plain = self._plain(space)
        if plain is not None:
            try:
                note, old_title = plain.rename_note(clean_id, clean_title)
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
            # Inside Plain, [[Old]] and [[Plain:Old]] both name this note.
            # Anywhere else [[Plain:Old]] names a Plain note and stays as it is.
            links_updated = self._rewrite_links_to(
                space,
                source_ids,
                old_title,
                note.title,
                exclude_id=note.id,
                spaces=None if space == "plain" else {""},
            )
            if space == "plain":
                # Out there only [[Plain:Old]] is this note: a bare [[Old]]
                # names a note of that space and must keep pointing at it.
                for other, other_ids in self._links_out_to_plain(old_title).items():
                    links_updated += self._rewrite_links_to(
                        other, other_ids, old_title, note.title, spaces={"plain"}
                    )
        return {"title": note.title, "id": note.id, "links_updated": links_updated}

    def _rewrite_links_to(
        self,
        space_id: str,
        source_ids: Iterable[str],
        old_title: str,
        new_title: str,
        exclude_id: str = "",
        spaces: Collection[str] | None = None,
    ) -> int:
        """Point the links in ``source_ids`` at ``new_title``; return the count.

        ``spaces`` is passed to :func:`vaultnotes.links.rename_links` to pick
        which links are rewritten (``{"plain"}``: only ``[[Plain:Title]]``).
        A note that vanished or cannot be written is skipped rather than
        aborting the whole rename: the rename itself already succeeded and must
        not be reported as failed because of one unwritable neighbour (M7).
        """
        if not old_title or not new_title or old_title == new_title:
            return 0
        plain = self._plain(space_id)
        store = None if plain is not None else self.vault_stores.get(space_id)
        if plain is None and (store is None or store.locked):
            return 0

        updated = 0
        for source_id in source_ids:
            if not source_id or source_id == exclude_id:
                continue
            try:
                source = (
                    self._read_plain_note(space_id, source_id)
                    if plain is not None
                    else store.read_note(source_id)
                )
                new_body, count = rename_links(source.body, old_title, new_title, spaces=spaces)
                if not count:
                    continue
                saved = (
                    plain.save_note(source_id, new_body)
                    if plain is not None
                    else store.save_note(source_id, new_body)
                )
            except (BridgeError, FileNotFoundError, OSError, ValueError, VaultStoreError):
                continue
            self._index_update(space_id, saved)
            updated += count
        return updated

    @bridge_method
    def count_links_to(self, space_id: str, note_id: str) -> dict[str, Any]:
        """Count the notes linking here: the ones a rename would rewrite.

        Used by the "Update N links?" rename prompt and the move warning.
        Links stay inside their space except ``[[Plain:Title]]`` (M10), so a
        Plain note also counts the AI-Notes and unlocked-vault notes linking
        out to it.  For a Plain note ``spaces`` says where the notes are and
        ``locked`` names the vaults nobody looked in: a locked vault is never
        decrypted or counted.  A locked vault's own note reports 0 rather than
        admitting anything about its notes.
        """
        space = self._known_space(space_id)
        clean_id = self._note_id_text(space, note_id)
        index = self._sync_index(space)
        if index is None:
            return {"count": 0}
        if space in self.vault_stores:
            if self.vault_stores[space].locked:
                return {"count": 0}
            if not index.has(clean_id):
                return {"count": 0}
        same_space = len(index.backlinks(clean_id))
        if space != "plain":
            return {"count": same_space}

        per_space = {space: same_space} if same_space else {}
        title = index.title_of(clean_id)
        if title:
            for other, sources in self._links_out_to_plain(title).items():
                per_space[other] = len(sources)
        return {
            "count": sum(per_space.values()),
            "spaces": [
                {"space_id": key, "name": SPACE_NAMES[key], "count": value}
                for key, value in per_space.items()
            ],
            "locked": [
                {"space_id": key, "name": SPACE_NAMES[key]}
                for key, store in self.vault_stores.items()
                if store.locked and store.has_header
            ],
        }

    @bridge_method
    def delete_note(self, space_id: str, note_id: str) -> dict[str, Any]:
        """Move a note into that space's trash, still encrypted for vaults."""
        space = self._known_space(space_id)
        clean_id = self._note_id_text(space, note_id)

        plain = self._plain(space)
        if plain is not None:
            note = plain.delete_note(clean_id)
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

        plain = self._plain(space)
        if plain is not None:
            note = plain.restore_note(clean_id)
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
        plain = self._plain(space)
        if plain is not None:
            return [
                {"id": note.id, "title": note.title, "modified": note.modified}
                for note in plain.list_trash()
            ]
        store = self.vault_stores[space]
        if store.locked:
            return []
        return [
            {"id": note.id, "title": note.title, "modified": note.modified}
            for note in store.list_trash()
        ]

    def _read_any_note(self, space_id: str, note_id: str) -> Any:
        """Read a note from Plain, AI-Notes or an unlocked vault.

        Raises VaultLockedError, FileNotFoundError, or ValueError for a bad
        vault note id.
        """
        plain = self._plain(space_id)
        if plain is not None:
            return plain.read_note(note_id)
        store = self._vault(space_id)
        if store is None:  # pragma: no cover - callers validate the space first
            raise FileNotFoundError(f"Space not found: {space_id}")
        return store.read_note(note_id)

    def _count_broken_links_on_move(self, space_id: str, note: Note) -> int:
        """Count the links that break when a note leaves its space.

        Moving a note breaks the notes that link to it and its own links to
        its old neighbours, counted as the move dialog shows them: linking
        notes, and the notes this one links to.  A Plain note also leaves its
        ``[[Plain:Title]]`` links in AI-Notes and unlocked vaults behind (a
        locked vault is never asked), while a ``[[Plain:Title]]`` the note
        holds keeps working wherever it goes.  A note linking to itself keeps
        working after the move and is not counted.
        """
        index = self._sync_index(space_id)
        if index is None:
            return 0
        if not index.has(note.id):
            index.update(note)
        incoming = len(index.backlinks(note.id))
        if space_id == "plain":
            incoming += sum(len(sources) for sources in self._links_out_to_plain(note.title).values())
        # outgoing() leaves out self-links and links naming another space.
        outgoing = sum(1 for link in index.outgoing(note.id) if link["resolved"])
        return incoming + outgoing

    @bridge_method
    def move_note(self, space_id: str, note_id: str, target_space_id: str) -> dict[str, Any]:
        """Move a note between spaces, encrypting or decrypting as needed.

        The note is created in the target space first; the source copy is
        only removed afterwards, so a failure never loses the note. Returns
        ``{new_id, title, broken_links}`` where ``broken_links`` counts the
        links that stop working because of the move (see
        :meth:`_count_broken_links_on_move`).
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
        target_plain = self._plain(target)
        try:
            if target_plain is not None:
                created = target_plain.create_note(title=source.title, body=source.body)
                if created.body != source.body:
                    created = target_plain.save_note(created.id, source.body)
            else:
                assert target_store is not None  # narrowed by the checks above
                created = target_store.create_note(title=source.title, body=source.body)
                if created.body != source.body:
                    created = target_store.save_note(created.id, source.body)
        except (ValueError, OSError, VaultStoreError) as exc:
            return _error("move_failed", f"Could not move the note: {exc}")

        # The target copy exists; now remove the source without using trash,
        # because a move is not a delete.
        source_plain = self._plain(space)
        try:
            if source_plain is not None:
                source_path = source_plain._resolve_note_path(clean_id)  # noqa: SLF001 - same package
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
        plain = self._plain(space)
        if plain is not None:
            try:
                title = plain.purge_note(clean_id)
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
        plain = self._plain(space)
        if plain is not None:
            return {"ok": True, "purged": plain.empty_trash()}
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
        """Import ``.md`` files into Plain, AI-Notes or an unlocked vault.

        The frontend calls this without paths, causing Python to open the
        native file picker itself (the frontend never sends file paths).
        ``file_paths`` exists only for headless callers and tests.
        """
        space = self._known_space(space_id)
        plain = self._plain(space)
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
                if plain is not None:
                    note = plain.create_note(title=title, body=body)
                    if note.body != body:
                        note = plain.save_note(note.id, body)
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
        # Everything in the notes folder is backed up as it is, so an exported
        # vault note there would reach Google Drive unencrypted.
        try:
            inside_notes = destination.resolve().is_relative_to(self.config.notes_root.resolve())
        except (OSError, ValueError):
            inside_notes = False
        if inside_notes:
            return _error(
                "export_inside_notes",
                "Choose a folder outside your VaultNotes notes folder. Files there are backed up "
                "as they are, so an exported copy would be uploaded unencrypted.",
            )
        try:
            atomic_write(destination, note.body.encode("utf-8"))
        except OSError as exc:
            detail = exc.strerror or exc.__class__.__name__
            return _error("export_failed", f"Could not write the file: {detail}")
        return {"ok": True, "name": destination.name}

    # ------------------------------------------------------------------
    # Markdown links and preview
    # ------------------------------------------------------------------
    def _note_body_by_title(self, space_id: str, title: str) -> str | None:
        """One note's Markdown looked up by title, for ``![[embeds]]`` (M10).

        Any problem - a locked vault, a note deleted a moment ago, a damaged
        file - means "no embed", which the preview shows as a plain link.  A
        preview must never fail because of a second note.
        """
        index = self.link_indexes.get(space_id)
        if index is None:
            return None
        note_id = index.note_id_for(title)
        if not note_id:
            return None
        try:
            note = self._read_any_note(space_id, note_id)
        except (OSError, ValueError, VaultLockedError):
            return None
        body = getattr(note, "body", None)
        return body if isinstance(body, str) else None

    @bridge_method
    def render_preview(self, space_id: str, body: str) -> str:
        """Render Markdown, resolving links against same-space titles only.

        A locked vault contributes no titles, so its notes can never be
        discovered through a preview (security rule 11).  ``spaces`` is only
        ever given the Plain space: vault and AI-Notes notes may link out to
        Plain notes (``[[Plain:Title]]``, M10) and never the other way around.
        """
        space = self._known_space(space_id)
        text_body = self._body_text(body)
        titles = self._titles_for(space)
        return render_preview(
            text_body,
            titles=titles,
            read_note=lambda title: self._note_body_by_title(space, title),
            spaces={"plain": self._titles_for("plain")},
        )

    @bridge_method
    def toggle_task(self, space_id: str, body: str, index: int) -> dict[str, Any]:
        """Flip one checklist item of ``body`` (the note as the editor holds it).

        ``index`` is the checkbox's place among the preview's checkboxes.  The
        new text is returned, not saved: the editor puts it in place and its
        normal auto-save writes it, so Plain, Personal and Encrypted notes all
        go through their usual save path.
        """
        self._known_space(space_id)
        text_body = self._body_text(body)
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise BridgeError("invalid_input", "Checklist item must be a number")
        changed = toggle_task(text_body, index)
        if changed is None:
            raise BridgeError("not_found", "That checklist item is no longer in the note.")
        return {"ok": True, "body": changed}

    @bridge_method
    def list_titles(self, space_id: str) -> list[str]:
        """Return the ``[[`` suggestion titles for an open space only."""
        space = self._known_space(space_id)
        return self._titles_for(space)

    @bridge_method
    def list_tags(self, space_id: str) -> list[dict[str, Any]]:
        """``[{tag, count}]`` for an open space, the most used tag first.

        Comes from the space's index, like the titles, so a locked vault never
        reveals a tag (security rule 11) and a long list costs no disk reads.
        """
        space = self._known_space(space_id)
        index = self._sync_index(space)
        return index.tag_counts() if index is not None else []

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

    @bridge_method
    def open_note_by_title(
        self,
        space_id: str,
        title: str,
        heading: str = "",
    ) -> dict[str, Any]:
        """Resolve one ``[[link]]`` title into a note the app can open.

        The frontend owns the click, so it asks here rather than guessing from
        its own filtered list: this resolves case, a trailing ``.md`` and an
        alias exactly like the preview does.  ``space_id`` is the space to
        search, which for ``[[Plain:Title]]`` is ``"plain"`` (M10).  A missing
        note is reported as ``not_found``; creating it is a separate, explicit
        call so a link can never write to a file by itself.
        """
        space = self._known_space(space_id)
        wanted = str(title or "").strip()
        if not wanted:
            return _error("invalid_title", "That link has no title to open.")
        if len(wanted) > MAX_TITLE_LENGTH:
            return _error("invalid_title", "That link title is too long to be a note.")
        vault = self._vault(space)
        if vault is not None and vault.locked:
            # Say why, instead of claiming the note does not exist.
            return _error("locked", f"Unlock {SPACE_NAMES[space]} first.")

        index = self._sync_index(space)
        note_id = index.note_id_for(wanted) if index is not None else None
        if not note_id:
            return _error("not_found", f"No note named “{wanted}” in {SPACE_NAMES[space]}.")
        result: dict[str, Any] = {
            "ok": True,
            "space_id": space,
            "note_id": note_id,
            "title": index.title_of(note_id) or wanted,
        }
        text_heading = self._text(heading, "Heading", max_length=200)
        if text_heading:
            result["heading"] = text_heading
        return result

    @bridge_method
    def get_graph(self, space_id: str) -> dict[str, Any]:
        """Notes and links of one space, for the graph view (M10).

        Nodes are the notes; an edge is one note linking to another.  A link to
        a missing note is returned as a ghost node (``id=None``) so the graph can
        show where a note is wanted, but never as something to open.
        """
        space = self._known_space(space_id)
        index = self._sync_index(space)
        vault = self._vault(space)
        if vault is not None and vault.locked:
            return {"space_id": space, "nodes": [], "edges": [], "locked": True}

        notes = self.list_notes(space, "", "title")
        summaries = notes if isinstance(notes, list) else []
        nodes = [
            {
                "id": item["id"],
                "title": item["title"],
                "links": int(item.get("link_count") or 0),
            }
            for item in summaries
        ]
        edges: list[dict[str, Any]] = []
        if index is not None:
            for node in nodes:
                for target in index.outgoing(node["id"]):
                    edges.append(
                        {
                            "from": node["id"],
                            "to": target["id"],
                            "title": target["title"],
                            "resolved": bool(target["resolved"]),
                        }
                    )
        return {
            "space_id": space,
            "nodes": nodes,
            "edges": edges,
            "locked": False,
        }

    # ------------------------------------------------------------------
    # Vault creation, native key dialogs, unlock and lock
    # ------------------------------------------------------------------
    def _file_dialog(self, kind: str, **kwargs: Any) -> Any:
        """Show a native dialog; ``kind`` is ``"OPEN"``, ``"SAVE"`` or ``"FOLDER"``.

        Other Bridge calls (autosave) keep running while the user decides, so
        callers must re-check any state they rely on afterwards.
        """
        import webview  # type: ignore

        dialogs = getattr(webview, "FileDialog", None)  # pywebview 5+
        dialog_type = getattr(dialogs, kind, None) if dialogs is not None else None
        if dialog_type is None:
            dialog_type = getattr(webview, f"{kind}_DIALOG")
        try:
            with self._calls.released():
                return self.window.create_file_dialog(dialog_type, **kwargs)
        except Exception as exc:
            # Reported as an error, never as "cancelled": a bad file filter once
            # stopped every dialog from opening while the app said "cancelled".
            sys.stderr.write(f"VaultNotes: the file dialog failed: {type(exc).__name__}: {exc}\n")
            raise BridgeError(
                "dialog_failed", f"The file dialog could not be opened ({type(exc).__name__})."
            ) from exc

    def _choose_file(self, save: bool, suggested_name: str) -> Path | None:
        """Open a native key-file dialog, never a browser file input."""
        if self.window is None:
            return None
        kwargs: dict[str, Any] = {"allow_multiple": False, "file_types": KEY_FILE_TYPES}
        if save:
            kwargs["save_filename"] = suggested_name
        selected = self._file_dialog("SAVE" if save else "OPEN", **kwargs)
        if isinstance(selected, (list, tuple)):
            selected = selected[0] if selected else None
        if not selected:
            return None
        return Path(str(selected)).expanduser().resolve()

    def _choose_import_files(self) -> list[Path]:
        """Open a native multi-select picker for ``.md`` files to import."""
        if self.window is None:
            return []
        selected = self._file_dialog("OPEN", allow_multiple=True, file_types=MARKDOWN_FILE_TYPES)
        if not selected:
            return []
        if isinstance(selected, (str, Path)):
            selected = [selected]
        return [Path(str(each)).expanduser() for each in selected]

    def _choose_export_file(self, suggested_name: str) -> Path | None:
        """Open a native Save dialog for exporting one ``.md`` file."""
        if self.window is None:
            return None
        selected = self._file_dialog(
            "SAVE", allow_multiple=False, file_types=MARKDOWN_FILE_TYPES, save_filename=suggested_name
        )
        if isinstance(selected, (list, tuple)):
            selected = selected[0] if selected else None
        if not selected:
            return None
        return Path(str(selected)).expanduser()

    def _choose_client_secret_file(self) -> Path | None:
        """Open a native picker for the OAuth client JSON Google downloaded."""
        if self.window is None:
            return None
        kwargs: dict[str, Any] = {"allow_multiple": False, "file_types": CLIENT_SECRET_FILE_TYPES}
        downloads = Path.home() / "Downloads"
        if downloads.is_dir():
            kwargs["directory"] = str(downloads)
        selected = self._file_dialog("OPEN", **kwargs)
        if isinstance(selected, (list, tuple)):
            selected = selected[0] if selected else None
        if not selected:
            return None
        return Path(str(selected)).expanduser()

    def _choose_folder(self) -> Path | None:
        """Open a native folder picker and return its server-side path."""
        if self.window is None:
            return None
        selected = self._file_dialog("FOLDER", allow_multiple=False)
        if isinstance(selected, (list, tuple)):
            selected = selected[0] if selected else None
        if not selected:
            return None
        return Path(str(selected)).expanduser().resolve()

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
        self._build_plain_stores()
        self._pending_key_paths.clear()
        self._build_vault_stores()
        for index in self.link_indexes.values():
            index.clear()
        self._plain_fingerprints.clear()
        self._sync_plain_indexes()

    @bridge_method
    def choose_key_file(self, space_id: str) -> dict[str, Any]:
        """Let the native UI choose a key and keep its path server-side."""
        space = self._known_space(space_id)
        if space in self.plain_stores:
            return _error("invalid_space", f"{SPACE_NAMES[space]} is not encrypted and has no key file")
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
    def create_vault(
        self,
        space_id: str,
        key_path: Path | str | None = None,
        passphrase: str = "",
    ) -> dict[str, Any]:
        """Create one configured vault and generate its external key file.

        ``key_path`` is optional for Python callers/tests.  The frontend calls
        this without a path, causing a native Save dialog.  In a headless
        environment a deterministic location next to ``settings.json`` is
        used; it is still outside the notes root.  A ``passphrase`` writes the
        wrapped key file of section 4.5 (M10), where the file alone unlocks
        nothing.
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
            return {
                "error": "key_exists",
                "message": (
                    f"{selected.name} already exists there, and VaultNotes never replaces a key "
                    "file. If it belongs to vaults you already have, they are in another notes "
                    "folder: press 'Choose notes folder' and pick that folder instead of creating "
                    "new vaults. Otherwise, choose a new file name."
                ),
            }
        key: VaultKey | None = None
        try:
            vault_id = str(uuid.uuid4())
            key = generate_key_file(
                selected,
                vault_id,
                definition["name"],
                passphrase=str(passphrase or ""),
            )
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
                "protected": bool(str(passphrase or "")),
            }
        except (OSError, KeyFileError, VaultStoreError, ValueError) as exc:
            return {"error": "damaged", "message": str(exc)}
        finally:
            if key is not None:
                key.wipe()

    @bridge_method
    def initialize_vaults(
        self,
        key_paths: Mapping[str, Path | str] | None = None,
        passphrases: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Create the built-in Encrypted and Personal vaults if needed.

        ``passphrases`` (M10) optionally wraps each new key file; an empty
        value keeps the plain form of section 4.2.
        """
        paths = key_paths or {}
        phrases = passphrases or {}
        created: list[str] = []
        for space_id in SPACE_DEFINITIONS:
            store = self.vault_stores[space_id]
            if store.has_header:
                continue
            name = SPACE_DEFINITIONS[space_id]["name"]
            result = self.create_vault(
                space_id,
                paths.get(space_id, paths.get(name)),
                passphrase=str(phrases.get(space_id, phrases.get(name, "")) or ""),
            )
            if result.get("error"):
                return {"error": result["error"], "message": result.get("message", ""), "created": created}
            created.append(space_id)
        return {"ok": True, "created": created}

    @bridge_method
    def unlock_vault(self, space_id: str, passphrase: str = "") -> dict[str, Any]:
        """Load an external key, verify the vault, and decrypt notes in memory.

        Files that cannot be decrypted are skipped, counted and reported in
        ``warnings`` rather than locking the user out of the whole vault; they
        are never overwritten (M7, security rule 8).  A wrapped key file
        (section 4.5, M10) needs its passphrase: without one the answer is
        ``passphrase_required`` so the dialog can ask, and a wrong one is
        ``wrong_passphrase``.  The passphrase is never stored or echoed.
        """
        space = self._known_space(space_id)
        if space in self.plain_stores:
            return _error("invalid_space", f"{SPACE_NAMES[space]} is always open and cannot be unlocked")
        store = self.vault_stores[space]
        if store.header_error is not None:
            return {"error": "damaged", "message": str(store.header_error)}
        if not store.has_header:
            name = SPACE_DEFINITIONS[space]["name"]
            return {
                "error": "not_initialized",
                "message": (
                    f"There is no {name} vault in the notes folder {self.config.notes_root}. "
                    "If your vaults are in another folder, choose it with 'Choose notes folder'; "
                    "otherwise create them with 'New vault'."
                ),
            }

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
            key = load_key_file(path, passphrase=str(passphrase or ""))
            count = store.unlock(key)
        except PassphraseRequired:
            return {
                "error": "passphrase_required",
                "message": "This key file is protected. Enter its passphrase to unlock.",
            }
        except WrongPassphrase:
            return {
                "error": "wrong_passphrase",
                "message": "Wrong passphrase for this key file.",
            }
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
        """AutoLock callback (timer thread): waits for any call in flight, so a
        vault never locks halfway through a save."""
        with self._calls:
            locked = self._lock_all(emit=False)
        if locked:
            emit_event(self.window, "vault_locked", {"space_ids": locked})

    def _any_vault_unlocked(self) -> bool:
        return any(not store.locked for store in self.vault_stores.values())

    @bridge_method
    def ready_to_close(self) -> dict[str, bool]:
        """The page has saved its last edit; app.py may now close the window."""
        self.page_saved_for_close.set()
        return {"ok": True}

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
            if "panels_collapsed" in look and not isinstance(look["panels_collapsed"], bool):
                return {"error": "invalid_settings", "message": "Panels collapsed must be true or false"}
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
        backup_changes: dict[str, Any] = {}
        if "backup" in changes:
            requested = changes["backup"]
            if not isinstance(requested, Mapping):
                return {"error": "invalid_settings", "message": "Backup settings must be an object"}
            # drive_folder_id and last_backup are valid settings keys, but the
            # app owns them; anything else in the block is refused (12f).
            unknown_backup = sorted(
                set(requested) - BACKUP_SETTING_KEYS - {"drive_folder_id", "last_backup"}
            )
            if unknown_backup:
                return {
                    "error": "invalid_settings",
                    "message": f"Unknown backup setting(s): {', '.join(unknown_backup)}",
                }
            if "interval_minutes" in requested:
                try:
                    interval = float(requested["interval_minutes"])
                except (TypeError, ValueError):
                    return {"error": "invalid_settings", "message": "Backup interval must be a number"}
                if not 5.0 <= interval <= 1440.0:
                    return {
                        "error": "invalid_settings",
                        "message": "Auto-backup must be between 5 minutes and 1 day",
                    }
                backup_changes["interval_minutes"] = interval
            if "enabled" in requested:
                # "Back up every N minutes" only makes sense once signed in,
                # but the setting is remembered either way so reconnecting
                # resumes the schedule the user chose.
                backup_changes["enabled"] = bool(requested["enabled"])
            # drive_folder_id and last_backup are written by the app only;
            # anything else in the block is dropped rather than persisted.
            changes = {key: value for key, value in changes.items() if key != "backup"}
            if backup_changes:
                changes["backup"] = backup_changes
        # The notes folder is moved with the native folder dialog
        # (choose_notes_folder), never by a settings write: rule 12f keeps the
        # frontend from naming paths, and a hostile value here could otherwise
        # re-point the whole app at an arbitrary folder and create it.
        if "notes_root" in changes:
            return {
                "error": "invalid_settings",
                "message": "Use the folder dialog to change the notes folder.",
            }
        # Key-file locations likewise come only from the native dialogs
        # (unlock, setup).  A path from the page could be malformed enough to
        # stop the next start, or a \\server\share path that every get_state
        # would then touch.
        if "vaults" in changes:
            return {
                "error": "invalid_settings",
                "message": "Key files are chosen with the file dialog.",
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
            self._build_plain_stores()
            self._pending_key_paths.clear()
            self._build_vault_stores()
            for index in self.link_indexes.values():
                index.clear()
            self._plain_fingerprints.clear()
            self._sync_plain_indexes()
        if "autolock_minutes" in changes:
            try:
                self.autolock.set_minutes(float(changes["autolock_minutes"]))
            except (TypeError, ValueError):
                pass
        if backup_changes:
            self.backup.configure(**self._backup_schedule_settings())
        return updated

    # ------------------------------------------------------------------
    # Google Drive backup (section 8)
    # ------------------------------------------------------------------
    def _emit(self, event_name: str, data: Any) -> None:
        """Send one Python-originated event to the frontend (section 4.8)."""
        emit_event(self.window, event_name, data)

    def _manifest_path(self) -> Path:
        """``backup_manifest.json`` in the app-settings folder (section 3.2)."""
        return self.app_dir / MANIFEST_NAME

    def _backup_context(self) -> dict[str, Any]:
        """Everything the worker needs for one run, read fresh each time."""
        backup = self.config.get("backup", {})
        folder_id = backup.get("drive_folder_id") if isinstance(backup, Mapping) else None
        manifest = BackupManifest(self._manifest_path())
        manifest.load()
        return {
            "notes_root": self.config.notes_root,
            "manifest": manifest,
            "folder_id": str(folder_id) if folder_id else None,
            "on_folder": self._remember_drive_folder,
        }

    def _backup_schedule_settings(self) -> dict[str, Any]:
        """The two settings that drive the auto-backup timer."""
        backup = self.config.get("backup", {})
        if not isinstance(backup, Mapping):
            backup = {}
        try:
            interval = float(backup.get("interval_minutes", 60))
        except (TypeError, ValueError):
            interval = 60.0
        return {"enabled": bool(backup.get("enabled")), "interval_minutes": interval}

    def _remember_drive_folder(self, folder_id: str) -> None:
        """Persist the id of the "VaultNotes Backup" folder (section 8.2)."""
        with self._calls:  # backup thread
            self.config.update({"backup": {"drive_folder_id": str(folder_id)}})

    def _drive_connected(self) -> bool:
        """Whether a Google sign-in is stored, without any network call."""
        return is_connected(self.drive_store)

    def _drive_service(self) -> GoogleDriveClient:
        """Build a Drive client from the stored refresh token.

        Raises :class:`DriveAuthError` (turned into a ``backup_done`` error by
        the runner) when the app is not connected or Google refuses the token.
        """
        from vaultnotes.backup.gdrive_auth import build_drive_service, get_credentials

        credentials = get_credentials(self.app_dir, store=self.drive_store)
        return GoogleDriveClient(build_drive_service(credentials))

    def _backup_finished(self, report: BackupReport) -> None:
        """Stamp the settings with the outcome, after the worker is done."""
        changes: dict[str, Any] = {}
        if report.drive_folder_id:
            changes["drive_folder_id"] = report.drive_folder_id
        if report.kind == "backup" and report.ok:
            changes["last_backup"] = report.finished or _now_stamp()
        if changes:
            with self._calls:  # backup thread
                self.config.update({"backup": changes})

    def _auto_backup_due(self) -> None:
        """Timer callback: back up when the user connected and enabled it."""
        if not self._drive_connected():
            return
        self.backup.start("backup")

    def _backup_state(self) -> dict[str, Any]:
        """Connection and schedule state for the Drive card and Settings."""
        backup = self.config.get("backup", {})
        if not isinstance(backup, Mapping):
            backup = {}
        return {
            "connected": self._drive_connected(),
            "enabled": bool(backup.get("enabled")),
            "interval_minutes": backup.get("interval_minutes", 60),
            "last_backup": backup.get("last_backup"),
            "has_folder": bool(backup.get("drive_folder_id")),
            "folder_name": BACKUP_FOLDER_NAME,
            "running": self.backup.running,
            "kind": self.backup.kind,
            "manifest_entries": len(BackupManifest(self._manifest_path())),
            "client_secret": (self.app_dir / CLIENT_SECRET_NAME).is_file(),
        }

    @bridge_method
    def choose_client_secret(self, file_path: Path | str | None = None) -> dict[str, Any]:
        """Install the Google OAuth client file, picked in a native dialog.

        ``file_path`` is honoured only when no window exists (tests), exactly
        like :meth:`choose_notes_folder` - the page never sends a path.
        """
        if self.window is not None or file_path is None:
            selected = self._choose_client_secret_file()
        elif isinstance(file_path, (str, Path)):
            selected = Path(str(file_path)).expanduser()
        else:
            return _error("invalid_input", "That is not a file path.")
        if selected is None:
            return _error("cancelled", "No file was selected.")
        try:
            install_client_secret(selected, self.app_dir)
        except DriveAuthError as exc:
            return _error(exc.code, exc.message)
        return {"ok": True, "backup": self._backup_state()}

    @bridge_method
    def connect_drive(self) -> dict[str, Any]:
        """Run the Google sign-in flow once and keep only the refresh token.

        The browser window belongs to Python: the frontend just asks for the
        connection (security rule 12f).
        """
        try:
            # The browser sign-in can take minutes; keep autosave working.
            with self._calls.released():
                result = sign_in(self.app_dir, store=self.drive_store)
        except DriveAuthError as exc:
            return _error(exc.code, exc.message)
        except FileNotFoundError as exc:
            return _error("client_secret_missing", str(exc))
        self.backup.configure(**self._backup_schedule_settings())
        return result

    @bridge_method
    def disconnect_drive(self) -> dict[str, Any]:
        """Forget the stored token; nothing on Drive is deleted."""
        from vaultnotes.backup.gdrive_auth import disconnect

        try:
            result = disconnect(self.drive_store)
        except DriveAuthError as exc:
            return _error(exc.code, exc.message)
        self.backup.stop()
        self.backup.configure(**self._backup_schedule_settings())
        return result

    @bridge_method
    def backup_now(self) -> dict[str, Any]:
        """Start a one-way backup in the background and answer at once."""
        if self.backup.running:
            return _error("busy", "A backup or restore is already running.")
        if not self._drive_connected():
            return _error(
                "not_connected",
                "Connect Google Drive first (Settings, then 'Connect Google Drive').",
            )
        started = self.backup.start("backup")
        if not started.get("started"):
            return _error("busy", "A backup or restore is already running.")
        return {"ok": True, "started": True, "folder": BACKUP_FOLDER_NAME}

    @bridge_method
    def restore_from_drive(self, target_folder: Path | str | None = None) -> dict[str, Any]:
        """Download the backup into an empty folder chosen in Python.

        ``target_folder`` is honoured only when no window exists (tests and
        scripts), exactly like :meth:`choose_notes_folder` - the desktop app
        always asks the native dialog, so the frontend never sends a path.
        """
        if self.backup.running:
            return _error("busy", "Wait for the running backup to finish first.")
        if not self._drive_connected():
            return _error("not_connected", "Connect Google Drive first.")

        if self.window is not None or target_folder is None:
            selected = self._choose_folder()
            cancelled_message = "No folder was selected, so nothing was restored."
        else:
            if not isinstance(target_folder, (str, Path)) or not str(target_folder).strip():
                return {"error": "invalid_folder", "message": "That is not a folder path."}
            candidate = Path(str(target_folder)).expanduser()
            if not candidate.is_absolute():
                return {
                    "error": "invalid_folder",
                    "message": "Restore needs an absolute folder path.",
                }
            selected = candidate.resolve()
            cancelled_message = "No folder was selected, so nothing was restored."
        if selected is None:
            return {"error": "cancelled", "message": cancelled_message}
        if selected.exists() and any(selected.iterdir()):
            return {
                "error": "target_not_empty",
                "message": "Restore needs an empty folder so nothing of yours is overwritten.",
            }

        started = self.backup.start("restore", selected)
        if not started.get("started"):
            return _error("busy", "A backup or restore is already running.")
        return {"ok": True, "started": True, "target": str(selected)}

    @bridge_method
    def prune_drive_backup(self) -> dict[str, Any]:
        """Delete Drive copies of files removed from this computer over 30 days
        ago (M10).  Only entries the manifest still remembers are considered.
        """
        if self.backup.running:
            return _error("busy", "Wait for the running backup to finish first.")
        if not self._drive_connected():
            return _error("not_connected", "Connect Google Drive first.")
        from vaultnotes.backup.gdrive_backup import prune_deleted

        context = self._backup_context()
        try:
            client = self._drive_service()
        except DriveAuthError as exc:
            return _error(exc.code, exc.message)
        except Exception as exc:  # noqa: BLE001 - the bridge never crashes
            return _error("network", f"Google Drive could not be reached ({type(exc).__name__}).")
        try:
            removed = prune_deleted(context["notes_root"], context["manifest"], client)
        except BackupError as exc:
            return _error(exc.code, exc.message)
        except Exception as exc:  # noqa: BLE001
            return _error("network", f"The cleanup stopped early ({type(exc).__name__}).")
        return {"ok": True, "removed": len(removed)}

    # ------------------------------------------------------------------
    # The CMD space: a shell, and its recent and favorite commands
    # ------------------------------------------------------------------
    def _terminal_settings(self) -> dict[str, Any]:
        block = self.config.data.get("terminal")
        if not isinstance(block, dict):
            block = {"enabled": False, "shell": "cmd", "recent": [], "favorites": []}
            self.config.data["terminal"] = block
        return block

    def _terminal_lists(self) -> dict[str, list[str]]:
        block = self._terminal_settings()
        return {
            "recent": clean_command_list(block.get("recent"), MAX_RECENT),
            "favorites": clean_command_list(block.get("favorites"), MAX_FAVORITES),
        }

    def _save_terminal(self, **changes: Any) -> None:
        self._terminal_settings().update(changes)
        self.config.save()

    def _require_terminal(self) -> None:
        if self._terminal_settings().get("enabled") is not True:
            raise BridgeError("terminal_off", "The CMD space is off. Turn it on first.")

    @bridge_method
    def terminal_state(self) -> dict[str, Any]:
        """Whether the CMD space is on, the shells this PC has, and the lists."""
        block = self._terminal_settings()
        enabled = block.get("enabled") is True
        shells = available_shells()
        shell = block.get("shell")
        if shells and shell not in {row["id"] for row in shells}:
            shell = shells[0]["id"]
        return {
            "enabled": enabled,
            "shells": shells,
            "shell": shell,
            "running": self.terminal.running() if enabled else [],
            **self._terminal_lists(),
        }

    @bridge_method
    def terminal_enable(self) -> dict[str, Any]:
        """Turn the CMD space on, but only after the user says yes in a native dialog.

        Python asks in a Windows dialog, not the page, so nothing running in
        the page can switch the shell on by itself.
        """
        if self._terminal_settings().get("enabled") is True:
            return {"ok": True, "enabled": True}
        if self.window is None:
            raise BridgeError("no_window", "The CMD space can only be turned on in the app window.")
        if not available_shells():
            raise BridgeError("no_shell", "No shell was found on this PC.")
        try:
            with self._calls.released():
                allowed = self.window.create_confirmation_dialog(
                    "Turn on the CMD space?",
                    "The CMD space runs a real shell (CMD, PowerShell or Git Bash) inside VaultNotes. "
                    "Anything typed there runs on this PC with your permissions, exactly like a "
                    "normal command window.\n\nTurn it on?",
                )
        except Exception as exc:  # noqa: BLE001
            raise BridgeError("dialog_failed", f"The question could not be shown ({type(exc).__name__}).") from exc
        if allowed is not True:
            return _error("cancelled", "The CMD space stays off.")
        self._save_terminal(enabled=True)
        return {"ok": True, "enabled": True}

    @bridge_method
    def terminal_disable(self) -> dict[str, Any]:
        """Stop the shell and turn the CMD space off (no question needed)."""
        self.terminal.stop()
        self._save_terminal(enabled=False)
        return {"ok": True, "enabled": False}

    @bridge_method
    def terminal_start(self, shell_id: str, cols: int = 80, rows: int = 24) -> dict[str, Any]:
        """Start a shell by id (``cmd``, ``powershell``, ``bash``) for a new tab."""
        self._require_terminal()
        started = self.terminal.start(shell_id, cols, rows)
        if self._terminal_settings().get("shell") != shell_id:
            self._save_terminal(shell=shell_id)
        return {"ok": True, **started}

    @bridge_method
    def terminal_write(self, session_id: str, data: str) -> dict[str, bool]:
        """Keys typed (or text pasted) into the terminal."""
        self._require_terminal()
        self.terminal.write(session_id, data)
        return {"ok": True}

    @bridge_method
    def terminal_resize(self, session_id: str, cols: int, rows: int) -> dict[str, bool]:
        """The terminal's size in character cells changed."""
        self._require_terminal()
        self.terminal.resize(session_id, cols, rows)
        return {"ok": True}

    @bridge_method
    def terminal_stop(self, session_id: str | None = None) -> dict[str, bool]:
        """End one tab's shell, or every shell when no id is given."""
        self.terminal.stop(session_id)
        return {"ok": True}

    @bridge_method
    def terminal_remember(self, command: str) -> dict[str, Any]:
        """Put a command that was just run at the top of Recent.

        A command with a leading space, or more than one line, is not kept.
        """
        text = clean_command(command)
        lists = self._terminal_lists()
        if text is None:
            return {"ok": True, "saved": False, **lists}
        lists["recent"] = remember(lists["recent"], text, MAX_RECENT)
        self._save_terminal(recent=lists["recent"])
        return {"ok": True, "saved": True, **lists}

    @bridge_method
    def terminal_set_favorite(self, command: str, favorite: bool) -> dict[str, Any]:
        """Star or unstar a command."""
        text = clean_command(command)
        if text is None:
            raise BridgeError(
                "invalid_input", "That is not a command that can be kept (one line, no leading space)."
            )
        lists = self._terminal_lists()
        if favorite is True:
            if text not in lists["favorites"]:
                if len(lists["favorites"]) >= MAX_FAVORITES:
                    raise BridgeError("too_many", f"You can keep up to {MAX_FAVORITES} favorite commands.")
                lists["favorites"] = [*lists["favorites"], text]
        else:
            lists["favorites"] = [item for item in lists["favorites"] if item != text]
        self._save_terminal(favorites=lists["favorites"])
        return {"ok": True, **lists}

    @bridge_method
    def terminal_forget(self, command: str) -> dict[str, Any]:
        """Take one command off Recent."""
        lists = self._terminal_lists()
        lists["recent"] = [item for item in lists["recent"] if item != command]
        self._save_terminal(recent=lists["recent"])
        return {"ok": True, **lists}

    @bridge_method
    def terminal_clear_recent(self) -> dict[str, Any]:
        """Empty Recent; favorites stay."""
        self._save_terminal(recent=[])
        return {"ok": True, **self._terminal_lists()}

    # ------------------------------------------------------------------
    # The SQL space: saved connections and query tabs
    # ------------------------------------------------------------------
    def _sql_settings(self) -> dict[str, Any]:
        block = self.config.data.get("sql")
        if not isinstance(block, dict):
            block = {"enabled": False}
            self.config.data["sql"] = block
        return block

    def _sql_enabled(self) -> bool:
        return self._sql_settings().get("enabled") is True

    def _require_sql(self) -> None:
        if not self._sql_enabled():
            raise BridgeError("sql_off", "The SQL space is off. Turn it on first.")

    def _sql_public(self, profile: dict[str, Any]) -> dict[str, Any]:
        has_password = False
        if profile.get("auth") == "sql":
            try:
                has_password = self.sql_passwords.get(profile["id"]) is not None
            except SqlError:
                has_password = False
        return public_connection(profile, has_password)

    def _sql_connections(self) -> list[dict[str, Any]]:
        return [self._sql_public(profile) for profile in self.sql_store.list_connections()]

    def _sql_password_for(self, profile: dict[str, Any], typed: Any = None) -> str | None:
        """The password to sign in with: one just typed, else the saved one."""
        if profile.get("auth") != "sql":
            return None
        if isinstance(typed, str) and typed:
            return typed
        return self.sql_passwords.get(profile["id"]) if profile.get("id") else None

    def _choose_sqlite_file(self) -> Path | None:
        """Open a native picker for a SQLite database file."""
        if self.window is None:
            return None
        selected = self._file_dialog("OPEN", allow_multiple=False, file_types=SQLITE_FILE_TYPES)
        if isinstance(selected, (list, tuple)):
            selected = selected[0] if selected else None
        if not selected:
            return None
        return Path(str(selected)).expanduser().resolve()

    def _sqlite_file(self, file_path: Path | str | None) -> Path | None:
        """A SQLite file from the native dialog (or a test's path); None when cancelled."""
        if self.window is None and file_path:
            path: Path | None = Path(file_path).expanduser().resolve()
        else:
            path = self._choose_sqlite_file()
        if path is None:
            return None
        if not path.is_file():
            raise BridgeError("not_found", "That file was not found.")
        try:
            with path.open("rb") as handle:
                header = handle.read(16)
        except OSError as exc:
            detail = exc.strerror or type(exc).__name__
            raise BridgeError("io_error", f"That file could not be read ({detail}).") from exc
        if header and header != b"SQLite format 3\x00":
            raise BridgeError("invalid_input", f"{path.name} is not a SQLite database.")
        return path

    @bridge_method
    def sql_state(self) -> dict[str, Any]:
        """Whether the SQL space is on, the saved connections and the open tabs."""
        enabled = self._sql_enabled()
        return {
            "enabled": enabled,
            "driver": mssql_driver(),
            "connections": self._sql_connections() if enabled else [],
            "queries": self.sql_store.list_queries() if enabled else [],
            "vtables": self.vt_store.list() if enabled else [],
            "sessions": self.sql.sessions() if enabled else [],
        }

    @bridge_method
    def sql_enable(self) -> dict[str, Any]:
        """Turn the SQL space on, but only after the user says yes in a native dialog.

        Python asks in a Windows dialog, not the page, so nothing running in
        the page can switch it on by itself.
        """
        if self._sql_enabled():
            return {"ok": True, "enabled": True}
        if self.window is None:
            raise BridgeError("no_window", "The SQL space can only be turned on in the app window.")
        try:
            with self._calls.released():
                allowed = self.window.create_confirmation_dialog(
                    "Turn on the SQL space?",
                    "The SQL space connects to SQL Server and SQLite databases and runs the SQL you "
                    "write there with your sign-in, including statements that change data.\n\n"
                    "Saved connections are kept on this PC, not in your notes folder, and SQL "
                    "login passwords go in the Windows Credential Manager.\n\nTurn it on?",
                )
        except Exception as exc:  # noqa: BLE001
            raise BridgeError("dialog_failed", f"The question could not be shown ({type(exc).__name__}).") from exc
        if allowed is not True:
            return _error("cancelled", "The SQL space stays off.")
        self._sql_settings()["enabled"] = True
        self.config.save()
        return {"ok": True, "enabled": True}

    @bridge_method
    def sql_disable(self) -> dict[str, Any]:
        """Close every query tab and turn the SQL space off; connections stay saved."""
        self.sql.close()
        self._sql_settings()["enabled"] = False
        self.config.save()
        return {"ok": True, "enabled": False}

    @bridge_method
    def sql_save_connection(self, connection: dict[str, Any], password: str | None = None) -> dict[str, Any]:
        """Add a connection, or change the one named by ``connection["id"]``.

        A password is saved only when one is typed; leaving the box empty keeps
        the saved one.  Switching to Windows sign-in forgets it.
        """
        self._require_sql()
        if password is not None and (not isinstance(password, str) or len(password) > 1024):
            raise BridgeError("invalid_input", "That password was not accepted.")
        profile = clean_profile(connection)
        connection_id = connection.get("id")
        if connection_id is not None:
            old = self.sql_store.get_connection(connection_id)
            if old["engine"] != profile["engine"]:
                raise BridgeError("invalid_input", "A connection cannot change its database type. Make a new one.")
            profile["file"] = old["file"]
        elif profile["engine"] == "sqlite":
            raise BridgeError("invalid_input", "Add a SQLite connection with SQLite file… instead.")
        saved = self.sql_store.save_connection(profile, connection_id)
        if saved["auth"] == "sql" and password:
            self.sql_passwords.set(saved["id"], password)
        elif saved["auth"] != "sql":
            self.sql_passwords.delete(saved["id"])
        return {"ok": True, "connection": self._sql_public(saved), "connections": self._sql_connections()}

    @bridge_method
    def sql_add_sqlite(self, file_path: Path | str | None = None) -> dict[str, Any]:
        """Add a SQLite connection to a file chosen in the native dialog."""
        self._require_sql()
        path = self._sqlite_file(file_path)
        if path is None:
            return _error("cancelled", "No file was chosen.")
        self._require_sql()  # again: the dialog let other calls run
        profile = clean_profile({"engine": "sqlite", "name": path.stem[:100] or "SQLite"})
        profile["file"] = str(path)
        saved = self.sql_store.save_connection(profile)
        return {"ok": True, "connection": self._sql_public(saved), "connections": self._sql_connections()}

    @bridge_method
    def sql_choose_sqlite_file(self, connection_id: str, file_path: Path | str | None = None) -> dict[str, Any]:
        """Point a SQLite connection at another file, chosen in the native dialog."""
        self._require_sql()
        if self.sql_store.get_connection(connection_id)["engine"] != "sqlite":
            raise BridgeError("invalid_input", "Only a SQLite connection has a file.")
        path = self._sqlite_file(file_path)
        if path is None:
            return _error("cancelled", "No file was chosen.")
        self._require_sql()
        profile = self.sql_store.get_connection(connection_id)
        profile["file"] = str(path)
        saved = self.sql_store.save_connection(profile, connection_id)
        return {"ok": True, "connection": self._sql_public(saved), "connections": self._sql_connections()}

    @bridge_method
    def sql_delete_connection(self, connection_id: str) -> dict[str, Any]:
        """Forget a connection, its saved queries and its password, and close its tabs."""
        self._require_sql()
        self.sql.close_connection(connection_id)
        if self.sql_store.delete_connection(connection_id):
            self.sql_passwords.delete(connection_id)
        return {"ok": True, "connections": self._sql_connections(), "queries": self.sql_store.list_queries()}

    @bridge_method
    def sql_save_query(self, query: dict[str, Any]) -> dict[str, Any]:
        """Save SQL on a connection: a new query, or ``query["id"]`` changed.

        Saving an existing query with another ``connection`` moves it there.
        """
        self._require_sql()
        if not isinstance(query, dict):
            raise BridgeError("invalid_input", "A saved query must be an object.")
        connection_id = query.get("connection")
        if not isinstance(connection_id, str):
            raise BridgeError("invalid_input", "A saved query needs a connection.")
        query_id = query.get("id")
        if query_id is not None and not isinstance(query_id, str):
            raise BridgeError("invalid_input", "That saved query does not exist any more.")
        saved = self.sql_store.save_query(
            connection_id, clean_query_name(query.get("name")), clean_query_text(query.get("text")), query_id
        )
        return {"ok": True, "query": saved, "queries": self.sql_store.list_queries()}

    @bridge_method
    def sql_get_query(self, query_id: str) -> dict[str, Any]:
        """One saved query with its SQL, to open in a tab."""
        self._require_sql()
        return {"ok": True, "query": self.sql_store.get_query(query_id)}

    @bridge_method
    def sql_delete_query(self, query_id: str) -> dict[str, Any]:
        """Forget a saved query; tabs showing it keep their text."""
        self._require_sql()
        self.sql_store.delete_query(query_id)
        return {"ok": True, "queries": self.sql_store.list_queries()}

    @bridge_method
    def sql_test_connection(self, connection: dict[str, Any], password: str | None = None) -> dict[str, Any]:
        """Sign in with the dialog's fields and hang up, without saving anything."""
        self._require_sql()
        profile = clean_profile(connection)
        connection_id = connection.get("id")
        if connection_id is not None:
            old = self.sql_store.get_connection(connection_id)
            profile["id"] = old["id"]
            profile["file"] = old["file"]
        elif profile["engine"] == "sqlite":
            raise BridgeError("invalid_input", "Add a SQLite connection with SQLite file… instead.")
        secret = self._sql_password_for(profile, password)
        started = time.monotonic()
        # Signing in to a far server takes a while; notes keep saving meanwhile.
        with self._calls.released():
            self.sql.test(profile, secret)
        return {"ok": True, "elapsedMs": int((time.monotonic() - started) * 1000)}

    def _vt_public(self) -> dict[str, Any]:
        """The virtual tables' own database, shaped like a saved connection."""
        return {
            "id": VT_CONNECTION_ID,
            "name": VT_NAME,
            "engine": "sqlite",
            "engineName": "SQLite",
            "server": "",
            "database": "",
            "auth": "windows",
            "username": "",
            "encrypt": False,
            "trust_cert": False,
            "file": "",
            "where": "vt.db",
            "hasPassword": False,
        }

    @bridge_method
    def sql_open(self, connection_id: str) -> dict[str, Any]:
        """Connect a new query tab to a saved connection, or to ``vt`` for the virtual tables."""
        self._require_sql()
        if connection_id == VT_CONNECTION_ID:
            profile = self.vt_store.profile()
            session = self.sql.open(profile, None)
            return {"ok": True, **session, "connection": self._vt_public()}
        profile = self.sql_store.get_connection(connection_id)
        secret = self._sql_password_for(profile)
        self.sql.check_room()
        with self._calls.released():
            session = self.sql.open(profile, secret)
        if not self._sql_enabled():
            self.sql.close(session["id"])
            raise BridgeError("sql_off", "The SQL space was turned off.")
        return {"ok": True, **session, "connection": self._sql_public(profile)}

    @bridge_method
    def sql_run(self, session_id: str, text: str, query_id: str | None = None) -> dict[str, Any]:
        """Start running SQL in a tab; the result arrives as a ``sql_done`` event.

        ``query_id`` names the saved query the tab shows, so its "last run"
        time moves forward.
        """
        self._require_sql()
        started = self.sql.run(session_id, text)
        if isinstance(query_id, str):
            self.sql_store.mark_run(query_id)
        return {"ok": True, **started}

    @bridge_method
    def sql_cancel(self, session_id: str) -> dict[str, Any]:
        """Ask the server to stop the query running in a tab."""
        self._require_sql()
        return {"ok": True, "cancelled": self.sql.cancel(session_id)}

    @bridge_method
    def sql_rows(self, session_id: str, result_index: int, offset: int = 0, limit: int = 200) -> dict[str, Any]:
        """A page of rows of one result of a tab's last run."""
        self._require_sql()
        return {"ok": True, **self.sql.rows(session_id, result_index, offset, limit)}

    @bridge_method
    def sql_copy(self, session_id: str, result_index: int) -> dict[str, Any]:
        """A whole result as tab-separated text with a header row, for the clipboard."""
        self._require_sql()
        return {"ok": True, "text": self.sql.copy_text(session_id, result_index)}

    @bridge_method
    def sql_vt_list(self) -> dict[str, Any]:
        """The virtual tables, counted afresh (SQL in the space may have changed them)."""
        self._require_sql()
        self.vt_store.forget_stale_meta()
        return {"ok": True, "tables": self.vt_store.list()}

    @bridge_method
    def sql_save_vt(self, session_id: str, result_index: int, name: str, replace: bool = False) -> dict[str, Any]:
        """Keep one result of a tab's last run as a virtual table named ``name``.

        Every row is copied into ``vt.db``, so the table outlives the
        connection.  An existing table of that name is replaced only with
        ``replace``; otherwise the answer is ``exists``.
        """
        self._require_sql()
        table = clean_table_name(name)
        origin, result = self.sql.finished_result(session_id, result_index)
        if origin["connection"] == VT_CONNECTION_ID:
            where = "vt.db"
        else:
            try:
                where = self._sql_public(self.sql_store.get_connection(origin["connection"]))["where"]
            except SqlError:
                where = ""
        source = {"connection": origin["connection"], "name": origin["name"], "where": where}
        rows = list(result.rows)  # a snapshot: the tab may run again meanwhile
        # Writing a big result takes a while; notes keep saving meanwhile.
        with self._calls.released():
            saved = self.vt_store.save(
                table, result.names, [kind or "text" for kind in result.kinds], rows,
                source=source, query=origin["query"], replace=replace is True,
            )
        return {"ok": True, "table": saved, "tables": self.vt_store.list()}

    @bridge_method
    def sql_rename_vt(self, name: str, new_name: str) -> dict[str, Any]:
        """Give a virtual table another name."""
        self._require_sql()
        self.vt_store.rename(clean_table_name(name), clean_table_name(new_name))
        return {"ok": True, "tables": self.vt_store.list()}

    @bridge_method
    def sql_delete_vt(self, name: str) -> dict[str, Any]:
        """Drop a virtual table and forget where it came from."""
        self._require_sql()
        self.vt_store.delete(clean_table_name(name))
        return {"ok": True, "tables": self.vt_store.list()}

    @bridge_method
    def sql_close(self, session_id: str | None = None) -> dict[str, bool]:
        """Close one tab's connection, or every one when no id is given."""
        self.sql.close(session_id)
        return {"ok": True}


def bridge_function_names() -> tuple[str, ...]:
    """The Bridge API (section 4.8): every ``@bridge_method`` method of :class:`Api`."""
    return tuple(
        sorted(name for name, value in vars(Api).items() if getattr(value, "_bridge_endpoint", False))
    )


def expose_bridge(window: Any, api: Api) -> None:
    """Give the page exactly the Bridge API and nothing else (security rule 12f).

    ``api`` is deliberately NOT passed to pywebview as ``js_api``.  For a
    js_api object pywebview walks every attribute it can reach and exposes
    what it finds -- here that includes the window, the unlocked vault stores
    and the Drive token store -- and it dispatches any dotted name the page
    sends, underscores included.  Walking the native window also froze the
    app on Windows before the page had loaded.  ``window.expose`` registers
    plain functions by exact name, and ``api`` is only reachable from inside
    them.
    """
    window.expose(*(_endpoint(api, name, window) for name in bridge_function_names()))


def _origin(url: str) -> tuple[str, str]:
    parts = urllib.parse.urlsplit(url)
    return parts.scheme.lower(), parts.netloc.lower()


def _page_is_ours(window: Any) -> bool:
    """Whether the window still shows the app's own page.

    pywebview injects the bridge into whatever page the window navigates to
    (a link or an .html file dropped on it, say), so every call checks the
    page's origin against the one the app was started with.
    """
    home = getattr(window, "real_url", None)
    get_url = getattr(window, "get_current_url", None)
    current = get_url() if callable(get_url) else None
    if not home or not current:
        return True  # nothing loaded yet, or a test double
    return _origin(str(current)) == _origin(str(home))


#: Parameters Python fills in from its own native dialogs.  Tests pass real
#: paths through them; the page must leave them out or null (rule 12f: the
#: frontend never sends file paths), or it could, say, export a decrypted
#: vault note to any folder on disk.
DIALOG_ONLY_PARAMETERS = frozenset(
    {"folder_path", "key_path", "key_paths", "file_path", "file_paths", "dest_path", "target_folder"}
)


def _endpoint(api: Api, name: str, window: Any = None) -> Callable[..., Any]:
    """A plain function named ``name`` that forwards to ``api.<name>``."""
    method = getattr(api, name)
    signature = inspect.signature(method)
    dialog_only = [param for param in signature.parameters if param in DIALOG_ONLY_PARAMETERS]

    def endpoint(*args: Any) -> Any:
        if window is not None and not _page_is_ours(window):
            return _error("forbidden", "Only the VaultNotes page can use VaultNotes.")
        if dialog_only:
            try:
                given = signature.bind(*args).arguments
            except TypeError:
                return _error("invalid_input", "That input was not accepted.")
            if any(given.get(param) is not None for param in dialog_only):
                return _error("invalid_input", "Files and folders are chosen in the file dialog.")
        return method(*args)

    endpoint.__name__ = endpoint.__qualname__ = name
    return endpoint
