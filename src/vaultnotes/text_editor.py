"""The Editor space: plain text files opened in tabs, like Notepad++.

The page shows and edits the text; Python does all the file work.  A file is
only ever picked in a native Open or Save dialog, and the page gets back an
opaque id for it, so it never sends a path.  Saving writes to the file behind
that id.

Some folders are refused because they hold ciphertext or keys, and editing
them as text would break them or leak them: every vault folder (the Encrypted
vault holds PHI), the AI-Notes folder and any ``.vnkey`` key file.

Unsaved text lives only in the page's memory.  Nothing here writes a draft,
backup or session file, so text pasted into a new tab never reaches the disk
until the user saves it somewhere.
"""

from __future__ import annotations

import os
import secrets
import tempfile
from pathlib import Path
from typing import Any, Iterable

#: Biggest file the editor opens.  CodeMirror copes with more, but the bridge
#: sends the whole text as one JSON string.
MAX_FILE_BYTES = 20 * 1024 * 1024
#: Most files open at once.
MAX_OPEN_FILES = 64
#: How much of a file is checked for NUL bytes to spot a binary file.
SNIFF_BYTES = 8192

#: Line endings the page may ask for, and how each is written.
EOLS = {"LF": "\n", "CRLF": "\r\n", "CR": "\r"}
#: Encodings a file can be read as and saved back in.
ENCODINGS = ("utf-8", "utf-8-sig", "utf-16", "cp1252")

#: What the native dialogs offer.  pywebview wants "Description (*.ext)".
TEXT_FILE_TYPES = (
    "All files (*.*)",
    "Text (*.txt;*.log;*.md;*.csv)",
    "Code (*.py;*.js;*.ts;*.json;*.sql;*.xml;*.html;*.css;*.ps1;*.sh;*.yml;*.yaml)",
)


