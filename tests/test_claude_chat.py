"""The Claude space: chats with Claude Code that is off until the user allows it.

A fake ``claude`` process stands in for the real program, so these tests never
start Claude Code or touch the network.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from vaultnotes import claude_permit, claude_queue
from vaultnotes.api import Api
from vaultnotes.backup.gdrive_auth import TokenStore
from vaultnotes.claude_chat import (
    MAX_CHATS_KEPT,
    MAX_PROMPT_LENGTH,
    ClaudeError,
    ClaudeManager,
    Translator,
    allow_rule,
    always_rule,
    build_args,
    build_terminal_args,
    clean_title,
    deny_rules,
    history_path,
    names_folder,
    read_history,
    summarize_tool,
    terminal_command,
)
from vaultnotes.config import Config
from vaultnotes.storage.ai_store import UnavailableAIStore

SESSION = "46733bfa-5ee0-455a-b5eb-e27c48027277"
SESSION_2 = "11111111-2222-3333-4444-555555555555"


# ----------------------------------------------------------------------
# A fake claude process and a way to wait for its reply
# ----------------------------------------------------------------------
def line(obj: dict[str, Any]) -> bytes:
    return json.dumps(obj).encode() + b"\n"


def delta(text: str, message_id: str = "msg_1") -> bytes:
    return line({"type": "stream_event", "session_id": SESSION,
                 "event": {"type": "content_block_delta", "index": 0,
                           "delta": {"type": "text_delta", "text": text}}})


def message_start(message_id: str = "msg_1") -> bytes:
    return line({"type": "stream_event", "session_id": SESSION,
                 "event": {"type": "message_start", "message": {"id": message_id}}})


def assistant(blocks: list[dict[str, Any]], message_id: str = "msg_1") -> bytes:
    return line({"type": "assistant", "session_id": SESSION,
                 "message": {"id": message_id, "role": "assistant", "content": blocks}})


def result(**extra: Any) -> bytes:
    return line({"type": "result", "subtype": "success", "is_error": False, "session_id": SESSION,
                 "total_cost_usd": 0.01, "duration_ms": 50, "permission_denials": [], **extra})


class FakeStdin:
    def __init__(self) -> None:
        self.data = b""
        self.closed = False

    def write(self, data: bytes) -> None:
        self.data += data

    def close(self) -> None:
        self.closed = True


class FakeProcess:
    """Prints ``lines`` and ends; with ``hold`` it waits until it is killed."""

    def __init__(self, lines: list[bytes], *, code: int = 0, stderr: bytes = b"", hold: bool = False) -> None:
        self.stdin = FakeStdin()
        self.pid = 4242
        self._lines = lines
        self._code = code
        self.stderr = iter([stderr] if stderr else [])
        self._killed = threading.Event()
        self._hold = hold
        self.killed = False
        self.stdout = self._out()

    def _out(self):
        yield from self._lines
        if self._hold:
            self._killed.wait(5)

    def wait(self) -> int:
        return self._code

    def kill(self) -> None:
        self.killed = True
        self._killed.set()


class Harness:
    """A manager with a fake spawn; ``replies`` is what each next process prints."""

    def __init__(self, tmp_path: Path, replies: list[FakeProcess]) -> None:
        self.events: list[dict[str, Any]] = []
        self.spawned: list[tuple[list[str], str, FakeProcess]] = []
        self.changes = 0
        self._replies = list(replies)
        self._done = threading.Event()
        self.root = tmp_path / "notes"
        self.root.mkdir(exist_ok=True)
        self.encrypted = self.root / "vaults" / "encrypted"
        self.queue_root = tmp_path / "queue"
        self.permit_root = tmp_path / "permit"
        self.manager = ClaudeManager(
            self._emit, self._changed, spawn=self._spawn, find=lambda: "claude", kill=lambda p: p.kill(),
            queue_root=self.queue_root, permit_root=self.permit_root,
        )

    def _emit(self, name: str, data: dict[str, Any]) -> None:
        assert name == "claude_event"
        self.events.append(data)
        if data["kind"] == "done":
            self._done.set()

    def _changed(self) -> None:
        self.changes += 1

    def _spawn(self, argv: list[str], cwd: str, env: dict[str, str]) -> FakeProcess:
        proc = self._replies.pop(0)
        self.spawned.append((argv, cwd, proc))
        return proc

    def send(self, chat_id: Any, text: Any, wait: bool = True) -> dict[str, Any]:
        self._done.clear()
        self.events.clear()
        res = self.manager.send(chat_id, text, cwd=self.root, encrypted_dir=self.encrypted, notes_root=self.root)
        if wait:
            self.wait()
        return res

    def wait(self) -> None:
        assert self._done.wait(5), "the reply never finished"
        # the sender thread emits "done" then saves; give it a moment to settle
        for _ in range(100):
            if all(not c["running"] for c in self.manager.chats()):
                break
            threading.Event().wait(0.01)

    def kinds(self) -> list[str]:
        return [e["kind"] for e in self.events]


def reply(*lines: bytes, **kw: Any) -> FakeProcess:
    return FakeProcess(list(lines), **kw)


def eventually(check: Any, seconds: float = 3.0) -> bool:
    """Whether ``check()`` turns true soon: saving happens just after the page hears "done"."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.01)
    return bool(check())


# ----------------------------------------------------------------------
# What the page is shown
# ----------------------------------------------------------------------
def test_streamed_text_is_not_repeated_by_the_final_message() -> None:
    tr = Translator()
    out = tr.feed(json.loads(message_start()))
    out += tr.feed(json.loads(delta("Hi ")))
    out += tr.feed(json.loads(delta("there")))
    out += tr.feed(json.loads(assistant([{"type": "text", "text": "Hi there"}])))
    assert [e["text"] for e in out] == ["Hi ", "there"]
    assert tr.session_id == SESSION


def test_text_without_partial_output_comes_from_the_final_message() -> None:
    tr = Translator()
    out = tr.feed(json.loads(assistant([{"type": "text", "text": "All at once"}], "msg_9")))
    assert out == [{"kind": "text", "text": "All at once"}]


def test_tool_calls_and_their_results() -> None:
    tr = Translator()
    out = tr.feed(json.loads(assistant([
        {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "C:/n/a.md"}},
    ])))
    assert out == [{"kind": "tool", "id": "t1", "name": "Read", "summary": "C:/n/a.md"}]
    # The same block repeated is shown once.
    assert tr.feed(json.loads(assistant([{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]))) == []
    res = tr.feed({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "x" * 5000, "is_error": True},
    ]}})
    assert res[0]["ok"] is False and res[0]["id"] == "t1"
    assert len(res[0]["preview"]) < 900


def test_a_sub_agents_chatter_is_ignored() -> None:
    tr = Translator()
    obj = json.loads(assistant([{"type": "text", "text": "inner"}]))
    obj["parent_tool_use_id"] = "toolu_x"
    assert tr.feed(obj) == []


def test_junk_lines_are_ignored() -> None:
    tr = Translator()
    for junk in (None, 5, "x", [], {}, {"type": "stream_event", "event": 3}, {"type": "assistant", "message": 4},
                 {"type": "user", "message": {"content": "text"}}):
        assert tr.feed(junk) == []


