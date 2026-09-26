"""Bridge API implementation connecting pywebview to the Python engine."""

from __future__ import annotations

import time
import urllib.parse
import webbrowser
from typing import Any

from vaultnotes.config import Config, get_config
from vaultnotes.events import emit_event
from vaultnotes.links import (
    calculate_backlinks,
    count_links,
    rename_links_in_body,
)
from vaultnotes.render import render_preview
from vaultnotes.storage.plain_store import PlainStore, make_snippet


class Api:
    """Methods exposed to JavaScript via window.pywebview.api."""

    def __init__(self, config: Config | None = None, window: Any = None) -> None:
        self.config = config or get_config()
        self.window = window
        self.plain_store = PlainStore(self.config.plain_dir)

    def set_window(self, window: Any) -> None:
        """Store pywebview window reference for emitting events."""
        self.window = window

    def get_state(self) -> dict[str, Any]:
        """Return application state: spaces list, look settings, and backup info."""
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
            {
                "id": "encrypted",
                "name": "Encrypted",
                "kind": "vault",
                "locked": True,
                "colorVar": "--encrypted",
                "note_count": 0,
                "key_path": self.config.get_vault_key_path("Encrypted"),
            },
            {
                "id": "personal",
                "name": "Personal",
                "kind": "vault",
                "locked": True,
                "colorVar": "--personal",
                "note_count": 0,
                "key_path": self.config.get_vault_key_path("Personal"),
            },
        ]
        return {
            "spaces": spaces,
            "look": self.config.get("look", {}),
            "last_backup": self.config.get("backup", {}).get("last_backup"),
        }

    def list_notes(self, space_id: str, query: str = "", sort: str = "modified") -> list[dict[str, Any]] | dict[str, str]:
        """List note summaries in space_id, optionally filtered by query and sorted."""
        if space_id == "plain":
            notes = self.plain_store.list_notes(query=query, sort=sort)
            return [
                {
                    "id": n.id,
                    "title": n.title,
                    "snippet": make_snippet(n.body),
                    "modified": n.modified,
                    "link_count": count_links(n.body),
                }
                for n in notes
            ]
        elif space_id in ("encrypted", "personal"):
            # Vaults are locked in M2
            return []
        return {"error": "invalid_space", "message": f"Space not found: {space_id}"}

    def open_note(self, space_id: str, note_id: str) -> dict[str, Any]:
        """Retrieve full note body and backlinks."""
        if space_id == "plain":
            try:
                note = self.plain_store.read_note(note_id)
                all_notes = self.plain_store.list_notes()
                backlinks = calculate_backlinks(all_notes, note.title)
                return {
                    "id": note.id,
                    "title": note.title,
                    "body": note.body,
                    "modified": note.modified,
                    "backlinks": backlinks,
                }
            except FileNotFoundError:
                return {"error": "not_found", "message": f"Note not found: {note_id}"}
        elif space_id in ("encrypted", "personal"):
            return {"error": "locked", "message": "Vault is locked"}
        return {"error": "invalid_space", "message": f"Space not found: {space_id}"}

    def create_note(self, space_id: str, title: str = "Untitled") -> dict[str, Any]:
        """Create a new note in space_id."""
        if space_id == "plain":
            note = self.plain_store.create_note(title=title)
            return {
                "id": note.id,
                "title": note.title,
                "body": note.body,
                "modified": note.modified,
                "snippet": "",
                "link_count": 0,
                "backlinks": [],
            }
        elif space_id in ("encrypted", "personal"):
            return {"error": "locked", "message": "Unlock vault first"}
        return {"error": "invalid_space", "message": f"Space not found: {space_id}"}

    def save_note(self, space_id: str, note_id: str, body: str) -> dict[str, Any]:
        """Save updated note body to disk."""
        if space_id == "plain":
            try:
                note = self.plain_store.save_note(note_id, body)
                return {"modified": note.modified}
            except FileNotFoundError:
                return {"error": "not_found", "message": f"Note not found: {note_id}"}
        elif space_id in ("encrypted", "personal"):
            return {"error": "locked", "message": "Vault is locked"}
        return {"error": "invalid_space", "message": f"Space not found: {space_id}"}

    def rename_note(
        self, space_id: str, note_id: str, new_title: str, update_links: bool = True
    ) -> dict[str, Any]:
        """Rename note and optionally update links referencing it across other notes."""
        if space_id == "plain":
            try:
                note, old_title = self.plain_store.rename_note(note_id, new_title)
                links_updated = 0
                if update_links:
                    other_notes = self.plain_store.list_notes()
                    for other in other_notes:
                        if other.id != note.id:
                            new_body, count = rename_links_in_body(other.body, old_title, note.title)
                            if count > 0:
                                self.plain_store.save_note(other.id, new_body)
                                links_updated += count

                return {
                    "title": note.title,
                    "id": note.id,
                    "links_updated": links_updated,
                }
            except FileExistsError:
                return {"error": "collision", "message": "A note with that title already exists"}
            except ValueError as ve:
                return {"error": "invalid_title", "message": str(ve)}
            except FileNotFoundError:
                return {"error": "not_found", "message": f"Note not found: {note_id}"}
        elif space_id in ("encrypted", "personal"):
            return {"error": "locked", "message": "Vault is locked"}
        return {"error": "invalid_space", "message": f"Space not found: {space_id}"}

    def count_links_to(self, space_id: str, note_id: str) -> dict[str, int]:
        """Count how many notes link to note_id in space_id."""
        if space_id == "plain":
            try:
                note = self.plain_store.read_note(note_id)
                all_notes = self.plain_store.list_notes()
                backlinks = calculate_backlinks(all_notes, note.title)
                return {"count": len(backlinks)}
            except FileNotFoundError:
                return {"count": 0}
        return {"count": 0}

    def delete_note(self, space_id: str, note_id: str) -> dict[str, Any]:
        """Move note to .trash/ folder."""
        if space_id == "plain":
            try:
                note = self.plain_store.delete_note(note_id)
                return {
                    "ok": True,
                    "deleted": {
                        "id": note.id,
                        "title": note.title,
                        "modified": note.modified,
                    },
                }
            except FileNotFoundError:
                return {"error": "not_found", "message": f"Note not found: {note_id}"}
        elif space_id in ("encrypted", "personal"):
            return {"error": "locked", "message": "Vault is locked"}
        return {"error": "invalid_space", "message": f"Space not found: {space_id}"}

    def restore_note(self, space_id: str, note_id: str) -> dict[str, Any]:
        """Restore note from .trash/ folder."""
        if space_id == "plain":
            try:
                note = self.plain_store.restore_note(note_id)
                return {
                    "ok": True,
                    "note": {
                        "id": note.id,
                        "title": note.title,
                        "modified": note.modified,
                    },
                }
            except FileNotFoundError:
                return {"error": "not_found", "message": f"Note not found in trash: {note_id}"}
        elif space_id in ("encrypted", "personal"):
            return {"error": "locked", "message": "Vault is locked"}
        return {"error": "invalid_space", "message": f"Space not found: {space_id}"}

    def list_trash(self, space_id: str) -> list[dict[str, str]] | dict[str, str]:
        """List notes in .trash/ for space_id."""
        if space_id == "plain":
            trash_notes = self.plain_store.list_trash()
            return [
                {
                    "id": n.id,
                    "title": n.title,
                    "modified": n.modified,
                }
                for n in trash_notes
            ]
        elif space_id in ("encrypted", "personal"):
            return []
        return {"error": "invalid_space", "message": f"Space not found: {space_id}"}

    def move_note(self, space_id: str, note_id: str, target_space_id: str) -> dict[str, Any]:
        """Move a note between spaces."""
        if target_space_id in ("encrypted", "personal"):
            return {"error": "locked", "message": f"Target vault {target_space_id} is locked"}
        return {"error": "not_implemented", "message": "Moving notes between spaces is available in M5"}

    def render_preview(self, space_id: str, body: str) -> str:
        """Render markdown to HTML, resolving wikilinks in space_id."""
        titles: list[str] = []
        if space_id == "plain":
            titles = [n.title for n in self.plain_store.list_notes()]
        return render_preview(body, titles=titles)

    def list_titles(self, space_id: str) -> list[str]:
        """Return titles of all notes in space_id for autocomplete."""
        if space_id == "plain":
            return [n.title for n in self.plain_store.list_notes()]
        return []

    def unlock_vault(self, space_id: str) -> dict[str, Any]:
        """Unlock vault (stub until M4)."""
        return {"error": "locked", "message": f"Vault unlock will be implemented in M4"}

    def lock_vault(self, space_id: str) -> dict[str, Any]:
        """Lock vault (stub until M4)."""
        emit_event(self.window, "vault_locked", {"space_id": space_id})
        return {"ok": True}

    def lock_all(self) -> dict[str, Any]:
        """Lock all vaults."""
        locked = ["encrypted", "personal"]
        emit_event(self.window, "vault_locked", {"space_ids": locked})
        return {"ok": True, "locked_spaces": locked}

    def touch(self) -> dict[str, int]:
        """Reset autolock activity timer."""
        autolock_min = self.config.get("autolock_minutes", 10)
        locks_at = int(time.time() * 1000) + autolock_min * 60 * 1000
        return {"locks_at": locks_at}

    def open_external(self, url: str) -> dict[str, Any]:
        """Open http/https/mailto link in user's default browser."""
        try:
            parsed = urllib.parse.urlparse(url)
            if parsed.scheme.lower() in ("http", "https", "mailto"):
                webbrowser.open(url)
                return {"ok": True}
        except Exception:
            pass
        return {"error": "invalid_url", "message": "Blocked unsafe URL scheme"}

    def get_settings(self) -> dict[str, Any]:
        """Return settings dictionary."""
        return self.config.data

    def update_settings(self, changes: dict[str, Any]) -> dict[str, Any]:
        """Update and persist settings."""
        return self.config.update(changes)

    def backup_now(self) -> dict[str, Any]:
        """Trigger immediate backup (stub until M8)."""
        return {"ok": True, "last_backup": "just now"}

    def connect_drive(self) -> dict[str, Any]:
        """Connect Google Drive account (stub until M8)."""
        return {"ok": True, "connected": True}

    def disconnect_drive(self) -> dict[str, Any]:
        """Disconnect Google Drive account (stub until M8)."""
        return {"ok": True, "connected": False}

    def restore_from_drive(self, target_folder: str) -> dict[str, Any]:
        """Restore notes from Google Drive (stub until M8)."""
        return {"ok": True, "restored_files": 0}
