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

# Characters that would break a [[link]] to the note: "#" starts a heading,
# "|" the shown text, "^" a block reference, and brackets end the link early.
# Obsidian refuses the same ones in note names.
LINK_CHARS_RE = re.compile(r"[#^\[\]|]")

# Snippets only ever look at the start of a note, so a huge body stays cheap.
_SNIPPET_LINE_LIMIT = 60

# Longest title we are willing to turn into a file name.
MAX_TITLE_LENGTH = 120


def sanitize_title(title: str) -> str:
    """Sanitize a title for use as a filesystem filename.

    Replaces characters Windows forbids with '-', trims whitespace, dots and
    trailing dashes, and caps the length so the resulting path stays well
    inside the Windows MAX_PATH limit.  Raises ``ValueError`` for input that is
    not text, so a crafted Bridge API call gets a clean error instead of a
    crash (M7).
    """
    if not isinstance(title, str):
        raise ValueError("Title must be text")
    cleaned = LINK_CHARS_RE.sub("-", INVALID_CHARS_RE.sub("-", title))
    cleaned = cleaned.strip(" .\t\r\n-")
    # The file name adds ".md" itself: a title "README.md" would become
    # "README.md.md", and its id "README.md" is also how [[README.md]] names
    # the note "README".
    while cleaned.lower().endswith(".md"):
        cleaned = cleaned[:-3].strip(" .\t\r\n-")
    cleaned = re.sub(r"-+", "-", cleaned)
    if len(cleaned) > MAX_TITLE_LENGTH:
        cleaned = cleaned[:MAX_TITLE_LENGTH].rstrip(" .-")
    # Control characters have no business in a file name either.
    cleaned = re.sub(r"[\x00-\x1f]", "-", cleaned).strip(" .\t\r\n-")
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
    """Generate a clean single-line snippet from markdown content.

    Only the first lines are examined: a 1 MB note must not cost a full scan on
    every note-list refresh (M7 performance).
    """
    if not isinstance(body, str) or not body:
        return ""

    cleaned_lines: list[str] = []
    for line in body.split("\n", _SNIPPET_LINE_LIMIT)[:_SNIPPET_LINE_LIMIT]:
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
        if sum(len(item) for item in cleaned_lines) >= max_length:
            break

    joined = " · ".join(cleaned_lines)
    return joined[:max_length].strip()