def test_done_carries_denials_and_errors() -> None:
    tr = Translator()
    (done,) = tr.feed(json.loads(result(permission_denials=[
        {"tool_name": "Write", "tool_use_id": "t9", "tool_input": {"file_path": "C:/n/x.md"}},
        {"tool_name": "Bad"},
    ])))
    assert done["ok"] is True and [d["tool_use_id"] for d in done["denials"]] == ["t9"]
    tr = Translator()
    (failed,) = tr.feed({"type": "result", "subtype": "error_max_turns", "is_error": True, "result": "Too long"})
    assert failed["ok"] is False and failed["error"] == "Too long"


def test_summaries_are_one_short_line() -> None:
    assert summarize_tool("PowerShell", {"command": "git\n  status"}) == "git status"
    assert summarize_tool("Grep", {"pattern": "todo", "path": "plain"}) == "todo in plain"
    assert summarize_tool("Mystery", {"description": "does a thing"}) == "does a thing"
    assert len(summarize_tool("Read", {"file_path": "x" * 1000})) <= 300
    assert summarize_tool("Read", "not a dict") == ""


# ----------------------------------------------------------------------
# The command line is built in Python only
# ----------------------------------------------------------------------
def test_build_args() -> None:
    argv = build_args("claude", session_id=SESSION, allowed={"Edit(C:/a b.md)", "Read"},
                      denied=["Read(C:/v/**)"], prompt="be careful")
    assert argv[:2] == ["claude", "-p"]
    assert argv[argv.index("--resume") + 1] == SESSION
    assert argv[argv.index("--permission-mode") + 1] == "default"
    allowed_at = argv.index("--allowedTools")
    assert argv[allowed_at + 1:allowed_at + 3] == ["Edit(C:/a b.md)", "Read"]
    assert argv[argv.index("--disallowedTools") + 1] == "Read(C:/v/**)"
    bare = build_args("claude", session_id=None, allowed=[], denied=[], prompt="p")
    assert "--resume" not in bare and "--allowedTools" not in bare and "--disallowedTools" not in bare


def test_the_encrypted_vault_is_denied_to_reads_and_edits(tmp_path: Path) -> None:
    rules = deny_rules(tmp_path / "vaults" / "encrypted")
    posix = (tmp_path / "vaults" / "encrypted").as_posix()
    assert rules == [f"Read({posix}/**)", f"Edit({posix}/**)"]


# ----------------------------------------------------------------------
# Allow rules come from a reported denial, never from the page
# ----------------------------------------------------------------------
ENC = Path("C:/notes/vaults/encrypted")


def test_allow_rule_for_a_file_write() -> None:
    denial = {"tool_name": "Write", "tool_input": {"file_path": r"C:\notes\ai-notes\x.md"}}
    assert allow_rule(denial, "exact", ENC) == "Edit(C:/notes/ai-notes/x.md)"
    assert allow_rule(denial, "tool", ENC) == "Write"


def test_allow_rule_never_covers_the_encrypted_vault() -> None:
    denial = {"tool_name": "Edit", "tool_input": {"file_path": "C:/notes/vaults/Encrypted/n.vnote"}}
    assert allow_rule(denial, "exact", ENC) is None


def test_allow_rule_for_a_command() -> None:
    denial = {"tool_name": "PowerShell", "tool_input": {"command": "python notes.py list ai"}}
    assert allow_rule(denial, "exact", ENC) == "PowerShell(python notes.py list ai)"
    assert allow_rule(denial, "tool", ENC) == "PowerShell"


@pytest.mark.parametrize("command", ["Write-Host (1+1)", "ls *", "a\nb", "git status:", ""])
def test_a_command_a_rule_cannot_spell_gets_no_exact_rule(command: str) -> None:
    denial = {"tool_name": "PowerShell", "tool_input": {"command": command}}
    assert allow_rule(denial, "exact", ENC) is None


def test_allow_rule_for_a_web_address_is_its_domain() -> None:
    denial = {"tool_name": "WebFetch", "tool_input": {"url": "https://docs.example.com/page?q=1"}}
    assert allow_rule(denial, "exact", ENC) == "WebFetch(domain:docs.example.com)"


@pytest.mark.parametrize("name", ["mcp__claude_ai_Atlassian_Rovo__getJiraIssue", "mcp__claude-in-chrome__navigate"])
def test_allow_rule_for_an_mcp_tool_is_the_tool(name: str) -> None:
    denial = {"tool_name": name, "tool_input": {"issueIdOrKey": "DATAINGEST-1"}}
    assert allow_rule(denial, "exact", ENC) == name
    assert allow_rule(denial, "tool", ENC) == name


@pytest.mark.parametrize("denial", [
    {}, {"tool_name": "Read(x)", "tool_input": {}}, {"tool_name": "Write", "tool_input": "x"},
    {"tool_name": "Write", "tool_input": {"file_path": "C:/a/(b).md"}}, {"tool_name": 5, "tool_input": {}},
])
def test_odd_denials_get_no_rule(denial: dict[str, Any]) -> None:
    assert allow_rule(denial, "exact", ENC) is None
    assert allow_rule({**denial}, "bogus", ENC) is None


# ----------------------------------------------------------------------
# Sending, replying, stopping
# ----------------------------------------------------------------------
def test_a_reply_streams_and_the_chat_is_saved(tmp_path: Path) -> None:
    h = Harness(tmp_path, [reply(
        message_start(), delta("Hel"), delta("lo"),
        assistant([{"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "a.md"}}]),
        line({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "hi"}]}}),
        result(),
    )])
    sent = h.send(None, "  Summarize my notes\nplease  ")
    chat_id = sent["chat"]["id"]
    assert h.kinds() == ["text", "tool", "tool_result", "done"]
    assert h.events[0] == {"chat": chat_id, "kind": "text", "text": "Hello"}  # one flush, merged
    assert h.events[-1]["gone"] is False and h.events[-1]["ok"] is True
    # The message went in on stdin, never on the command line.
    argv, cwd, proc = h.spawned[0]
    assert proc.stdin.data == b"  Summarize my notes\nplease  " and proc.stdin.closed
    assert "Summarize" not in " ".join(argv)
    assert cwd == str(h.root)
    assert h.manager.dump() == [{"id": chat_id, "session": SESSION, "title": "Summarize my notes",
                                 "updated": h.manager.dump()[0]["updated"]}]
    assert eventually(lambda: h.changes >= 1)


def test_the_next_message_resumes_the_session(tmp_path: Path) -> None:
    h = Harness(tmp_path, [reply(delta("a"), result()), reply(delta("b"), result())])
    chat_id = h.send(None, "one")["chat"]["id"]
    assert "--resume" not in h.spawned[0][0]
    h.send(chat_id, "two")
    argv = h.spawned[1][0]
    assert argv[argv.index("--resume") + 1] == SESSION
    assert [c["id"] for c in h.manager.chats()] == [chat_id]


def test_only_one_reply_at_a_time_per_chat(tmp_path: Path) -> None:
    h = Harness(tmp_path, [reply(delta("a"), hold=True)])
    chat_id = h.send(None, "one", wait=False)["chat"]["id"]
    with pytest.raises(ClaudeError) as exc:
        h.manager.send(chat_id, "two", cwd=h.root, encrypted_dir=h.encrypted, notes_root=h.root)
    assert exc.value.code == "busy"
    h.manager.stop(chat_id)
    h.wait()


