"""The permission-prompt tool: questions written to a folder, answered by the app.

No Claude Code here: the tests play both sides, and one test runs the tool as a
real ``python -I claude_permit.py`` process speaking MCP over stdio.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from vaultnotes import claude_permit
from vaultnotes.claude_permit import (
    DENIED_BROKEN,
    DENIED_GONE,
    DENIED_TIMEOUT,
    PROMPT_TOOL,
    Server,
    answer,
    ask,
    clear,
    decision_text,
    pending,
)

REQUEST = {"tool_name": "Write", "input": {"file_path": "C:/n/a.md", "content": "hi"}, "tool_use_id": "toolu_1"}


def answer_when_asked(folder: Path, allow: bool, message: str = "", seconds: float = 3.0) -> threading.Thread:
    """Play the app: wait for one question, then answer it."""
    def run() -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            asks = pending(folder)
            if asks:
                answer(folder, asks[0][0], allow, message)
                return
            time.sleep(0.01)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


# ----------------------------------------------------------------------
# The folder both sides share
# ----------------------------------------------------------------------
def test_a_question_waits_until_it_is_answered(tmp_path: Path) -> None:
    answer_when_asked(tmp_path, True)
    assert ask(tmp_path, REQUEST, 3) == (True, "")
    assert list(tmp_path.iterdir()) == []  # both files are gone afterwards


def test_a_refusal_carries_its_message(tmp_path: Path) -> None:
    answer_when_asked(tmp_path, False, "Not that file.")
    assert ask(tmp_path, REQUEST, 3) == (False, "Not that file.")


def test_a_refusal_without_a_message_still_says_no(tmp_path: Path) -> None:
    answer_when_asked(tmp_path, False)
    allowed, message = ask(tmp_path, REQUEST, 3)
    assert allowed is False and message


def test_nobody_answering_is_a_refusal(tmp_path: Path) -> None:
    now = [0.0]
    allowed, message = ask(tmp_path, REQUEST, 5, clock=lambda: now[0],
                           sleep=lambda s: now.__setitem__(0, now[0] + 1))
    assert (allowed, message) == (False, DENIED_TIMEOUT)
    assert list(tmp_path.iterdir()) == []


def test_the_folder_going_away_is_a_refusal(tmp_path: Path) -> None:
    folder = tmp_path / "chat"
    folder.mkdir()

    def stop_the_reply() -> None:
        while not pending(folder):
            time.sleep(0.01)
        clear(folder)
    threading.Thread(target=stop_the_reply, daemon=True).start()
    assert ask(folder, REQUEST, 3) == (False, DENIED_GONE)


def test_no_folder_at_all_is_a_refusal(tmp_path: Path) -> None:
    assert ask(tmp_path / "missing", REQUEST, 3) == (False, DENIED_BROKEN)


def test_pending_reads_only_real_questions_in_order(tmp_path: Path) -> None:
    (tmp_path / "junk.ask").write_text("{}", encoding="utf-8")
    (tmp_path / "1-0000000a.ask").write_text("not json", encoding="utf-8")
    (tmp_path / "2-0000000b.ask").write_text(json.dumps({"input": {}}), encoding="utf-8")  # no tool name
    (tmp_path / "4-0000000d.ask").write_text(json.dumps({"tool_name": "Edit", "input": "x"}), encoding="utf-8")
    (tmp_path / "3-0000000c.ask").write_text(json.dumps(REQUEST), encoding="utf-8")
    (tmp_path / "5-0000000e.ask").write_text(json.dumps(REQUEST), encoding="utf-8")
    (tmp_path / "5-0000000e.answer").write_text("{}", encoding="utf-8")  # already answered
    asks = pending(tmp_path)
    assert [a for a, _ in asks] == ["3-0000000c", "4-0000000d"]
    assert asks[1][1] == {"tool_name": "Edit", "input": {}, "tool_use_id": None}


def test_only_a_waiting_question_can_be_answered(tmp_path: Path) -> None:
    assert answer(tmp_path, "1-0000000a", True) is False
    assert answer(tmp_path, "../../evil", True) is False
    (tmp_path / "1-0000000a.ask").write_text(json.dumps(REQUEST), encoding="utf-8")
    assert answer(tmp_path, "1-0000000a", True) is True
    assert json.loads((tmp_path / "1-0000000a.answer").read_text(encoding="utf-8")) == {"allow": True, "message": ""}


def test_decision_text_is_what_claude_code_expects() -> None:
    assert json.loads(decision_text(True, "", {"a": 1})) == {"behavior": "allow", "updatedInput": {"a": 1}}
    assert json.loads(decision_text(False, "no", {"a": 1})) == {"behavior": "deny", "message": "no"}


# ----------------------------------------------------------------------
# The MCP server
# ----------------------------------------------------------------------
def rpc(mid: Any, method: str, params: Any = None) -> str:
    msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if mid is not None:
        msg["id"] = mid
    if params is not None:
        msg["params"] = params
    return json.dumps(msg)


def call(mid: int, tool_input: dict[str, Any], name: str = claude_permit.TOOL_NAME) -> str:
    return rpc(mid, "tools/call", {"name": name, "arguments": {
        "tool_name": "Write", "input": tool_input, "tool_use_id": "toolu_1"}})


def replies(out: io.StringIO) -> dict[Any, dict[str, Any]]:
    return {m["id"]: m for m in map(json.loads, out.getvalue().splitlines())}


def test_the_server_introduces_its_one_tool(tmp_path: Path) -> None:
    out = io.StringIO()
    Server(tmp_path, 3, out).serve([
        rpc(1, "initialize", {"protocolVersion": "2025-06-18"}),
        rpc(None, "notifications/initialized"),
        rpc(2, "tools/list"),
        rpc(3, "ping"),
        rpc(4, "resources/list"),
        "not json",
    ])
    got = replies(out)
    assert got[1]["result"]["protocolVersion"] == "2025-06-18"
    assert got[1]["result"]["serverInfo"]["name"] == claude_permit.SERVER_NAME
    assert [t["name"] for t in got[2]["result"]["tools"]] == [claude_permit.TOOL_NAME]
    assert got[3]["result"] == {}
    assert got[4]["error"]["code"] == -32601
    assert PROMPT_TOOL == "mcp__vaultnotes_permit__ask_user"


def test_a_call_waits_for_the_app_and_returns_its_decision(tmp_path: Path) -> None:
    out = io.StringIO()
    answer_when_asked(tmp_path, True)
    Server(tmp_path, 3, out).serve([call(7, {"file_path": "C:/n/a.md"})])
    text = replies(out)[7]["result"]["content"][0]["text"]
    assert json.loads(text) == {"behavior": "allow", "updatedInput": {"file_path": "C:/n/a.md"}}


def test_a_call_for_another_tool_is_refused_without_asking(tmp_path: Path) -> None:
    out = io.StringIO()
    Server(tmp_path, 3, out).serve([call(8, {}, name="something_else")])
    assert json.loads(replies(out)[8]["result"]["content"][0]["text"]) == {"behavior": "deny", "message": DENIED_BROKEN}
    assert pending(tmp_path) == []


def test_the_real_tool_process_speaks_mcp(tmp_path: Path) -> None:
    proc = subprocess.Popen(
        [sys.executable, "-I", claude_permit.__file__, str(tmp_path), "5"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8",
    )
    try:
        proc.stdin.write(rpc(1, "initialize", {"protocolVersion": "2024-11-05"}) + "\n")
        proc.stdin.flush()
        assert json.loads(proc.stdout.readline())["id"] == 1
        answer_when_asked(tmp_path, False, "Not now.", seconds=5)
        proc.stdin.write(call(2, {"file_path": "C:/n/a.md"}) + "\n")
        proc.stdin.flush()
        got = json.loads(proc.stdout.readline())
        assert got["id"] == 2
        assert json.loads(got["result"]["content"][0]["text"]) == {"behavior": "deny", "message": "Not now."}
    finally:
        proc.stdin.close()
        proc.wait(5)
