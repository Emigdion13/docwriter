"""The notes CLI: Plain and Personal read-only, AI-Notes writable, Encrypted never opened."""

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


def cli(*args: str, stdin=None) -> tuple[int, str, str]:
    out = io.StringIO()
    err = io.StringIO()
    import contextlib

    with contextlib.redirect_stderr(err):
        code = notes_cli.run(list(args), out=out, stdin=stdin)
    return code, out.getvalue(), err.getvalue()


def snapshot(folder: Path) -> dict[str, tuple[int, int]]:
    return {
        str(path.relative_to(folder)): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in folder.rglob("*")
    }


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


def test_reading_writes_nothing(notes_root: Path, tmp_path: Path) -> None:
    before = snapshot(tmp_path)
    key = str(notes_root.parent / "keys" / "personal.vnkey")
    for command in (
        ["list", "plain"],
        ["search", "personal", "friday"],
        ["read", "personal", "Trip plan"],
        ["list", "ai"],  # no AI-Notes folder yet: empty, and still not made
        ["search", "AI-Notes", "x"],
    ):
        assert cli("--root", str(notes_root), "--personal-key", key, *command)[0] == 0
    empty = tmp_path / "not-a-notes-folder"
    assert cli("--root", str(empty), "list", "plain")[0] == notes_cli.EXIT_ERROR
    assert cli("--root", str(empty), "list", "ai")[0] == notes_cli.EXIT_ERROR
    assert snapshot(tmp_path) == before
    assert not empty.exists()


def test_unknown_and_ambiguous_names_explain_themselves(notes_root: Path) -> None:
    code, _, err = cli("--root", str(notes_root), "list", "work")
    assert code == notes_cli.EXIT_ERROR and "plain, personal, ai" in err
    code, _, err = cli("--root", str(notes_root), "read", "plain", "list")
    assert code == notes_cli.EXIT_ERROR and "2 notes match" in err
    code, _, err = cli("--root", str(notes_root), "read", "plain", "nothing like it")
    assert code == notes_cli.EXIT_ERROR and "No note matches" in err


# ----------------------------------------------------------------------
# AI-Notes: the one space this tool writes
# ----------------------------------------------------------------------
def test_writes_create_append_replace_and_trash_ai_notes(notes_root: Path) -> None:
    root = str(notes_root)
    ai = notes_root / "ai-notes"

    code, out, err = cli("--root", root, "write", "ai", "PR 42: review", "--text", "# PR 42\n\nLooks good.")
    assert code == 0, err
    assert out.strip() == 'Created "PR 42- review" in AI-Notes.'
    assert (ai / "PR 42- review.md").read_text(encoding="utf-8") == "# PR 42\n\nLooks good.\n"
    assert (ai / "About AI-Notes.md").is_file(), "the first write brings the guide note"

    # The title as typed finds the note it became.
    code, out, _ = cli("--root", root, "append", "ai", "PR 42: review", "--text", "\n- one nit\n")
    assert code == 0 and out.startswith('Appended to "PR 42- review"')
    assert (ai / "PR 42- review.md").read_text(encoding="utf-8") == "# PR 42\n\nLooks good.\n\n- one nit\n"

    code, _, err = cli("--root", root, "write", "ai", "pr 42- REVIEW", "--text", "gone?")
    assert code == notes_cli.EXIT_ERROR and "--replace" in err
    code, out, _ = cli("--root", root, "write", "ai", "PR 42- review", "--text", "# Rewritten", "--replace")
    assert code == 0 and out.startswith('Replaced "PR 42- review"')
    assert (ai / "PR 42- review.md").read_text(encoding="utf-8") == "# Rewritten\n"

    code, out, _ = cli("--root", root, "read", "ai", "rewritten")
    assert code == notes_cli.EXIT_ERROR  # read matches titles, not text
    code, out, _ = cli("--root", root, "read", "ai", "pr 42")
    assert code == 0 and "# Rewritten" in out

    code, out, _ = cli("--root", root, "delete", "ai", "PR 42- review")
    assert code == 0 and "trash" in out
    assert not (ai / "PR 42- review.md").exists()
    assert (ai / ".trash" / "PR 42- review.md").is_file()
    assert [line.split("\t")[0] for line in cli("--root", root, "list", "ai")[1].splitlines()] == ["About AI-Notes"]


def test_append_creates_a_missing_note_and_never_guesses(notes_root: Path) -> None:
    root = str(notes_root)
    assert cli("--root", root, "append", "ai", "Session log", "--text", "- started")[0] == 0
    assert cli("--root", root, "append", "ai", "session LOG", "--text", "- finished")[0] == 0
    log = notes_root / "ai-notes" / "Session log.md"
    assert log.read_text(encoding="utf-8") == "- started\n\n- finished\n"

    # A fragment is a new title for append, and not enough for delete.
    assert cli("--root", root, "append", "ai", "Session", "--text", "other")[0] == 0
    assert (notes_root / "ai-notes" / "Session.md").is_file()
    code, _, err = cli("--root", root, "delete", "ai", "log")
    assert code == notes_cli.EXIT_ERROR and "exact title" in err
    assert log.is_file()


