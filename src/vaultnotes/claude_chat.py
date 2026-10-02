"""The Claude space: a chat with Claude Code, drawn by the page.

Each message starts one ``claude -p`` process in the notes folder (the same
Claude Code, login and ``~/.claude/CLAUDE.md`` as in a terminal) and the next
message resumes the same session.  Claude Code prints one JSON object per line
(``--output-format stream-json``); this module turns those into the few events
the page draws (``text``, ``tool``, ``tool_result``, ``done``) and sends them
as ``claude_event``, batched so a fast reply does not flood the window.

The page never names a program, an argument, a session id or a permission
rule.  Python finds ``claude`` itself, builds every argument, keeps the session
ids, and works out an "Allow" rule only from a denial Claude Code reported.

Permissions: Claude Code runs in its default mode, so it reads freely but a
write or a command it does not already trust is *denied* and reported at the
end of the turn.  The page shows each denial as a card; Allow adds a rule for
that chat only (kept in memory, gone when the app closes) and resumes the turn
so Claude retries.  The Encrypted vault folder is always denied.

Chat titles (the first words of the first message) and session ids are kept in
settings.json under ``claude`` so the list survives a restart; the messages
themselves stay in Claude Code's own history, which :func:`read_history` reads.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path, PurePath
from typing import Any, Callable, Iterable

#: Chats kept in settings.json, newest first.
MAX_CHATS_KEPT = 30
#: Replies being written at once.
MAX_RUNNING = 4
#: The most text one message may hold (a pasted log is fine, a book is not).
MAX_PROMPT_LENGTH = 100_000
MAX_TITLE_LENGTH = 60
#: How long the reader gathers output before sending one event.
FLUSH_SECONDS = 0.03
#: Longest one-line summary of a tool call, and longest result preview.
MAX_SUMMARY_LENGTH = 300
MAX_PREVIEW_LENGTH = 800
#: Most events one reopened chat shows, newest kept.
MAX_HISTORY_EVENTS = 400

CHAT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
SESSION_ID_RE = re.compile(r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
TOOL_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,40}$")

#: Tools that change a file; an Allow rule for one is an ``Edit(path)`` rule.
EDIT_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
#: Tools that run a command line.
SHELL_TOOLS = frozenset({"PowerShell", "Bash"})

Emit = Callable[[str, Any], None]
Spawn = Callable[[list[str], str, dict[str, str]], Any]


class ClaudeError(Exception):
    """A Claude-space problem with a user-facing code and message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ----------------------------------------------------------------------
# Finding Claude Code, and what it is told
# ----------------------------------------------------------------------
def find_claude() -> str | None:
    """The ``claude`` program on this PC; nothing the page sent."""
    found = shutil.which("claude")
    if found:
        return found
    for name in ("claude.exe", "claude"):
        candidate = Path.home() / ".local" / "bin" / name
        if candidate.is_file():
            return str(candidate)
    return None


def deny_rules(encrypted_dir: Path | str) -> list[str]:
    """Rules that keep Claude out of the Encrypted vault (it holds PHI)."""
    base = Path(encrypted_dir).as_posix().rstrip("/")
    return [f"Read({base}/**)", f"Edit({base}/**)"]


def system_prompt(notes_root: Path | str, encrypted_dir: Path | str) -> str:
    """Added to Claude Code's own prompt, on top of the user's CLAUDE.md."""
    return (
        "You are running in the Claude space inside the VaultNotes app, which shows your replies "
        "in a chat window. "
        f"The notes folder is {notes_root}. "
        "Write notes only to the AI-Notes space, through notes.py. "
        f"Never open, list or search the Encrypted vault ({encrypted_dir}) or its key: it holds PHI. "
        "Never put PHI in a note. Keep replies readable in a narrow chat window."
    )


def build_args(
    exe: str,
    *,
    session_id: str | None,
    allowed: Iterable[str],
    denied: Iterable[str],
    prompt: str,
) -> list[str]:
    """The whole command line. The message itself goes in on stdin, not here."""
    argv = [
        exe,
        "-p",
        "--output-format", "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--permission-mode", "default",
        "--append-system-prompt", prompt,
    ]
    if session_id:
        argv += ["--resume", session_id]
    allowed = sorted(set(allowed))
    if allowed:
        argv += ["--allowedTools", *allowed]
    denied = list(denied)
    if denied:
        argv += ["--disallowedTools", *denied]
    return argv


