"""Read-only command-line access to Plain and Personal notes, for helpers such as Claude.

    python -m vaultnotes.notes_cli --root <notes folder> list plain
    python -m vaultnotes.notes_cli --root <notes folder> read plain "Shopping list"
    python -m vaultnotes.notes_cli --root <notes folder> --personal-key <file> search personal flights

``--root`` and ``--personal-key`` can also come from the ``VAULTNOTES_ROOT`` and
``VAULTNOTES_PERSONAL_KEY`` environment variables.  The app's settings.json is
not read, so the notes folder and key are always the ones named here.

The **Encrypted** vault is refused before anything is opened: it holds PHI, so
this tool never lists it, never opens its folder and never decrypts it.  A key
file whose vault id is not Personal's (the Encrypted key under any file name)
is refused before its key is decoded.  Nothing is written either; folders the
app has not created yet are reported, not made.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Sequence

from vaultnotes.crypto.keyfile import KeyFileError, PassphraseRequired, load_key_file
from vaultnotes.models import Note
from vaultnotes.storage.plain_store import PlainStore
from vaultnotes.storage.vault_store import VaultStore, VaultStoreError

#: The only spaces this tool opens.
SPACES = ("plain", "personal")

#: Exit codes: 1 for "not found" and other errors, 2 for a refused request.
EXIT_ERROR = 1
EXIT_REFUSED = 2

ENCRYPTED_REFUSAL = (
    "The Encrypted vault is off-limits: it holds PHI. This tool never lists it, "
    "never opens its folder and never decrypts it. Open it in VaultNotes yourself."
)


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
    if wanted not in SPACES:
        raise CliError(f"Unknown space {name!r}. Use one of: {', '.join(SPACES)}.")
    return wanted


def _existing_folder(folder: Path, what: str) -> Path:
    # The stores create their folders when missing; checking first keeps this
    # tool from writing anything at all.
    if not (folder / ".trash").is_dir():
        raise CliError(f"No {what} at {folder}. Is --root the notes folder VaultNotes uses?")
    return folder


class NotesReader:
    """Opens Plain or Personal on demand and forgets the Personal key when closed."""

    def __init__(self, root: Path, personal_key: Path | None) -> None:
        self.root = root
        self.personal_key = personal_key
        self._vault: VaultStore | None = None

    def notes(self, space: str, query: str = "") -> list[Note]:
        if space == "plain":
            return self._plain().list_notes(query, sort="title")
        return self._personal().list_notes(query, sort="title")

    def _plain(self) -> PlainStore:
        return PlainStore(_existing_folder(self.root / "plain", "Plain folder"))

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


def _matching_lines(body: str, needle: str, limit: int = 3) -> list[str]:
    lines = [line.strip() for line in body.splitlines() if needle in line.casefold()]
    return lines[:limit]


def run(argv: Sequence[str] | None = None, out=None) -> int:
    out = out or sys.stdout
    parser = argparse.ArgumentParser(
        prog="notes.py",
        description="Read Plain and Personal VaultNotes notes. The Encrypted vault is always refused.",
    )
    parser.add_argument("--root", default=os.environ.get("VAULTNOTES_ROOT"), help="the notes folder")
    parser.add_argument(
        "--personal-key",
        default=os.environ.get("VAULTNOTES_PERSONAL_KEY"),
        help="the Personal vault's .vnkey file (needed for the personal space)",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="list note titles").add_argument("space")
    read = commands.add_parser("read", help="print one note")
    read.add_argument("space")
    read.add_argument("note", help="an id, a title, or a unique part of a title")
    search = commands.add_parser("search", help="find notes whose title or text contains TEXT")
    search.add_argument("space")
    search.add_argument("text")
    args = parser.parse_args(argv)

    reader: NotesReader | None = None
    try:
        space = _space(args.space)  # before anything else, so Encrypted is refused first
        if not args.root:
            raise CliError("Name the notes folder with --root (or VAULTNOTES_ROOT).")
        reader = NotesReader(
            Path(args.root).expanduser(),
            Path(args.personal_key) if args.personal_key else None,
        )
        if args.command == "list":
            for note in reader.notes(space):
                print(f"{note.title}\t{note.modified}\t{note.id}", file=out)
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
