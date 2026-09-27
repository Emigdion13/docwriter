"""Application configuration and settings management for VaultNotes."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
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
        #: Problems found while starting (damaged settings, unreachable notes
        #: folder).  Shown to the user through ``get_state()["warnings"]``.
        self.warnings: list[str] = []
        #: The notes folder from settings.json while a fallback is in use:
        #: saving must keep it, so the real folder is used again once reachable.
        self._unreachable_root: str | None = None
        self.load()

        if auto_init_folders:
            try:
                self.ensure_folders()
            except OSError as exc:
                self._fall_back_from_unreachable_root(exc)

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
        """Load settings from settings.json or populate with defaults.

        A file that cannot be parsed is kept under another name rather than
        overwritten, and each setting with an unusable value falls back to its
        default on its own, so one bad value never stops the app from opening.
        """
        if self.settings_file.is_file():
            try:
                loaded = json.loads(self.settings_file.read_text(encoding="utf-8"))
                if not isinstance(loaded, dict):
                    raise ValueError("settings.json does not hold an object")
            except (OSError, ValueError) as exc:  # JSON and decoding errors are ValueErrors
                self._set_aside_damaged_settings(exc)
            else:
                merged = json.loads(json.dumps(DEFAULT_SETTINGS))
                self._deep_update(merged, loaded)
                self.data = merged
                self._repair()
                return self.data

        self.data = json.loads(json.dumps(DEFAULT_SETTINGS))
        self.save()
        return self.data

    def _set_aside_damaged_settings(self, exc: Exception) -> None:
        """Rename an unreadable settings.json so starting fresh loses nothing."""
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        kept = self.settings_file.with_name(f"settings.damaged-{stamp}.json")
        try:
            os.replace(self.settings_file, kept)
        except OSError:
            kept = self.settings_file
        self.warnings.append(
            f"settings.json could not be read ({type(exc).__name__}), so VaultNotes started with "
            f"default settings. The old file was kept as {kept.name}."
        )

    def _repair(self) -> None:
        """Reset any setting whose value has the wrong type or range."""
        defaults = json.loads(json.dumps(DEFAULT_SETTINGS))
        reset: list[str] = []

        def number(value: Any, low: float, high: float) -> bool:
            return isinstance(value, (int, float)) and not isinstance(value, bool) and low <= value <= high

        root = self.data.get("notes_root")
        if not isinstance(root, str) or not root.strip() or not Path(root.strip()).expanduser().is_absolute():
            self.data["notes_root"] = defaults["notes_root"]
            reset.append("notes_root")

        vaults = self.data.get("vaults")
        good = [
            entry
            for entry in (vaults if isinstance(vaults, list) else [])
            if isinstance(entry, dict)
            and isinstance(entry.get("name"), str)
            and isinstance(entry.get("folder"), str)
            and isinstance(entry.get("key_path", ""), str)
        ]
        if not isinstance(vaults, list) or len(good) != len(vaults):
            reset.append("vaults")
        names = {entry["name"].casefold() for entry in good}
        # The two built-in vaults must always be there; the app is built on them.
        good += [entry for entry in defaults["vaults"] if entry["name"].casefold() not in names]
        self.data["vaults"] = good

        if not number(self.data.get("autolock_minutes"), 1, 120):
            self.data["autolock_minutes"] = defaults["autolock_minutes"]
            reset.append("autolock_minutes")

        for block in ("look", "backup"):
            if not isinstance(self.data.get(block), dict):
                self.data[block] = defaults[block]
                reset.append(block)
        look, backup = self.data["look"], self.data["backup"]
        checks = {
            ("look", "theme"): look.get("theme") in ("nebula", "synthwave", "arctic"),
            ("look", "effects"): look.get("effects") in ("full", "lite", "off"),
            ("look", "view_mode"): look.get("view_mode") in ("edit", "split", "preview"),
            ("look", "editor_font_size"): number(look.get("editor_font_size"), 10, 20),
            ("backup", "enabled"): isinstance(backup.get("enabled"), bool),
            ("backup", "interval_minutes"): number(backup.get("interval_minutes"), 5, 1440),
            ("backup", "drive_folder_id"): backup.get("drive_folder_id") is None
            or isinstance(backup.get("drive_folder_id"), str),
            ("backup", "last_backup"): backup.get("last_backup") is None
            or isinstance(backup.get("last_backup"), str),
        }
        for (block, key), ok in checks.items():
            if not ok:
                self.data[block][key] = defaults[block][key]
                reset.append(f"{block}.{key}")

        if reset:
            self.warnings.append(
                "Some settings had values VaultNotes could not use and were reset: " + ", ".join(reset) + "."
            )

    def _fall_back_from_unreachable_root(self, exc: OSError) -> None:
        """Open with the default folder when the configured one is unreachable.

        The configured folder stays in settings.json (see :meth:`save`), so the
        next start uses it again once the drive is back.
        """
        configured = str(self.data.get("notes_root", ""))
        fallback = str(get_default_notes_root())
        if Path(configured).expanduser() == Path(fallback):
            raise exc
        self._unreachable_root = configured
        self.data["notes_root"] = fallback
        self.ensure_folders()
        self.warnings.append(
            f"Your notes folder {configured} could not be opened ({exc.strerror or type(exc).__name__}). "
            f"VaultNotes opened {fallback} for now. Reconnect the drive and restart, or choose "
            "another notes folder in Settings."
        )

    def save(self) -> None:
        """Save current configuration to settings.json atomically."""
        data = self.data
        if self._unreachable_root is not None:
            # Never let a temporary fallback replace the folder the user chose.
            data = {**self.data, "notes_root": self._unreachable_root}
        content = json.dumps(data, indent=2) + "\n"
        atomic_write(self.settings_file, content.encode("utf-8"))

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def update(self, changes: dict[str, Any]) -> dict[str, Any]:
        """Deep update settings and persist to disk."""
        if "notes_root" in changes:
            # A relative or non-text root would be resolved against whatever
            # the current working directory happens to be, so it is refused
            # before anything is written or created.
            raw = changes["notes_root"]
            if not isinstance(raw, (str, Path)) or not str(raw).strip():
                raise ValueError("Notes folder must be a folder path")
            if not Path(str(raw).strip()).expanduser().is_absolute():
                raise ValueError("Notes folder must be an absolute path")
        previous_root = self.data.get("notes_root")
        self._deep_update(self.data, changes)
        if "notes_root" in changes:
            # Create the folders first: if the disk refuses (permission, full,
            # drive removed) settings.json still names the old, working root.
            try:
                self.ensure_folders()
            except OSError:
                self.data["notes_root"] = previous_root
                raise
            # A folder the user picked replaces any start-up fallback.
            self._unreachable_root = None
        self.save()
        return self.data

    def get_vault_key_path(self, vault_name: str) -> str:
        """Return the remembered key-file path for a configured vault."""
        vaults = self.data.get("vaults", [])
        for v in vaults:
            if v.get("name", "").lower() == vault_name.lower():
                return str(v.get("key_path", "") or "")
        return ""

    def get_vault(self, vault_id_or_name: str) -> dict[str, Any] | None:
        """Return a configured vault entry by id/name, without exposing secrets."""
        wanted = str(vault_id_or_name).casefold()
        for vault in self.data.get("vaults", []):
            name = str(vault.get("name", ""))
            folder = str(vault.get("folder", ""))
            # The built-in space ids are the lower-case folder names.  Accept
            # the display name as well because settings use display names.
            if wanted in {name.casefold(), Path(folder).name.casefold(), folder.casefold()}:
                return vault
        return None

    def vault_dir(self, vault_id_or_name: str) -> Path:
        """Resolve a configured vault folder below :attr:`notes_root`."""
        vault = self.get_vault(vault_id_or_name)
        if vault is None:
            raise KeyError(f"Vault not found: {vault_id_or_name}")
        folder = Path(str(vault.get("folder", "")))
        if folder.is_absolute():
            # Settings are user-editable, but a vault must remain in the notes
            # root just like key-file paths must remain outside it.
            candidate = folder.resolve()
        else:
            candidate = (self.notes_root / folder).resolve()
        try:
            candidate.relative_to(self.notes_root.resolve())
        except ValueError as exc:
            raise ValueError("Vault folder must be inside the notes root") from exc
        return candidate

    def set_vault_key_path(self, vault_id_or_name: str, key_path: Path | str) -> None:
        """Remember a key-file location after it has passed API validation."""
        vault = self.get_vault(vault_id_or_name)
        if vault is None:
            raise KeyError(f"Vault not found: {vault_id_or_name}")
        vault["key_path"] = str(Path(key_path).resolve())
        self.save()

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
