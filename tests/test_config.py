"""settings.json problems must never stop the app from opening, or lose the user's choices."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import vaultnotes.config as config_module
from vaultnotes.config import Config


def _write(path: Path, data: object) -> Path:
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


def test_a_damaged_settings_file_is_kept_not_overwritten(tmp_path: Path) -> None:
    settings = _write(tmp_path / "settings.json", '{"notes_root": "C:/Notes", "vaul')
    cfg = Config(settings_path=settings, auto_init_folders=False)

    kept = list(tmp_path.glob("settings.damaged-*.json"))
    assert len(kept) == 1 and kept[0].read_text(encoding="utf-8").startswith('{"notes_root"')
    assert cfg.warnings and kept[0].name in cfg.warnings[0]


@pytest.mark.parametrize(
    "bad",
    [
        {"vaults": None},
        {"vaults": "x"},
        {"vaults": [None]},
        {"autolock_minutes": "ten"},
        {"look": "dark"},
        {"look": {"editor_font_size": "huge"}},
        {"backup": {"interval_minutes": None}},
        {"notes_root": 5},
    ],
)
def test_a_wrong_value_resets_only_that_setting(tmp_path: Path, bad: dict) -> None:
    good_root = str(tmp_path / "notes")
    settings = _write(tmp_path / "settings.json", {"notes_root": good_root, **bad})
    cfg = Config(settings_path=settings, auto_init_folders=False)

    assert [v["name"] for v in cfg.data["vaults"]][:2] == ["Encrypted", "Personal"]
    assert isinstance(cfg.data["autolock_minutes"], (int, float))
    assert isinstance(cfg.data["look"]["editor_font_size"], (int, float))
    assert cfg.warnings, "the user is told something was reset"
    if "notes_root" not in bad:
        assert cfg.data["notes_root"] == good_root


def test_valid_key_paths_survive_a_repair(tmp_path: Path) -> None:
    key = str(tmp_path / "keys" / "personal.vnkey")
    settings = _write(
        tmp_path / "settings.json",
        {"notes_root": str(tmp_path / "notes"), "vaults": [{"name": "Personal", "folder": "vaults/personal", "key_path": key}, None]},
    )
    cfg = Config(settings_path=settings, auto_init_folders=False)
    assert cfg.get_vault_key_path("Personal") == key
    assert cfg.get_vault("Encrypted") is not None


def test_an_unreachable_notes_folder_opens_the_default_and_keeps_the_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fallback = tmp_path / "home" / "Documents" / "VaultNotes"
    monkeypatch.setattr(config_module, "get_default_notes_root", lambda: fallback)
    unreachable = tmp_path / "blocker"
    unreachable.write_text("a file where the folder should be", encoding="utf-8")
    settings = _write(tmp_path / "settings.json", {"notes_root": str(unreachable / "Notes")})

    cfg = Config(settings_path=settings)
    assert cfg.notes_root == fallback.resolve()
    assert (fallback / "plain").is_dir()
    assert cfg.warnings and str(unreachable / "Notes") in cfg.warnings[0]

    cfg.update({"look": {"theme": "arctic"}})
    saved = json.loads(settings.read_text(encoding="utf-8"))
    assert saved["notes_root"] == str(unreachable / "Notes"), "the user's folder stays configured"
    assert saved["look"]["theme"] == "arctic"
