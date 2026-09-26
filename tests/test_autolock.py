"""Tests for the Python-side auto-lock timer."""

from __future__ import annotations

from vaultnotes.autolock import AutoLock


def test_autolock_can_be_checked_with_a_deterministic_clock() -> None:
    now = [100.0]
    locked: list[str] = []
    timer = AutoLock(
        minutes=1 / 60,  # one second
        on_lock=lambda: locked.append("locked"),
        start=False,
        clock=lambda: now[0],
        wall_clock=lambda: now[0],
    )

    deadline = timer.touch()
    assert deadline == 101000
    assert timer.check() is False
    now[0] = 101.01
    assert timer.check() is True
    assert locked == ["locked"]
    assert timer.check() is False
    timer.stop()


def test_touch_resets_deadline() -> None:
    now = [0.0]
    timer = AutoLock(minutes=1, start=False, clock=lambda: now[0], wall_clock=lambda: now[0])
    first = timer.touch()
    now[0] = 10.0
    second = timer.touch()
    assert second > first
    timer.stop()
