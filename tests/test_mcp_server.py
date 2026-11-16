"""The VM server: VaultNotes as an MCP server on the Hyper-V Default Switch."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from vaultnotes import mcp_server
from vaultnotes.mcp_server import (
    TOOLS,
    WITHHELD,
    McpServer,
    ToolInputError,
    find_switch_address,
    handle_message,
    load_token,
    run_tool,
    tool_argv,
)
from vaultnotes.storage.plain_store import PlainStore


def same(text: str) -> str:
    return text


@pytest.fixture
def root(tmp_path: Path) -> Path:
    folder = tmp_path / "VaultNotes"
    plain = PlainStore(folder / "plain")
    plain.create_note("Shopping list", "# Shopping list\n\n- flour\n- eggs\n")
    (folder / "vaults" / "encrypted").mkdir(parents=True)
    return folder


def call(root: Path, name: str, args: dict[str, Any], redact=same) -> tuple[str, bool]:
    return run_tool(name, args, root, redact)


# ----------------------------------------------------------------------
# Tools are notes.py command lines
# ----------------------------------------------------------------------
def test_tool_arguments_become_a_safe_command_line() -> None:
    assert tool_argv("read_note", {"space": "plain", "note": "-x"}) == ["read", "--", "plain", "-x"]
    assert tool_argv("write_note", {"title": "T", "text": "--replace", "replace": True}) == [
        "write", "--text=--replace", "--replace", "--", "ai", "T"]
    assert tool_argv("list_notes", {"space": "ai", "tags": ["a", "b"]}) == ["list", "--tag=a", "--tag=b", "--", "ai"]
    assert tool_argv("mark_note", {"title": "T", "important": False}) == ["mark", "--clear", "--", "ai", "T"]
    assert tool_argv("tag_note", {"title": "T", "tags": ["x"], "remove": True}) == ["tag", "--remove", "--", "ai", "T", "x"]


@pytest.mark.parametrize("name, args", [
    ("read_note", {"space": "encrypted", "note": "x"}),
    ("read_note", {"space": "personal", "note": "x"}),
    ("read_note", {"space": "plain"}),
    ("list_notes", {"space": "plain", "tags": "work"}),
    ("tag_note", {"title": "T", "tags": []}),
    ("write_note", {"title": "T", "text": "x", "replace": "yes"}),
    ("delete_note", {"title": "T"}),
    ("read_note", "not an object"),
])
def test_bad_tool_arguments_are_refused(name: str, args: Any) -> None:
    with pytest.raises(ToolInputError):
        tool_argv(name, args)


def test_every_tool_has_a_command_line() -> None:
    sample = {"space": "plain", "note": "n", "text": "t", "title": "T", "tags": ["x"]}
    for tool in TOOLS:
        args = {k: sample[k] for k in tool["inputSchema"]["properties"] if k in tool["inputSchema"].get("required", [])}
        assert tool_argv(tool["name"], args)


def test_reading_and_writing_go_through_notes_py(root: Path) -> None:
    text, failed = call(root, "list_notes", {"space": "plain"})
    assert not failed and text.startswith("Shopping list")
    text, failed = call(root, "read_note", {"space": "plain", "note": "shopping"})
    assert not failed and "- flour" in text
    text, failed = call(root, "write_note", {"title": "DATAINGEST-1 — From the VM", "text": "# Hi\n"})
    assert not failed and "Created" in text
    text, failed = call(root, "append_note", {"title": "DATAINGEST-1 — From the VM", "text": "more"})
    assert not failed and "Appended" in text
    text, failed = call(root, "read_note", {"space": "ai", "note": "DATAINGEST-1"})
    assert "more" in text
    text, failed = call(root, "tag_note", {"title": "DATAINGEST-1 — From the VM", "tags": ["vm"]})
    assert not failed and "#vm" in text


def test_a_missing_note_is_an_error_not_a_crash(root: Path) -> None:
    text, failed = call(root, "read_note", {"space": "plain", "note": "nothing like it"})
    assert failed and "No note matches" in text


def test_replies_go_through_the_phi_filter(root: Path) -> None:
    PlainStore(root / "plain").create_note("Intake", "# Intake\n\nSSN 078-05-1120\n")
    text, _ = call(root, "read_note", {"space": "plain", "note": "Intake"}, redact=lambda t: t.replace("078-05-1120", "⟦PHI:SSN:x⟧"))
    assert "078-05-1120" not in text and "⟦PHI:SSN:x⟧" in text


def test_the_real_phi_filter_tokenises(root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PHI_HOME", str(tmp_path / "phi"))
    PlainStore(root / "plain").create_note("Intake", "# Intake\n\nPatient SSN 078-05-1120, MRN 55512345\n")
    text, _ = call(root, "read_note", {"space": "plain", "note": "Intake"}, redact=mcp_server.phi_redact)
    assert "078-05-1120" not in text and "⟦PHI:" in text


def test_a_failing_filter_withholds_the_reply(root: Path) -> None:
    def broken(_: str) -> str:
        raise RuntimeError("boom")

    assert call(root, "read_note", {"space": "plain", "note": "shopping"}, redact=broken) == (WITHHELD, True)


# ----------------------------------------------------------------------
# JSON-RPC
# ----------------------------------------------------------------------
def rpc(method: str, params: dict[str, Any] | None = None, msg_id: Any = 1) -> dict[str, Any]:
    msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if msg_id is not None:
        msg["id"] = msg_id
    return msg


def test_initialize_list_and_call(root: Path) -> None:
    init = handle_message(rpc("initialize", {"protocolVersion": "2025-03-26"}), root, same)
    assert init["result"]["protocolVersion"] == "2025-03-26"
    assert init["result"]["capabilities"] == {"tools": {"listChanged": False}}
    assert handle_message(rpc("initialize", {"protocolVersion": "1999-01-01"}), root, same)["result"][
        "protocolVersion"] == mcp_server.PROTOCOL_VERSIONS[0]
    assert handle_message(rpc("notifications/initialized", msg_id=None), root, same) is None
    names = [t["name"] for t in handle_message(rpc("tools/list"), root, same)["result"]["tools"]]
    assert "read_note" in names and not any("delete" in n for n in names)
    res = handle_message(rpc("tools/call", {"name": "list_notes", "arguments": {"space": "plain"}}), root, same)
    assert res["result"]["isError"] is False and "Shopping list" in res["result"]["content"][0]["text"]
    assert handle_message(rpc("ping"), root, same)["result"] == {}


def test_unknown_methods_and_tools_are_errors(root: Path) -> None:
    assert handle_message(rpc("resources/list"), root, same)["error"]["code"] == -32601
    assert handle_message(rpc("tools/call", {"name": "delete_note"}), root, same)["error"]["code"] == -32602
    assert handle_message({"method": "ping", "id": 1}, root, same)["error"]["code"] == -32600
    assert handle_message("junk", root, same)["error"]["code"] == -32600


# ----------------------------------------------------------------------
# Where it listens, and who may call
# ----------------------------------------------------------------------
def test_only_a_private_address_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    assert mcp_server._private_v4("172.25.240.1") == "172.25.240.1"
    for bad in ("0.0.0.0", "127.0.0.1", "8.8.8.8", "169.254.1.1", "::1", "nonsense", ""):
        assert mcp_server._private_v4(bad) is None
    monkeypatch.setattr(mcp_server.sys, "platform", "linux")
    monkeypatch.setattr(mcp_server.socket, "gethostbyname", lambda name: "8.8.8.8")
    assert find_switch_address() is None


def test_the_token_is_made_once_and_can_be_renewed(tmp_path: Path) -> None:
    first = load_token(tmp_path)
    assert len(first) >= 32 and load_token(tmp_path) == first
    assert load_token(tmp_path, renew=True) != first


def test_no_switch_means_no_server(tmp_path: Path, root: Path) -> None:
    server = McpServer(lambda: root, tmp_path, find_host=lambda: None, redact=same, port=0)
    server.start()
    assert not server.running and "Default Switch" in server.error
    assert server.status()["command"] is None


@pytest.fixture
def live(tmp_path: Path, root: Path):
    server = McpServer(lambda: root, tmp_path / "token", find_host=lambda: "127.0.0.1", redact=same, port=0)
    # 127.0.0.1 stands in for the switch address here: tests never open a real port elsewhere.
    server.start()
    assert server.running, server.error
    server.port = server._server.server_address[1]
    yield server
    server.stop()


def post(server: McpServer, body: Any, headers: dict[str, str] | None = None, path: str = "/mcp") -> tuple[int, Any]:
    data = json.dumps(body).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{server.port}{path}", data=data, method="POST",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        return exc.code, None


def auth(server: McpServer) -> dict[str, str]:
    return {"Authorization": f"Bearer {load_token(server._token_dir)}"}


def test_the_server_answers_with_the_token_only(live: McpServer) -> None:
    status, reply = post(live, rpc("tools/list"), auth(live))
    assert status == 200 and len(reply["result"]["tools"]) == len(TOOLS)
    assert post(live, rpc("tools/list"))[0] == 401
    assert post(live, rpc("tools/list"), {"Authorization": "Bearer wrong"})[0] == 401
    assert post(live, rpc("tools/list"), {**auth(live), "Origin": "http://evil.example"})[0] == 403
    assert post(live, rpc("tools/list"), auth(live), path="/other")[0] == 404
    assert post(live, rpc("notifications/initialized", msg_id=None), auth(live))[0] == 202
    assert post(live, [rpc("ping")], auth(live))[0] == 400


def test_a_new_token_locks_the_old_one_out(live: McpServer) -> None:
    old = auth(live)
    live.renew_token()
    assert post(live, rpc("ping"), old)[0] == 401
    assert post(live, rpc("ping"), auth(live))[0] == 200


def test_status_shows_both_commands(live: McpServer) -> None:
    status = live.status()
    assert status["running"] and status["url"].endswith(".mshome.net:%d/mcp" % live.port)
    assert load_token(live._token_dir) in status["command"]
    assert "-InterfaceAlias 'vEthernet (Default Switch)'" in status["firewall"]
    live.stop()
    assert not live.running and live.status()["command"] is None


# ----------------------------------------------------------------------
# The app's switch
# ----------------------------------------------------------------------
class FakeWindow:
    def __init__(self, answer: bool) -> None:
        self.answer = answer

    def create_confirmation_dialog(self, title: str, message: str) -> bool:
        return self.answer


def make_api(tmp_path: Path, root: Path, window: Any):
    from vaultnotes.api import Api
    from vaultnotes.backup.gdrive_auth import TokenStore
    from vaultnotes.config import Config

    config = Config(settings_path=tmp_path / "settings.json")
    config.data["notes_root"] = str(root)
    config.save()
    config.ensure_folders()
    server = McpServer(lambda: root, tmp_path / "token", find_host=lambda: "127.0.0.1", redact=same, port=0)
    api = Api(config=config, app_dir=tmp_path / "app", sql_dir=tmp_path / "sql",
              drive_store=TokenStore(memory=True), mcp_server=server)
    api.set_window(window)
    return api


def test_it_stays_off_until_windows_says_yes(tmp_path: Path, root: Path) -> None:
    api = make_api(tmp_path, root, FakeWindow(False))
    try:
        assert api.mcp_state()["enabled"] is False
        assert api.mcp_enable()["error"] == "cancelled"
        assert not api.mcp.running
        api.window = FakeWindow(True)
        on = api.mcp_enable()
        assert on["ok"] and on["enabled"] and on["running"]
        assert json.loads((tmp_path / "settings.json").read_text())["mcp"] == {"enabled": True}
        off = api.mcp_disable()
        assert off["enabled"] is False and off["running"] is False
        # The page cannot switch it on behind the dialog's back.
        assert api.update_settings({"mcp": {"enabled": True}})["error"] == "invalid_settings"
    finally:
        api.close()


def test_a_damaged_setting_counts_as_off(tmp_path: Path) -> None:
    from vaultnotes.config import Config

    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"mcp": {"enabled": "yes"}}), encoding="utf-8")
    assert Config(settings_path=path, auto_init_folders=False).data["mcp"] == {"enabled": False}