# ----------------------------------------------------------------------
# Turning Claude Code's lines into page events
# ----------------------------------------------------------------------
def _one_line(value: Any, limit: int = MAX_SUMMARY_LENGTH) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def summarize_tool(name: str, tool_input: Any) -> str:
    """One line saying what a tool call does, for the collapsed row."""
    data = tool_input if isinstance(tool_input, dict) else {}

    def pick(*keys: str) -> str:
        for key in keys:
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return ""

    if name in EDIT_TOOLS or name == "Read":
        return _one_line(pick("file_path", "notebook_path", "path"))
    if name in SHELL_TOOLS:
        return _one_line(pick("command", "description"))
    if name == "Glob":
        return _one_line(pick("pattern"))
    if name == "Grep":
        where = pick("path")
        return _one_line(pick("pattern") + (f"  in {where}" if where else ""))
    if name == "WebFetch":
        return _one_line(pick("url"))
    if name == "WebSearch":
        return _one_line(pick("query"))
    return _one_line(pick("description", "prompt", "skill", "command", "file_path", "path", "query", "pattern"))


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def _preview(content: Any) -> str:
    text = _result_text(content).strip()
    return text if len(text) <= MAX_PREVIEW_LENGTH else text[:MAX_PREVIEW_LENGTH] + "…"


def _posix(path: str) -> str:
    return PurePath(path).as_posix()


def allow_rule(denial: dict[str, Any], scope: str, encrypted_dir: Path | str | None = None) -> str | None:
    """The ``--allowedTools`` rule for one reported denial, or None.

    ``scope`` is ``"exact"`` (this file or this command only) or ``"tool"``
    (every use of the tool in this chat).  Built from what Claude Code reported,
    never from anything the page sent.
    """
    name = denial.get("tool_name")
    data = denial.get("tool_input")
    if not isinstance(name, str) or not TOOL_NAME_RE.match(name) or not isinstance(data, dict):
        return None
    if scope == "tool":
        return name
    if scope != "exact":
        return None
    if name in EDIT_TOOLS:
        path = data.get("file_path") or data.get("notebook_path")
        if not isinstance(path, str) or not path.strip() or any(ch in path for ch in "()*?[]\n"):
            return None
        posix = _posix(path)
        if encrypted_dir is not None:
            vault = Path(encrypted_dir).as_posix().rstrip("/").casefold()
            if posix.casefold() == vault or posix.casefold().startswith(vault + "/"):
                return None
        return f"Edit({posix})"
    if name in SHELL_TOOLS:
        command = data.get("command")
        if not isinstance(command, str) or not command.strip():
            return None
        if any(ch in command for ch in "()*\n\r") or command.rstrip().endswith(":"):
            return None  # a rule cannot spell these exactly
        return f"{name}({command.strip()})"
    if name == "WebFetch":
        url = data.get("url")
        host = re.match(r"^https?://([A-Za-z0-9.-]+)(?:[:/?#]|$)", url) if isinstance(url, str) else None
        return f"WebFetch(domain:{host.group(1)})" if host else None
    return None


