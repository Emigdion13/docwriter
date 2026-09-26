"""Python-side idle auto-lock timer for VaultNotes.

The timer is intentionally independent of the web UI.  A background daemon
thread watches a monotonic deadline and invokes a callback when it expires;
``touch`` is called by the Bridge API whenever the user is active.  The timer
can also be polled synchronously with :meth:`AutoLock.check`, which keeps it
easy to test without waiting for a real minute.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Optional


class AutoLock:
    """Resettable idle timer that calls ``on_lock`` once when it expires.

    ``minutes`` is allowed to be a fractional value for tests.  The public
    ``touch`` method returns the wall-clock deadline in milliseconds, which is
    the same shape used by the Bridge API and the status bar.
    """

    def __init__(
        self,
        minutes: float = 10,
        on_lock: Callable[[], None] | None = None,
        *,
        start: bool = True,
        clock: Callable[[], float] | None = None,
        wall_clock: Callable[[], float] | None = None,
    ) -> None:
        self.minutes = max(0.001, float(minutes))
        self.on_lock = on_lock
        self._clock = clock or time.monotonic
        self._wall_clock = wall_clock or time.time
        self._condition = threading.Condition()
        self._deadline: float | None = None
        self._stopped = False
        self._thread: threading.Thread | None = None
        if start:
            self.start()

    @property
    def deadline(self) -> float | None:
        """Monotonic deadline, or ``None`` when the timer is not armed."""
        with self._condition:
            return self._deadline

    @property
    def locks_at(self) -> int | None:
        """Wall-clock deadline in epoch milliseconds, if armed."""
        with self._condition:
            if self._deadline is None:
                return None
            remaining = max(0.0, self._deadline - self._clock())
        return int((self._wall_clock() + remaining) * 1000)

    @property
    def running(self) -> bool:
        """Whether the daemon worker is running."""
        return bool(self._thread and self._thread.is_alive() and not self._stopped)

    def start(self) -> None:
        """Start the daemon worker if it is not already running."""
        with self._condition:
            if self._thread and self._thread.is_alive():
                return
            self._stopped = False
            self._thread = threading.Thread(
                target=self._run,
                name="VaultNotes-auto-lock",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        """Stop the worker and disarm the timer."""
        with self._condition:
            self._stopped = True
            self._deadline = None
            self._condition.notify_all()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        self._thread = None

    def set_minutes(self, minutes: float) -> None:
        """Change the timeout for future touches and wake the worker."""
        with self._condition:
            self.minutes = max(0.001, float(minutes))
            if self._deadline is not None:
                self._deadline = self._clock() + self.minutes * 60.0
            self._condition.notify_all()

    def touch(self) -> int:
        """Arm or reset the timer and return its epoch-millisecond deadline."""
        with self._condition:
            if self._stopped:
                self._stopped = False
                # Restarting is safe even when this is called after stop().
                if not self._thread or not self._thread.is_alive():
                    self._thread = threading.Thread(
                        target=self._run,
                        name="VaultNotes-auto-lock",
                        daemon=True,
                    )
                    self._thread.start()
            self._deadline = self._clock() + self.minutes * 60.0
            deadline = self._deadline
            self._condition.notify_all()
        remaining = max(0.0, deadline - self._clock())
        return int((self._wall_clock() + remaining) * 1000)

    def clear(self) -> None:
        """Disarm the timer without stopping its worker."""
        with self._condition:
            self._deadline = None
            self._condition.notify_all()

    def check(self) -> bool:
        """Synchronously run the callback if the armed deadline has expired.

        Returns ``True`` when the callback was invoked.  This is useful for
        deterministic tests and is also harmless to call from the UI thread.
        """
        callback: Callable[[], None] | None = None
        with self._condition:
            if self._deadline is not None and self._clock() >= self._deadline:
                self._deadline = None
                callback = self.on_lock
        if callback is not None:
            callback()
            return True
        return False

    def _run(self) -> None:
        while True:
            callback: Callable[[], None] | None = None
            with self._condition:
                if self._stopped:
                    return
                if self._deadline is None:
                    self._condition.wait()
                    continue
                remaining = self._deadline - self._clock()
                if remaining > 0:
                    self._condition.wait(timeout=remaining)
                    continue
                self._deadline = None
                callback = self.on_lock
            if callback is not None:
                try:
                    callback()
                except Exception:
                    # An auto-lock callback must never kill the worker.  The
                    # next user interaction can still arm it again.
                    pass


# Names that make the role obvious to callers and older integrations.
AutoLockTimer = AutoLock
IdleAutoLock = AutoLock
AutoLockManager = AutoLock


__all__ = ["AutoLock", "AutoLockTimer", "IdleAutoLock", "AutoLockManager"]