def test_stop_ends_the_process_and_the_page_hears_it(tmp_path: Path) -> None:
    h = Harness(tmp_path, [reply(delta("a"), hold=True)])
    chat_id = h.send(None, "one", wait=False)["chat"]["id"]
    h.manager.stop(chat_id)
    h.wait()
    assert h.spawned[0][2].killed
    done = h.events[-1]
    assert done["kind"] == "done" and done.get("stopped") is True and done["ok"] is True


def test_a_process_that_dies_reports_its_error(tmp_path: Path) -> None:
    h = Harness(tmp_path, [reply(code=1, stderr=b"Error: not logged in\n")])
    h.send(None, "hi")
    done = h.events[-1]
    assert done["ok"] is False and "not logged in" in done["error"]
    assert done["gone"] is True and h.manager.chats() == []  # it never got a session: no chat kept


def test_a_failed_start_keeps_nothing(tmp_path: Path) -> None:
    h = Harness(tmp_path, [])
    h.manager._spawn = lambda *a: (_ for _ in ()).throw(OSError("nope"))
    with pytest.raises(ClaudeError) as exc:
        h.send(None, "hi", wait=False)
    assert exc.value.code == "start_failed" and h.manager.chats() == []


@pytest.mark.parametrize(
    "text",
    [None, 5, "", "   \n", pytest.param("x" * (MAX_PROMPT_LENGTH + 1), id="too-long")],
)
def test_bad_messages_are_refused(tmp_path: Path, text: Any) -> None:
    h = Harness(tmp_path, [])
    with pytest.raises(ClaudeError) as exc:
        h.send(None, text, wait=False)
    assert exc.value.code == "invalid_input" and h.spawned == []


def test_unknown_chats_and_missing_claude(tmp_path: Path) -> None:
    h = Harness(tmp_path, [])
    for bad in ("nope", 7, ["x"]):
        with pytest.raises(ClaudeError) as exc:
            h.manager.send(bad, "hi", cwd=h.root, encrypted_dir=h.encrypted, notes_root=h.root)
        assert exc.value.code == "not_found"
    h.manager._find = lambda: None
    with pytest.raises(ClaudeError) as exc:
        h.send(None, "hi", wait=False)
    assert exc.value.code == "no_claude" and h.manager.available() is False


@pytest.mark.parametrize("text", ["/mcp", "  /login  ", "/permissions", "/model opus"])
def test_terminal_commands_are_recognised(text: str) -> None:
    assert terminal_command(text) == text.strip()


@pytest.mark.parametrize(
    "text",
    [None, 5, "", "/review-pr 123", "/code-review", "mcp", "please run /mcp", "/MCP", "/mcp\n/login",
     "/mcp \x07", "/" + "m" * 50, "/model " + "x" * 400],
)
def test_other_text_is_not_a_terminal_command(text: Any) -> None:
    assert terminal_command(text) is None


def test_build_terminal_args_keeps_the_vault_denied_and_ends_options() -> None:
    argv = build_terminal_args("claude", command="/mcp", denied=["Read(x/**)"], prompt="P")
    assert argv[0] == "claude" and "-p" not in argv and "--resume" not in argv
    assert argv[argv.index("--disallowedTools") + 1] == "Read(x/**)"
    assert argv[-2:] == ["--", "/mcp"]  # the variadic deny list cannot swallow the command


def test_a_terminal_command_opens_a_window_not_a_reply(tmp_path: Path) -> None:
    h = Harness(tmp_path, [])
    opened: list[tuple[list[str], str]] = []
    h.manager._console = lambda argv, cwd, env: opened.append((argv, cwd))
    res = h.send(None, "/mcp", wait=False)
    assert res == {"terminal": "/mcp"}
    assert h.spawned == [] and h.manager.chats() == [] and h.changes == 0
    argv, cwd = opened[0]
    assert cwd == str(h.root) and argv[-1] == "/mcp"
    assert any(str(h.encrypted.as_posix()) in a for a in argv)  # still denied in the window


def test_a_terminal_that_cannot_open_says_so(tmp_path: Path) -> None:
    h = Harness(tmp_path, [])

    def boom(argv: list[str], cwd: str, env: dict[str, str]) -> None:
        raise OSError("no console")

    h.manager._console = boom
    with pytest.raises(ClaudeError) as exc:
        h.send(None, "/login", wait=False)
    assert exc.value.code == "start_failed" and "/login" in exc.value.message


def test_skills_still_go_to_claude(tmp_path: Path) -> None:
    h = Harness(tmp_path, [reply(result())])
    h.manager._console = lambda *a: pytest.fail("a skill must not open a terminal")
    h.send(None, "/review-pr 123")
    assert h.spawned[0][2].stdin.data == b"/review-pr 123"


def test_only_a_few_replies_run_at_once(tmp_path: Path) -> None:
    h = Harness(tmp_path, [reply(hold=True) for _ in range(5)])
    ids = [h.send(None, f"chat {i}", wait=False)["chat"]["id"] for i in range(4)]
    with pytest.raises(ClaudeError) as exc:
        h.send(None, "one more", wait=False)
    assert exc.value.code == "too_many"
    h.manager.stop_all()
    assert all(h.spawned[i][2].killed for i in range(4)) and len(ids) == 4


# ----------------------------------------------------------------------
# Allow: a denial becomes a rule for that chat and Claude retries
# ----------------------------------------------------------------------
def denial_reply() -> FakeProcess:
    return reply(result(permission_denials=[
        {"tool_name": "Write", "tool_use_id": "t7", "tool_input": {"file_path": "C:/notes/ai-notes/x.md", "content": "c"}},
        {"tool_name": "PowerShell", "tool_use_id": "t8", "tool_input": {"command": "Write-Host (1)"}},
    ]))


def test_denials_become_cards(tmp_path: Path) -> None:
    h = Harness(tmp_path, [denial_reply()])
    h.send(None, "write it")
    cards = h.events[-1]["denials"]
    assert cards == [
        {"id": "t7", "tool": "Write", "summary": "C:/notes/ai-notes/x.md", "exact": True},
        {"id": "t8", "tool": "PowerShell", "summary": "Write-Host (1)", "exact": False},
    ]


def test_allow_adds_a_rule_and_resumes(tmp_path: Path) -> None:
    h = Harness(tmp_path, [denial_reply(), reply(delta("done"), result())])
    chat_id = h.send(None, "write it")["chat"]["id"]
    h._done.clear()
    h.events.clear()
    h.manager.allow(chat_id, ["t7"], "exact", cwd=h.root, encrypted_dir=h.encrypted, notes_root=h.root)
    h.wait()
    argv, _, proc = h.spawned[1]
    assert argv[argv.index("--allowedTools") + 1] == "Edit(C:/notes/ai-notes/x.md)"
    assert argv[argv.index("--resume") + 1] == SESSION
    assert b"retry" in proc.stdin.data and b"C:/notes/ai-notes/x.md" in proc.stdin.data


def test_allow_a_whole_tool_for_the_chat(tmp_path: Path) -> None:
    h = Harness(tmp_path, [denial_reply(), reply(result()), reply(result())])
    chat_id = h.send(None, "go")["chat"]["id"]
    h._done.clear()
    h.manager.allow(chat_id, ["t8"], "tool", cwd=h.root, encrypted_dir=h.encrypted, notes_root=h.root)
    h.wait()
    h.send(chat_id, "again")
    assert "PowerShell" in h.spawned[2][0]  # still allowed on the next message


