"""Messages typed while Claude is replying, handed over at its next step.

The app writes each queued message as one file in a folder of its own per chat
(``<id>.txt``).  Claude Code runs this file as a PostToolUse hook (registered by
:func:`vaultnotes.claude_chat.build_args` through ``--settings``), so after every
tool call it takes whatever is waiting and gives it to Claude as extra context,
the way the CLI hands over a message typed mid-turn.  What is still waiting when
the reply ends is sent by the app as the next message.

Taking a message is a rename (``.txt`` to ``.taken``), so a message is delivered
once: either the hook takes it, or the app does (to send it or because the user
removed it), never both.

Standard library only: the hook runs as ``python -I claude_queue.py <folder>``.
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

#: ``<nanoseconds>-<random>``: sorts in the order the messages were typed.
QUEUE_ID_RE = re.compile(r"^[0-9]{1,24}-[0-9a-f]{8}$")
#: Most messages waiting in one chat.
MAX_QUEUED = 20

HEADER = (
    "The user typed the following message in the VaultNotes chat while you were working. "
    "It was queued and is handed to you now. Read it and take it into account from here on "
    "(it may change or add to what you are doing):"
)


_last_stamp = 0
_stamp_lock = threading.Lock()


def _stamp() -> int:
    """Nanoseconds, always larger than the last one: Windows' clock can repeat a value."""
    global _last_stamp
    with _stamp_lock:
        _last_stamp = max(time.time_ns(), _last_stamp + 1)
        return _last_stamp


def enqueue(folder: Path, text: str) -> str:
    """Write one waiting message and return its id."""
    folder.mkdir(parents=True, exist_ok=True)
    if len(waiting(folder)) >= MAX_QUEUED:
        raise OverflowError("too many queued messages")
    queue_id = f"{_stamp()}-{secrets.token_hex(4)}"
    tmp = folder / f"{queue_id}.tmp"
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, folder / f"{queue_id}.txt")
    return queue_id


def waiting(folder: Path) -> set[str]:
    """Ids of the messages nobody has taken yet."""
    try:
        return {p.stem for p in folder.glob("*.txt") if QUEUE_ID_RE.match(p.stem)}
    except OSError:
        return set()


def _claim(folder: Path, queue_id: str) -> str | None:
    """Take one message: its text, or None when someone else took it first."""
    src, taken = folder / f"{queue_id}.txt", folder / f"{queue_id}.taken"
    try:
        os.replace(src, taken)  # fails if it was already taken
    except OSError:
        return None
    try:
        return taken.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    finally:
        try:
            taken.unlink()
        except OSError:
            pass


def take(folder: Path, queue_id: str) -> str | None:
    """Take one message by id (None when it was already delivered)."""
    if not isinstance(queue_id, str) or not QUEUE_ID_RE.match(queue_id):
        return None
    return _claim(folder, queue_id)


def take_all(folder: Path) -> list[tuple[str, str]]:
    """Take every waiting message, oldest first, as ``(id, text)``."""
    out = []
    for queue_id in sorted(waiting(folder), key=lambda q: int(q.split("-")[0])):
        text = _claim(folder, queue_id)
        if text is not None and text.strip():
            out.append((queue_id, text))
    return out


def clear(folder: Path) -> None:
    """Drop everything waiting, and the folder."""
    take_all(folder)
    try:
        for leftover in folder.iterdir():
            leftover.unlink()
        folder.rmdir()
    except OSError:
        pass


def hook_output(messages: list[tuple[str, str]]) -> str | None:
    """The PostToolUse hook's JSON for these messages, or None for no output."""
    if not messages:
        return None
    body = "\n\n".join(text.strip() for _, text in messages)
    return json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": f"{HEADER}\n\n{body}",
        }
    })


def main(argv: list[str]) -> int:
    """The hook: never fails the tool call, whatever happens."""
    try:
        sys.stdin.read()  # the tool call itself; not needed
    except Exception:  # noqa: BLE001
        pass
    try:
        if len(argv) == 1:
            out = hook_output(take_all(Path(argv[0])))
            if out:
                sys.stdout.write(out)
    except Exception:  # noqa: BLE001 - a queued message is never worth a failed tool call
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
