"""Command-line access to VaultNotes for helpers such as Claude.

AI helpers have a space of their own, **AI-Notes** (``ai``): they may create,
change and delete notes there and nowhere else.  Plain and Personal are
read-only, and the Encrypted vault is always refused.

    python -m vaultnotes.notes_cli --root <notes folder> list plain [--tag work]
    python -m vaultnotes.notes_cli --root <notes folder> tags plain
    python -m vaultnotes.notes_cli --root <notes folder> read plain "Shopping list"
    python -m vaultnotes.notes_cli --root <notes folder> --personal-key <file> search personal flights
    python -m vaultnotes.notes_cli --root <notes folder> write ai "PR 42 review" --file review.md
    python -m vaultnotes.notes_cli --root <notes folder> append ai "Session log" --text "Done: tests"
    python -m vaultnotes.notes_cli --root <notes folder> delete ai "Old draft"
    python -m vaultnotes.notes_cli --root <notes folder> mark ai "PR 42 review" [--clear]
    python -m vaultnotes.notes_cli --root <notes folder> tag ai "PR 42 review" review backend [--remove]

``--root`` and ``--personal-key`` can also come from the ``VAULTNOTES_ROOT`` and
``VAULTNOTES_PERSONAL_KEY`` environment variables.  The app's settings.json is
not read, so the notes folder and key are always the ones named here.

The **Encrypted** vault is refused before anything is opened: it holds PHI, so
this tool never lists it, never opens its folder and never decrypts it.  A key
file whose vault id is not Personal's (the Encrypted key under any file name)
is refused before its key is decoded.

Only the ``ai-notes`` folder is ever written.  ``write``, ``append`` and
``delete`` refuse every other space, never copy a key file or an encrypted note
into a note, and ``delete`` only moves a note to the AI-Notes trash.  The first
write creates the folder if the app has not yet, but only inside a real notes
folder; reading never creates anything.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Sequence

from vaultnotes.config import AI_NOTES_FOLDER, ensure_ai_notes_folder
from vaultnotes.crypto.keyfile import KeyFileError, PassphraseRequired, load_key_file
from vaultnotes.frontmatter import is_important, set_important, set_tags
from vaultnotes.models import Note
from vaultnotes.storage.plain_store import MAX_TITLE_LENGTH, PlainStore, sanitize_title
from vaultnotes.storage.vault_store import VaultStore, VaultStoreError
from vaultnotes.tags import MAX_TAGS_PER_NOTE, clean_tag, count_tags, extract_tags, has_tags, tag_key, unique_tags

#: The only spaces this tool opens, and the one it may write.
SPACES = ("plain", "personal", "ai")
WRITABLE_SPACES = ("ai",)
SPACE_NAMES = {"plain": "Plain", "personal": "Personal", "ai": "AI-Notes"}
SPACE_ALIASES = {"ai-notes": "ai", "ai_notes": "ai", "ai notes": "ai", "ainotes": "ai"}

WRITE_COMMANDS = ("write", "append", "delete", "mark", "tag")

#: The largest note this tool writes: the same limit as importing a .md file.
MAX_NOTE_BYTES = 5 * 1024 * 1024

#: Exit codes: 1 for "not found" and other errors, 2 for a refused request.
EXIT_ERROR = 1
EXIT_REFUSED = 2

ENCRYPTED_REFUSAL = (
    "The Encrypted vault is off-limits: it holds PHI. This tool never lists it, "
    "never opens its folder and never decrypts it. Open it in VaultNotes yourself."
)

READ_ONLY_REFUSAL = (
    "{name} is read-only for AI helpers, so your notes and theirs never mix. "
    'Write in AI-Notes instead: notes.py write ai "<title>".'
)

KEY_MATERIAL_REFUSAL = "Key files and encrypted notes are never copied into a note."

#: What every VaultNotes key file says about itself (build plan, section 4.2).
_KEY_FILE_MARKER = re.compile(r'"format"\s*:\s*"vaultnotes-key"')


class Refused(Exception):
    """A request this tool will not carry out (the Encrypted vault, above all)."""


class CliError(Exception):
    """Anything else that stops a command, with a message for the user."""


def _mentions_encrypted(value: str | Path) -> bool:
    """Whether a space name, folder or file name points at the Encrypted vault."""
    parts = Path(str(value)).parts or (str(value),)
    return any("encrypted" in part.casefold() for part in parts)


def _space(name: str) -> str:
    wanted = name.strip().casefold()
    if _mentions_encrypted(wanted):
        raise Refused(ENCRYPTED_REFUSAL)
    wanted = SPACE_ALIASES.get(wanted, wanted)
    if wanted not in SPACES:
        raise CliError(f"Unknown space {name!r}. Use one of: {', '.join(SPACES)}.")
    return wanted


def _writable_space(name: str) -> str:
    space = _space(name)  # the Encrypted refusal comes first
    if space not in WRITABLE_SPACES:
        raise Refused(READ_ONLY_REFUSAL.format(name=SPACE_NAMES[space]))
    return space


def _existing_folder(folder: Path, what: str) -> Path:
    # The stores create their folders when missing; checking first keeps this
    # tool from writing anything at all.
    if not (folder / ".trash").is_dir():
        raise CliError(f"No {what} at {folder}. Is --root the notes folder VaultNotes uses?")
    return folder


class NotesReader:
    """Opens Plain, AI-Notes or Personal on demand and forgets the Personal key when closed."""

    def __init__(self, root: Path, personal_key: Path | None) -> None:
        self.root = root
        self.personal_key = personal_key
        self._vault: VaultStore | None = None

    def notes(self, space: str, query: str = "") -> list[Note]:
        if space == "plain":
            return self._plain().list_notes(query, sort="title")
        if space == "ai":
            store = self._ai()
            return store.list_notes(query, sort="title") if store is not None else []
        return self._personal().list_notes(query, sort="title")

    def _plain(self) -> PlainStore:
        return PlainStore(_existing_folder(self.root / "plain", "Plain folder"))

    def _ai(self) -> PlainStore | None:
        return _ai_store(self.root, create=False)

    def _personal(self) -> VaultStore:
        if self._vault is not None:
            return self._vault
        folder = _existing_folder(self.root / "vaults" / "personal", "Personal vault")
        if _mentions_encrypted(folder.resolve()):
            raise Refused(ENCRYPTED_REFUSAL)
        try:
            vault = VaultStore(folder)
        except VaultStoreError as exc:
            raise CliError(f"The Personal vault could not be read ({exc}).") from exc
        if vault.vault_id is None or _mentions_encrypted(vault.name):
            raise Refused(ENCRYPTED_REFUSAL)

        key_path = self._personal_key_path(vault)
        try:
            key = load_key_file(key_path)
        except PassphraseRequired as exc:
            raise CliError(
                "The Personal key file is passphrase protected, and this tool does not "
                "take passphrases. Open the vault in VaultNotes instead."
            ) from exc
        except (KeyFileError, OSError) as exc:
            raise CliError(f"The Personal key file could not be read ({exc}).") from exc
        try:
            vault.unlock(key)
        except VaultStoreError as exc:
            raise CliError(f"The Personal vault did not open ({exc}).") from exc
        finally:
            key.wipe()
        self._vault = vault
        return vault

    def _personal_key_path(self, vault: VaultStore) -> Path:
        if self.personal_key is None:
            raise CliError("Reading Personal needs --personal-key (or VAULTNOTES_PERSONAL_KEY).")
        path = self.personal_key.expanduser()
        if _mentions_encrypted(path):
            raise Refused(ENCRYPTED_REFUSAL)
        # Look at the key file's public vault id before its key is decoded, so
        # another vault's key (the Encrypted one, say) is refused unread.
        try:
            with open(path, "r", encoding="utf-8") as handle:
                vault_id = json.load(handle).get("vault_id")
        except (OSError, ValueError, AttributeError) as exc:
            raise CliError(f"The Personal key file could not be read ({type(exc).__name__}).") from exc
        if str(vault_id).casefold() != str(vault.vault_id).casefold():
            raise Refused("That key file does not belong to the Personal vault, so it was not used.")
        return path

    def close(self) -> None:
        if self._vault is not None:
            self._vault.lock()
            self._vault = None


def _ai_store(root: Path, *, create: bool) -> PlainStore | None:
    """AI-Notes, or ``None`` while nothing has made its folder yet.

    ``create`` makes the folder, for the first write.  Only inside a real notes
    folder: a mistyped ``--root`` must not grow an ``ai-notes`` folder elsewhere.
    """
    _existing_folder(root / "plain", "Plain folder")
    if create:
        return PlainStore(ensure_ai_notes_folder(root))
    folder = root / AI_NOTES_FOLDER
    return PlainStore(folder) if (folder / ".trash").is_dir() else None


def _find(notes: list[Note], wanted: str) -> Note:
    """Match a note by id, then exact title, then a unique title fragment."""
    needle = wanted.strip().casefold()
    for match in (
        [note for note in notes if note.id.casefold() == needle],
        [note for note in notes if note.title.casefold() == needle],
        [note for note in notes if needle in note.title.casefold()],
    ):
        if len(match) == 1:
            return match[0]
        if len(match) > 1:
            names = "\n".join(f"  {note.title}  ({note.id})" for note in match)
            raise CliError(f"{len(match)} notes match {wanted!r}; use one of these ids:\n{names}")
    raise CliError(f"No note matches {wanted!r}.")


def _existing(store: PlainStore, wanted: str) -> Note | None:
    """The note titled exactly ``wanted`` (any case), as typed or as it would be saved.

    Writes never guess from a fragment the way ``read`` does: appending to or
    deleting the wrong note is worse than being told there is none.
    """
    if not wanted.strip():
        return None  # sanitize_title("") is "Untitled", a real note's name
    for candidate in dict.fromkeys((wanted.strip(), sanitize_title(wanted))):
        try:
            return store.read_note(candidate)
        except (FileNotFoundError, ValueError):
            continue
    return None


def _title(raw: str) -> str:
    text = raw.strip()
    if not text:
        raise CliError("A note needs a title.")
    if len(text) > MAX_TITLE_LENGTH:
        raise CliError(f"That title has {len(text)} characters; the limit is {MAX_TITLE_LENGTH}.")
    return sanitize_title(text)


def _check_size(text: str) -> str:
    size = len(text.encode("utf-8"))
    if size > MAX_NOTE_BYTES:
        raise CliError(f"That note would be {size / 1_048_576:.1f} MB; notes.py writes notes up to 5 MB.")
    return text


def _read_text_file(path: Path, root: Path) -> str:
    resolved = path.expanduser().resolve()
    if resolved.suffix.lower() in {".vnkey", ".vnote"}:
        raise Refused(KEY_MATERIAL_REFUSAL)
    if resolved.is_relative_to((root / "vaults").resolve()):
        raise Refused(KEY_MATERIAL_REFUSAL)
    try:
        if resolved.stat().st_size > MAX_NOTE_BYTES:
            raise CliError(f"{path.name} is larger than 5 MB, the most notes.py writes into a note.")
        data = resolved.read_bytes()
    except OSError as exc:
        raise CliError(f"{path} could not be read ({exc.strerror or type(exc).__name__}).") from exc
    return data.decode("utf-8-sig", errors="replace")


def _note_text(args: argparse.Namespace, root: Path, stdin) -> str:
    """The text for ``write``/``append``: ``--text``, ``--file`` or standard input."""
    if args.body is not None and args.body_file is not None:
        raise CliError("Give the text with --text or --file, not both.")
    if args.body is not None:
        text = args.body
    elif args.body_file is not None:
        text = _read_text_file(Path(args.body_file), root)
    else:
        stream = stdin if stdin is not None else sys.stdin
        if stream is None or (hasattr(stream, "isatty") and stream.isatty()):
            raise CliError("Give the note's text with --text, --file or on standard input.")
        raw = stream.buffer.read() if hasattr(stream, "buffer") else stream.read()
        text = raw.decode("utf-8-sig", errors="replace") if isinstance(raw, bytes) else raw
    text = text.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise CliError("The text is empty, so nothing was written.")
    if _KEY_FILE_MARKER.search(text):
        raise Refused(KEY_MATERIAL_REFUSAL)
    return _check_size(text)


def _write(store: PlainStore, title: str, text: str, replace: bool) -> str:
    body = text if text.endswith("\n") else text + "\n"
    existing = _existing(store, title)
    if existing is not None:
        if not replace:
            raise CliError(
                f'AI-Notes already has "{existing.title}". Add --replace to overwrite it, '
                "or use append to add to it."
            )
        note = store.save_note(existing.id, body)
        return f'Replaced "{note.title}" in AI-Notes.'
    note = store.create_note(title=title, body=body)
    return f'Created "{note.title}" in AI-Notes.'


def _append(store: PlainStore, wanted: str, text: str) -> str:
    addition = text.strip("\n")
    existing = _existing(store, wanted)
    if existing is None:
        note = store.create_note(title=_title(wanted), body=f"{addition}\n")
        return f'Created "{note.title}" in AI-Notes.'
    old = existing.body.rstrip()
    body = _check_size(f"{old}\n\n{addition}\n" if old else f"{addition}\n")
    note = store.save_note(existing.id, body)
    return f'Appended to "{note.title}" in AI-Notes.'


def _delete(store: PlainStore | None, wanted: str) -> str:
    existing = _existing(store, wanted) if store is not None else None
    if existing is None:
        raise CliError(f"AI-Notes has no note titled {wanted!r}. delete needs the exact title.")
    store.delete_note(existing.id)
    return f'Moved "{existing.title}" to the AI-Notes trash; it can be restored from there in VaultNotes.'


def _mark(store: PlainStore | None, wanted: str, important: bool) -> str:
    """Set or clear the important mark, the same front matter line the app writes."""
    existing = _existing(store, wanted) if store is not None else None
    if existing is None:
        raise CliError(f"AI-Notes has no note titled {wanted!r}. mark needs the exact title.")
    body = set_important(existing.body, important)
    if body == existing.body:
        state = "already marked important" if important else "not marked important"
        return f'"{existing.title}" is {state}; nothing changed.'
    note = store.save_note(existing.id, body)
    if important:
        return f'Marked "{note.title}" important; it now sits at the top of the AI-Notes list.'
    return f'Cleared the important mark on "{note.title}".'


def _tag(store: PlainStore | None, wanted: str, tags: list[str], remove: bool) -> str:
    """Add or remove tags: the same front matter line the app's tag box writes."""
    existing = _existing(store, wanted) if store is not None else None
    if existing is None:
        raise CliError(f"AI-Notes has no note titled {wanted!r}. tag needs the exact title.")
    clean = [clean_tag(tag) for tag in tags]
    if not all(clean):
        raise CliError(f"Not a tag: {tags[clean.index('')]!r}. Use letters, digits, - _ or /.")
    current = extract_tags(existing.body)
    if remove:
        gone = {tag_key(tag) for tag in clean}
        new = [tag for tag in current if tag_key(tag) not in gone]
    else:
        new = unique_tags(current + clean)
    if len(new) > MAX_TAGS_PER_NOTE:
        raise CliError(f"A note can have up to {MAX_TAGS_PER_NOTE} tags.")
    if new == current:
        return f'"{existing.title}" already has those tags; nothing changed.'
    note = store.save_note(existing.id, set_tags(existing.body, new))
    shown = " ".join(f"#{tag}" for tag in new) or "no tags"
    return f'"{note.title}" now has {shown}.'