@pytest.mark.parametrize("ids,scope,code", [
    (["nope"], "exact", "not_found"),
    (["t8"], "exact", "not_allowed"),   # a command with brackets cannot be allowed one use at a time
    (["t7"], "everything", "invalid_input"),
    ([], "exact", "invalid_input"),
    ("t7", "exact", "invalid_input"),
])
def test_allow_refuses_what_it_cannot_do(tmp_path: Path, ids: Any, scope: Any, code: str) -> None:
    h = Harness(tmp_path, [denial_reply()])
    chat_id = h.send(None, "go")["chat"]["id"]
    with pytest.raises(ClaudeError) as exc:
        h.manager.allow(chat_id, ids, scope, cwd=h.root, encrypted_dir=h.encrypted, notes_root=h.root)
    assert exc.value.code == code
    assert len(h.spawned) == 1


def test_a_new_message_forgets_old_denials(tmp_path: Path) -> None:
    h = Harness(tmp_path, [denial_reply(), reply(result())])
    chat_id = h.send(None, "go")["chat"]["id"]
    h.send(chat_id, "never mind")
    with pytest.raises(ClaudeError):
        h.manager.allow(chat_id, ["t7"], "exact", cwd=h.root, encrypted_dir=h.encrypted, notes_root=h.root)


# ----------------------------------------------------------------------
# The saved list
# ----------------------------------------------------------------------
def test_load_drops_bad_entries_and_dump_caps_the_list() -> None:
    manager = ClaudeManager(lambda *_: None)
    good = {"id": "a" * 32, "session": SESSION, "title": "Hello\x07 there", "updated": "2026-01-01T00:00:00+00:00"}
    manager.load([good, {"id": "short", "session": SESSION}, {"id": "b" * 32, "session": "not-a-uuid"},
                  {"id": "c" * 32}, "junk", None, {**good, "id": 5}])
    assert [c["id"] for c in manager.chats()] == ["a" * 32]
    assert manager.chats()[0]["title"] == "Hello there"
    many = [{"id": f"{i:032x}", "session": SESSION_2, "title": f"t{i}", "updated": f"2026-01-{i % 28 + 1:02d}"}
            for i in range(MAX_CHATS_KEPT + 10)]
    manager.load(many)
    assert len(manager.dump()) == MAX_CHATS_KEPT
    manager.load("not a list")
    assert manager.dump() == []


def test_titles_are_the_first_line_and_short() -> None:
    assert clean_title("\n\n  First line\nsecond") == "First line"
    assert len(clean_title("w" * 500)) == 60 and clean_title("w" * 500).endswith("…")
    assert clean_title("   ") == ""


def test_forget_takes_a_chat_off_the_list(tmp_path: Path) -> None:
    h = Harness(tmp_path, [reply(delta("a"), result())])
    chat_id = h.send(None, "hi")["chat"]["id"]
    h.manager.forget(chat_id)
    assert h.manager.chats() == [] and h.manager.dump() == []
    with pytest.raises(ClaudeError):
        h.manager.forget(chat_id)


# ----------------------------------------------------------------------
# Reopening a chat
# ----------------------------------------------------------------------
def test_history_is_read_from_claude_codes_own_file(tmp_path: Path) -> None:
    home, cwd = tmp_path / "home", Path("C:/Users/Me/Desktop/VaultNotes")
    path = history_path(SESSION, cwd, home)
    assert path.parent.name == "C--Users-Me-Desktop-VaultNotes"
    path.parent.mkdir(parents=True)
    rows = [
        {"type": "queue-operation"},
        {"type": "user", "message": {"role": "user", "content": "Hello"}},
        {"type": "user", "isMeta": True, "message": {"role": "user", "content": "meta"}},
        {"type": "user", "message": {"role": "user", "content": "<system-reminder>x</system-reminder>"}},
        {"type": "assistant", "message": {"id": "m", "content": [{"type": "text", "text": "Hi!"}]}},
        {"type": "assistant", "message": {"id": "m", "content": [
            {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "a.md"}}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "body"}]}},
        {"type": "assistant", "isSidechain": True, "message": {"id": "s", "content": [{"type": "text", "text": "side"}]}},
        {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "Thanks"}]}},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n", encoding="utf-8")
    events = read_history(SESSION, cwd, home)
    assert [e["kind"] for e in events] == ["user", "text", "tool", "tool_result", "user"]
    assert events[0]["text"] == "Hello" and events[-1]["text"] == "Thanks"


def test_history_that_cannot_be_read_is_empty(tmp_path: Path) -> None:
    assert read_history(SESSION, tmp_path, tmp_path) == []
    assert read_history("../../etc/passwd", tmp_path, tmp_path) == []


# ----------------------------------------------------------------------
# Through the Bridge API
# ----------------------------------------------------------------------
class FakeWindow:
    def __init__(self, allow: bool = True) -> None:
        self.allow = allow
        self.asked = 0
        self.scripts: list[str] = []

    def create_confirmation_dialog(self, title: str, message: str) -> bool:
        self.asked += 1
        return self.allow

    def evaluate_js(self, script: str) -> None:
        self.scripts.append(script)


def make_api(tmp_path: Path, window: Any = None) -> Api:
    cfg = Config(settings_path=tmp_path / "settings.json")
    cfg.data["notes_root"] = str(tmp_path / "notes")
    cfg.save()
    cfg.ensure_folders()
    api = Api(config=cfg, app_dir=tmp_path / "appdata", drive_store=TokenStore(memory=True))
    api.set_window(window)
    return api


def saved(tmp_path: Path) -> dict[str, Any]:
    return json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))["claude"]


def test_the_claude_space_is_off_by_default(tmp_path: Path) -> None:
    cfg = Config(settings_path=tmp_path / "settings.json", auto_init_folders=False)
    assert cfg.data["claude"] == {"enabled": False, "chats": []}


def test_damaged_claude_settings_are_repaired(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"claude": {"enabled": "yes", "chats": "x"}}), encoding="utf-8")
    cfg = Config(settings_path=path, auto_init_folders=False)
    assert cfg.data["claude"] == {"enabled": False, "chats": []}


def test_update_settings_cannot_switch_it_on(tmp_path: Path) -> None:
    api = make_api(tmp_path, FakeWindow())
    assert api.update_settings({"claude": {"enabled": True}})["error"] == "invalid_settings"


def test_it_stays_off_until_the_native_dialog_says_yes(tmp_path: Path) -> None:
    window = FakeWindow(allow=False)
    api = make_api(tmp_path, window)
    api.claude._find = lambda: "claude"
    assert api.claude_state()["enabled"] is False
    assert api.claude_send(None, "hi")["error"] == "claude_off"
    assert api.claude_enable()["error"] == "cancelled" and window.asked == 1
    assert saved(tmp_path)["enabled"] is False
    window.allow = True
    assert api.claude_enable() == {"ok": True, "enabled": True}
    assert saved(tmp_path)["enabled"] is True
    assert api.claude_disable() == {"ok": True, "enabled": False}