class EditorError(Exception):
    """An Editor-space problem with a user-facing code and message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ----------------------------------------------------------------------
# Reading and writing
# ----------------------------------------------------------------------
def detect_eol(text: str) -> str:
    """``"CRLF"``, ``"LF"`` or ``"CR"``: whichever the text uses first."""
    for index, ch in enumerate(text):
        if ch == "\r":
            return "CRLF" if text[index + 1:index + 2] == "\n" else "CR"
        if ch == "\n":
            return "LF"
    return "CRLF" if os.name == "nt" else "LF"


def decode(raw: bytes) -> tuple[str, str]:
    """``(text, encoding)``.  A BOM first, then UTF-8, then Windows-1252 (never fails)."""
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig"), "utf-8-sig"
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return raw.decode("utf-16"), "utf-16"
        except UnicodeDecodeError:
            pass
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace"), "cp1252"


def read_text_file(path: Path) -> dict[str, Any]:
    """The text of ``path`` with its line endings turned into ``\\n``, plus how it was stored."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise EditorError("not_found", "That file could not be found.") from exc
    if not path.is_file():
        raise EditorError("not_found", "That is not a file.")
    if size > MAX_FILE_BYTES:
        raise EditorError("too_big", f"That file is over {MAX_FILE_BYTES // (1024 * 1024)} MB, too big to open here.")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise EditorError("read_failed", f"That file could not be read ({exc.strerror or type(exc).__name__}).") from exc
    is_utf16 = raw.startswith((b"\xff\xfe", b"\xfe\xff"))
    if not is_utf16 and b"\x00" in raw[:SNIFF_BYTES]:
        raise EditorError("binary", "That looks like a binary file, not text.")
    text, encoding = decode(raw)
    eol = detect_eol(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return {"text": text, "encoding": encoding, "eol": eol}


def write_text_file(path: Path, text: str, *, encoding: str, eol: str) -> None:
    """Write ``text`` (``\\n`` line endings) with ``eol`` and ``encoding``, all or nothing."""
    if encoding not in ENCODINGS:
        encoding = "utf-8"
    newline = EOLS.get(eol, "\n")
    body = text.replace("\r\n", "\n").replace("\r", "\n")
    if newline != "\n":
        body = body.replace("\n", newline)
    try:
        data = body.encode(encoding)
    except UnicodeEncodeError as exc:
        raise EditorError(
            "encoding", f"This text has characters {encoding} cannot hold. Change the encoding to UTF-8 and save again."
        ) from exc
    try:
        handle, temp = tempfile.mkstemp(prefix=".vn-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(handle, "wb") as out:
                out.write(data)
            os.replace(temp, path)
        except BaseException:
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise
    except OSError as exc:
        raise EditorError("write_failed", f"The file could not be saved ({exc.strerror or type(exc).__name__}).") from exc


# ----------------------------------------------------------------------
# Folders the editor keeps out of
# ----------------------------------------------------------------------
def _inside(path: Path, folder: Path) -> bool:
    try:
        path.relative_to(folder)
        return True
    except ValueError:
        return False


def refused_reason(path: Path, blocked: Iterable[Path]) -> str | None:
    """Why ``path`` may not be opened or saved, or None when it may."""
    if path.suffix.casefold() == ".vnkey":
        return "Key files cannot be opened in the editor."
    for folder in blocked:
        if _inside(path, folder):
            return "Files in a vault or AI-Notes folder are encrypted and cannot be opened in the editor."
    return None


# ----------------------------------------------------------------------
# The open files
# ----------------------------------------------------------------------
class _File:
    def __init__(self, path: Path, encoding: str) -> None:
        self.path = path
        self.encoding = encoding


class EditorFiles:
    """The files the editor has open, by opaque id.  Paths never come from the page."""

    def __init__(self) -> None:
        self._files: dict[str, _File] = {}

    @staticmethod
    def _public(file_id: str, entry: _File, **extra: Any) -> dict[str, Any]:
        return {"id": file_id, "name": entry.path.name, "path": str(entry.path), "encoding": entry.encoding, **extra}

    def _get(self, file_id: Any) -> _File:
        entry = self._files.get(file_id) if isinstance(file_id, str) else None
        if entry is None:
            raise EditorError("not_found", "That file is not open any more. Use Save As.")
        return entry

    def _add(self, path: Path, encoding: str) -> str:
        for file_id, entry in self._files.items():
            if entry.path == path:  # already open: the same id, so both tabs save to one place
                entry.encoding = encoding
                return file_id
        if len(self._files) >= MAX_OPEN_FILES:
            raise EditorError("too_many", f"Up to {MAX_OPEN_FILES} files can be open at once. Close one first.")
        file_id = secrets.token_hex(16)
        self._files[file_id] = _File(path, encoding)
        return file_id

    def open(self, path: Path, blocked: Iterable[Path]) -> dict[str, Any]:
        """Read a file the user picked and give it an id."""
        path = path.resolve()
        reason = refused_reason(path, blocked)
        if reason:
            raise EditorError("refused", reason)
        content = read_text_file(path)
        file_id = self._add(path, content["encoding"])
        return self._public(file_id, self._files[file_id], text=content["text"], eol=content["eol"])

    def save(self, file_id: Any, text: Any, eol: Any, encoding: Any, blocked: Iterable[Path]) -> dict[str, Any]:
        """Write ``text`` back to the open file ``file_id``."""
        entry = self._get(file_id)
        self._check(text, eol)
        reason = refused_reason(entry.path, blocked)  # the settings may have moved a vault since
        if reason:
            raise EditorError("refused", reason)
        enc = encoding if encoding in ENCODINGS else entry.encoding
        write_text_file(entry.path, text, encoding=enc, eol=eol)
        entry.encoding = enc
        return self._public(file_id, entry, eol=eol)

    def save_as(
        self, path: Path, text: Any, eol: Any, encoding: Any, blocked: Iterable[Path], old_id: Any = None,
    ) -> dict[str, Any]:
        """Write ``text`` to a path the user picked; the tab gets that file's id."""
        path = path.resolve()
        reason = refused_reason(path, blocked)
        if reason:
            raise EditorError("refused", reason)
        self._check(text, eol)
        enc = encoding if encoding in ENCODINGS else "utf-8"
        write_text_file(path, text, encoding=enc, eol=eol)
        if isinstance(old_id, str) and old_id in self._files and self._files[old_id].path != path:
            self._files.pop(old_id)
        file_id = self._add(path, enc)
        return self._public(file_id, self._files[file_id], eol=eol)

    def close(self, file_id: Any) -> None:
        if isinstance(file_id, str):
            self._files.pop(file_id, None)

    def close_all(self) -> None:
        self._files.clear()

    @staticmethod
    def _check(text: Any, eol: Any) -> None:
        if not isinstance(text, str):
            raise EditorError("invalid_input", "There is no text to save.")
        if eol not in EOLS:
            raise EditorError("invalid_input", "Pick a line ending: LF, CRLF or CR.")
        if len(text) > MAX_FILE_BYTES:
            raise EditorError("too_big", "That text is too big to save from here.")
