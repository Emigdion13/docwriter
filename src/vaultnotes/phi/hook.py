"""Claude Code hook: tokenise PHI in tool output, and gate reveals.

Registered in ``~/.claude/settings.json`` for two events, same command::

    python phi.py hook

* **PostToolUse** (Bash, Read, Grep, WebFetch, Agent, MCP tools): every string in
  the tool's output goes through :class:`Redactor`; when anything changed, the
  hook returns ``updatedToolOutput`` so Claude only ever sees tokens.  The one
  exception is a bare ``phi.py reveal ...`` command in a verified Vertex
  session, whose output is the point.
* **PreToolUse** (Bash): a ``phi.py reveal`` command is denied outside a
  verified Vertex session, and otherwise asks Juan to confirm each reveal.

If the filter itself fails, it fails closed: the output is withheld (shape
kept) and the error goes to ``hook-errors.log``, without any of the text.
"""

from __future__ import annotations

import os
import re
import sys
import traceback
from datetime import datetime, timezone
from typing import Any

VERTEX_PROJECT = "dev-code-assist-464720"

#: ``[cd <dir> &&] [python] <path>phi.py reveal <tokens...>`` and nothing else:
#: no pipes, redirects, ``;`` or substitutions that could smuggle other output
#: past the filter.
_SAFE = r"[^\s;&|`$<>()\"'\\]"
_REVEAL_RE = re.compile(
    rf"^\s*(?:cd\s+{_SAFE}+\s*&&\s*)?"
    rf"(?:{_SAFE}*python(?:3)?(?:\.exe)?\s+)?"
    rf"{_SAFE}*phi\.py\s+(?:--{_SAFE}+\s+)*reveal(?:\s+{_SAFE}+)+\s*$"
)

#: Keys whose string values are part of the output's shape, not its content.
_SHAPE_KEYS = {"type", "filePath", "file_path", "path", "media_type", "mimeType", "encoding"}
WITHHELD = "[PHI filter failed: output withheld. See %LOCALAPPDATA%\\VaultNotes\\phi\\hook-errors.log]"


def is_vertex() -> bool:
    return (os.environ.get("CLAUDE_CODE_USE_VERTEX") == "1"
            and os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID") == VERTEX_PROJECT)


def is_reveal_command(command: object) -> bool:
    return isinstance(command, str) and bool(_REVEAL_RE.match(command))


def _withhold(node: Any, key: str | None = None) -> Any:
    if isinstance(node, str):
        return node if key in _SHAPE_KEYS else WITHHELD
    if isinstance(node, dict):
        return {k: _withhold(v, k) for k, v in node.items()}
    if isinstance(node, list):
        return [_withhold(v, key) for v in node]
    return node


def _log_error(home) -> None:
    try:
        home.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        # Only the stack, never the exception message: it could quote the text.
        frames = "".join(traceback.format_tb(sys.exc_info()[2]))
        exc_type = type(sys.exc_info()[1]).__name__
        with open(home / "hook-errors.log", "a", encoding="utf-8") as f:
            f.write(f"{stamp} {exc_type}\n{frames}\n")
    except Exception:
        pass


def pre_tool_use(event: dict[str, Any]) -> dict[str, Any] | None:
    if event.get("tool_name") != "Bash":
        return None
    command = (event.get("tool_input") or {}).get("command")
    if not isinstance(command, str) or "phi.py" not in command or "reveal" not in command:
        return None
    if not is_reveal_command(command):
        decision, reason = "deny", ("phi.py reveal must run on its own: no pipes, redirects, "
                                    "';' or other commands in the same call.")
    elif not is_vertex():
        decision, reason = "deny", ("PHI can only be revealed in a verified Vertex session "
                                    f"(CLAUDE_CODE_USE_VERTEX=1, project {VERTEX_PROJECT}).")
    else:
        decision, reason = "ask", "Reveal PHI to Claude? The decrypted values will be in this conversation."
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "permissionDecision": decision,
                                   "permissionDecisionReason": reason}}


def post_tool_use(event: dict[str, Any]) -> dict[str, Any] | None:
    from vaultnotes.phi.redact import Redactor, redact_tree
    from vaultnotes.phi.vault import Vault

    tool_input = event.get("tool_input") or {}
    if event.get("tool_name") == "Bash" and is_reveal_command(tool_input.get("command")) and is_vertex():
        return None

    response = event.get("tool_response")
    if response is None:
        return None
    vault = Vault()
    try:
        redactor = Redactor(vault)
        updated = redact_tree(response, redactor)
        if not redactor.replaced:
            return None
        note = (f"PHI filter: {redactor.replaced} value(s) in this output were replaced with "
                "⟦PHI:KIND:id⟧ tokens. Same value, same token. Reveal only when Juan asks.")
    except Exception:
        _log_error(vault.home)
        updated, note = _withhold(response), "PHI filter failed, so this output was withheld."
    finally:
        vault.close()
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                   "updatedToolOutput": updated,
                                   "additionalContext": note}}


def handle(event: dict[str, Any]) -> dict[str, Any] | None:
    name = event.get("hook_event_name")
    if name == "PreToolUse":
        return pre_tool_use(event)
    if name == "PostToolUse":
        return post_tool_use(event)
    return None