def test_it_cannot_be_turned_on_without_claude_code(tmp_path: Path) -> None:
    api = make_api(tmp_path, FakeWindow())
    api.claude._find = lambda: None
    assert api.claude_enable()["error"] == "no_claude"
    assert api.claude_state()["available"] is False


def test_send_runs_in_the_notes_folder_and_keeps_the_encrypted_vault_out(tmp_path: Path) -> None:
    api = make_api(tmp_path, FakeWindow())
    h = Harness(tmp_path, [reply(delta("ok"), result())])
    api.claude = ClaudeManager(h._emit, api._claude_chats_changed, spawn=h._spawn, find=lambda: "claude")
    api.claude_enable()
    sent = api.claude_send(None, "Hello")
    assert sent["ok"] is True
    h.wait()
    argv, cwd, _ = h.spawned[0]
    assert cwd == str(api.config.notes_root)
    encrypted = api.config.vault_dir("encrypted").as_posix()
    assert argv[argv.index("--disallowedTools") + 1:] == [f"Read({encrypted}/**)", f"Edit({encrypted}/**)"]
    # The chat list reached settings.json: ids and titles, no messages.
    assert eventually(lambda: saved(tmp_path)["chats"])
    chats = saved(tmp_path)["chats"]
    assert [c["title"] for c in chats] == ["Hello"] and "ok" not in json.dumps(chats)
    assert api.claude_state()["chats"][0]["id"] == sent["chat"]["id"]


def test_a_restart_brings_the_list_back(tmp_path: Path) -> None:
    api = make_api(tmp_path, FakeWindow())
    h = Harness(tmp_path, [reply(delta("ok"), result())])
    api.claude = ClaudeManager(h._emit, api._claude_chats_changed, spawn=h._spawn, find=lambda: "claude")
    api.claude_enable()
    chat_id = api.claude_send(None, "Remember me")["chat"]["id"]
    h.wait()
    assert eventually(lambda: saved(tmp_path)["chats"])
    again = make_api(tmp_path, FakeWindow())
    assert [c["id"] for c in again.claude.chats()] == [chat_id]


def test_build_args_passes_only_listed_model_and_effort() -> None:
    argv = build_args("claude", session_id=None, allowed=[], denied=[], prompt="p", model="sonnet", effort="max")
    assert argv[argv.index("--model") + 1] == "sonnet" and argv[argv.index("--effort") + 1] == "max"
    odd = build_args("claude", session_id=None, allowed=[], denied=[], prompt="p", model="--evil", effort="huge")
    assert "--model" not in odd and "--effort" not in odd
    term = build_terminal_args("claude", command="/mcp", denied=[], prompt="p", model="haiku", effort="low")
    assert term[term.index("--model") + 1] == "haiku" and term[-2:] == ["--", "/mcp"]


def test_the_picked_model_and_effort_reach_the_command_and_survive_a_restart(tmp_path: Path) -> None:
    api = make_api(tmp_path, FakeWindow())
    h = Harness(tmp_path, [reply(delta("ok"), result())])
    api.claude = ClaudeManager(h._emit, api._claude_chats_changed, spawn=h._spawn, find=lambda: "claude")
    api.claude_enable()
    choice = api.claude_state()["choice"]
    assert (choice["model"], choice["effort"]) == ("opus", "high")
    assert [m["id"] for m in choice["models"]] == ["opus", "sonnet", "haiku", "fable"]
    assert api.claude_set_choice("sonnet", "xhigh")["choice"]["model"] == "sonnet"
    assert api.claude_set_choice("gpt", "high")["error"] == "invalid_input"
    assert api.claude_set_choice("haiku", "--x")["error"] == "invalid_input"
    assert (api.claude.model, api.claude.effort) == ("sonnet", "xhigh")
    api.claude_send(None, "Hello")
    h.wait()
    argv = h.spawned[0][0]
    assert argv[argv.index("--model") + 1] == "sonnet" and argv[argv.index("--effort") + 1] == "xhigh"
    assert (saved(tmp_path)["model"], saved(tmp_path)["effort"]) == ("sonnet", "xhigh")
    again = make_api(tmp_path, FakeWindow())
    assert (again.claude.model, again.claude.effort) == ("sonnet", "xhigh")


def test_a_damaged_saved_pick_falls_back_to_the_defaults(tmp_path: Path) -> None:
    manager = ClaudeManager(lambda *_: None)
    manager.load_choice({"model": "nope", "effort": "high"})
    assert (manager.model, manager.effort) == ("opus", "high")
    manager.load_choice("junk")
    assert (manager.model, manager.effort) == ("opus", "high")


def test_a_reply_can_not_pull_notes_in_through_links(tmp_path: Path) -> None:
    api = make_api(tmp_path, FakeWindow())
    html = api.render_chat("**bold** and [[Some Note]] and ![[Embedded]]")
    assert "<strong>bold</strong>" in html
    assert "data-note" not in html and "Embedded" in html  # plain text, no embed card
    assert api.render_chat(None) == "" or isinstance(api.render_chat(None), str)


def test_hostile_chat_ids_get_an_error_not_a_crash(tmp_path: Path) -> None:
    api = make_api(tmp_path, FakeWindow())
    api.claude_enable()
    for bad in (None, 0, "", [], {}, "../../x"):
        for call in (api.claude_stop, api.claude_forget, api.claude_history):
            assert call(bad).get("error"), call


# ----------------------------------------------------------------------
# The "Vertex ready" badge: PHI may go in AI-Notes only on Vertex with an
# encrypted AI-Notes.
# ----------------------------------------------------------------------
VERTEX_ENV = {"CLAUDE_CODE_USE_VERTEX": "1", "ANTHROPIC_VERTEX_PROJECT_ID": "dev-code-assist-464720"}


def set_vertex_env(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    for name in VERTEX_ENV:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)


def test_vertex_ready_needs_vertex_and_an_encrypted_ai_notes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    api = make_api(tmp_path, FakeWindow())
    set_vertex_env(monkeypatch, VERTEX_ENV)
    vertex = api.claude_state()["vertex"]
    assert vertex["ready"] is True and vertex["vertex"] is True and vertex["ai_encrypted"] is True


def test_vertex_ready_is_off_outside_vertex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    api = make_api(tmp_path, FakeWindow())
    set_vertex_env(monkeypatch, {})
    vertex = api.claude_state()["vertex"]
    assert vertex["ready"] is False and vertex["vertex"] is False and "no PHI" in vertex["reason"]
    set_vertex_env(monkeypatch, {**VERTEX_ENV, "ANTHROPIC_VERTEX_PROJECT_ID": "some-other-project"})
    assert api.claude_state()["vertex"]["ready"] is False


def test_vertex_ready_is_off_while_ai_notes_holds_plain_notes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    api = make_api(tmp_path, FakeWindow())
    set_vertex_env(monkeypatch, VERTEX_ENV)
    (api.config.ai_dir / "Leftover.md").write_text("# Leftover\n", encoding="utf-8")
    vertex = api.claude_state()["vertex"]
    assert vertex["ready"] is False and vertex["vertex"] is True and vertex["ai_encrypted"] is False


def test_vertex_ready_is_off_when_ai_notes_cannot_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    api = make_api(tmp_path, FakeWindow())
    set_vertex_env(monkeypatch, VERTEX_ENV)
    api.plain_stores["ai"] = UnavailableAIStore(api.config.ai_dir, "the key file is missing")
    assert api.claude_state()["vertex"]["ready"] is False


