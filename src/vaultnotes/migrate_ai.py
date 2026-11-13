"""One-time move of AI-Notes from plain ``.md`` files to its encrypted vault.

    python -m vaultnotes.migrate_ai --root <notes folder> --dry-run
    python -m vaultnotes.migrate_ai --root <notes folder>

Close VaultNotes first.  The steps, in order:

1. List the ``ai-notes/*.md`` notes and the ``ai-notes/.trash/*.md`` ones.
2. Create the AI-Notes key file (``--key``, by default
   ``%LOCALAPPDATA%\\VaultNotes\\ai-notes.vnkey``), or reuse this PC's existing
   AI-Notes key, and write the vault header.  A key file is never overwritten.
3. Store every note encrypted, with its title, its text (front matter, so tags
   and the important mark, included) and its modified time.
4. Open the vault again, from disk, and compare every title and text with the
   plain files.  Only when all of them match are the plain files **moved** (not
   deleted) to ``%LOCALAPPDATA%\\VaultNotes\\ai-notes-plaintext-<time>``, outside
   the notes folder, so the backup no longer sees them and they can be checked
   before being deleted by hand.

If anything fails before the plain files move, what was created (encrypted
files, header, and the key if this run made it) is removed again and the plain
notes are left untouched.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Sequence

from vaultnotes.config import AI_NOTES_FOLDER, get_ai_key_path, get_local_app_dir
from vaultnotes.storage.ai_store import EncryptedAIStore, plaintext_note_files
from vaultnotes.storage.vault_store import NOTE_SUFFIX


class MigrationError(Exception):
    """The migration stopped; the message says why and what was left as it was."""


@dataclass
class MigrationReport:
    folder: Path
    key_path: Path
    active: list[str] = field(default_factory=list)
    trashed: list[str] = field(default_factory=list)
    renamed: list[tuple[str, str]] = field(default_factory=list)
    moved_to: Path | None = None
    dry_run: bool = False

    def summary(self) -> str:
        lines = [
            f"AI-Notes folder: {self.folder}",
            f"Key file:        {self.key_path}",
            f"Notes:           {len(self.active)} active, {len(self.trashed)} in the trash",
        ]
        for old, new in self.renamed:
            lines.append(f'  "{old}" is stored as "{new}" (the title was taken)')
        if self.dry_run:
            lines.append("Dry run: nothing was changed.")
            lines.extend(f"  would encrypt: {title}" for title in self.active)
            lines.extend(f"  would encrypt (trash): {title}" for title in self.trashed)
        elif self.moved_to is not None:
            lines.append(f"Plain copies moved to: {self.moved_to}")
            lines.append("Check the notes in VaultNotes, then delete that folder.")
            lines.append("Keep a copy of the key file somewhere safe: without it these notes cannot be read.")
        return "\n".join(lines)


def _read(path: Path) -> str:
    return path.read_bytes().decode("utf-8", errors="replace")


def _cleanup(folder: Path, key_path: Path | None) -> None:
    """Remove what a failed run created; plain files are never touched."""
    for directory in (folder, folder / ".trash"):
        if directory.is_dir():
            for path in directory.iterdir():
                if path.is_file() and path.suffix.lower() == NOTE_SUFFIX:
                    path.unlink(missing_ok=True)
    (folder / "vault.json").unlink(missing_ok=True)
    if key_path is not None:
        key_path.unlink(missing_ok=True)


def migrate(
    notes_root: Path | str,
    key_path: Path | str | None = None,
    backup_dir: Path | str | None = None,
    *,
    dry_run: bool = False,
) -> MigrationReport:
    root = Path(notes_root).expanduser().resolve()
    folder = root / AI_NOTES_FOLDER
    key = Path(key_path).expanduser() if key_path is not None else get_ai_key_path()
    report = MigrationReport(folder=folder, key_path=key, dry_run=dry_run)

    if not (root / "plain").is_dir():
        raise MigrationError(f"{root} does not look like a VaultNotes notes folder (no 'plain' folder).")
    if not folder.is_dir():
        raise MigrationError(f"{folder} does not exist, so there is nothing to migrate.")

    files = plaintext_note_files(folder)
    if (folder / "vault.json").exists():
        if files:
            raise MigrationError(
                f"{folder} already has an encrypted vault but also {len(files)} plain .md file(s). "
                "Nothing was changed; move those files out and add them in the app."
            )
        raise MigrationError("AI-Notes is already encrypted; nothing to do.")
    try:
        key.resolve().relative_to(root)
    except ValueError:
        pass
    else:
        raise MigrationError("The key file must not be inside the notes folder: the backup would carry it.")

    sources = [(path, path.parent != folder) for path in files]
    for path, in_trash in sources:
        (report.trashed if in_trash else report.active).append(path.stem)
    if dry_run:
        return report

    expected: dict[tuple[bool, str], str] = {}
    key_existed = key.exists()
    try:
        store = EncryptedAIStore(folder, key, create=True, allow_plaintext=True)
        for path, in_trash in sources:
            body = _read(path)
            note = store.import_note(path.stem, body, path.stat().st_mtime, trash=in_trash)
            if note.title != path.stem:
                report.renamed.append((path.stem, note.title))
            expected[(in_trash, note.title)] = body
        store.close()

        # Check from disk with a fresh store, as the app and notes.py will open it.
        check = EncryptedAIStore(folder, key, allow_plaintext=True)
        stored = {(in_trash, note.title): note.body for in_trash, note in check.iter_all()}
        check.close()
        if stored != expected:
            missing = sorted(title for (_, title) in expected.keys() - stored.keys())
            differ = sorted(title for k, title in expected if k in stored and stored[k] != expected[k])
            raise MigrationError(
                f"The encrypted copy did not match the plain notes (missing: {missing}, different: {differ})."
            )
    except Exception as exc:
        _cleanup(folder, None if key_existed else key)
        if isinstance(exc, MigrationError):
            raise MigrationError(f"{exc} Nothing was changed.") from exc
        raise MigrationError(f"The migration failed ({exc}). Nothing was changed.") from exc

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = Path(backup_dir) if backup_dir is not None else get_local_app_dir()
    target = target / f"ai-notes-plaintext-{stamp}"
    for path, in_trash in sources:
        destination = target / (".trash" if in_trash else "") / path.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(destination))
    report.moved_to = target
    return report


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        prog="python -m vaultnotes.migrate_ai",
        description="Encrypt AI-Notes: move its plain .md notes into an encrypted vault. Close VaultNotes first.",
    )
    parser.add_argument("--root", required=True, help="the notes folder")
    parser.add_argument("--key", help="where to create the AI-Notes key file (%%LOCALAPPDATA%%\\VaultNotes\\ai-notes.vnkey by default)")
    parser.add_argument("--backup-dir", help="where the plain copies are moved (%%LOCALAPPDATA%%\\VaultNotes by default)")
    parser.add_argument("--dry-run", action="store_true", help="only show what would happen")
    args = parser.parse_args(argv)
    try:
        report = migrate(args.root, args.key, args.backup_dir, dry_run=args.dry_run)
    except MigrationError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(report.summary())
    return 0


if __name__ == "__main__":
    sys.exit(main())