class Translator:
    """Reads Claude Code's JSON lines one at a time and yields page events."""

    def __init__(self) -> None:
        self.session_id: str | None = None
        self._message_id: str | None = None
        self._streamed: set[str] = set()
        self._tools: set[str] = set()
        self.finished = False

    def feed(self, obj: Any) -> list[dict[str, Any]]:
        if not isinstance(obj, dict):
            return []
        kind = obj.get("type")
        session = obj.get("session_id")
        if isinstance(session, str) and SESSION_ID_RE.match(session):
            self.session_id = session
        if obj.get("parent_tool_use_id"):
            return []  # a sub-agent's own chatter; its tool call row is enough
        if kind == "stream_event":
            return self._stream(obj.get("event"))
        if kind == "assistant":
            return self._assistant(obj.get("message"))
        if kind == "user":
            return self._user(obj.get("message"))
        if kind == "result":
            return [self._result(obj)]
        return []

    def _stream(self, event: Any) -> list[dict[str, Any]]:
        if not isinstance(event, dict):
            return []
        if event.get("type") == "message_start":
            message = event.get("message")
            self._message_id = message.get("id") if isinstance(message, dict) else None
        elif event.get("type") == "content_block_delta":
            delta = event.get("delta")
            if isinstance(delta, dict) and delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
                if self._message_id:
                    self._streamed.add(self._message_id)
                return [{"kind": "text", "text": delta["text"]}]
        return []

    def _assistant(self, message: Any) -> list[dict[str, Any]]:
        if not isinstance(message, dict) or not isinstance(message.get("content"), list):
            return []
        out: list[dict[str, Any]] = []
        for block in message["content"]:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                # Already drawn from the deltas unless partial output was off.
                if message.get("id") not in self._streamed and block["text"].strip():
                    out.append({"kind": "text", "text": block["text"]})
            elif block.get("type") == "tool_use":
                tool_id, name = block.get("id"), block.get("name")
                if isinstance(tool_id, str) and isinstance(name, str) and tool_id not in self._tools:
                    self._tools.add(tool_id)
                    out.append({
                        "kind": "tool", "id": tool_id, "name": name[:60],
                        "summary": summarize_tool(name, block.get("input")),
                    })
        return out

    def _user(self, message: Any) -> list[dict[str, Any]]:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            return []
        return [
            {
                "kind": "tool_result", "id": block.get("tool_use_id"),
                "ok": block.get("is_error") is not True, "preview": _preview(block.get("content")),
            }
            for block in content
            if isinstance(block, dict) and block.get("type") == "tool_result"
            and isinstance(block.get("tool_use_id"), str)
        ]

    def _result(self, obj: dict[str, Any]) -> dict[str, Any]:
        self.finished = True
        failed = obj.get("is_error") is True or obj.get("subtype") not in (None, "success")
        error = None
        if failed:
            text = obj.get("result") if isinstance(obj.get("result"), str) else ""
            error = _one_line(text or obj.get("subtype") or "Claude Code stopped with an error.", 400)
        denials = []
        for item in obj.get("permission_denials") or []:
            if isinstance(item, dict) and isinstance(item.get("tool_use_id"), str):
                denials.append(item)
        return {
            "kind": "done", "ok": not failed, "error": error, "denials": denials,
            "cost": obj.get("total_cost_usd") if isinstance(obj.get("total_cost_usd"), (int, float)) else None,
            "ms": obj.get("duration_ms") if isinstance(obj.get("duration_ms"), int) else None,
        }


# ----------------------------------------------------------------------
# Reopening a chat: Claude Code's own history file
# ----------------------------------------------------------------------
def history_path(session_id: str, cwd: Path | str, home: Path | None = None) -> Path:
    """``~/.claude/projects/<folder, non-letters as dashes>/<session>.jsonl``."""
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
    return (home or Path.home()) / ".claude" / "projects" / slug / f"{session_id}.jsonl"


def read_history(session_id: str, cwd: Path | str, home: Path | None = None) -> list[dict[str, Any]]:
    """What was said, as page events (``user`` added); empty when it cannot be read."""
    if not SESSION_ID_RE.match(session_id):
        return []
    path = history_path(session_id, cwd, home)
    events: list[dict[str, Any]] = []
    tools = Translator()
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(obj, dict) or obj.get("isSidechain") or obj.get("isMeta"):
                    continue
                if obj.get("type") == "user":
                    message = obj.get("message")
                    content = message.get("content") if isinstance(message, dict) else None
                    if isinstance(content, str):
                        text = content.strip()
                        if text and not text.startswith("<"):
                            events.append({"kind": "user", "text": text[:MAX_PROMPT_LENGTH]})
                    elif isinstance(content, list):
                        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
                        text = "\n".join(parts).strip()
                        if text and not text.startswith("<"):
                            events.append({"kind": "user", "text": text[:MAX_PROMPT_LENGTH]})
                        events += tools._user(message)
                elif obj.get("type") == "assistant":
                    message = obj.get("message")
                    if isinstance(message, dict):
                        # No deltas in a saved file: every text block counts.
                        events += tools._assistant({**message, "id": None})
    except OSError:
        return []
    return events[-MAX_HISTORY_EVENTS:]