def _matching_lines(body: str, needle: str, limit: int = 3) -> list[str]:
    lines = [line.strip() for line in body.splitlines() if needle in line.casefold()]
    return lines[:limit]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="notes.py",
        description=(
            "Read Plain, Personal and AI-Notes notes, and write AI-Notes (space 'ai'). "
            "The Encrypted vault is always refused."
        ),
    )
    parser.add_argument("--root", default=os.environ.get("VAULTNOTES_ROOT"), help="the notes folder")
    parser.add_argument(
        "--personal-key",
        default=os.environ.get("VAULTNOTES_PERSONAL_KEY"),
        help="the Personal vault's .vnkey file (needed for the personal space)",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="list note titles")
    listing.add_argument("space")
    listing.add_argument(
        "--tag", action="append", default=[], help="only notes with this #tag (repeat for several)"
    )
    commands.add_parser("tags", help="list the #tags used in a space, with note counts").add_argument("space")
    read = commands.add_parser("read", help="print one note")
    read.add_argument("space")
    read.add_argument("note", help="an id, a title, or a unique part of a title")
    search = commands.add_parser("search", help="find notes whose title or text contains TEXT")
    search.add_argument("space")
    search.add_argument("text")

    def text_options(command: argparse.ArgumentParser) -> None:
        command.add_argument("--text", dest="body", help="the text itself")
        command.add_argument(
            "--file", dest="body_file", help="a UTF-8 file holding the text (standard input otherwise)"
        )

    write = commands.add_parser("write", help="create a note in AI-Notes")
    write.add_argument("space", help="must be ai")
    write.add_argument("title")
    text_options(write)
    write.add_argument("--replace", action="store_true", help="overwrite a note that has this title")
    append = commands.add_parser("append", help="add text to the end of an AI-Notes note, creating it if needed")
    append.add_argument("space", help="must be ai")
    append.add_argument("note", help="the note's exact title")
    text_options(append)
    delete = commands.add_parser("delete", help="move an AI-Notes note to its trash")
    delete.add_argument("space", help="must be ai")
    delete.add_argument("note", help="the note's exact title")
    mark = commands.add_parser("mark", help="mark an AI-Notes note important (--clear removes the mark)")
    mark.add_argument("space", help="must be ai")
    mark.add_argument("note", help="the note's exact title")
    mark.add_argument("--clear", action="store_true", help="remove the important mark instead")
    tag = commands.add_parser("tag", help="add tags to an AI-Notes note (--remove takes them off)")
    tag.add_argument("space", help="must be ai")
    tag.add_argument("note", help="the note's exact title")
    tag.add_argument("tags", nargs="+", help="one or more tags, with or without #")
    tag.add_argument("--remove", action="store_true", help="remove these tags instead")
    return parser


