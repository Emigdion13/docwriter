"""Plain-text Markdown storage provider."""

from __future__ import annotations

import os
import re
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from vaultnotes.models import Note
from vaultnotes.storage.atomic import atomic_write

# Disallowed Windows filename characters
INVALID_CHARS_RE = re.compile(r'[\\/:*?"<>|]')


def sanitize_title(title: str) -> str:
    """Sanitize title for use as a filesystem filename.

    Replaces invalid characters with '-', strips whitespace, dots, and trailing dashes.
    """
    cleaned = INVALID_CHARS_RE.sub("-", title)
    cleaned = cleaned.strip(" .\t\r\n-")
    cleaned = re.sub(r"-+", "-", cleaned)
    return cleaned if cleaned else "Untitled"


def format_timestamp(ts: float) -> str:
    """Format file modification timestamp for human display."""
    now = datetime.now()
    dt = datetime.fromtimestamp(ts)
    if dt.date() == now.date():
        return f"Today {dt.strftime('%H:%M')}"
    elif dt.date() == (now.date() - timedelta(days=1)):
        return f"Yesterday {dt.strftime('%H:%M')}"
    elif dt.year == now.year:
        return dt.strftime("%b %d")
    else:
        return dt.strftime("%Y-%m-%d")


def make_snippet(body: str, max_length: int = 120) -> str:
    """Generate a clean single-line snippet from markdown content."""
    lines = body.split("\n")
    cleaned_lines: list[str] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # Skip headers, code blocks, horizontal rules
        if re.match(r"^(#|```|~~~|\|?\s*:?-{3,})", line):
            continue
        # Remove list markers and task check boxes
        line = re.sub(r"^[-*+]\s+(\[[ xX]\]\s+)?", "", line)
        # Simplify wikilinks [[target|display]] -> display
        line = re.sub(r"\[\[(?:[^\]|#]+\|)?([^\]#]+)(?:#[^\]]*)?\]\]", r"\1", line)
        # Remove inline formatting
        line = re.sub(r"[*_`>|]", "", line)
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            cleaned_lines.append(line)

    joined = " · ".join(cleaned_lines)
    return joined[:max_length].strip()