def test_the_chat_prompt_allows_phi_notes_only_when_vertex_ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    api = make_api(tmp_path, FakeWindow())
    h = Harness(tmp_path, [reply(delta("ok"), result()), reply(delta("ok"), result())])
    api.claude = ClaudeManager(h._emit, api._claude_chats_changed, spawn=h._spawn, find=lambda: "claude")
    api.claude_enable()

    def prompt_of(argv: list[str]) -> str:
        return argv[argv.index("--append-system-prompt") + 1]

    set_vertex_env(monkeypatch, {})
    api.claude_send(None, "Outside Vertex")
    h.wait()
    assert "Never put PHI in a note." in prompt_of(h.spawned[0][0])

    set_vertex_env(monkeypatch, VERTEX_ENV)
    api.claude_send(None, "On Vertex")
    assert eventually(lambda: len(h.spawned) == 2)
    prompt = prompt_of(h.spawned[1][0])
    assert "PHI may go in AI-Notes notes" in prompt and "Never put PHI in a note." not in prompt


# ----------------------------------------------------------------------
# Messages queued while Claude replies
# ----------------------------------------------------------------------
class GatedProcess(FakeProcess):
    """Prints ``before``, waits for :meth:`go` (or a kill), then prints ``after``."""

    def __init__(self, before: list[bytes], after: list[bytes]) -> None:
        self._gate = threading.Event()
        self._after = after
        super().__init__(before)

    def _out(self):
        yield from self._lines
        while not self._gate.wait(0.01):
            if self.killed:
                return
        yield from self._after

    def go(self) -> None:
        self._gate.set()


def tool_round(tool_id: str = "t1") -> list[bytes]:
    return [
        assistant([{"type": "tool_use", "id": tool_id, "name": "Read", "input": {"file_path": "a.md"}}]),
        line({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tool_id, "content": "x"}]}}),
    ]


def test_queue_files_are_taken_once_and_in_order(tmp_path: Path) -> None:
    folder = tmp_path / "q"
    first = claude_queue.enqueue(folder, "first")
    second = claude_queue.enqueue(folder, "second")
    assert claude_queue.waiting(folder) == {first, second}
    assert claude_queue.take_all(folder) == [(first, "first"), (second, "second")]
    assert claude_queue.take_all(folder) == [] and claude_queue.take(folder, first) is None
    assert claude_queue.take(folder, "../../etc") is None


def test_the_hook_hands_queued_text_to_claude(tmp_path: Path) -> None:
    folder = tmp_path / "q"
    claude_queue.enqueue(folder, "also check the Jira ticket")
    out = subprocess.run(
        [sys.executable, "-I", claude_queue.__file__, str(folder)],
        input=b'{"hook_event_name": "PostToolUse"}', capture_output=True, timeout=30,
    )
    assert out.returncode == 0
    data = json.loads(out.stdout)["hookSpecificOutput"]
    assert data["hookEventName"] == "PostToolUse"
    assert data["additionalContext"].endswith("also check the Jira ticket")
    assert claude_queue.waiting(folder) == set()
    # Nothing waiting (or no folder at all): no output, and never a failed tool call.
    empty = subprocess.run([sys.executable, "-I", claude_queue.__file__, str(tmp_path / "none")],
                           input=b"{}", capture_output=True, timeout=30)
    assert empty.returncode == 0 and empty.stdout == b""


def test_every_turn_registers_the_queue_hook(tmp_path: Path) -> None:
    argv = build_args("claude", session_id=None, allowed=[], denied=[], prompt="p", queue_folder=tmp_path / "q")
    hooks = json.loads(argv[argv.index("--settings") + 1])["hooks"]["PostToolUse"][0]["hooks"]
    assert hooks[0]["args"] == ["-I", Path(claude_queue.__file__).as_posix(), (tmp_path / "q").as_posix()]
    assert "--settings" not in build_args("claude", session_id=None, allowed=[], denied=[], prompt="p")
    h = Harness(tmp_path, [reply(delta("a"), result())])
    chat_id = h.send(None, "hi")["chat"]["id"]
    assert (h.queue_root / chat_id).as_posix() in h.spawned[0][0][h.spawned[0][0].index("--settings") + 1]


def test_a_message_the_hook_takes_is_reported_delivered(tmp_path: Path) -> None:
    proc = GatedProcess([delta("working")], [*tool_round(), delta("done"), result()])
    h = Harness(tmp_path, [proc])
    chat_id = h.send(None, "one", wait=False)["chat"]["id"]
    queue_id = h.manager.queue(chat_id, "and also this")["id"]
    # What Claude Code does after the tool call: run the hook.
    assert claude_queue.take_all(h.queue_root / chat_id) == [(queue_id, "and also this")]
    proc.go()
    h.wait()
    assert {"chat": chat_id, "kind": "queue_delivered", "ids": [queue_id]} in h.events
    assert len(h.spawned) == 1  # nothing left over to send


def test_a_message_left_over_goes_out_as_the_next_one(tmp_path: Path) -> None:
    proc = GatedProcess([delta("working")], [delta("done"), result()])
    h = Harness(tmp_path, [proc, reply(delta("ok"), result())])
    chat_id = h.send(None, "one", wait=False)["chat"]["id"]
    first = h.manager.queue(chat_id, "two")["id"]
    second = h.manager.queue(chat_id, "three")["id"]
    proc.go()
    assert eventually(lambda: len(h.spawned) == 2)
    assert {"chat": chat_id, "kind": "queue_sent", "ids": [first, second], "text": "two\n\nthree"} in h.events
    argv, _, nxt = h.spawned[1]
    assert nxt.stdin.data == b"two\n\nthree" and argv[argv.index("--resume") + 1] == SESSION
    assert eventually(lambda: not h.manager.chats()[0]["running"])


def test_after_a_stop_queued_messages_come_back(tmp_path: Path) -> None:
    proc = GatedProcess([delta("working")], [])
    h = Harness(tmp_path, [proc])
    chat_id = h.send(None, "one", wait=False)["chat"]["id"]
    queue_id = h.manager.queue(chat_id, "never mind")["id"]
    h.manager.stop(chat_id)
    assert eventually(lambda: any(e["kind"] == "queue_returned" for e in h.events))
    returned = next(e for e in h.events if e["kind"] == "queue_returned")
    assert returned["ids"] == [queue_id] and returned["text"] == "never mind"
    assert len(h.spawned) == 1


def test_unqueue_gives_the_text_back_unless_claude_has_it(tmp_path: Path) -> None:
    proc = GatedProcess([delta("working")], [result()])
    h = Harness(tmp_path, [proc])
    chat_id = h.send(None, "one", wait=False)["chat"]["id"]
    kept = h.manager.queue(chat_id, "take me back")["id"]
    taken = h.manager.queue(chat_id, "too late")["id"]
    assert h.manager.unqueue(chat_id, kept) == {"text": "take me back"}
    claude_queue.take(h.queue_root / chat_id, taken)  # the hook got there first
    with pytest.raises(ClaudeError) as exc:
        h.manager.unqueue(chat_id, taken)
    assert exc.value.code == "delivered"
    proc.go()
    h.wait()


