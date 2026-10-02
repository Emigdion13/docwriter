"""The Claude space: chats with Claude Code that is off until the user allows it.

A fake ``claude`` process stands in for the real program, so these tests never
start Claude Code or touch the network.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from vaultnotes.api import Api
from vaultnotes.backup.gdrive_auth import TokenStore
from vaultnotes.claude_chat import (
    MAX_CHATS_KEPT,
    MAX_PROMPT_LENGTH,
    ClaudeError,
    ClaudeManager,
    Translator,
    allow_rule,
    build_args,
    clean_title,
    deny_rules,
    history_path,
    read_history,
    summarize_tool,
)
from vaultnotes.config import Config

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
        self.manager = ClaudeManager(
            self._emit, self._changed, spawn=self._spawn, find=lambda: "claude", kill=lambda p: p.kill(),
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