# ----------------------------------------------------------------------
# Chats and the running process
# ----------------------------------------------------------------------
def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_title(text: str) -> str:
    line = next((ln for ln in text.splitlines() if ln.strip()), "")
    line = "".join(ch for ch in line if ch.isprintable()).strip()
    return line if len(line) <= MAX_TITLE_LENGTH else line[: MAX_TITLE_LENGTH - 1].rstrip() + "…"


class _Chat:
    def __init__(self, chat_id: str, title: str, session_id: str | None = None, updated: str = "") -> None:
        self.id = chat_id
        self.title = title
        self.session_id = session_id
        self.updated = updated or _now()
        #: Rules the user allowed in this chat; memory only.
        self.allowed: set[str] = set()
        #: tool_use_id -> the denial Claude Code reported at the last turn's end.
        self.denials: dict[str, dict[str, Any]] = {}
        self.turn: "_Turn | None" = None

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "updated": self.updated, "running": self.turn is not None}


class _Turn:
    """One ``claude -p`` process: a reader for its output, a sender for the page."""

    def __init__(
        self, manager: "ClaudeManager", chat: _Chat, process: Any, kill: Callable[[Any], None]
    ) -> None:
        self.manager = manager
        self.chat = chat
        self.process = process
        self._kill = kill
        self.stopped = False
        self.translator = Translator()
        self._pending: list[dict[str, Any]] = []
        self._ended = False
        self._wake = threading.Condition()
        self._stderr: list[bytes] = []
        self._stderr_reader = threading.Thread(target=self._read_stderr, name="VaultNotes-claude-err", daemon=True)
        self._stderr_reader.start()
        threading.Thread(target=self._read_stdout, name="VaultNotes-claude-read", daemon=True).start()
        threading.Thread(target=self._send_loop, name="VaultNotes-claude-send", daemon=True).start()

    def _push(self, event: dict[str, Any]) -> None:
        with self._wake:
            self._pending.append(event)
            self._wake.notify()

    def _read_stderr(self) -> None:
        try:
            for line in self.process.stderr:
                self._stderr.append(line)
                del self._stderr[:-20]
        except Exception:  # noqa: BLE001 - a closed pipe ends it
            pass

    def _read_stdout(self) -> None:
        try:
            for raw in self.process.stdout:
                try:
                    obj = json.loads(raw.decode("utf-8", errors="replace"))
                except ValueError:
                    continue
                for event in self.translator.feed(obj):
                    self._push(event)
        except Exception:  # noqa: BLE001 - a broken pipe ends the turn
            pass
        code = None
        try:
            code = self.process.wait()
        except Exception:  # noqa: BLE001
            pass
        if not self.translator.finished:
            if self.stopped:
                self._push({"kind": "done", "ok": True, "error": None, "denials": [], "cost": None, "ms": None,
                            "stopped": True})
            else:
                self._stderr_reader.join(2)  # the last words of a dying process
                tail = b"".join(self._stderr).decode("utf-8", errors="replace").strip()
                reason = _one_line(tail, 400) if tail else f"Claude Code ended unexpectedly (exit code {code})."
                self._push({"kind": "done", "ok": False, "error": reason, "denials": [], "cost": None, "ms": None})
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
                batch, self._pending = self._pending, []
                ended = self._ended
            merged: list[dict[str, Any]] = []
            for event in batch:
                if event["kind"] == "text" and merged and merged[-1]["kind"] == "text":
                    merged[-1] = {"kind": "text", "text": merged[-1]["text"] + event["text"]}
                else:
                    merged.append(event)
            for event in merged:
                finished = event["kind"] == "done"
                if finished:
                    event = self.manager._turn_done(self, event)
                self.manager._emit("claude_event", {"chat": self.chat.id, **event})
                if finished:
                    # After the page has the reply: saving waits for the call lock.
                    self.manager._changed()
            if ended and not self._pending:
                break

    def stop(self) -> None:
        self.stopped = True
        try:
            self._kill(self.process)
        except Exception:  # noqa: BLE001 - it may have exited already
            pass


