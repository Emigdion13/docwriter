"""Tests for atomic_write utility."""

from __future__ import annotations

from pathlib import Path
from vaultnotes.storage.atomic import atomic_write


def test_atomic_write_bytes(tmp_path: Path) -> None:
    dest = tmp_path / "test.bin"
    atomic_write(dest, b"hello bytes")
    assert dest.read_bytes() == b"hello bytes"
    assert not (tmp_path / "test.bin.tmp").exists()


def test_atomic_write_str(tmp_path: Path) -> None:
    dest = tmp_path / "test.txt"
    atomic_write(dest, "hello string")
    assert dest.read_text(encoding="utf-8") == "hello string"
    assert not (tmp_path / "test.txt.tmp").exists()


def test_atomic_write_overwrite(tmp_path: Path) -> None:
    dest = tmp_path / "test.txt"
    atomic_write(dest, "initial")
    atomic_write(dest, "overwritten")
    assert dest.read_text(encoding="utf-8") == "overwritten"
