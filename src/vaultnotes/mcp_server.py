"""VaultNotes as an MCP server for a Claude running in a Hyper-V VM.

Off by default.  When it is on, the app serves MCP's "Streamable HTTP"
transport (JSON-RPC 2.0 in a POST, a JSON reply) at ``http://<host>:<port>/mcp``
for a Claude Code inside a local VM (CachyOS on the Hyper-V Default Switch)::

    claude mcp add --transport http vaultnotes \\
        http://<this PC>.mshome.net:8765/mcp --header "Authorization: Bearer <token>"

What keeps it narrow:

* **Where it listens:** only the Hyper-V Default Switch adapter's own address,
  looked up every time it starts (the switch gets a new subnet after a reboot).
  Never ``0.0.0.0``, never a public address.  No switch, no server.
* **Who may call:** a bearer token the app makes (kept in
  ``%LOCALAPPDATA%\\VaultNotes``, not in settings.json), checked in constant
  time.  A request with an ``Origin`` header (a web page) is refused.
* **What it does:** the tools run :func:`vaultnotes.notes_cli.run`, so the rules
  are the ones ``notes.py`` already enforces: Plain and AI-Notes are read, only
  AI-Notes is written, and the Encrypted vault is refused before anything is
  opened.  Personal is not offered (it needs its key file).  Nothing is deleted.
* **PHI:** the server cannot know whether the Claude calling it runs on Vertex,
  so every reply goes through the PHI filter (:class:`vaultnotes.phi.redact.Redactor`)
  and only tokens leave this PC.  If the filter fails, the reply is withheld.

Requests and replies are never logged: a log line could quote a note.
"""

from __future__ import annotations

import hmac
import io
import ipaddress
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from vaultnotes import notes_cli

DEFAULT_PORT = 8765
SWITCH_ALIAS = "vEthernet (Default Switch)"
PATH = "/mcp"
#: A 5 MB note as JSON, with room to spare.
MAX_BODY = 12 * 1024 * 1024
#: Seconds a connection may sit idle.
SOCKET_TIMEOUT = 30
SERVER_NAME = "vaultnotes"
SERVER_VERSION = "1.0"
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
TOKEN_FILE = "mcp-token"

INSTRUCTIONS = (
    "VaultNotes, the user's notes app on the Windows host. Read the Plain and AI-Notes spaces; "
    "write only to AI-Notes. Note titles look like 'DATAINGEST-1234 — Short description'. "
    "PHI in replies is replaced with ⟦PHI:KIND:id⟧ tokens: never try to undo them, and never "
    "write PHI into a note."
)

WITHHELD = "The PHI filter failed, so this reply was withheld."

Redact = Callable[[str], str]


# ----------------------------------------------------------------------
# The tools: each one is a notes.py command line
# ----------------------------------------------------------------------
_SPACE = {"type": "string", "enum": ["plain", "ai"], "description": "plain (read-only) or ai (AI-Notes)"}
_TITLE = {"type": "string", "description": "the note's exact title"}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_notes",
        "description": "List note titles in a space (title, modified, id; important notes first).",
        "inputSchema": {"type": "object", "properties": {
            "space": _SPACE,
            "tags": {"type": "array", "items": {"type": "string"}, "description": "only notes with all these #tags"},
        }, "required": ["space"], "additionalProperties": False},
    },
    {
        "name": "list_tags",
        "description": "List the #tags used in a space, with note counts.",
        "inputSchema": {"type": "object", "properties": {"space": _SPACE},
                        "required": ["space"], "additionalProperties": False},
    },
    {
        "name": "read_note",
        "description": "Read one note: by id, exact title, or a unique part of a title.",
        "inputSchema": {"type": "object", "properties": {
            "space": _SPACE, "note": {"type": "string", "description": "id, title or unique part of a title"},
        }, "required": ["space", "note"], "additionalProperties": False},
    },
    {
        "name": "search_notes",
        "description": "Find notes whose title or text contains some text; shows matching lines.",
        "inputSchema": {"type": "object", "properties": {"space": _SPACE, "text": {"type": "string"}},
                        "required": ["space", "text"], "additionalProperties": False},
    },
    {
        "name": "write_note",
        "description": "Create a note in AI-Notes (replace: true overwrites one with the same title).",
        "inputSchema": {"type": "object", "properties": {
            "title": _TITLE, "text": {"type": "string", "description": "Markdown"},
            "replace": {"type": "boolean", "default": False},
        }, "required": ["title", "text"], "additionalProperties": False},
    },
    {
        "name": "append_note",
        "description": "Add text to the end of an AI-Notes note, creating it if needed.",
        "inputSchema": {"type": "object", "properties": {"title": _TITLE, "text": {"type": "string"}},
                        "required": ["title", "text"], "additionalProperties": False},
    },
    {
        "name": "tag_note",
        "description": "Add #tags to an AI-Notes note (remove: true takes them off).",
        "inputSchema": {"type": "object", "properties": {
            "title": _TITLE, "tags": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "remove": {"type": "boolean", "default": False},
        }, "required": ["title", "tags"], "additionalProperties": False},
    },
    {
        "name": "mark_note",
        "description": "Mark an AI-Notes note important, or clear the mark (important: false).",
        "inputSchema": {"type": "object", "properties": {
            "title": _TITLE, "important": {"type": "boolean", "default": True},
        }, "required": ["title"], "additionalProperties": False},
    },
]
TOOL_NAMES = {tool["name"] for tool in TOOLS}


