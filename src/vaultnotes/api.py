"""Bridge API connecting the pywebview frontend to VaultNotes' engine."""

from __future__ import annotations

import time
import urllib.parse
import uuid
import webbrowser
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from vaultnotes.autolock import AutoLock
from vaultnotes.config import Config, get_config
from vaultnotes.crypto.keyfile import KeyFileError, VaultKey, generate_key_file, load_key_file
from vaultnotes.events import emit_event
from vaultnotes.links import calculate_backlinks, count_links, rename_links_in_body
from vaultnotes.render import render_preview
from vaultnotes.storage.plain_store import PlainStore, make_snippet
from vaultnotes.storage.vault_store import (
    DamagedVaultError,
    VaultLockedError,
    VaultStore,
    VaultStoreError,
    WrongKeyError,
    WrongVaultError,
)

SPACE_DEFINITIONS = {
    "encrypted": {"name": "Encrypted", "colorVar": "--encrypted"},
    "personal": {"name": "Personal", "colorVar": "--personal"},
}


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
            try:
                count = len(store.list_notes())
            except VaultLockedError:
                count = 0
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

    def get_state(self) -> dict[str, Any]:
        """Return spaces, look settings, backup status, and setup state."""
        plain_notes = self.plain_store.list_notes()
        spaces = [
            {
                "id": "plain",
                "name": "Plain",
                "kind": "plain",
                "locked": False,
                "colorVar": "--plain",
                "note_count": len(plain_notes),
            },
            self._space_summary("encrypted"),
            self._space_summary("personal"),
        ]
        needs_setup = any(not self.vault_stores[space_id].has_header for space_id in SPACE_DEFINITIONS)
        return {
            "spaces": spaces,
            "look": self.config.get("look", {}),
            "autolock_minutes": self.config.get("autolock_minutes", 10),
            "last_backup": self.config.get("backup", {}).get("last_backup"),
            "needs_setup": needs_setup,
            "locks_at": self.autolock.locks_at,
        }

    @staticmethod
    def _note_summary(note: Any) -> dict[str, Any]:
        return {
            "id": note.id,
            "title": note.title,
            "snippet": make_snippet(note.body),
            "modified": note.modified,
            "link_count": count_links(note.body),
        }

    @staticmethod
    def _note_result(note: Any, backlinks: list[dict[str, str]] | None = None) -> dict[str, Any]:
        return {
            "id": note.id,
            "title": note.title,
            "body": note.body,
            "modified": note.modified,
            "created": note.created,
            "tags": list(note.tags),
            "snippet": make_snippet(note.body),
            "link_count": count_links(note.body),
            "backlinks": backlinks or [],
        }

    # ------------------------------------------------------------------
    # Note CRUD
    # ------------------------------------------------------------------
    def list_notes(
        self,
        space_id: str,
        query: str = "",
        sort: str = "modified",
    ) -> list[dict[str, Any]] | dict[str, str]:
        """List note summaries in one space; locked vaults return no titles."""
        if space_id == "plain":
            notes = self.plain_store.list_notes(query=query, sort=sort)
            return [self._note_summary(note) for note in notes]
        store = self._vault(space_id)
        if store is None:
            return self._invalid_space(space_id)
        if store.locked:
            return []
        try:
            return [self._note_summary(note) for note in store.list_notes(query=query, sort=sort)]
        except VaultStoreError as exc:
            return {"error": "locked", "message": str(exc)}

    def open_note(self, space_id: str, note_id: str) -> dict[str, Any]:
        """Retrieve a full note and backlinks from the same space."""
        if space_id == "plain":
            try:
                note = self.plain_store.read_note(note_id)
                backlinks = calculate_backlinks(self.plain_store.list_notes(), note.title)
                return self._note_result(note, backlinks)
            except FileNotFoundError:
                return {"error": "not_found", "message": f"Note not found: {note_id}"}
        store = self._vault(space_id)
        if store is None:
            return self._invalid_space(space_id)
        if store.locked:
            return {"error": "locked", "message": "Vault is locked"}
        try:
            note = store.read_note(note_id)
            backlinks = calculate_backlinks(store.list_notes(), note.title)
            return self._note_result(note, backlinks)
        except VaultLockedError:
            return {"error": "locked", "message": "Vault is locked"}
        except (FileNotFoundError, ValueError):
            return {"error": "not_found", "message": f"Note not found: {note_id}"}

    def create_note(self, space_id: str, title: str = "Untitled") -> dict[str, Any]:
        """Create a note in Plain or an unlocked encrypted vault."""
        try:
            if space_id == "plain":
                note = self.plain_store.create_note(title=title)
                return self._note_result(note)
            store = self._vault(space_id)
            if store is None:
                return self._invalid_space(space_id)
            if store.locked:
                return {"error": "locked", "message": "Unlock vault first"}
            note = store.create_note(title=title)
            backlinks = calculate_backlinks(store.list_notes(), note.title)
            return self._note_result(note, backlinks)
        except FileExistsError as exc:
            return {"error": "collision", "message": str(exc)}
        except ValueError as exc:
            return {"error": "invalid_title", "message": str(exc)}
        except VaultStoreError as exc:
            return {"error": "locked", "message": str(exc)}

    def save_note(self, space_id: str, note_id: str, body: str) -> dict[str, Any]:
        """Atomically save a note body, encrypting vault notes first."""
        try:
            if space_id == "plain":
                note = self.plain_store.save_note(note_id, body)
            else:
                store = self._vault(space_id)
                if store is None:
                    return self._invalid_space(space_id)
                if store.locked:
                    return {"error": "locked", "message": "Vault is locked"}
                note = store.save_note(note_id, body)
            self.autolock.touch() if self._any_vault_unlocked() else None
            return {"modified": note.modified}
        except FileNotFoundError:
            return {"error": "not_found", "message": f"Note not found: {note_id}"}
        except ValueError as exc:
            return {"error": "invalid_note", "message": str(exc)}
        except VaultStoreError as exc:
            return {"error": "locked", "message": str(exc)}

    def rename_note(
        self,
        space_id: str,
        note_id: str,
        new_title: str,
        update_links: bool = True,
    ) -> dict[str, Any]:
        """Rename a note and optionally update same-space wikilinks."""
        try:
            if space_id == "plain":
                note, old_title = self.plain_store.rename_note(note_id, new_title)
                other_notes = self.plain_store.list_notes()
                save = self.plain_store.save_note
            else:
                store = self._vault(space_id)
                if store is None:
                    return self._invalid_space(space_id)
                if store.locked:
                    return {"error": "locked", "message": "Vault is locked"}
                note, old_title = store.rename_note(note_id, new_title)
                other_notes = store.list_notes()
                save = store.save_note

            links_updated = 0
            if update_links:
                for other in other_notes:
                    if other.id == note.id:
                        continue
                    new_body, count = rename_links_in_body(other.body, old_title, note.title)
                    if count:
                        save(other.id, new_body)
                        links_updated += count
            return {
                "title": note.title,
                "id": note.id,
                "links_updated": links_updated,
            }
        except FileExistsError:
            return {"error": "collision", "message": "A note with that title already exists"}
        except ValueError as exc:
            return {"error": "invalid_title", "message": str(exc)}
        except FileNotFoundError:
            return {"error": "not_found", "message": f"Note not found: {note_id}"}
        except VaultStoreError as exc:
            return {"error": "locked", "message": str(exc)}

    def count_links_to(self, space_id: str, note_id: str) -> dict[str, int]:
        """Count backlinking notes without crossing space boundaries."""
        try:
            if space_id == "plain":
                note = self.plain_store.read_note(note_id)
                return {"count": len(calculate_backlinks(self.plain_store.list_notes(), note.title))}
            store = self._vault(space_id)
            if store is None or store.locked:
                return {"count": 0}
            note = store.read_note(note_id)
            return {"count": len(calculate_backlinks(store.list_notes(), note.title))}
        except (FileNotFoundError, ValueError, VaultStoreError):
            return {"count": 0}

    def delete_note(self, space_id: str, note_id: str) -> dict[str, Any]:
        """Move a note into that space's trash, still encrypted for vaults."""
        try:
            if space_id == "plain":
                note = self.plain_store.delete_note(note_id)
            else:
                store = self._vault(space_id)
                if store is None:
                    return self._invalid_space(space_id)
                if store.locked:
                    return {"error": "locked", "message": "Vault is locked"}
                note = store.delete_note(note_id)
            return {
                "ok": True,
                "deleted": {"id": note.id, "title": note.title, "modified": note.modified},
            }
        except FileNotFoundError:
            return {"error": "not_found", "message": f"Note not found: {note_id}"}
        except (ValueError, VaultStoreError) as exc:
            return {"error": "invalid_note", "message": str(exc)}

    def restore_note(self, space_id: str, note_id: str) -> dict[str, Any]:
        """Restore a note from that space's trash."""
        try:
            if space_id == "plain":
                note = self.plain_store.restore_note(note_id)
            else:
                store = self._vault(space_id)
                if store is None:
                    return self._invalid_space(space_id)
                if store.locked:
                    return {"error": "locked", "message": "Vault is locked"}
                note = store.restore_note(note_id)
            return {
                "ok": True,
                "note": {"id": note.id, "title": note.title, "modified": note.modified},
            }
        except FileNotFoundError:
            return {"error": "not_found", "message": f"Note not found in trash: {note_id}"}
        except (ValueError, VaultStoreError) as exc:
            return {"error": "invalid_note", "message": str(exc)}

    def list_trash(self, space_id: str) -> list[dict[str, str]] | dict[str, str]:
        """List trash entries without ever returning locked vault titles."""
        if space_id == "plain":
            return [
                {"id": note.id, "title": note.title, "modified": note.modified}
                for note in self.plain_store.list_trash()
            ]
        store = self._vault(space_id)
        if store is None:
            return self._invalid_space(space_id)
        if store.locked:
            return []
        try:
            return [
                {"id": note.id, "title": note.title, "modified": note.modified}
                for note in store.list_trash()
            ]
        except VaultStoreError as exc:
            return {"error": "locked", "message": str(exc)}

    def move_note(self, space_id: str, note_id: str, target_space_id: str) -> dict[str, Any]:
        """Moving notes is intentionally deferred to M5."""
        if target_space_id in SPACE_DEFINITIONS:
            target = self._vault(target_space_id)
            if target is None or target.locked:
                return {"error": "locked", "message": f"Target vault {target_space_id} is locked"}
        return {"error": "not_implemented", "message": "Moving notes between spaces is available in M5"}

    # ------------------------------------------------------------------
    # Markdown links and preview
    # ------------------------------------------------------------------
    def render_preview(self, space_id: str, body: str) -> str:
        """Render Markdown with only same-space unlocked note titles."""
        titles: list[str] = []
        if space_id == "plain":
            titles = [note.title for note in self.plain_store.list_notes()]
        elif (store := self._vault(space_id)) is not None and not store.locked:
            titles = [note.title for note in store.list_notes()]
        return render_preview(body, titles=titles)

    def list_titles(self, space_id: str) -> list[str]:
        """Return autocomplete titles only for an open space."""
        if space_id == "plain":
            return [note.title for note in self.plain_store.list_notes()]
        store = self._vault(space_id)
        if store is None or store.locked:
            return []
        return [note.title for note in store.list_notes()]

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

    def choose_notes_folder(self, folder_path: Path | str | None = None) -> dict[str, Any]:
        """Choose and persist the notes root through a native folder dialog."""
        selected = Path(folder_path).expanduser().resolve() if folder_path is not None else self._choose_folder()
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

        for store in self.vault_stores.values():
            store.lock()
        self.autolock.clear()
        self.config.update({"notes_root": str(selected)})
        self.plain_store = PlainStore(self.config.plain_dir)
        self._pending_key_paths.clear()
        self._build_vault_stores()
        return {"ok": True, "name": selected.name or str(selected)}

    def choose_key_file(self, space_id: str) -> dict[str, Any]:
        """Let the native UI choose a key and keep its path server-side."""
        store = self._vault(space_id)
        if store is None:
            return self._invalid_space(space_id)
        selected = self._choose_file(False, f"{space_id}.vnkey")
        if selected is None:
            return {"error": "key_not_found", "message": "No key file was selected."}
        try:
            selected = self._validate_key_path(selected)
        except ValueError as exc:
            return {"error": "key_inside_notes", "message": str(exc)}
        self._pending_key_paths[space_id] = selected
        return {"ok": True, "name": selected.name}

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

        if key_path is None:
            if self.window is not None:
                selected = self._choose_file(True, f"{space_id}.vnkey")
                if selected is None:
                    return {"error": "cancelled", "message": "Vault setup was cancelled."}
            else:
                selected = self.config.settings_file.parent / "keys" / f"{space_id}.vnkey"
        else:
            selected = Path(key_path)
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

    def unlock_vault(self, space_id: str) -> dict[str, Any]:
        """Load an external key, verify the vault, and decrypt notes in memory."""
        store = self._vault(space_id)
        if store is None:
            return self._invalid_space(space_id)
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
        self.config.set_vault_key_path(SPACE_DEFINITIONS[space_id]["name"], path)
        locks_at = self.autolock.touch()
        return {"ok": True, "count": count, "locks_at": locks_at}

    def lock_vault(self, space_id: str) -> dict[str, Any]:
        """Lock one vault and clear its key and decrypted notes."""
        store = self._vault(space_id)
        if store is None:
            return self._invalid_space(space_id)
        store.lock()
        if not self._any_vault_unlocked():
            self.autolock.clear()
        emit_event(self.window, "vault_locked", {"space_id": space_id})
        return {"ok": True, "space_id": space_id}

    def _lock_all(self, emit: bool = True) -> list[str]:
        locked: list[str] = []
        for space_id, store in self.vault_stores.items():
            if not store.locked:
                store.lock()
                locked.append(space_id)
        self.autolock.clear()
        if emit and locked:
            emit_event(self.window, "vault_locked", {"space_ids": locked})
        return locked

    def lock_all(self) -> dict[str, Any]:
        """Lock every open vault."""
        return {"ok": True, "locked_spaces": self._lock_all(emit=True)}

    def _auto_lock_expired(self) -> None:
        """AutoLock callback; never touches the frontend synchronously."""
        self._lock_all(emit=True)

    def _any_vault_unlocked(self) -> bool:
        return any(not store.locked for store in self.vault_stores.values())

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
    def open_external(self, url: str) -> dict[str, Any]:
        """Open only http/https/mailto URLs in the user's browser."""
        try:
            parsed = urllib.parse.urlparse(url)
            if parsed.scheme.lower() in ("http", "https", "mailto"):
                webbrowser.open(url)
                return {"ok": True}
        except Exception:
            pass
        return {"error": "invalid_url", "message": "Blocked unsafe URL scheme"}

    def get_settings(self) -> dict[str, Any]:
        """Return a copy of persisted settings."""
        return self.config.data

    def update_settings(self, changes: dict[str, Any]) -> dict[str, Any]:
        """Update settings and keep the Python auto-lock timer in sync.

        Key paths are validated here as well as during unlock/setup so a
        crafted bridge call cannot place a key file inside the notes root.
        """
        if not isinstance(changes, dict):
            return {"error": "invalid_settings", "message": "Settings changes must be an object"}

        candidate_root = Path(str(changes.get("notes_root", self.config.notes_root))).expanduser().resolve()
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
            self.plain_store = PlainStore(self.config.plain_dir)
            self._pending_key_paths.clear()
            self._build_vault_stores()
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
