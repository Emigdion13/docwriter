"""The Editor space: text files opened and saved through native dialogs.

The dialogs are replaced by lambdas, so nothing here opens a window.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vaultnotes.api import Api
from vaultnotes.backup.gdrive_auth import TokenStore
from vaultnotes.config import Config
from vaultnotes.text_editor import (
    MAX_FILE_BYTES,
    EditorError,
    EditorFiles,
    decode,
    detect_eol,
    read_text_file,
    refused_reason,
    write_text_file,
)


def make_api(tmp_path: Path) -> Api:
    cfg = Config(settings_path=tmp_path / "settings.json")
    cfg.data["notes_root"] = str(tmp_path / "notes")
    cfg.save()
    cfg.ensure_folders()
    return Api(
        config=cfg, app_dir=tmp_path / "appdata", drive_store=TokenStore(memory=True),
        ai_key_path=tmp_path / "keys" / "ai-notes.vnkey",
    )


# ----------------------------------------------------------------------
# Reading and writing
# ----------------------------------------------------------------------
def test_detect_eol() -> None:
    assert detect_eol("a\r\nb\nc") == "CRLF"
    assert detect_eol("a\nb\r\n") == "LF"
    assert detect_eol("a\rb") == "CR"


def test_decode_prefers_bom_then_utf8_then_cp1252() -> None:
    assert decode("é".encode("utf-8")) == ("é", "utf-8")
    assert decode(b"\xef\xbb\xbfhi") == ("hi", "utf-8-sig")
    assert decode("hi".encode("utf-16")) == ("hi", "utf-16")
    assert decode(b"caf\xe9") == ("café", "cp1252")


def test_a_crlf_file_round_trips_byte_for_byte(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    original = "line one\r\nlíne two\r\n".encode("utf-8-sig")
    path.write_bytes(original)
    content = read_text_file(path)
    assert content == {"text": "line one\nlíne two\n", "encoding": "utf-8-sig", "eol": "CRLF"}
    write_text_file(path, content["text"], encoding=content["encoding"], eol=content["eol"])
    assert path.read_bytes() == original
    assert [p.name for p in tmp_path.iterdir()] == ["a.txt"]  # no temp file left behind


def test_binary_and_huge_files_are_refused(tmp_path: Path) -> None:
    binary = tmp_path / "x.bin"
    binary.write_bytes(b"MZ\x00\x01\x02")
    with pytest.raises(EditorError) as err:
        read_text_file(binary)
    assert err.value.code == "binary"
    huge = tmp_path / "huge.txt"
    with huge.open("wb") as handle:
        handle.truncate(MAX_FILE_BYTES + 1)
    with pytest.raises(EditorError) as err:
        read_text_file(huge)
    assert err.value.code == "too_big"


def test_text_cp1252_cannot_hold_is_reported_not_mangled(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    path.write_bytes(b"old")
    with pytest.raises(EditorError) as err:
        write_text_file(path, "emoji 😀", encoding="cp1252", eol="LF")
    assert err.value.code == "encoding"
    assert path.read_bytes() == b"old"


def test_keys_and_vault_folders_are_refused(tmp_path: Path) -> None:
    vault = tmp_path / "vaults" / "encrypted"
    assert refused_reason(tmp_path / "k.VNKEY", []) is not None
    assert refused_reason(vault / "note.md", [vault]) is not None
    assert refused_reason(tmp_path / "ok.txt", [vault]) is None


def test_open_save_and_save_as_go_by_id(tmp_path: Path) -> None:
    files = EditorFiles()
    path = tmp_path / "a.txt"
    path.write_bytes(b"hi\n")  # write_text would turn \n into \r\n on Windows
    opened = files.open(path, [])
    assert opened["text"] == "hi\n" and opened["eol"] == "LF" and opened["name"] == "a.txt"
    assert files.open(path, [])["id"] == opened["id"]  # the same file keeps one id
    files.save(opened["id"], "bye\n", "CRLF", "utf-8", [])
    assert path.read_bytes() == b"bye\r\n"
    other = tmp_path / "b.txt"
    moved = files.save_as(other, "b\n", "LF", "utf-8", [], old_id=opened["id"])
    assert other.read_bytes() == b"b\n" and moved["name"] == "b.txt"
    with pytest.raises(EditorError):
        files.save(opened["id"], "x", "LF", "utf-8", [])  # the old id was given up
    with pytest.raises(EditorError):
        files.save("not-an-id", "x", "LF", "utf-8", [])
    with pytest.raises(EditorError):
        files.save(moved["id"], "x", "\n", "utf-8", [])  # line ending names only


# ----------------------------------------------------------------------
# Through the Bridge, with the dialogs replaced
# ----------------------------------------------------------------------
def test_the_bridge_opens_and_saves_picked_files(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    path = tmp_path / "work" / "query.sql"
    path.parent.mkdir()
    path.write_bytes(b"SELECT 1;\r\n")
    api._editor_pick_open = lambda: [path]
    res = api.editor_open()
    assert res["ok"] is True and res["failed"] == []
    f = res["files"][0]
    assert (f["name"], f["text"], f["eol"]) == ("query.sql", "SELECT 1;\n", "CRLF")
    assert api.editor_save(f["id"], "SELECT 2;\n", "CRLF", "utf-8")["ok"] is True
    assert path.read_bytes() == b"SELECT 2;\r\n"

    target = tmp_path / "work" / "new.txt"
    api._editor_pick_save = lambda name: target
    saved = api.editor_save_as(None, "fresh", "LF", "utf-8", "new 1.txt")
    assert saved["ok"] is True and target.read_text(encoding="utf-8") == "fresh"
    assert api.editor_close(saved["file"]["id"]) == {"ok": True}
    assert api.editor_save(saved["file"]["id"], "x", "LF", "utf-8")["error"] == "not_found"


def test_the_bridge_refuses_vault_and_ai_notes_files(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    root = api.config.notes_root
    inside_vault = root / "vaults" / "encrypted" / "secret.txt"
    inside_vault.parent.mkdir(parents=True, exist_ok=True)
    inside_vault.write_text("phi", encoding="utf-8")
    inside_ai = api.config.ai_dir / "note.md"
    inside_ai.parent.mkdir(parents=True, exist_ok=True)
    inside_ai.write_text("x", encoding="utf-8")
    api._editor_pick_open = lambda: [inside_vault, inside_ai]
    res = api.editor_open()
    assert res["files"] == [] and len(res["failed"]) == 2
    api._editor_pick_save = lambda name: inside_vault
    assert api.editor_save_as(None, "overwrite", "LF", "utf-8", "x.txt")["error"] == "refused"
    assert inside_vault.read_text(encoding="utf-8") == "phi"


def test_a_cancelled_dialog_says_so(tmp_path: Path) -> None:
    api = make_api(tmp_path)  # no window: the pickers return nothing
    assert api.editor_open()["error"] == "cancelled"
    assert api.editor_save_as(None, "text", "LF", "utf-8", "a.txt")["error"] == "cancelled"