class ToolInputError(ValueError):
    """Arguments that do not fit a tool's schema."""


def _str(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ToolInputError(f"{key} must be a non-empty string")
    return value


def _space(args: dict[str, Any]) -> str:
    space = _str(args, "space")
    if space not in ("plain", "ai"):
        raise ToolInputError("space must be plain or ai")
    return space


def _strings(args: dict[str, Any], key: str, required: bool) -> list[str]:
    value = args.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ToolInputError(f"{key} must be a list of strings")
    if required and not value:
        raise ToolInputError(f"{key} needs at least one item")
    return value


def _flag(args: dict[str, Any], key: str, default: bool) -> bool:
    value = args.get(key, default)
    if not isinstance(value, bool):
        raise ToolInputError(f"{key} must be true or false")
    return value


def tool_argv(name: str, args: Any) -> list[str]:
    """The notes.py command line for one tool call.

    Options use ``--opt=value`` and positionals follow ``--``, so text or a
    title starting with ``-`` is never read as an option.
    """
    if not isinstance(args, dict):
        raise ToolInputError("arguments must be an object")
    if name == "list_notes":
        return ["list", *[f"--tag={t}" for t in _strings(args, "tags", False)], "--", _space(args)]
    if name == "list_tags":
        return ["tags", "--", _space(args)]
    if name == "read_note":
        return ["read", "--", _space(args), _str(args, "note")]
    if name == "search_notes":
        return ["search", "--", _space(args), _str(args, "text")]
    if name == "write_note":
        replace = ["--replace"] if _flag(args, "replace", False) else []
        return ["write", f"--text={_str(args, 'text')}", *replace, "--", "ai", _str(args, "title")]
    if name == "append_note":
        return ["append", f"--text={_str(args, 'text')}", "--", "ai", _str(args, "title")]
    if name == "tag_note":
        remove = ["--remove"] if _flag(args, "remove", False) else []
        return ["tag", *remove, "--", "ai", _str(args, "title"), *_strings(args, "tags", True)]
    if name == "mark_note":
        clear = [] if _flag(args, "important", True) else ["--clear"]
        return ["mark", *clear, "--", "ai", _str(args, "title")]
    raise ToolInputError(f"unknown tool {name!r}")


def run_tool(name: str, args: Any, notes_root: Path, redact: Redact) -> tuple[str, bool]:
    """``(text, is_error)`` for one tool call, already through the PHI filter."""
    try:
        argv = ["--root", str(notes_root), *tool_argv(name, args)]
    except ToolInputError as exc:
        return f"Bad arguments: {exc}.", True
    out, err = io.StringIO(), io.StringIO()
    try:
        code = notes_cli.run(argv, out=out, err=err)
    except SystemExit:  # argparse refused the command line
        code, err = notes_cli.EXIT_ERROR, io.StringIO("Bad arguments.")
    text = out.getvalue() if code == 0 else err.getvalue()
    text = text.strip() or ("Done." if code == 0 else "That did not work.")
    try:
        text = redact(text)
    except Exception:  # noqa: BLE001 - fail closed: nothing unfiltered leaves this PC
        return WITHHELD, True
    return text, code != 0


def phi_redact(text: str) -> str:
    """The PHI filter Claude Code's hook uses, with the same token vault."""
    from vaultnotes.phi.redact import Redactor
    from vaultnotes.phi.vault import Vault

    vault = Vault()
    try:
        return Redactor(vault).redact(text)
    finally:
        vault.close()


# ----------------------------------------------------------------------
# JSON-RPC
# ----------------------------------------------------------------------
def _rpc_error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _rpc_result(msg_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def handle_message(message: Any, notes_root: Path, redact: Redact) -> dict[str, Any] | None:
    """The reply to one JSON-RPC message, or None for a notification."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _rpc_error(None, -32600, "Invalid request")
    method, msg_id = message.get("method"), message.get("id")
    if not isinstance(method, str):
        # A client's reply to a server request (we send none): nothing to say.
        return None
    if "id" not in message:
        return None  # notifications/initialized and friends
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    if method == "initialize":
        asked = params.get("protocolVersion")
        version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return _rpc_result(msg_id, {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": INSTRUCTIONS,
        })
    if method == "ping":
        return _rpc_result(msg_id, {})
    if method == "tools/list":
        return _rpc_result(msg_id, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        if name not in TOOL_NAMES:
            return _rpc_error(msg_id, -32602, f"Unknown tool: {str(name)[:60]}")
        text, failed = run_tool(name, params.get("arguments") or {}, notes_root, redact)
        return _rpc_result(msg_id, {"content": [{"type": "text", "text": text}], "isError": failed})
    return _rpc_error(msg_id, -32601, f"Method not found: {method[:60]}")


# ----------------------------------------------------------------------
# Where to listen, and the token
# ----------------------------------------------------------------------
def _private_v4(text: str) -> str | None:
    try:
        ip = ipaddress.ip_address(text.strip())
    except ValueError:
        return None
    if ip.version != 4 or not ip.is_private or ip.is_loopback or ip.is_unspecified or ip.is_link_local:
        return None
    return str(ip)


def find_switch_address() -> str | None:
    """The Default Switch adapter's IPv4 address on this PC, or None.

    Asks Windows for the adapter itself; its ``<pc>.mshome.net`` name (which
    Hyper-V keeps in hosts.ics) is the fallback.  Only a private address counts.
    """
    if sys.platform == "win32":
        try:
            out = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                 f"(Get-NetIPAddress -InterfaceAlias '{SWITCH_ALIAS}' -AddressFamily IPv4 "
                 "-ErrorAction Stop).IPAddress"],
                capture_output=True, text=True, timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            for line in out.stdout.splitlines():
                found = _private_v4(line)
                if found:
                    return found
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        return _private_v4(socket.gethostbyname(f"{socket.gethostname()}.mshome.net"))
    except OSError:
        return None


def host_name() -> str:
    """What the VM calls this PC."""
    return f"{socket.gethostname()}.mshome.net"


def load_token(folder: Path, renew: bool = False) -> str:
    """The bearer token, made on first use (or when ``renew``)."""
    path = folder / TOKEN_FILE
    if not renew:
        try:
            token = path.read_text(encoding="ascii").strip()
            if len(token) >= 32:
                return token
        except (OSError, UnicodeDecodeError):
            pass
    folder.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(token, encoding="ascii")
    os.replace(tmp, path)
    return token


# ----------------------------------------------------------------------
# The HTTP server
# ----------------------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    server: "_Server"
    protocol_version = "HTTP/1.1"
    timeout = SOCKET_TIMEOUT
    server_version = "VaultNotes-MCP"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return  # never log: a request line or body could quote a note

    def _send(self, status: int, body: dict[str, Any] | None = None, extra: dict[str, str] | None = None) -> None:
        data = json.dumps(body).encode("utf-8") if body is not None else b""
        self.send_response(status)
        if body is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _allowed(self) -> bool:
        """Path, origin and token; replies with the refusal itself when not allowed."""
        if self.path.split("?", 1)[0] != PATH:
            self._send(404, {"error": "not found"})
            return False
        if self.headers.get("Origin"):
            self._send(403, {"error": "browser requests are refused"})
            return False
        given = self.headers.get("Authorization", "")
        expected = f"Bearer {self.server.token}"
        if not hmac.compare_digest(given.encode("utf-8", "replace"), expected.encode("utf-8")):
            self._send(401, {"error": "unauthorized"}, {"WWW-Authenticate": 'Bearer realm="vaultnotes"'})
            return False
        return True

    def do_POST(self) -> None:  # noqa: N802
        if not self._allowed():
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._send(411, {"error": "Content-Length needed"})
            return
        if length < 0 or length > MAX_BODY:
            self._send(413, {"error": "too large"})
            self.close_connection = True
            return
        try:
            message = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send(400, _rpc_error(None, -32700, "Parse error"))
            return
        if isinstance(message, list):
            self._send(400, _rpc_error(None, -32600, "Batches are not supported"))
            return
        reply = handle_message(message, self.server.notes_root(), self.server.redact)
        if reply is None:
            self._send(202)
        else:
            self._send(200, reply)

    def do_GET(self) -> None:  # noqa: N802
        if self._allowed():
            self._send(405, {"error": "no event stream; POST JSON-RPC to /mcp"}, {"Allow": "POST"})

    def do_DELETE(self) -> None:  # noqa: N802
        if self._allowed():
            self._send(405, {"error": "no sessions"}, {"Allow": "POST"})


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False  # Windows lets a second process share the port otherwise

    def __init__(self, address: tuple[str, int], token: str, notes_root: Callable[[], Path], redact: Redact) -> None:
        self.token = token
        self.notes_root = notes_root
        self.redact = redact
        super().__init__(address, _Handler)


class McpServer:
    """Starts and stops the server; :meth:`status` is what Settings shows."""

    def __init__(
        self,
        notes_root: Callable[[], Path],
        token_dir: Path,
        *,
        find_host: Callable[[], str | None] = find_switch_address,
        redact: Redact = phi_redact,
        port: int = DEFAULT_PORT,
    ) -> None:
        self._notes_root = notes_root
        self._token_dir = token_dir
        self._find_host = find_host
        self._redact = redact
        self.port = port
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.host: str | None = None
        self.error: str | None = None

    @property
    def running(self) -> bool:
        return self._server is not None

    def start(self) -> None:
        """Listen on the Default Switch; on failure ``error`` says why."""
        with self._lock:
            if self._server is not None:
                return
            self.error = None
            host = self._find_host()
            if not host:
                self.host = None
                self.error = ("The Hyper-V Default Switch was not found, so there is nowhere safe to listen. "
                              "Start the VM, then turn this on again.")
                return
            try:
                server = _Server((host, self.port), load_token(self._token_dir), self._notes_root, self._redact)
            except OSError as exc:
                self.host = host
                self.error = f"Port {self.port} on {host} could not be opened ({exc.strerror or type(exc).__name__})."
                return
            self.host, self._server = host, server
            self._thread = threading.Thread(target=server.serve_forever, name="VaultNotes-mcp", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            server, self._server = self._server, None
            thread, self._thread = self._thread, None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(5)

    def renew_token(self) -> None:
        """A new token; a running server switches to it at once."""
        token = load_token(self._token_dir, renew=True)
        with self._lock:
            if self._server is not None:
                self._server.token = token

    def status(self) -> dict[str, Any]:
        token = load_token(self._token_dir) if self.running else None
        url = f"http://{host_name()}:{self.port}{PATH}"
        return {
            "running": self.running,
            "host": self.host,
            "port": self.port,
            "url": url,
            # Not "error": in a bridge reply that key means the call itself failed.
            "problem": self.error,
            "command": (
                f'claude mcp add --transport http vaultnotes {url} --header "Authorization: Bearer {token}"'
                if token else None
            ),
            "firewall": firewall_command(self.port),
        }


def firewall_command(port: int) -> str:
    """The one inbound rule Windows needs, for an admin PowerShell (only the Default Switch)."""
    return (
        f"New-NetFirewallRule -DisplayName 'VaultNotes MCP (Hyper-V VM)' -Direction Inbound "
        f"-Protocol TCP -LocalPort {port} -InterfaceAlias '{SWITCH_ALIAS}' -Action Allow"
    )
