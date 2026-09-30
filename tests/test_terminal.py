"""The CMD space: a shell that is off until the user allows it natively.

The page names a shell only by id, the lists never keep a command that starts
with a space, and ``update_settings`` can never switch the shell on.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from vaultnotes.api import Api
from vaultnotes.backup.gdrive_auth import TokenStore
from vaultnotes.config import Config
from vaultnotes.terminal import (
    MAX_COMMAND_LENGTH,
    MAX_RECENT,
    MAX_WRITE_LENGTH,
    TerminalError,
    TerminalManager,
    clean_command,
    clean_command_list,
    remember,
    shell_command,
)


class FakeWindow:
    """Answers the native question and records the events sent to the page."""

    def __init__(self, allow: bool = True) -> None:
        self.allow = allow
        self.asked = 0
        self.scripts: list[str] = []
        self._lock = threading.Lock()

    def create_confirmation_dialog(self, title: str, message: str) -> bool:
        self.asked += 1
        return self.allow

    def evaluate_js(self, script: str) -> None:
        with self._lock:
            self.scripts.append(script)

    def output(self) -> str:
        """Everything the terminal printed, decoded from the emitted events."""
        text = []
        with self._lock:
            scripts = list(self.scripts)
        for script in scripts:
            prefix = 'window.vn && window.vn.emit("terminal_output", '
            if script.startswith(prefix):
                text.append(json.loads(script[len(prefix):-2])["data"])
        return "".join(text)


def make_api(tmp_path: Path, window: Any = None) -> Api:
    cfg = Config(settings_path=tmp_path / "settings.json")
    cfg.data["notes_root"] = str(tmp_path / "notes")
    cfg.save()
    cfg.ensure_folders()
    api = Api(config=cfg, app_dir=tmp_path / "appdata", drive_store=TokenStore(memory=True))
    api.set_window(window)
    return api


def saved_terminal(tmp_path: Path) -> dict[str, Any]:
    return json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))["terminal"]


# ----------------------------------------------------------------------
# Commands kept in the lists
# ----------------------------------------------------------------------
def test_clean_command_keeps_one_line_commands() -> None:
    assert clean_command("git status") == "git status"
    assert clean_command("npm run build   ") == "npm run build"


@pytest.mark.parametrize(
    "command",
    [" set TOKEN=abc", "\tsecret", "", "   ", "two\nlines", "bell\x07", None, 42, "x" * (MAX_COMMAND_LENGTH + 1)],
)
def test_clean_command_refuses(command: Any) -> None:
    assert clean_command(command) is None


def test_remember_moves_to_front_and_caps() -> None:
    assert remember(["a", "b", "c"], "b", 10) == ["b", "a", "c"]
    assert remember(["a", "b"], "c", 2) == ["c", "a"]


def test_clean_command_list_drops_junk_and_duplicates() -> None:
    assert clean_command_list(["a", " secret", "a", 3, "b"], 10) == ["a", "b"]
    assert clean_command_list("not a list", 10) == []


# ----------------------------------------------------------------------
# Only known shells, never a path from the page
# ----------------------------------------------------------------------
@pytest.mark.parametrize("shell_id", ["calc", r"C:\Windows\System32\calc.exe", "cmd.exe", "", None])
def test_unknown_shells_are_refused(shell_id: Any) -> None:
    with pytest.raises(TerminalError) as exc:
        shell_command(shell_id)
    assert exc.value.code == "invalid_shell"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows shells")
def test_cmd_is_found_on_windows() -> None:
    assert Path(shell_command("cmd")[0]).name.lower() == "cmd.exe"


# ----------------------------------------------------------------------
# Settings: off by default, repaired when damaged
# ----------------------------------------------------------------------
def test_terminal_is_off_by_default(tmp_path: Path) -> None:
    cfg = Config(settings_path=tmp_path / "settings.json", auto_init_folders=False)
    assert cfg.data["terminal"] == {"enabled": False, "shell": "cmd", "recent": [], "favorites": []}


def test_damaged_terminal_settings_are_repaired(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps({"terminal": {"enabled": "yes", "shell": "calc", "recent": ["ok", " hidden", 5], "favorites": "x"}}),
        encoding="utf-8",
    )
    cfg = Config(settings_path=path, auto_init_folders=False)
    assert cfg.data["terminal"] == {"enabled": False, "shell": "cmd", "recent": ["ok"], "favorites": []}


def test_update_settings_cannot_switch_the_shell_on(tmp_path: Path) -> None:
    api = make_api(tmp_path, FakeWindow())
    result = api.update_settings({"terminal": {"enabled": True}})
    assert result["error"] == "invalid_settings"
    assert api.terminal_state()["enabled"] is False


# ----------------------------------------------------------------------
# Turning it on needs the native question
# ----------------------------------------------------------------------
def test_everything_is_refused_while_off(tmp_path: Path) -> None:
    api = make_api(tmp_path, FakeWindow())
    assert api.terminal_start("cmd")["error"] == "terminal_off"
    assert api.terminal_write("0" * 32, "dir\r")["error"] == "terminal_off"
    assert api.terminal_resize("0" * 32, 80, 24)["error"] == "terminal_off"


def test_saying_no_keeps_it_off(tmp_path: Path) -> None:
    window = FakeWindow(allow=False)
    api = make_api(tmp_path, window)
    assert api.terminal_enable()["error"] == "cancelled"
    assert window.asked == 1
    assert saved_terminal(tmp_path)["enabled"] is False


@pytest.mark.skipif(sys.platform != "win32", reason="needs a Windows shell to offer")
def test_saying_yes_turns_it_on_and_off_again(tmp_path: Path) -> None:
    window = FakeWindow(allow=True)
    api = make_api(tmp_path, window)
    assert api.terminal_enable() == {"ok": True, "enabled": True}
    assert saved_terminal(tmp_path)["enabled"] is True
    # Already on: no second question.
    api.terminal_enable()
    assert window.asked == 1
    assert api.terminal_disable()["enabled"] is False
    assert saved_terminal(tmp_path)["enabled"] is False


def test_no_window_no_shell(tmp_path: Path) -> None:
    api = make_api(tmp_path, None)
    assert api.terminal_enable()["error"] == "no_window"


# ----------------------------------------------------------------------
# Recent and favorite commands
# ----------------------------------------------------------------------
def test_recent_and_favorites_are_saved(tmp_path: Path) -> None:
    api = make_api(tmp_path, FakeWindow())
    api.terminal_remember("git status")
    api.terminal_remember("npm run build")
    result = api.terminal_remember("git status")
    assert result["recent"] == ["git status", "npm run build"]

    skipped = api.terminal_remember(" set TOKEN=abc")
    assert skipped["saved"] is False
    assert " set TOKEN=abc" not in json.dumps(saved_terminal(tmp_path))

    assert api.terminal_set_favorite("npm run build", True)["favorites"] == ["npm run build"]
    assert api.terminal_set_favorite("npm run build", True)["favorites"] == ["npm run build"]
    assert api.terminal_forget("git status")["recent"] == ["npm run build"]
    assert api.terminal_clear_recent()["recent"] == []
    saved = saved_terminal(tmp_path)
    assert saved["recent"] == [] and saved["favorites"] == ["npm run build"]
    assert api.terminal_set_favorite("npm run build", False)["favorites"] == []


def test_recent_is_capped(tmp_path: Path) -> None:
    api = make_api(tmp_path, FakeWindow())
    for n in range(MAX_RECENT + 5):
        api.terminal_remember(f"echo {n}")
    recent = api.terminal_state()["recent"]
    assert len(recent) == MAX_RECENT
    assert recent[0] == f"echo {MAX_RECENT + 4}"


def test_bad_favorite_is_refused(tmp_path: Path) -> None:
    api = make_api(tmp_path, FakeWindow())
    assert api.terminal_set_favorite("rm -rf /\nshutdown", True)["error"] == "invalid_input"


# ----------------------------------------------------------------------
# The manager, with a pretend pseudo-console
# ----------------------------------------------------------------------
class FakePty:
    def __init__(self) -> None:
        self.written: list[str] = []
        self.size = (24, 80)
        self.alive = True
        self._out: list[str] = ["hello\r\n"]
        self.exitstatus = 0

    def read(self, size: int) -> str:
        while self.alive:
            if self._out:
                return self._out.pop(0)
            time.sleep(0.01)
        raise EOFError

    def write(self, data: str) -> None:
        self.written.append(data)

    def setwinsize(self, rows: int, cols: int) -> None:
        self.size = (rows, cols)

    def isalive(self) -> bool:
        return self.alive

    def terminate(self, force: bool = False) -> None:
        self.alive = False


def test_manager_sends_output_and_checks_the_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("vaultnotes.terminal.shell_command", lambda shell_id: ["fake.exe"])
    events: list[tuple[str, Any]] = []
    pty = FakePty()
    spawned: dict[str, Any] = {}

    def spawn(argv: list[str], **kwargs: Any) -> FakePty:
        spawned.update(argv=argv, **kwargs)
        return pty

    manager = TerminalManager(lambda name, data: events.append((name, data)), spawn=spawn)

    started = manager.start("cmd", 9999, 1)
    assert spawned["argv"] == ["fake.exe"]
    assert spawned["dimensions"] == (5, 500)  # rows, cols clamped to the limits
    assert spawned["cwd"] == str(Path.home())
    deadline = time.time() + 2
    while not any(name == "terminal_output" for name, _ in events) and time.time() < deadline:
        time.sleep(0.02)
    assert ("terminal_output", {"id": started["id"], "data": "hello\r\n"}) in events

    manager.write(started["id"], "dir\r")
    assert pty.written == ["dir\r"]
    with pytest.raises(TerminalError):
        manager.write("someone-else", "dir\r")
    with pytest.raises(TerminalError):
        manager.write(started["id"], "x" * (MAX_WRITE_LENGTH + 1))

    manager.resize(started["id"], 10_000, 0)
    assert pty.size == (5, 500)  # clamped to the limits

    manager.stop()
    deadline = time.time() + 2
    while not any(name == "terminal_exit" for name, _ in events) and time.time() < deadline:
        time.sleep(0.02)
    exits = [data for name, data in events if name == "terminal_exit"]
    assert exits and exits[0]["stopped"] is True
    assert manager.running() is None


# ----------------------------------------------------------------------
# A real CMD, end to end (Windows only)
# ----------------------------------------------------------------------
@pytest.mark.skipif(sys.platform != "win32", reason="Windows pseudo-console")
def test_real_cmd_round_trip(tmp_path: Path) -> None:
    pytest.importorskip("winpty")
    window = FakeWindow(allow=True)
    api = make_api(tmp_path, window)
    api.terminal_enable()
    started = api.terminal_start("cmd", 100, 30)
    assert started["ok"] is True
    try:
        # A real terminal answers the pseudo-console's "who are you?" query;
        # xterm.js does this in the app.
        api.terminal_write(started["id"], "\x1b[?1;2c")
        api.terminal_write(started["id"], "echo VN-MARKER-%USERNAME:~0,0%OK\r")
        deadline = time.time() + 15
        while "VN-MARKER-OK" not in window.output() and time.time() < deadline:
            time.sleep(0.1)
        assert "VN-MARKER-OK" in window.output()
        assert api.terminal_state()["running"]["shell"] == "cmd"
    finally:
        api.terminal_stop()
        api.close()