class PlainStore:
    """Storage provider for unencrypted markdown notes in a directory."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()
        self.trash_dir = self.root / ".trash"
        self.root.mkdir(parents=True, exist_ok=True)
        self.trash_dir.mkdir(parents=True, exist_ok=True)

    def _resolve_note_path(self, note_id: str, in_trash: bool = False) -> Path:
        """Resolve a note ID to its .md file path."""
        folder = self.trash_dir if in_trash else self.root
        clean_id = note_id[:-3] if note_id.lower().endswith(".md") else note_id
        # Direct lookup
        direct = folder / f"{clean_id}.md"
        if direct.is_file():
            return direct

        # Case-insensitive lookup
        clean_lower = clean_id.lower()
        if folder.is_dir():
            for p in folder.iterdir():
                if p.is_file() and p.suffix.lower() == ".md":
                    if p.stem.lower() == clean_lower:
                        return p

        return direct

    def _get_unique_path(self, title: str, folder: Path, exclude_path: Path | None = None) -> tuple[str, Path]:
        """Find an available title and path, handling collisions by appending (2), (3)..."""
        clean = sanitize_title(title)
        candidate_title = clean
        candidate_path = folder / f"{candidate_title}.md"
        counter = 2

        while candidate_path.exists():
            if exclude_path and candidate_path.resolve() == exclude_path.resolve():
                break
            candidate_title = f"{clean} ({counter})"
            candidate_path = folder / f"{candidate_title}.md"
            counter += 1

        return candidate_title, candidate_path

    def create_note(self, title: str = "Untitled", body: str = "") -> Note:
        """Create a new plain note, resolving filename collisions if necessary."""
        unique_title, file_path = self._get_unique_path(title, self.root)
        content = body if body else f"# {unique_title}\n\n"

        atomic_write(file_path, content.encode("utf-8"))

        stat = file_path.stat()
        mtime_str = format_timestamp(stat.st_mtime)

        return Note(
            id=unique_title,
            title=unique_title,
            body=content,
            modified=mtime_str,
            created=mtime_str,
        )

    def read_note(self, note_id: str) -> Note:
        """Read and parse an existing note from disk."""
        path = self._resolve_note_path(note_id)
        if not path.is_file():
            raise FileNotFoundError(f"Note not found: {note_id}")

        with open(path, "r", encoding="utf-8", errors="replace") as f:
            body = f.read()

        stat = path.stat()
        mtime_str = format_timestamp(stat.st_mtime)
        created_str = format_timestamp(getattr(stat, "st_birthtime", stat.st_ctime))
        title = path.stem

        return Note(
            id=title,
            title=title,
            body=body,
            modified=mtime_str,
            created=created_str,
        )

    def save_note(self, note_id: str, body: str) -> Note:
        """Update note content atomically."""
        path = self._resolve_note_path(note_id)
        if not path.is_file():
            raise FileNotFoundError(f"Note not found: {note_id}")

        atomic_write(path, body.encode("utf-8"))

        stat = path.stat()
        mtime_str = format_timestamp(stat.st_mtime)
        title = path.stem

        return Note(
            id=title,
            title=title,
            body=body,
            modified=mtime_str,
            created=format_timestamp(getattr(stat, "st_birthtime", stat.st_ctime)),
        )

    def rename_note(self, note_id: str, new_title: str) -> tuple[Note, str]:
        """Rename note file and update its title heading.

        Returns (updated_note, old_title).
        Raises ValueError if new title is empty.
        Raises FileExistsError if new title collides with another note.
        """
        old_path = self._resolve_note_path(note_id)
        if not old_path.is_file():
            raise FileNotFoundError(f"Note not found: {note_id}")

        old_title = old_path.stem
        clean_new = sanitize_title(new_title)
        if not clean_new:
            raise ValueError("Title cannot be empty")

        new_path = self.root / f"{clean_new}.md"

        # Check collision with a different note
        if new_path.exists() and new_path.resolve() != old_path.resolve():
            raise FileExistsError(f"A note with title '{clean_new}' already exists")

        # Read existing body
        with open(old_path, "r", encoding="utf-8", errors="replace") as f:
            body = f.read()

        # Update note heading if it matches # Old Title
        updated_body = re.sub(
            r"^#\s+" + re.escape(old_title) + r"\s*$",
            f"# {clean_new}",
            body,
            count=1,
            flags=re.MULTILINE,
        )

        # Write to old path first if body changed
        if updated_body != body:
            atomic_write(old_path, updated_body.encode("utf-8"))

        # Rename to new path
        if old_path.resolve() != new_path.resolve():
            old_path.rename(new_path)

        stat = new_path.stat()
        mtime_str = format_timestamp(stat.st_mtime)

        return (
            Note(
                id=clean_new,
                title=clean_new,
                body=updated_body,
                modified=mtime_str,
                created=format_timestamp(getattr(stat, "st_birthtime", stat.st_ctime)),
            ),
            old_title,
        )

    def delete_note(self, note_id: str) -> Note:
        """Move note to .trash/ folder."""
        src_path = self._resolve_note_path(note_id)
        if not src_path.is_file():
            raise FileNotFoundError(f"Note not found: {note_id}")

        note = self.read_note(note_id)

        trash_title, dest_path = self._get_unique_path(note.title, self.trash_dir)
        shutil.move(str(src_path), str(dest_path))

        return Note(
            id=trash_title,
            title=trash_title,
            body=note.body,
            modified=note.modified,
            created=note.created,
        )

    def restore_note(self, note_id: str) -> Note:
        """Restore note from .trash/ folder back to root."""
        trash_path = self._resolve_note_path(note_id, in_trash=True)
        if not trash_path.is_file():
            raise FileNotFoundError(f"Note not in trash: {note_id}")

        with open(trash_path, "r", encoding="utf-8", errors="replace") as f:
            body = f.read()

        restored_title, dest_path = self._get_unique_path(trash_path.stem, self.root)
        shutil.move(str(trash_path), str(dest_path))

        stat = dest_path.stat()
        mtime_str = format_timestamp(stat.st_mtime)

        return Note(
            id=restored_title,
            title=restored_title,
            body=body,
            modified=mtime_str,
            created=format_timestamp(getattr(stat, "st_birthtime", stat.st_ctime)),
        )

    def list_notes(self, query: str = "", sort: str = "modified") -> list[Note]:
        """List active notes with optional search query and sorting."""
        notes: list[tuple[Note, float]] = []

        if self.root.is_dir():
            for p in self.root.iterdir():
                if p.is_file() and p.suffix.lower() == ".md" and not p.name.startswith("."):
                    try:
                        stat = p.stat()
                        with open(p, "r", encoding="utf-8", errors="replace") as f:
                            body = f.read()
                        title = p.stem
                        mtime = stat.st_mtime
                        notes.append(
                            (
                                Note(
                                    id=title,
                                    title=title,
                                    body=body,
                                    modified=format_timestamp(mtime),
                                    created=format_timestamp(getattr(stat, "st_birthtime", stat.st_ctime)),
                                ),
                                mtime,
                            )
                        )
                    except OSError:
                        pass

        # Filter by search query
        q = (query or "").strip().lower()
        if q:
            notes = [item for item in notes if q in item[0].title.lower() or q in item[0].body.lower()]

        # Sort
        if sort == "title":
            notes.sort(key=lambda item: item[0].title.lower())
        else:
            # Sort by modified time descending
            notes.sort(key=lambda item: item[1], reverse=True)

        return [item[0] for item in notes]

    def list_trash(self) -> list[Note]:
        """List notes currently in .trash/."""
        trash_notes: list[Note] = []
        if self.trash_dir.is_dir():
            for p in self.trash_dir.iterdir():
                if p.is_file() and p.suffix.lower() == ".md":
                    try:
                        stat = p.stat()
                        trash_notes.append(
                            Note(
                                id=p.stem,
                                title=p.stem,
                                modified=format_timestamp(stat.st_mtime),
                                created=format_timestamp(getattr(stat, "st_birthtime", stat.st_ctime)),
                            )
                        )
                    except OSError:
                        pass
        return trash_notes

    # Aliases
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
    list = list_notes