class PlainStore:
    """Storage provider for unencrypted markdown notes in a directory."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()
        self.trash_dir = self.root / ".trash"
        self.root.mkdir(parents=True, exist_ok=True)
        self.trash_dir.mkdir(parents=True, exist_ok=True)
        # Files that could not be read during the last listing.  Plain file
        # names are not secret (they sit on disk in clear text), so they are
        # safe to show in a warning.
        self.skipped_files: list[str] = []

    @staticmethod
    def _validate_note_id(note_id: str) -> str:
        """Reject anything that is not a bare file name.

        Plain note ids *are* file names, so this is what stops a crafted Bridge
        API call such as ``open_note("plain", "../../secret")`` from reading or
        overwriting a ``.md`` file outside the notes folder (security rule 12f).
        """
        if not isinstance(note_id, str):
            raise ValueError("Note id must be text")
        # An id is exactly a file stem.  Stripping a ".md" here once made the
        # id "README.md" (file "README.md.md") resolve to the note "README".
        clean_id = note_id.strip()
        if not clean_id or clean_id in {".", ".."}:
            raise ValueError(f"Invalid note id: {note_id!r}")
        if any(character in clean_id for character in ("/", "\\", ":", "\x00")):
            raise ValueError(f"Invalid note id: {note_id!r}")
        return clean_id

    def _resolve_note_path(self, note_id: str, in_trash: bool = False) -> Path:
        """Resolve a note ID to its .md file path, confined to this folder."""
        folder = self.trash_dir if in_trash else self.root
        clean_id = self._validate_note_id(note_id)
        # Direct lookup
        direct = folder / f"{clean_id}.md"
        if not self._inside(direct, folder):
            raise ValueError(f"Invalid note id: {note_id!r}")
        if direct.is_file():
            return direct

        # Case-insensitive lookup
        clean_lower = clean_id.lower()
        if folder.is_dir():
            try:
                entries = list(folder.iterdir())
            except OSError:
                entries = []
            for p in entries:
                try:
                    if p.is_file() and p.suffix.lower() == ".md" and p.stem.lower() == clean_lower:
                        return p
                except OSError:
                    continue

        return direct

    @staticmethod
    def _inside(path: Path, folder: Path) -> bool:
        """Whether ``path`` stays inside ``folder`` after resolution."""
        try:
            return path.resolve().is_relative_to(folder.resolve())
        except (OSError, ValueError):
            return False

    def fingerprint(self) -> tuple[int, int, int]:
        """Cheap change detector for the folder: (file count, newest mtime, total size).

        Reading 500 note bodies to answer "did anything change?" would defeat
        the point of the in-memory link index, so the index is only rebuilt
        when this fingerprint moves.  A note edited outside the app (in
        Obsidian, say) changes an mtime and is picked up on the next call.
        """
        count = 0
        newest = 0
        total = 0
        try:
            with os.scandir(self.root) as entries:
                for entry in entries:
                    try:
                        if not entry.is_file() or not entry.name.lower().endswith(".md"):
                            continue
                        if entry.name.startswith("."):
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

        # Rename to new path.  Compare names, not resolved paths: on Windows
        # "meeting notes.md" and "Meeting Notes.md" resolve to the same file,
        # and a case-only rename must still change the name on disk.
        if old_path.name != new_path.name:
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

        # The newest deletion keeps its real name, so Undo restores the note
        # exactly as it was (and its [[links]] still reach it); an older
        # trashed copy with the same name moves aside instead.
        trash_title, dest_path = note.title, self.trash_dir / src_path.name
        if dest_path.exists():
            _, aside = self._get_unique_path(note.title, self.trash_dir)
            dest_path.rename(aside)
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

        # Back under its own name when that is free, unchanged, so links to it
        # keep working; otherwise the next free "Title (2)".
        restored_title, dest_path = trash_path.stem, self.root / trash_path.name
        if dest_path.exists():
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

    def purge_note(self, note_id: str) -> str:
        """Permanently delete a note from .trash/ (cannot be undone).

        Returns the purged note's title. Raises FileNotFoundError when the
        note is not in trash.
        """
        trash_path = self._resolve_note_path(note_id, in_trash=True)
        if not trash_path.is_file():
            raise FileNotFoundError(f"Note not in trash: {note_id}")
        title = trash_path.stem
        trash_path.unlink()
        return title

    def empty_trash(self) -> int:
        """Permanently delete every note in .trash/. Returns the count."""
        count = 0
        if self.trash_dir.is_dir():
            for p in list(self.trash_dir.iterdir()):
                if p.is_file() and p.suffix.lower() == ".md":
                    try:
                        p.unlink()
                        count += 1
                    except OSError:
                        pass
        return count

    def list_notes(self, query: str = "", sort: str = "modified") -> list[Note]:
        """List active notes with optional search query and sorting.

        A file that cannot be read (removed underneath us, no permission, or a
        folder that is not readable) is **skipped and recorded** in
        :attr:`skipped_files` instead of failing the whole list, so one bad
        file cannot hide the other notes (M7).
        """
        notes: list[tuple[Note, float]] = []
        skipped: list[str] = []

        try:
            entries = list(self.root.iterdir()) if self.root.is_dir() else []
        except OSError as exc:
            self.skipped_files = [f"{self.root.name}: {exc.strerror or exc}"]
            return []

        for p in entries:
            try:
                if not (p.is_file() and p.suffix.lower() == ".md" and not p.name.startswith(".")):
                    continue
                stat = p.stat()
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    body = f.read()
            except OSError as exc:
                skipped.append(f"{p.name}: {exc.strerror or exc}")
                continue

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

        self.skipped_files = skipped

        # Filter by search query
        q = (query if isinstance(query, str) else "").strip().lower()
        if q:
            notes = [item for item in notes if q in item[0].title.lower() or q in item[0].body.lower()]

        # Sort
        if isinstance(sort, str) and sort.casefold() in {"title", "name"}:
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
    purge = purge_note
    list = list_notes