def test_queue_through_the_api(tmp_path: Path) -> None:
    api = make_api(tmp_path, FakeWindow())
    proc = GatedProcess([delta("working")], [result()])
    h = Harness(tmp_path, [proc])
    api.claude = h.manager
    assert api.claude_queue("f" * 32, "hi")["error"] == "claude_off"
    api.claude_enable()
    chat_id = api.claude_send(None, "one")["chat"]["id"]
    queued = api.claude_queue(chat_id, "two")
    assert queued["ok"] is True
    assert api.claude_unqueue(chat_id, queued["id"]) == {"ok": True, "text": "two"}
    assert api.claude_unqueue(chat_id, queued["id"])["error"] == "delivered"
    proc.go()
    h.wait()


def test_queue_needs_a_running_reply_and_real_text(tmp_path: Path) -> None:
    h = Harness(tmp_path, [reply(delta("a"), result())])
    chat_id = h.send(None, "one")["chat"]["id"]
    with pytest.raises(ClaudeError) as exc:
        h.manager.queue(chat_id, "late")
    assert exc.value.code == "not_running"
    for bad in (None, "", "  ", "x" * (MAX_PROMPT_LENGTH + 1)):
        with pytest.raises(ClaudeError) as exc:
            h.manager.queue(chat_id, bad)
        assert exc.value.code == "invalid_input"
    with pytest.raises(ClaudeError) as exc:
        h.manager.queue("f" * 32, "hi")
    assert exc.value.code == "not_found"


# ----------------------------------------------------------------------
# Permission pop-ups: Claude waits while the user answers
# ----------------------------------------------------------------------
WRITE_REQ = {"tool_name": "Write", "input": {"file_path": "C:/n/a.md", "content": "x"}, "tool_use_id": "toolu_1"}


class AskingProcess(FakeProcess):
    """Prints ``before``, waits until ``gate`` is set (or it is killed), then prints ``after``."""

    def __init__(self, before: list[bytes], after: list[bytes], gate: threading.Event) -> None:
        self._before, self._after, self._gate = before, after, gate
        super().__init__([])

    def _out(self):
        yield from self._before
        while not self._gate.wait(0.02):
            if self._killed.is_set():
                return
        yield from self._after


def tool_asks(folder: Path, request: dict[str, Any], timeout: float = 5) -> tuple[threading.Thread, dict[str, Any]]:
    """Play Claude Code's permission tool: ask, and keep the answer."""
    box: dict[str, Any] = {}

    def run() -> None:
        box["answer"] = claude_permit.ask(folder, request, timeout)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, box


def first(h: Harness, kind: str) -> dict[str, Any]:
    assert eventually(lambda: any(e["kind"] == kind for e in h.events)), f"no {kind} event"
    return next(e for e in h.events if e["kind"] == kind)


def test_each_turn_registers_the_permission_tool(tmp_path: Path) -> None:
    argv = build_args("claude", session_id=None, allowed=[], denied=[], prompt="p", permit_folder=tmp_path / "p")
    server = json.loads(argv[argv.index("--mcp-config") + 1])["mcpServers"][claude_permit.SERVER_NAME]
    assert server["args"][:2] == ["-I", Path(claude_permit.__file__).as_posix()]
    assert server["args"][2:] == [(tmp_path / "p").as_posix(), "600"]
    assert argv[argv.index("--permission-prompt-tool") + 1] == claude_permit.PROMPT_TOOL
    plain = build_args("claude", session_id=None, allowed=[], denied=[], prompt="p")
    assert "--mcp-config" not in plain and "--permission-prompt-tool" not in plain


def test_always_allow_saves_only_exact_rules_for_safe_tools(tmp_path: Path) -> None:
    assert always_rule(WRITE_REQ) == "Edit(C:/n/a.md)"
    assert always_rule({"tool_name": "PowerShell", "input": {"command": "git status"}}) == "PowerShell(git status)"
    assert always_rule({"tool_name": "PowerShell", "input": {"command": "a (b)"}}) is None  # not exact
    assert always_rule({"tool_name": "Task", "input": {"prompt": "x"}}) is None  # whole tool only
    read_tool = "mcp__atlassian__searchJiraIssuesUsingJql"
    assert always_rule({"tool_name": read_tool, "input": {}}) == read_tool
    for outward in ("mcp__plugin_slack_slack__slack_send_message", "mcp__atlassian__addOrEditJiraIssueComment",
                    "mcp__atlassian__executeWrite", "mcp__atlassian__transitionJiraIssue"):
        assert always_rule({"tool_name": outward, "input": {}}) is None, outward
    # The server name is not judged: "postman" is not "post".
    assert always_rule({"tool_name": "mcp__plugin_postman_postman__getCollections", "input": {}}) is not None
    vault = tmp_path / "vaults" / "encrypted"
    inside = {"tool_name": "Edit", "input": {"file_path": (vault / "x.md").as_posix()}}
    assert always_rule(inside, vault) is None


def test_names_folder_finds_full_and_relative_paths(tmp_path: Path) -> None:
    root = tmp_path / "notes"
    vault = root / "vaults" / "encrypted"
    assert names_folder({"command": f"type {vault}\\key.vnkey".replace("/", "\\")}, vault, root)
    assert names_folder({"command": "Get-ChildItem VAULTS\\Encrypted"}, vault, root)
    assert names_folder({"nested": {"paths": [f"{vault.as_posix()}/a.md"]}}, vault, root)
    assert not names_folder({"command": "Get-ChildItem vaults\\personal"}, vault, root)
    assert not names_folder({"command": "dir"}, vault, None)


def test_a_question_pops_up_and_always_allow_saves_the_exact_rule(tmp_path: Path) -> None:
    gate = threading.Event()
    h = Harness(tmp_path, [AskingProcess([message_start()], [result()], gate), reply(result())])
    chat_id = h.send(None, "write it", wait=False)["chat"]["id"]
    folder = h.permit_root / chat_id
    assert folder.is_dir()
    thread, box = tool_asks(folder, WRITE_REQ)
    ev = first(h, "permission")
    assert (ev["chat"], ev["tool"], ev["summary"], ev["always"], ev["timeout"]) == (chat_id, "Write", "C:/n/a.md", True, 600)
    assert ev["title"] == "write it"
    assert [a["id"] for a in h.manager.waiting_asks()] == [ev["id"]]
    assert h.manager.answer_permission(chat_id, ev["id"], "always") == {"always_changed": True}
    thread.join(3)
    assert box["answer"] == (True, "")
    assert h.manager.always_list() == ["Edit(C:/n/a.md)"]
    assert first(h, "permission_done")["ids"] == [ev["id"]]
    assert h.manager.waiting_asks() == []
    gate.set()
    h.wait()
    # Every later turn starts with it allowed, so Claude Code does not even ask.
    h.send(chat_id, "again")
    argv = h.spawned[1][0]
    assert "Edit(C:/n/a.md)" in argv[argv.index("--allowedTools") + 1:]


