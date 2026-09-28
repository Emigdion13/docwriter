"""The read-only notes CLI: Plain and Personal open, Encrypted never does."""

from __future__ import annotations

import io
import uuid
from pathlib import Path

import pytest

from vaultnotes import notes_cli
from vaultnotes.crypto.keyfile import generate_key_file
from vaultnotes.storage.plain_store import PlainStore
from vaultnotes.storage.vault_store import VaultStore

SECRET_TITLE = "Patient intake 0042"  # stands in for PHI; must never be printed


def _vault(root: Path, space: str, name: str, key_path: Path, notes: dict[str, str], passphrase: str = "") -> None:
    vault_id = str(uuid.uuid4())
    key = generate_key_file(key_path, vault_id, name, passphrase=passphrase)
    store = VaultStore.create_vault(root / "vaults" / space, key, vault_id=vault_id, name=name)
    store.unlock(key)
    for title, body in notes.items():
        store.create_note(title, body)
    store.lock()


@pytest.fixture
def notes_root(tmp_path: Path) -> Path:
    root = tmp_path / "VaultNotes"
    plain = PlainStore(root / "plain")
    plain.create_note("Shopping list", "# Shopping list\n\n- flour\n- eggs\n")
    plain.create_note("Reading list", "# Reading list\n\nDune\n")
    keys = tmp_path / "keys"
    _vault(root, "personal", "Personal", keys / "personal.vnkey", {"Trip plan": "# Trip plan\n\nflights on Friday\n"})
    _vault(root, "encrypted", "Encrypted", keys / "encrypted.vnkey", {SECRET_TITLE: "# Intake\n\nDOB 1970-01-01\n"})
    return root


def cli(*args: str) -> tuple[int, str, str]:
    out = io.StringIO()
    err = io.StringIO()
    import contextlib

    with contextlib.redirect_stderr(err):
        code = notes_cli.run(list(args), out=out)
    return code, out.getvalue(), err.getvalue()


def test_lists_reads_and_searches_plain(notes_root: Path) -> None:
    code, out, _ = cli("--root", str(notes_root), "list", "plain")
    assert code == 0
    assert [line.split("\t")[0] for line in out.splitlines()] == ["Reading list", "Shopping list"]

    code, out, _ = cli("--root", str(notes_root), "read", "plain", "shopping")
    assert code == 0 and "- eggs" in out

    code, out, _ = cli("--root", str(notes_root), "search", "plain", "DUNE")
    assert code == 0 and out.splitlines() == ["Reading list  (Reading list)", "    Dune"]


def test_reads_personal_with_its_key(notes_root: Path) -> None:
    key = notes_root.parent / "keys" / "personal.vnkey"
    code, out, err = cli("--root", str(notes_root), "--personal-key", str(key), "read", "personal", "trip")
    assert code == 0, err
    assert "flights on Friday" in out


@pytest.mark.parametrize("space", ["encrypted", "Encrypted", " ENCRYPTED ", "vaults/encrypted", "vaults\\encrypted"])
def test_the_encrypted_vault_is_refused_before_anything_opens(
    notes_root: Path, monkeypatch: pytest.MonkeyPatch, space: str
) -> None:
    opened: list[str] = []
    monkeypatch.setattr(notes_cli, "VaultStore", lambda *a, **k: opened.append(str(a)) or pytest.fail("opened a vault"))
    monkeypatch.setattr(notes_cli, "load_key_file", lambda *a, **k: pytest.fail("loaded a key"))
    key = notes_root.parent / "keys" / "encrypted.vnkey"

    for command in (["list", space], ["read", space, "intake"], ["search", space, "DOB"]):
        code, out, err = cli("--root", str(notes_root), "--personal-key", str(key), *command)
        assert code == notes_cli.EXIT_REFUSED, (command, err)
        assert "PHI" in err and out == ""
        assert SECRET_TITLE not in err
    assert opened == []


def test_the_encrypted_key_is_never_used_for_personal(notes_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notes_cli, "load_key_file", lambda *a, **k: pytest.fail("loaded a key"))
    encrypted_key = notes_root.parent / "keys" / "encrypted.vnkey"
    renamed = notes_root.parent / "keys" / "totally-personal.vnkey"  # same key, harmless name
    renamed.write_bytes(encrypted_key.read_bytes())

    for key in (encrypted_key, renamed):
        code, out, err = cli("--root", str(notes_root), "--personal-key", str(key), "list", "personal")
        assert code == notes_cli.EXIT_REFUSED, err
        assert out == "" and SECRET_TITLE not in err


def test_a_wrapped_personal_key_is_not_opened(tmp_path: Path) -> None:
    root = tmp_path / "VaultNotes"
    key = tmp_path / "personal.vnkey"
    _vault(root, "personal", "Personal", key, {"Trip plan": "x"}, passphrase="correct horse")
    code, out, err = cli("--root", str(root), "--personal-key", str(key), "list", "personal")
    assert code == notes_cli.EXIT_ERROR and out == ""
    assert "passphrase" in err


def test_nothing_is_written(notes_root: Path, tmp_path: Path) -> None:
    def snapshot() -> dict[str, tuple[int, int]]:
        return {
            str(path.relative_to(tmp_path)): (path.stat().st_size, path.stat().st_mtime_ns)
            for path in tmp_path.rglob("*")
        }

    before = snapshot()
    key = str(notes_root.parent / "keys" / "personal.vnkey")
    for command in (["list", "plain"], ["search", "personal", "friday"], ["read", "personal", "Trip plan"]):
        assert cli("--root", str(notes_root), "--personal-key", key, *command)[0] == 0
    empty = tmp_path / "not-a-notes-folder"
    assert cli("--root", str(empty), "list", "plain")[0] == notes_cli.EXIT_ERROR
    assert snapshot() == before
    assert not empty.exists()


def test_unknown_and_ambiguous_names_explain_themselves(notes_root: Path) -> None:
    code, _, err = cli("--root", str(notes_root), "list", "work")
    assert code == notes_cli.EXIT_ERROR and "plain, personal" in err
    code, _, err = cli("--root", str(notes_root), "read", "plain", "list")
    assert code == notes_cli.EXIT_ERROR and "2 notes match" in err
    code, _, err = cli("--root", str(notes_root), "read", "plain", "nothing like it")
    assert code == notes_cli.EXIT_ERROR and "No note matches" in err