def test_text_comes_from_stdin_or_a_file(notes_root: Path, tmp_path: Path) -> None:
    root = str(notes_root)
    piped = io.BytesIO("\ufeff# Piped\r\n\r\nCafé ✓\r\n".encode("utf-8"))
    assert cli("--root", root, "write", "ai", "Piped", stdin=piped)[0] == 0
    assert (notes_root / "ai-notes" / "Piped.md").read_text(encoding="utf-8") == "# Piped\n\nCafé ✓\n"

    draft = tmp_path / "draft.md"
    draft.write_text("# From a file\n\nÜber\n", encoding="utf-8-sig")
    assert cli("--root", root, "write", "ai", "From a file", "--file", str(draft))[0] == 0
    assert (notes_root / "ai-notes" / "From a file.md").read_text(encoding="utf-8") == "# From a file\n\nÜber\n"

    code, _, err = cli("--root", root, "write", "ai", "Empty", stdin=io.BytesIO(b"  \n"))
    assert code == notes_cli.EXIT_ERROR and "empty" in err
    code, _, err = cli("--root", root, "write", "ai", "Both", "--text", "a", "--file", str(draft))
    assert code == notes_cli.EXIT_ERROR and "not both" in err
    assert not (notes_root / "ai-notes" / "Empty.md").exists()


@pytest.mark.parametrize(
    "command",
    [
        ["write", "plain", "Findings", "--text", "x"],
        ["append", "plain", "Shopping list", "--text", "x"],
        ["delete", "plain", "Shopping list"],
        ["write", "personal", "Findings", "--text", "x"],
        ["delete", "Personal", "Trip plan"],
    ],
)
def test_plain_and_personal_are_read_only(notes_root: Path, tmp_path: Path, command: list[str]) -> None:
    before = snapshot(tmp_path)
    code, out, err = cli("--root", str(notes_root), *command)
    assert code == notes_cli.EXIT_REFUSED and out == ""
    assert "read-only" in err and "write ai" in err
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("space", ["encrypted", "Encrypted", "vaults/encrypted"])
def test_writing_to_encrypted_is_refused_before_anything_opens(
    notes_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, space: str
) -> None:
    monkeypatch.setattr(notes_cli, "VaultStore", lambda *a, **k: pytest.fail("opened a vault"))
    before = snapshot(tmp_path)
    for command in (["write", space, "x", "--text", "y"], ["append", space, "x", "--text", "y"], ["delete", space, "x"]):
        code, out, err = cli("--root", str(notes_root), *command)
        assert code == notes_cli.EXIT_REFUSED and "PHI" in err and out == ""
    assert snapshot(tmp_path) == before


def test_key_material_never_becomes_a_note(notes_root: Path, tmp_path: Path) -> None:
    root = str(notes_root)
    key = notes_root.parent / "keys" / "personal.vnkey"
    renamed = tmp_path / "key.txt"
    renamed.write_bytes(key.read_bytes())
    vault_file = next((notes_root / "vaults" / "personal").glob("*.vnote"))
    header = notes_root / "vaults" / "personal" / "vault.json"

    for source in (key, renamed, vault_file, header):
        code, out, err = cli("--root", root, "write", "ai", "Backup of my key", "--file", str(source))
        assert code == notes_cli.EXIT_REFUSED, source
        assert "never copied" in err and out == ""
    code, _, _ = cli("--root", root, "append", "ai", "Keys", stdin=io.BytesIO(key.read_bytes()))
    assert code == notes_cli.EXIT_REFUSED
    assert not (notes_root / "ai-notes").exists()


def test_a_wrong_root_never_grows_an_ai_notes_folder(tmp_path: Path) -> None:
    somewhere = tmp_path / "Documents"
    somewhere.mkdir()
    code, _, err = cli("--root", str(somewhere), "write", "ai", "Findings", "--text", "x")
    assert code == notes_cli.EXIT_ERROR and "Is --root the notes folder" in err
    assert list(somewhere.iterdir()) == []


def test_delete_before_any_write_changes_nothing(notes_root: Path, tmp_path: Path) -> None:
    before = snapshot(tmp_path)
    code, _, err = cli("--root", str(notes_root), "delete", "ai", "Anything")
    assert code == notes_cli.EXIT_ERROR and "no note titled" in err
    assert snapshot(tmp_path) == before
