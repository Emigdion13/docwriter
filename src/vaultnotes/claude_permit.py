"""Permission questions answered in a VaultNotes pop-up while Claude waits.

Claude Code runs this file as a tiny MCP server (registered by
:func:`vaultnotes.claude_chat.build_args` through ``--mcp-config``) and names its
one tool with ``--permission-prompt-tool``.  So when Claude wants to do
something it is not yet allowed to, Claude Code calls that tool and *waits* for
the answer instead of refusing.

The tool and the app talk through a folder of their own per chat:

* the tool writes ``<id>.ask`` (the tool name, its input and the tool-use id);
* the app shows the pop-up and writes ``<id>.answer`` (``{"allow": bool,
  "message": str}``);
* the tool reads the answer, removes both files and hands Claude Code the
  decision.

No answer within the timeout, or the folder gone (the reply ended), is a
refusal, so a question never hangs a reply for ever.

Both files are written to a ``.tmp`` name and renamed, so neither side ever
reads half a file.  Standard library only: the server runs as
``python -I claude_permit.py <folder> <timeout seconds>``.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, TextIO

#: The MCP server's name and its one tool; Claude Code calls it ``mcp__<server>__<tool>``.
SERVER_NAME = "vaultnotes_permit"
TOOL_NAME = "ask_user"
PROMPT_TOOL = f"mcp__{SERVER_NAME}__{TOOL_NAME}"

#: ``<nanoseconds>-<random>``: sorts in the order the questions were asked.
ASK_ID_RE = re.compile(r"^[0-9]{1,24}-[0-9a-f]{8}$")
#: How long the tool waits for the pop-up by default.
DEFAULT_TIMEOUT = 600
#: How often both sides look for the other's file.
POLL_SECONDS = 0.1
#: Biggest ask file the app reads (a Write of a huge file still fits).
MAX_ASK_BYTES = 8_000_000

DENIED_TIMEOUT = (
    "Nobody answered the permission pop-up in VaultNotes in time, so this was not allowed. "
    "Tell the user what you needed and why, and let them decide."
)
DENIED_GONE = "The reply was stopped before the user answered, so this was not allowed."
DENIED_BROKEN = "VaultNotes could not ask the user, so this was not allowed."

_last_stamp = 0
_stamp_lock = threading.Lock()


def _stamp() -> int:
    """Nanoseconds, always larger than the last one: Windows' clock can repeat a value."""
    global _last_stamp
    with _stamp_lock:
        _last_stamp = max(time.time_ns(), _last_stamp + 1)
        return _last_stamp


def _write(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)


def _remove(*paths: Path) -> None:
    for path in paths:
        try:
            path.unlink()
        except OSError:
            pass


# ----------------------------------------------------------------------
# The app's side
# ----------------------------------------------------------------------
def pending(folder: Path) -> list[tuple[str, dict[str, Any]]]:
    """Questions waiting for an answer, oldest first, as ``(id, request)``.

    A request is ``{"tool_name": str, "input": dict, "tool_use_id": str|None}``;
    anything else in the folder is ignored.
    """
    out: list[tuple[int, str, dict[str, Any]]] = []
    try:
        files = list(folder.glob("*.ask"))
    except OSError:
        return []
    for path in files:
        ask_id = path.stem
        if not ASK_ID_RE.match(ask_id) or (folder / f"{ask_id}.answer").exists():
            continue
        try:
            if path.stat().st_size > MAX_ASK_BYTES:
                continue
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or not isinstance(data.get("tool_name"), str):
            continue
        tool_input = data.get("input")
        tool_use_id = data.get("tool_use_id")
        out.append((int(ask_id.split("-")[0]), ask_id, {
            "tool_name": data["tool_name"],
            "input": tool_input if isinstance(tool_input, dict) else {},
            "tool_use_id": tool_use_id if isinstance(tool_use_id, str) else None,
        }))
    out.sort()
    return [(ask_id, request) for _, ask_id, request in out]


def answer(folder: Path, ask_id: str, allow: bool, message: str | None = None) -> bool:
    """Answer one question; False when it is not waiting any more."""
    if not isinstance(ask_id, str) or not ASK_ID_RE.match(ask_id):
        return False
    if not (folder / f"{ask_id}.ask").exists():
        return False
    try:
        _write(folder / f"{ask_id}.answer", {"allow": bool(allow), "message": message or ""})
    except OSError:
        return False
    return True


def clear(folder: Path) -> None:
    """Drop every question and answer, and the folder: a waiting tool then refuses."""
    try:
        for leftover in folder.iterdir():
            _remove(leftover)
        folder.rmdir()
    except OSError:
        pass


