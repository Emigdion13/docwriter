"""The CMD space: a real shell in a Windows pseudo-console (ConPTY).

The page only ever names a shell by its id (``cmd``, ``powershell`` or
``bash``); Python alone decides which program that is, where it lives and
with which arguments, so a Bridge call can never start anything else.  Keys go
in through :meth:`TerminalManager.write` and the screen comes back as
``terminal_output`` events, batched so a command that prints a lot does not
flood the window with one event per byte.

Recent and favorite commands are kept here as plain lists; ``api.py`` stores
them in ``settings.json`` under ``terminal``.
"""

from __future__ import annotations

import os
import secrets
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

#: Shell ids in the order the picker shows them.
SHELL_IDS = ("cmd", "powershell", "bash")

#: Recent commands kept in settings.json, newest first.
MAX_RECENT = 50
#: Starred commands kept in settings.json.
MAX_FAVORITES = 100
#: The longest command the list keeps (a pasted script is not a "command").
MAX_COMMAND_LENGTH = 1000
#: The most text one ``terminal_write`` may send (a large paste).
MAX_WRITE_LENGTH = 65_536
#: Shells open at once (one per tab in the CMD space).
MAX_SESSIONS = 8
#: Terminal size limits, in character cells.
MIN_COLS, MAX_COLS = 20, 500
MIN_ROWS, MAX_ROWS = 5, 200

#: How long the reader gathers output before sending one event, and the most
#: text one event carries.
FLUSH_SECONDS = 0.016
MAX_EVENT_CHARS = 131_072

Emit = Callable[[str, Any], None]


