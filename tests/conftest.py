"""Shared test setup."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _ai_key_in_tmp(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the AI-Notes key at a per-test temporary file.

    Without this, every ``Api`` and ``notes.py`` run in the tests would create
    or open the real ``%LOCALAPPDATA%\\VaultNotes\\ai-notes.vnkey``.
    """
    key = tmp_path_factory.mktemp("ai-key") / "ai-notes.vnkey"
    monkeypatch.setenv("VAULTNOTES_AI_KEY", str(key))
    return key