def _popen(argv: list[str], cwd: str, env: dict[str, str]) -> Any:
    return subprocess.Popen(
        argv, cwd=cwd, env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _kill_tree(process: Any) -> None:
    """End Claude Code and anything it started (a command it was running)."""
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True, timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return
        except Exception:  # noqa: BLE001 - fall back to ending just the process
            pass
    process.kill()


class ClaudeManager:
    """The Claude space's chats, at most :data:`MAX_RUNNING` replying at once."""

    def __init__(
        self,
        emit: Emit,
        on_change: Callable[[], None] | None = None,
        *,
        spawn: Spawn | None = None,
        find: Callable[[], str | None] | None = None,
        kill: Callable[[Any], None] | None = None,
    ) -> None:
        self._emit = emit
        self._on_change = on_change
        self._spawn = spawn or _popen
        self._find = find or find_claude
        self._kill = kill or _kill_tree
        self._lock = threading.Lock()
        self._chats: dict[str, _Chat] = {}
        #: Where the running chats were started, for :meth:`allow` and history.
        self.cwd: Path | None = None
        self.encrypted_dir: Path | None = None

    # -- the saved list ------------------------------------------------
    def load(self, stored: Any) -> None:
        chats: dict[str, _Chat] = {}
        for item in stored if isinstance(stored, list) else []:
            if not isinstance(item, dict):
                continue
            chat_id, session = item.get("id"), item.get("session")
            if not isinstance(chat_id, str) or not CHAT_ID_RE.match(chat_id):
                continue
            if not isinstance(session, str) or not SESSION_ID_RE.match(session):
                continue
            title = item.get("title") if isinstance(item.get("title"), str) else ""
            updated = item.get("updated") if isinstance(item.get("updated"), str) else ""
            chats[chat_id] = _Chat(chat_id, clean_title(title) or "Chat", session, updated[:40])
        with self._lock:
            self._chats = chats

    def dump(self) -> list[dict[str, Any]]:
        with self._lock:
            kept = [c for c in self._chats.values() if c.session_id]
        kept.sort(key=lambda c: c.updated, reverse=True)
        return [
            {"id": c.id, "session": c.session_id, "title": c.title, "updated": c.updated}
            for c in kept[:MAX_CHATS_KEPT]
        ]

    def _changed(self) -> None:
        if self._on_change:
            try:
                self._on_change()
            except Exception:  # noqa: BLE001 - saving the list must never break a reply
                pass

    def available(self) -> bool:
        return self._find() is not None

    def chats(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = [c.public() for c in self._chats.values()]
        return sorted(rows, key=lambda r: r["updated"], reverse=True)

    def _chat(self, chat_id: Any) -> _Chat:
        with self._lock:
            chat = self._chats.get(chat_id) if isinstance(chat_id, str) else None
        if chat is None:
            raise ClaudeError("not_found", "That chat is not in the list any more.")
        return chat

    # -- talking -------------------------------------------------------
    def send(
        self,
        chat_id: Any,
        text: Any,
        *,
        cwd: Path,
        encrypted_dir: Path,
        notes_root: Path,
    ) -> dict[str, Any]:
        """Send a message in a chat (``None`` starts a new one) and start the reply."""
        if chat_id is not None and not isinstance(chat_id, str):
            raise ClaudeError("not_found", "That chat is not in the list any more.")
        if not isinstance(text, str) or not text.strip():
            raise ClaudeError("invalid_input", "Write a message first.")
        if len(text) > MAX_PROMPT_LENGTH:
            raise ClaudeError("invalid_input", "That message is too long.")
        exe = self._find()
        if not exe:
            raise ClaudeError("no_claude", "Claude Code was not found on this PC. Install it, then try again.")
        if not cwd.is_dir():
            raise ClaudeError("no_folder", "The notes folder could not be found.")
        self.cwd, self.encrypted_dir = cwd, encrypted_dir
        with self._lock:
            chat = self._chats.get(chat_id) if chat_id is not None else None
            if chat_id is not None and chat is None:
                raise ClaudeError("not_found", "That chat is not in the list any more.")
            if chat is not None and chat.turn is not None:
                raise ClaudeError("busy", "Claude is still replying in this chat.")
            if sum(1 for c in self._chats.values() if c.turn is not None) >= MAX_RUNNING:
                raise ClaudeError("too_many", f"Up to {MAX_RUNNING} replies can run at once. Wait for one to finish.")
            if chat is None:
                chat = _Chat(secrets.token_hex(16), clean_title(text) or "Chat")
                self._chats[chat.id] = chat
            chat.denials = {}
            argv = build_args(
                exe, session_id=chat.session_id, allowed=chat.allowed, denied=deny_rules(encrypted_dir),
                prompt=system_prompt(notes_root, encrypted_dir),
            )
            started = self._start(chat, argv, str(cwd), text)
        self._changed()
        return {"chat": started.public()}

    def _start(self, chat: _Chat, argv: list[str], cwd: str, text: str) -> _Chat:
        env = dict(os.environ)
        try:
            process = self._spawn(argv, cwd, env)
        except Exception as exc:  # noqa: BLE001
            if chat.session_id is None:
                self._chats.pop(chat.id, None)
            raise ClaudeError("start_failed", f"Claude Code could not be started ({type(exc).__name__}).") from exc
        try:
            process.stdin.write(text.encode("utf-8"))
            process.stdin.close()
        except Exception:  # noqa: BLE001 - the reader reports why it ended
            pass
        chat.updated = _now()
        chat.turn = _Turn(self, chat, process, self._kill)
        return chat

    def _turn_done(self, turn: _Turn, event: dict[str, Any]) -> dict[str, Any]:
        """The reply ended: keep the session, and turn raw denials into cards."""
        chat = turn.chat
        with self._lock:
            if turn.translator.session_id:
                chat.session_id = turn.translator.session_id
            chat.updated = _now()
            chat.turn = None
            cards = []
            for item in event.get("denials", []):
                chat.denials[item["tool_use_id"]] = item
                name = str(item.get("tool_name", ""))[:60]
                cards.append({
                    "id": item["tool_use_id"], "tool": name,
                    "summary": summarize_tool(name, item.get("tool_input")),
                    "exact": allow_rule(item, "exact", self.encrypted_dir) is not None,
                })
            gone = chat.session_id is None
            if gone:
                self._chats.pop(chat.id, None)  # it never got going: the page keeps its draft
        return {**event, "denials": cards, "gone": gone}

    def stop(self, chat_id: Any) -> None:
        """Stop the reply being written in a chat."""
        chat = self._chat(chat_id)
        turn = chat.turn
        if turn is not None:
            turn.stop()

    def allow(self, chat_id: Any, ids: Any, scope: Any, *, cwd: Path, encrypted_dir: Path, notes_root: Path) -> dict[str, Any]:
        """Allow the denied actions the user picked in this chat, and let Claude retry."""
        if scope not in ("exact", "tool") or not isinstance(ids, list) or not ids:
            raise ClaudeError("invalid_input", "Pick what to allow.")
        chat = self._chat(chat_id)
        rules, summaries = [], []
        for tool_use_id in ids[:20]:
            denial = chat.denials.get(tool_use_id) if isinstance(tool_use_id, str) else None
            if denial is None:
                raise ClaudeError("not_found", "That request is not waiting any more.")
            rule = allow_rule(denial, scope, encrypted_dir)
            if rule is None:
                raise ClaudeError("not_allowed", "That cannot be allowed one use at a time.")
            rules.append(rule)
            summaries.append(f"{denial['tool_name']}: {summarize_tool(denial['tool_name'], denial.get('tool_input'))}")
        chat.allowed.update(rules)
        prompt = "I have now allowed this, so please go ahead and retry it:\n" + "\n".join(f"- {s}" for s in summaries[:5])
        return self.send(chat.id, prompt, cwd=cwd, encrypted_dir=encrypted_dir, notes_root=notes_root)

    def forget(self, chat_id: Any) -> None:
        """Take a chat off the list (Claude Code's own history file is left alone)."""
        chat = self._chat(chat_id)
        if chat.turn is not None:
            chat.turn.stop()
        with self._lock:
            self._chats.pop(chat.id, None)
        self._changed()

    def history(self, chat_id: Any, cwd: Path) -> list[dict[str, Any]]:
        chat = self._chat(chat_id)
        return read_history(chat.session_id, cwd) if chat.session_id else []

    def stop_all(self) -> None:
        with self._lock:
            turns = [c.turn for c in self._chats.values() if c.turn is not None]
        for turn in turns:
            turn.stop()
