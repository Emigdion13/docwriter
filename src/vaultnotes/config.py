"""Application configuration and settings management for VaultNotes."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from vaultnotes.storage.atomic import atomic_write


def get_app_dir() -> Path:
    """Return platform-specific app data directory (%APPDATA%\\VaultNotes on Windows)."""
    if sys.platform == "win32" and "APPDATA" in os.environ:
        base = Path(os.environ["APPDATA"])
    elif "XDG_CONFIG_HOME" in os.environ:
        base = Path(os.environ["XDG_CONFIG_HOME"])
    else:
        base = Path.home() / ".config"

    app_dir = base / "VaultNotes"
    app_dir.mkdir(parents=True, exist_ok=True)
    return app_dir


def get_default_notes_root() -> Path:
    """Return default notes root (%USERPROFILE%\\Documents\\VaultNotes on Windows)."""
    if sys.platform == "win32" and "USERPROFILE" in os.environ:
        return Path(os.environ["USERPROFILE"]) / "Documents" / "VaultNotes"
    docs = Path.home() / "Documents"
    if docs.is_dir():
        return docs / "VaultNotes"
    return Path.home() / "VaultNotes"


DEFAULT_SETTINGS: dict[str, Any] = {
    "notes_root": str(get_default_notes_root()),
    "vaults": [
        {"name": "Encrypted", "folder": "vaults/encrypted", "key_path": ""},
        {"name": "Personal", "folder": "vaults/personal", "key_path": ""},
    ],
    "autolock_minutes": 10,
    "look": {
        "theme": "nebula",
        "effects": "full",
        "view_mode": "split",
        "editor_font_size": 13.5,
    },
    "backup": {
        "enabled": False,
        "interval_minutes": 60,
        "drive_folder_id": None,
        "last_backup": None,
    },
}

INITIAL_SHOPPING_LIST = """# Shopping list

- [x] Coffee beans
- [ ] Oat milk
- [ ] Batteries for the [[Home lab]] sensors
- [ ] Birthday card for Ana
"""

INITIAL_HOME_LAB = """# Home lab

Ideas for the little server shelf.

| Device | Status |
|---|---|
| Raspberry Pi 5 | running |
| NAS | ordering |
| Air sensor | needs batteries |

Parts go on the [[Shopping list]].
"""

INITIAL_READING_LIST = """# Reading list

> One chapter a day, no excuses.

- *Project Hail Mary*
- *Snow Crash*
- [[Book club]] picks for October
"""


class Config:
    """Manages settings.json and notes directory structure."""

    def __init__(self, settings_path: Path | str | None = None, auto_init_folders: bool = True) -> None:
        if settings_path is None:
            self.settings_file = get_app_dir() / "settings.json"
        else:
            self.settings_file = Path(settings_path).resolve()

        self.data: dict[str, Any] = {}
        self.load()

        if auto_init_folders:
            self.ensure_folders()

    @property
    def notes_root(self) -> Path:
        raw = self.data.get("notes_root")
        if raw:
            return Path(raw).resolve()
        return get_default_notes_root().resolve()

    @property
    def plain_dir(self) -> Path:
        return self.notes_root / "plain"

    def load(self) -> dict[str, Any]:
        """Load settings from settings.json or populate with defaults."""
        if self.settings_file.is_file():
            try:
                with open(self.settings_file, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                merged = json.loads(json.dumps(DEFAULT_SETTINGS))
                self._deep_update(merged, loaded)
                self.data = merged
                return self.data
            except Exception:
                pass

        self.data = json.loads(json.dumps(DEFAULT_SETTINGS))
        self.save()
        return self.data

    def save(self) -> None:
        """Save current configuration to settings.json atomically."""
        content = json.dumps(self.data, indent=2) + "\n"
        atomic_write(self.settings_file, content.encode("utf-8"))

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def update(self, changes: dict[str, Any]) -> dict[str, Any]:
        """Deep update settings and persist to disk."""
        self._deep_update(self.data, changes)
        self.save()
        if "notes_root" in changes:
            self.ensure_folders()
        return self.data

    def get_vault_key_path(self, vault_name: str) -> str:
        vaults = self.data.get("vaults", [])
        for v in vaults:
            if v.get("name", "").lower() == vault_name.lower():
                return v.get("key_path", "")
        return ""

    def ensure_folders(self) -> None:
        """Create necessary directories under notes_root."""
        root = self.notes_root
        plain = root / "plain"
        plain_trash = plain / ".trash"
        vaults_dir = root / "vaults"
        enc = vaults_dir / "encrypted"
        enc_trash = enc / ".trash"
        personal = vaults_dir / "personal"
        personal_trash = personal / ".trash"

        first_plain_run = not plain.exists()

        for d in (root, plain, plain_trash, vaults_dir, enc, enc_trash, personal, personal_trash):
            d.mkdir(parents=True, exist_ok=True)

        if first_plain_run:
            self._seed_sample_plain_notes(plain)

    def _seed_sample_plain_notes(self, plain_dir: Path) -> None:
        """Create initial sample notes if plain directory was just created."""
        samples = [
            ("Shopping list.md", INITIAL_SHOPPING_LIST),
            ("Home lab.md", INITIAL_HOME_LAB),
            ("Reading list.md", INITIAL_READING_LIST),
        ]
        for filename, content in samples:
            file_path = plain_dir / filename
            if not file_path.exists():
                atomic_write(file_path, content.encode("utf-8"))

    @staticmethod
    def _deep_update(base: dict[str, Any], updates: dict[str, Any]) -> None:
        for k, v in updates.items():
            if isinstance(v, dict) and k in base and isinstance(base[k], dict):
                Config._deep_update(base[k], v)
            else:
                base[k] = v


_config_instance: Config | None = None


def get_config() -> Config:
    """Return shared application Config instance."""
    global _config_instance
    if _config_instance is None:
        _config_instance = Config()
    return _config_instance