# ----------------------------------------------------------------------
# The tool's side
# ----------------------------------------------------------------------
def ask(
    folder: Path,
    request: dict[str, Any],
    timeout: float = DEFAULT_TIMEOUT,
    *,
    poll: float = POLL_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[bool, str]:
    """Ask the app and wait: ``(allowed, message for Claude when refused)``."""
    if not folder.is_dir():
        return False, DENIED_BROKEN
    ask_id = f"{_stamp()}-{secrets.token_hex(4)}"
    ask_path, answer_path = folder / f"{ask_id}.ask", folder / f"{ask_id}.answer"
    try:
        _write(ask_path, request)
    except OSError:
        return False, DENIED_BROKEN
    deadline = clock() + max(0.0, timeout)
    try:
        while True:
            try:
                data = json.loads(answer_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                data = None
            except (OSError, ValueError):
                data = None  # being written: look again
            if isinstance(data, dict):
                allowed = data.get("allow") is True
                message = data.get("message") if isinstance(data.get("message"), str) else ""
                return allowed, message or ("" if allowed else "The user said no.")
            if not folder.is_dir():
                return False, DENIED_GONE
            if clock() >= deadline:
                return False, DENIED_TIMEOUT
            sleep(poll)
    finally:
        _remove(ask_path, answer_path)


def decision_text(allowed: bool, message: str, tool_input: dict[str, Any]) -> str:
    """What the permission-prompt tool returns, as Claude Code expects it."""
    if allowed:
        return json.dumps({"behavior": "allow", "updatedInput": tool_input})
    return json.dumps({"behavior": "deny", "message": message})


TOOL_SCHEMA = {
    "name": TOOL_NAME,
    "description": "Asks the VaultNotes user whether Claude may use a tool (used by Claude Code itself).",
    "inputSchema": {
        "type": "object",
        "properties": {
            "tool_name": {"type": "string"},
            "input": {"type": "object"},
            "tool_use_id": {"type": "string"},
        },
        "required": ["tool_name", "input"],
    },
}


class Server:
    """A line-delimited JSON-RPC (MCP stdio) server with one tool.

    Each call is answered on its own thread, because Claude Code may ask about
    several tool calls at once.
    """

    def __init__(self, folder: Path, timeout: float, out: TextIO) -> None:
        self.folder = folder
        self.timeout = timeout
        self._out = out
        self._out_lock = threading.Lock()
        self._threads: list[threading.Thread] = []

    def send(self, obj: dict[str, Any]) -> None:
        with self._out_lock:
            try:
                self._out.write(json.dumps(obj) + "\n")
                self._out.flush()
            except (OSError, ValueError):
                pass  # Claude Code has gone

    def handle(self, msg: Any) -> None:
        if not isinstance(msg, dict):
            return
        method, mid = msg.get("method"), msg.get("id")
        if mid is None:
            return  # a notification ("initialized", "cancelled"): nothing to say
        if method == "initialize":
            params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
            version = params.get("protocolVersion") if isinstance(params.get("protocolVersion"), str) else "2024-11-05"
            self.send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": version,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": "1"},
            }})
        elif method == "tools/list":
            self.send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [TOOL_SCHEMA]}})
        elif method == "tools/call":
            thread = threading.Thread(target=self._call, args=(mid, msg.get("params")), daemon=True)
            self._threads.append(thread)
            thread.start()
        elif method == "ping":
            self.send({"jsonrpc": "2.0", "id": mid, "result": {}})
        else:
            self.send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "Method not found"}})

    def _call(self, mid: Any, params: Any) -> None:
        params = params if isinstance(params, dict) else {}
        args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        tool_input = args.get("input") if isinstance(args.get("input"), dict) else {}
        request = {
            "tool_name": args.get("tool_name") if isinstance(args.get("tool_name"), str) else "",
            "input": tool_input,
            "tool_use_id": args.get("tool_use_id") if isinstance(args.get("tool_use_id"), str) else None,
        }
        if params.get("name") != TOOL_NAME or not request["tool_name"]:
            allowed, message = False, DENIED_BROKEN
        else:
            try:
                allowed, message = ask(self.folder, request, self.timeout)
            except Exception:  # noqa: BLE001 - a broken question is a refusal, never an allow
                allowed, message = False, DENIED_BROKEN
        text = decision_text(allowed, message, tool_input)
        self.send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": text}]}})

    def serve(self, lines: Any) -> None:
        for line in lines:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            self.handle(msg)
        for thread in self._threads:
            thread.join(0.5)


def main(argv: list[str]) -> int:
    if not argv:
        return 2
    try:
        timeout = float(argv[1]) if len(argv) > 1 else DEFAULT_TIMEOUT
    except ValueError:
        timeout = DEFAULT_TIMEOUT
    Server(Path(argv[0]), timeout, sys.stdout).serve(sys.stdin)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
