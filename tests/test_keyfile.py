"""Tests for keyfile generation and loading (Milestone M3)."""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from vaultnotes.crypto.keyfile import (
    KeyFileError,
    VaultKey,
    generate_key_file,
    load_key_file,
    save_key_file,
)


def test_generate_and_load_key_file(tmp_path: Path) -> None:
    key_path = tmp_path / "personal.vnkey"
    vault_id = "0b6f2c1e-9a4d-4c55-8f0e-2d7c1b9a3e44"
    vault_name = "Personal"

    gen_key = generate_key_file(key_path, vault_id, vault_name)
    assert key_path.is_file()
    assert isinstance(gen_key, VaultKey)
    assert len(gen_key.key) == 32
    assert gen_key.vault_id == vault_id
    assert gen_key.vault_name == vault_name
    assert gen_key.protection == "none"
    assert gen_key.created

    # Read raw JSON to check format specification
    raw = json.loads(key_path.read_text(encoding="utf-8"))
    assert raw["format"] == "vaultnotes-key"
    assert raw["version"] == 1
    assert raw["vault_id"] == vault_id
    assert raw["vault_name"] == vault_name
    assert raw["protection"] == "none"
    assert "key_b64" in raw

    # Load back
    loaded = load_key_file(key_path)
    assert loaded.key == gen_key.key
    assert loaded.vault_id == gen_key.vault_id
    assert loaded.vault_name == gen_key.vault_name
    assert loaded.created == gen_key.created


def test_load_nonexistent_key_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_key_file(tmp_path / "missing.vnkey")


def test_load_corrupted_json(tmp_path: Path) -> None:
    bad_file = tmp_path / "bad.vnkey"
    bad_file.write_text("not json content", encoding="utf-8")
    with pytest.raises(ValueError):
        load_key_file(bad_file)


def test_load_invalid_format_or_version(tmp_path: Path) -> None:
    key_file = tmp_path / "test.vnkey"
    data = {
        "format": "wrong-format",
        "version": 1,
        "vault_id": "vid",
        "vault_name": "Name",
        "key_b64": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    }
    key_file.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        load_key_file(key_file)

    # Wrong version
    data["format"] = "vaultnotes-key"
    data["version"] = 2
    key_file.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        load_key_file(key_file)


def test_load_invalid_key_length(tmp_path: Path) -> None:
    key_file = tmp_path / "short_key.vnkey"
    data = {
        "format": "vaultnotes-key",
        "version": 1,
        "vault_id": "vid",
        "vault_name": "Name",
        "key_b64": "AAAA",  # only 3 bytes
    }
    key_file.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        load_key_file(key_file)


def test_key_wipe() -> None:
    key = VaultKey(
        key=b"\x42" * 32,
        vault_id="vid",
        vault_name="Name",
        created="now",
    )
    key.wipe()
    assert key.key == b"\x00" * 32
