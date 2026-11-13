"""The one-time move of AI-Notes from plain .md files into its encrypted vault."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from vaultnotes import migrate_ai
from vaultnotes.migrate_ai import MigrationError, migrate
from vaultnotes.storage.ai_store import EncryptedAIStore


@pytest.fixture
def root(tmp_path: Path) -> Path:
    notes = tmp_path / "notes"
    (notes / "plain").mkdir(parents=True)
    ai = notes / "ai-notes"
    (ai / ".trash").mkdir(parents=True)
    (ai / "Findings.md").write_text("---\ntags: [pr-review]\nimportant: true\n---\n# Findings\n\nok", encoding="utf-8", newline="")
    (ai / "Older.md").write_text("# Older", encoding="utf-8", newline="")
    (ai / ".trash" / "Thrown away.md").write_text("bye", encoding="utf-8", newline="")
    os.utime(ai / "Older.md", (1_600_000_000, 1_600_000_000))
    return notes


def _all(folder: Path, key: Path) -> tuple[dict[str, str], list[str]]:
    store = EncryptedAIStore(folder, key)
    try:
        return {n.title: n.body for n in store.list_notes()}, [n.title for n in store.list_trash()]
    finally:
        store.close()


def test_dry_run_changes_nothing(root: Path, tmp_path: Path) -> None:
    before = sorted(p.name for p in (root / "ai-notes").rglob("*"))
    report = migrate(root, tmp_path / "k.vnkey", tmp_path / "moved", dry_run=True)
    assert sorted(report.active) == ["Findings", "Older"] and report.trashed == ["Thrown away"]
    assert sorted(p.name for p in (root / "ai-notes").rglob("*")) == before
    assert not (tmp_path / "k.vnkey").exists() and not (tmp_path / "moved").exists()


def test_migration_encrypts_everything_and_moves_the_plain_copies_out(root: Path, tmp_path: Path) -> None:
    key = tmp_path / "local" / "ai-notes.vnkey"
    report = migrate(root, key, tmp_path / "moved")
    folder = root / "ai-notes"

    assert list(folder.rglob("*.md")) == []
    assert (folder / "vault.json").is_file() and key.is_file()
    notes, trash = _all(folder, key)
    assert notes == {
        "Findings": "---\ntags: [pr-review]\nimportant: true\n---\n# Findings\n\nok",
        "Older": "# Older",
    }
    assert trash == ["Thrown away"]
    # The modified time came along: the old note still sorts last.
    store = EncryptedAIStore(folder, key)
    assert [n.title for n in store.list_notes(sort="modified")] == ["Findings", "Older"]
    store.close()

    assert report.moved_to is not None and report.moved_to.parent == tmp_path / "moved"
    assert sorted(p.name for p in report.moved_to.rglob("*.md")) == ["Findings.md", "Older.md", "Thrown away.md"]
    assert (report.moved_to / ".trash" / "Thrown away.md").is_file()


def test_a_failed_check_undoes_everything(root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    key = tmp_path / "k.vnkey"
    real_iter_all = EncryptedAIStore.iter_all

    def lose_one(self):
        items = list(real_iter_all(self))
        return items[1:]

    monkeypatch.setattr(EncryptedAIStore, "iter_all", lose_one)
    with pytest.raises(MigrationError, match="did not match"):
        migrate(root, key, tmp_path / "moved")

    folder = root / "ai-notes"
    assert sorted(p.name for p in folder.rglob("*.md")) == ["Findings.md", "Older.md", "Thrown away.md"]
    assert not (folder / "vault.json").exists() and not list(folder.rglob("*.vnote"))
    assert not key.exists() and not (tmp_path / "moved").exists()


def test_an_existing_ai_key_is_reused_and_kept_on_failure(root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    key = tmp_path / "k.vnkey"
    EncryptedAIStore(tmp_path / "other-notes" / "ai-notes", key, create=True).close()
    before = key.read_bytes()

    monkeypatch.setattr(EncryptedAIStore, "iter_all", lambda self: iter(()))
    with pytest.raises(MigrationError):
        migrate(root, key, tmp_path / "moved")
    assert key.read_bytes() == before  # not this run's key, so never removed

    monkeypatch.undo()
    migrate(root, key, tmp_path / "moved")
    assert key.read_bytes() == before
    assert set(_all(root / "ai-notes", key)[0]) == {"Findings", "Older"}


def test_refusals(root: Path, tmp_path: Path) -> None:
    with pytest.raises(MigrationError, match="notes folder"):
        migrate(tmp_path / "nowhere", tmp_path / "k.vnkey")
    with pytest.raises(MigrationError, match="inside the notes folder"):
        migrate(root, root / "plain" / "k.vnkey")

    migrate(root, tmp_path / "k.vnkey", tmp_path / "moved")
    with pytest.raises(MigrationError, match="already encrypted"):
        migrate(root, tmp_path / "k.vnkey", tmp_path / "moved")
    (root / "ai-notes" / "Stray.md").write_text("x", encoding="utf-8", newline="")
    with pytest.raises(MigrationError, match="plain .md"):
        migrate(root, tmp_path / "k.vnkey", tmp_path / "moved")


def test_the_command_line(root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    args = ["--root", str(root), "--key", str(tmp_path / "k.vnkey"), "--backup-dir", str(tmp_path / "moved")]
    assert migrate_ai.main([*args, "--dry-run"]) == 0
    assert "Dry run" in capsys.readouterr().out
    assert migrate_ai.main(args) == 0
    assert "Keep a copy of the key file" in capsys.readouterr().out
    assert migrate_ai.main(args) == 1
    assert "already encrypted" in capsys.readouterr().err