class TerminalError(Exception):
    """A shell problem with a user-facing code and message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ----------------------------------------------------------------------
# Which shells this PC has
# ----------------------------------------------------------------------
def _system_root() -> Path:
    return Path(os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows")


def _find_cmd() -> list[str] | None:
    comspec = os.environ.get("ComSpec")
    candidates = [Path(comspec)] if comspec else []
    candidates.append(_system_root() / "System32" / "cmd.exe")
    for path in candidates:
        if path.name.lower() == "cmd.exe" and path.is_file():
            return [str(path)]
    return None


def _find_powershell() -> list[str] | None:
    # PowerShell 7 when it is installed, Windows PowerShell otherwise.
    pwsh = shutil.which("pwsh.exe")
    if pwsh:
        return [pwsh, "-NoLogo"]
    legacy = _system_root() / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if legacy.is_file():
        return [str(legacy), "-NoLogo"]
    return None


def _find_git_bash() -> list[str] | None:
    """Git for Windows' bash, never WSL's ``System32\\bash.exe``."""
    candidates: list[Path] = []
    git = shutil.which("git.exe")
    if git:
        # ...\Git\cmd\git.exe or ...\Git\bin\git.exe -> ...\Git\bin\bash.exe
        candidates.append(Path(git).resolve().parent.parent / "bin" / "bash.exe")
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramW6432"), os.environ.get("LOCALAPPDATA")):
        if base:
            candidates.append(Path(base) / "Git" / "bin" / "bash.exe")
            candidates.append(Path(base) / "Programs" / "Git" / "bin" / "bash.exe")
    system32 = (_system_root() / "System32").resolve()
    for path in candidates:
        try:
            if path.is_file() and path.resolve().parent != system32:
                return [str(path), "--login", "-i"]
        except OSError:
            continue
    return None


SHELL_NAMES = {"cmd": "CMD", "powershell": "PowerShell", "bash": "Git Bash"}
_FINDERS: dict[str, Callable[[], list[str] | None]] = {
    "cmd": _find_cmd,
    "powershell": _find_powershell,
    "bash": _find_git_bash,
}


def available_shells() -> list[dict[str, str]]:
    """The shells found on this PC, as ``{"id", "name"}`` for the picker."""
    if sys.platform != "win32":
        return []
    return [{"id": shell_id, "name": SHELL_NAMES[shell_id]} for shell_id in SHELL_IDS if _FINDERS[shell_id]()]


def shell_command(shell_id: str) -> list[str]:
    """The program and arguments for a shell id; nothing the page sent."""
    if shell_id not in SHELL_IDS:
        raise TerminalError("invalid_shell", "Unknown shell.")
    argv = _FINDERS[shell_id]() if sys.platform == "win32" else None
    if not argv:
        raise TerminalError("no_shell", f"{SHELL_NAMES[shell_id]} was not found on this PC.")
    return argv


# ----------------------------------------------------------------------
# Recent and favorite commands
# ----------------------------------------------------------------------
def clean_command(command: Any) -> str | None:
    """The command as the list keeps it, or None when it is not one.

    A command typed with a leading space is left out on purpose (like bash's
    ``HISTCONTROL=ignorespace``), so a command holding a secret can be kept
    out of settings.json.
    """
    if not isinstance(command, str) or not command or command[0].isspace():
        return None
    text = command.rstrip()
    if not text or len(text) > MAX_COMMAND_LENGTH:
        return None
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        return None  # one line, no control keys
    return text


def clean_command_list(value: Any, limit: int) -> list[str]:
    """A stored list with bad entries and duplicates dropped, at most ``limit``."""
    out: list[str] = []
    seen: set[str] = set()
    for item in value if isinstance(value, list) else []:
        text = clean_command(item)
        if text is not None and text not in seen:
            seen.add(text)
            out.append(text)
        if len(out) >= limit:
            break
    return out


def remember(commands: list[str], command: str, limit: int) -> list[str]:
    """``command`` moved (or added) to the front, the list cut to ``limit``."""
    return [command, *(item for item in commands if item != command)][:limit]


# ----------------------------------------------------------------------
# The running shell
# ----------------------------------------------------------------------
def _clamp(value: Any, low: int, high: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TerminalError("invalid_input", f"{name} must be a number.")
    return max(low, min(high, int(value)))


class _Session:
    """One shell in one pseudo-console, and the thread that reads it."""

    def __init__(
        self, session_id: str, shell_id: str, process: Any, emit: Emit, on_end: Callable[["_Session"], None]
    ) -> None:
        self.id = session_id
        self.shell_id = shell_id
        self.process = process
        self._emit = emit
        self._on_end = on_end
        self._closing = False
        # The reader only reads; the sender gathers what arrived within a
        # frame and sends it as one event, so evaluate_js never slows reading.
        self._pending: list[str] = []
        self._ended = False
        self._wake = threading.Condition()
        threading.Thread(target=self._read_loop, name="VaultNotes-terminal-read", daemon=True).start()
        threading.Thread(target=self._send_loop, name="VaultNotes-terminal-send", daemon=True).start()

    def _read_loop(self) -> None:
        while True:
            try:
                chunk = self.process.read(8192)
            except EOFError:
                break
            except Exception:  # noqa: BLE001 - a broken pipe ends the session
                break
            if chunk:
                with self._wake:
                    self._pending.append(chunk)
                    self._wake.notify()
        with self._wake:
            self._ended = True
            self._wake.notify()

    def _send_loop(self) -> None:
        while True:
            with self._wake:
                while not self._pending and not self._ended:
                    self._wake.wait()
                ended = self._ended
            if not ended:
                time.sleep(FLUSH_SECONDS)  # let the rest of this burst arrive
            with self._wake:
                data, self._pending = "".join(self._pending), []
                ended = self._ended
            for start in range(0, len(data), MAX_EVENT_CHARS):
                self._emit("terminal_output", {"id": self.id, "data": data[start:start + MAX_EVENT_CHARS]})
            if ended:
                break
        code = None
        try:
            code = self.process.exitstatus
        except Exception:  # noqa: BLE001
            pass
        self._on_end(self)
        self._emit("terminal_exit", {"id": self.id, "code": code, "stopped": self._closing})

    def write(self, data: str) -> None:
        self.process.write(data)

    def resize(self, cols: int, rows: int) -> None:
        self.process.setwinsize(rows, cols)

    def stop(self) -> None:
        self._closing = True
        try:
            if self.process.isalive():
                self.process.terminate(force=True)
        except Exception:  # noqa: BLE001 - it may have exited already
            pass


class TerminalManager:
    """The CMD space's shells, one per tab, at most :data:`MAX_SESSIONS`."""

    def __init__(self, emit: Emit, spawn: Callable[..., Any] | None = None) -> None:
        self._emit = emit
        self._spawn = spawn
        self._lock = threading.Lock()
        self._sessions: dict[str, _Session] = {}

    def _spawner(self) -> Callable[..., Any]:
        if self._spawn is not None:
            return self._spawn
        try:
            from winpty import PtyProcess  # type: ignore
        except ImportError as exc:
            raise TerminalError(
                "no_pty", "The terminal needs the pywinpty package (pip install -r requirements.txt)."
            ) from exc
        return PtyProcess.spawn

    def start(self, shell_id: str, cols: Any = 80, rows: Any = 24) -> dict[str, Any]:
        """Start ``shell_id`` in the user's home folder, next to the shells already open."""
        argv = shell_command(shell_id)
        cols = _clamp(cols, MIN_COLS, MAX_COLS, "Columns")
        rows = _clamp(rows, MIN_ROWS, MAX_ROWS, "Rows")
        spawn = self._spawner()
        env = dict(os.environ)
        env.setdefault("TERM", "xterm-256color")
        env["COLORTERM"] = "truecolor"
        with self._lock:
            if len(self._sessions) >= MAX_SESSIONS:
                raise TerminalError("too_many", f"Up to {MAX_SESSIONS} shells can be open at once. Close a tab first.")
        try:
            process = spawn(argv, cwd=str(Path.home()), env=env, dimensions=(rows, cols))
        except Exception as exc:  # noqa: BLE001
            raise TerminalError(
                "start_failed", f"{SHELL_NAMES[shell_id]} could not be started ({type(exc).__name__})."
            ) from exc
        session = _Session(secrets.token_hex(16), shell_id, process, self._emit, self._forget)
        with self._lock:
            self._sessions[session.id] = session
        return {"id": session.id, "shell": shell_id, "name": SHELL_NAMES[shell_id]}

    def _forget(self, session: _Session) -> None:
        """A shell ended: free its place."""
        with self._lock:
            if self._sessions.get(session.id) is session:
                del self._sessions[session.id]

    def _current(self, session_id: Any) -> _Session:
        with self._lock:
            session = self._sessions.get(session_id) if isinstance(session_id, str) else None
        if session is None:
            raise TerminalError("not_running", "That shell is not running any more.")
        return session

    def write(self, session_id: Any, data: Any) -> None:
        if not isinstance(data, str) or len(data) > MAX_WRITE_LENGTH:
            raise TerminalError("invalid_input", "That input was too long for the terminal.")
        session = self._current(session_id)
        try:
            session.write(data)
        except Exception as exc:  # noqa: BLE001
            raise TerminalError("not_running", "That shell is not running any more.") from exc

    def resize(self, session_id: Any, cols: Any, rows: Any) -> None:
        session = self._current(session_id)
        try:
            session.resize(_clamp(cols, MIN_COLS, MAX_COLS, "Columns"), _clamp(rows, MIN_ROWS, MAX_ROWS, "Rows"))
        except TerminalError:
            raise
        except Exception:  # noqa: BLE001 - resizing a shell that just exited
            pass

    def running(self) -> list[dict[str, Any]]:
        """The shells still alive, oldest first."""
        with self._lock:
            sessions = list(self._sessions.values())
        out = []
        for session in sessions:
            try:
                alive = session.process.isalive()
            except Exception:  # noqa: BLE001
                alive = False
            if alive:
                out.append({"id": session.id, "shell": session.shell_id, "name": SHELL_NAMES[session.shell_id]})
        return out

    def stop(self, session_id: Any = None) -> None:
        """End one shell by id, or every shell when no id is given."""
        with self._lock:
            if session_id is None:
                ended, self._sessions = list(self._sessions.values()), {}
            else:
                one = self._sessions.pop(session_id, None) if isinstance(session_id, str) else None
                ended = [one] if one is not None else []
        for session in ended:
            session.stop()