def run(argv: Sequence[str] | None = None, out=None, stdin=None) -> int:
    out = out or sys.stdout
    args = _parser().parse_args(argv)

    reader: NotesReader | None = None
    try:
        # Before anything else, so Encrypted (and a write outside AI-Notes) is
        # refused first.
        writing = args.command in WRITE_COMMANDS
        space = _writable_space(args.space) if writing else _space(args.space)
        if not args.root:
            raise CliError("Name the notes folder with --root (or VAULTNOTES_ROOT).")
        root = Path(args.root).expanduser()
        if args.command == "write":
            title, text = _title(args.title), _note_text(args, root, stdin)
            print(_write(_ai_store(root, create=True), title, text, args.replace), file=out)
            return 0
        if args.command == "append":
            text = _note_text(args, root, stdin)
            print(_append(_ai_store(root, create=True), args.note, text), file=out)
            return 0
        if args.command == "delete":
            print(_delete(_ai_store(root, create=False), args.note), file=out)
            return 0
        if args.command == "mark":
            print(_mark(_ai_store(root, create=False), args.note, not args.clear), file=out)
            return 0
        if args.command == "tag":
            print(_tag(_ai_store(root, create=False), args.note, args.tags, args.remove), file=out)
            return 0

        reader = NotesReader(root, Path(args.personal_key) if args.personal_key else None)
        if args.command == "list":
            wanted = [clean_tag(tag) for tag in args.tag]
            if not all(wanted):
                raise CliError(f"Not a tag: {args.tag[wanted.index('')]!r}. Tags look like #work or work.")
            notes = [note for note in reader.notes(space) if has_tags(extract_tags(note.body), wanted)]
            # The user's important notes first, flagged in a fourth column.
            notes = sorted(notes, key=lambda note: not is_important(note.body))
            for note in notes:
                flag = "\timportant" if is_important(note.body) else ""
                print(f"{note.title}\t{note.modified}\t{note.id}{flag}", file=out)
        elif args.command == "tags":
            for row in count_tags(extract_tags(note.body) for note in reader.notes(space)):
                print(f"#{row['tag']}	{row['count']}", file=out)
        elif args.command == "read":
            note = _find(reader.notes(space), args.note)
            print(f"<!-- {space} / {note.title} · {note.id} · modified {note.modified} -->", file=out)
            print(note.body, file=out)
        else:
            needle = args.text.strip().casefold()
            for note in reader.notes(space, args.text):
                print(f"{note.title}  ({note.id})", file=out)
                for line in _matching_lines(note.body, needle):
                    print(f"    {line}", file=out)
        return 0
    except Refused as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except CliError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_ERROR
    except OSError as exc:
        print(f"The notes folder could not be used ({exc.strerror or type(exc).__name__}).", file=sys.stderr)
        return EXIT_ERROR
    finally:
        if reader is not None:
            reader.close()


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    sys.exit(run())


if __name__ == "__main__":
    main()