def test_allow_for_the_chat_answers_the_next_question_without_a_pop_up(tmp_path: Path) -> None:
    gate = threading.Event()
    h = Harness(tmp_path, [AskingProcess([message_start()], [result()], gate)])
    chat_id = h.send(None, "run things", wait=False)["chat"]["id"]
    folder = h.permit_root / chat_id
    ps = {"tool_name": "PowerShell", "input": {"command": "git status"}, "tool_use_id": "toolu_1"}
    thread, box = tool_asks(folder, ps)
    ev = first(h, "permission")
    assert ev["chat_rule"] is True
    assert h.manager.answer_permission(chat_id, ev["id"], "chat") == {"always_changed": False}
    thread.join(3)
    assert box["answer"] == (True, "")
    h.events.clear()
    thread, box = tool_asks(folder, {**ps, "input": {"command": "git log"}, "tool_use_id": "toolu_2"})
    thread.join(3)
    assert box["answer"] == (True, "")
    assert "permission" not in h.kinds()
    assert h.manager.always_list() == []  # this chat only
    gate.set()
    h.wait()


def test_allow_once_asks_again_next_time(tmp_path: Path) -> None:
    gate = threading.Event()
    h = Harness(tmp_path, [AskingProcess([message_start()], [result()], gate)])
    chat_id = h.send(None, "write", wait=False)["chat"]["id"]
    folder = h.permit_root / chat_id
    thread, box = tool_asks(folder, WRITE_REQ)
    h.manager.answer_permission(chat_id, first(h, "permission")["id"], "once")
    thread.join(3)
    assert box["answer"] == (True, "")
    h.events.clear()
    tool_asks(folder, {**WRITE_REQ, "tool_use_id": "toolu_2"})
    assert first(h, "permission")["tool"] == "Write"
    gate.set()
    h.wait()


def test_deny_passes_the_note_on_and_leaves_no_card(tmp_path: Path) -> None:
    gate = threading.Event()
    denial = {"tool_name": "Write", "tool_use_id": "toolu_1", "tool_input": WRITE_REQ["input"]}
    h = Harness(tmp_path, [AskingProcess([message_start()], [result(permission_denials=[denial])], gate)])
    chat_id = h.send(None, "write", wait=False)["chat"]["id"]
    thread, box = tool_asks(h.permit_root / chat_id, WRITE_REQ)
    ev = first(h, "permission")
    h.manager.answer_permission(chat_id, ev["id"], "deny", "Use AI-Notes\ninstead.")
    thread.join(3)
    allowed, message = box["answer"]
    assert allowed is False and message.endswith("Their note: Use AI-Notes instead.")
    gate.set()
    h.wait()
    assert first(h, "done")["denials"] == []  # already refused in the pop-up


def test_the_encrypted_vault_is_refused_without_asking(tmp_path: Path) -> None:
    gate = threading.Event()
    h = Harness(tmp_path, [AskingProcess([message_start()], [result()], gate)])
    chat_id = h.send(None, "look", wait=False)["chat"]["id"]
    sneaky = {"tool_name": "PowerShell", "input": {"command": "Get-ChildItem vaults\\encrypted"}, "tool_use_id": "t9"}
    thread, box = tool_asks(h.permit_root / chat_id, sneaky)
    thread.join(3)
    allowed, message = box["answer"]
    assert allowed is False and "Encrypted vault" in message
    auto = first(h, "permission_auto")
    assert auto["tool"] == "PowerShell" and "Encrypted vault" in auto["message"]
    assert "permission" not in h.kinds()
    gate.set()
    h.wait()


def test_an_open_question_is_refused_when_the_reply_ends(tmp_path: Path) -> None:
    gate = threading.Event()
    h = Harness(tmp_path, [AskingProcess([message_start()], [result()], gate)])
    chat_id = h.send(None, "write", wait=False)["chat"]["id"]
    folder = h.permit_root / chat_id
    thread, box = tool_asks(folder, WRITE_REQ)
    ev = first(h, "permission")
    gate.set()
    h.wait()
    assert first(h, "done")["asks_gone"] == [ev["id"]]
    thread.join(3)
    assert box["answer"] == (False, claude_permit.DENIED_GONE)
    assert not folder.exists()
    with pytest.raises(ClaudeError) as exc:
        h.manager.answer_permission(chat_id, ev["id"], "once")
    assert exc.value.code == "not_found"


def test_answers_that_cannot_be_given_leave_the_question_waiting(tmp_path: Path) -> None:
    gate = threading.Event()
    h = Harness(tmp_path, [AskingProcess([message_start()], [result()], gate)])
    chat_id = h.send(None, "post", wait=False)["chat"]["id"]
    slack = {"tool_name": "mcp__plugin_slack_slack__slack_send_message", "input": {"text": "hi"}, "tool_use_id": "t1"}
    thread, box = tool_asks(h.permit_root / chat_id, slack)
    ev = first(h, "permission")
    assert ev["always"] is False
    for choice, code in (("always", "not_allowed"), ("forever", "invalid_input")):
        with pytest.raises(ClaudeError) as exc:
            h.manager.answer_permission(chat_id, ev["id"], choice)
        assert exc.value.code == code
    with pytest.raises(ClaudeError) as exc:
        h.manager.answer_permission(chat_id, "1-0000000a", "once")
    assert exc.value.code == "not_found"
    h.manager.answer_permission(chat_id, ev["id"], "once")  # still waiting, so this works
    thread.join(3)
    assert box["answer"] == (True, "")
    gate.set()
    h.wait()


def test_saved_rules_are_checked_when_loaded() -> None:
    manager = ClaudeManager(lambda *_: None)
    manager.load_always(["Edit(C:/n/a.md)", "PowerShell(git status)", "mcp__x__y", "", 5, "--evil", "a" * 3000])
    assert manager.always_list() == ["Edit(C:/n/a.md)", "mcp__x__y", "PowerShell(git status)"]
    manager.load_always("nope")
    assert manager.always_list() == []


def test_always_rules_are_saved_listed_removed_and_survive_a_restart(tmp_path: Path) -> None:
    api = make_api(tmp_path, FakeWindow())
    gate = threading.Event()
    h = Harness(tmp_path, [AskingProcess([message_start()], [result()], gate)])
    api.claude = ClaudeManager(h._emit, api._claude_chats_changed, spawn=h._spawn, find=lambda: "claude",
                               permit_root=h.permit_root)
    api.claude_enable()
    chat_id = api.claude_send(None, "write")["chat"]["id"]
    thread, box = tool_asks(h.permit_root / chat_id, WRITE_REQ)
    ev = first(h, "permission")
    assert [a["id"] for a in api.claude_state()["asks"]] == [ev["id"]]
    assert api.claude_permission(chat_id, ev["id"], "always") == {"ok": True}
    thread.join(3)
    assert saved(tmp_path)["always"] == ["Edit(C:/n/a.md)"]
    assert api.claude_always() == {"ok": True, "rules": ["Edit(C:/n/a.md)"]}
    gate.set()
    h.wait()
    assert eventually(lambda: saved(tmp_path)["chats"])  # the reply's own save is done
    again = make_api(tmp_path, FakeWindow())
    assert again.claude.always_list() == ["Edit(C:/n/a.md)"]
    assert again.claude_always_remove("Edit(C:/n/a.md)") == {"ok": True, "rules": []}
    assert saved(tmp_path)["always"] == []
    assert again.claude_always_remove("Edit(C:/n/a.md)")["error"] == "not_found"
    # The page cannot write rules any other way.
    assert again.update_settings({"claude": {"always": ["PowerShell"]}})["error"] == "invalid_settings"
    assert again.claude_permission(chat_id, "1-0000000a", "once")["error"] == "not_found"
