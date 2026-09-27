"""One Bridge API call at a time (see :class:`CallLock`)."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager


class CallLock:
    """Runs Bridge API calls one at a time, except while one waits on the user.

    pywebview runs every call from the page on its own thread, and the
    auto-lock timer and the Drive backup call back from theirs.  Without this
    lock, two saves of one note could interleave on disk, or a vault could
    lock halfway through encrypting a note.

    The lock is re-entrant because Bridge methods call each other.  A call
    that waits on the user -- a file dialog, the Google sign-in page -- steps
    out with :meth:`released` so autosave keeps working meanwhile, and must
    re-check the state it relies on afterwards.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._owner: int | None = None
        self._depth = 0

    def __enter__(self) -> "CallLock":
        me = threading.get_ident()
        # Only this thread can have set _owner to its own id, so reading it
        # without the lock is safe.
        if self._owner != me:
            self._lock.acquire()
            self._owner = me
        self._depth += 1
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._depth -= 1
        if self._depth == 0:
            self._owner = None
            self._lock.release()

    @contextmanager
    def released(self) -> Iterator[None]:
        """Let other calls run while this thread waits on the user."""
        me = threading.get_ident()
        if self._owner != me:
            yield
            return
        depth = self._depth
        self._owner, self._depth = None, 0
        self._lock.release()
        try:
            yield
        finally:
            self._lock.acquire()
            self._owner, self._depth = me, depth
