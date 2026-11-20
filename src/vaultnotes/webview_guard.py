"""Bring the page back when the window's browser (WebView2) dies.

The window is a WinForms form with a WebView2 control in it.  WebView2 runs the
page in its own msedgewebview2.exe processes, and when the main one exits (a
crash, the runtime updating itself, something killing it) the control is dead:
the window stays open, the Python side keeps running, and the user sees a black
window that never recovers.  pywebview does not listen for that, so this does.

WebView2 reports it through ``CoreWebView2.ProcessFailed``:

* the browser process exited: the control can never draw again, so a new one
  replaces it and loads the page;
* the page's renderer exited: the control is fine, so the page is reloaded;
* the page stopped responding: reloading would drop what the page holds, so the
  user is asked first;
* anything else (GPU, utility, a frame's renderer): WebView2 restarts those
  itself, so it is only written down.

Every failure is logged to ``webview.log`` in the app folder (kind, reason,
exit code; never page content).  When the page keeps dying, the guard stops
trying and tells the user to restart the app instead of looping.

While the page is gone, :data:`vaultnotes.events.PAGE_DOWN` is set, so nothing
waits on a page that cannot answer (see ``emit_event``).
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from vaultnotes.events import PAGE_DOWN

LOG_NAME = "webview.log"
#: The log is trimmed to its newest half once it grows past this.
LOG_LIMIT = 64 * 1024

#: More than this many recoveries within ``RETRY_WINDOW`` seconds and the guard
#: gives up: something keeps killing the page, and restarting the app is better
#: than a window that flashes forever.
RETRY_LIMIT = 3
RETRY_WINDOW = 300.0

RECREATE, RELOAD, ASK_RELOAD, IGNORE, GIVE_UP = "recreate", "reload", "ask_reload", "ignore", "give_up"

#: ``CoreWebView2ProcessFailedKind`` names and what to do about each.
ACTIONS = {
    "BrowserProcessExited": RECREATE,
    "RenderProcessExited": RELOAD,
    "RenderProcessUnresponsive": ASK_RELOAD,
}

GIVE_UP_TEXT = (
    "The VaultNotes window keeps losing its page, so it stopped trying to bring it back.\n\n"
    "Close VaultNotes and open it again. Your notes are saved. "
    "Details are in webview.log in the VaultNotes settings folder."
)
UNRESPONSIVE_TEXT = (
    "The VaultNotes page stopped responding.\n\n"
    "Reload it? Your saved notes are safe. Text typed in the last second may be lost."
)
FAILED_TEXT = (
    "The VaultNotes window lost its page and could not bring it back.\n\n"
    "Close VaultNotes and open it again. Your notes are saved."
)


def dotnet_name(value: Any) -> str:
    """The name of a .NET enum value (pythonnet gives objects, tests give strings)."""
    try:
        return str(value.ToString())
    except Exception:  # noqa: BLE001 - plain values have no ToString
        return str(value)


class Guard:
    """Decides what each failure needs, keeps count, and writes the log.

    No WinForms or WebView2 in here, so it can be tested anywhere.
    """

    def __init__(self, log_path: Path | None, *, clock: Callable[[], float] = time.monotonic,
                 limit: int = RETRY_LIMIT, window: float = RETRY_WINDOW) -> None:
        self.log_path = log_path
        self.clock = clock
        self.limit = limit
        self.window = window
        self.recoveries: deque[float] = deque()
        self.gave_up = False
        self.asking = False  # the "stopped responding" question is open
        self._lock = threading.Lock()

    def decide(self, kind: str) -> str:
        """What to do about a failure of this kind, counting it as a recovery if one is due."""
        action = ACTIONS.get(kind, IGNORE)
        if action == IGNORE:
            return IGNORE
        with self._lock:
            if self.gave_up:
                return IGNORE
            if action == ASK_RELOAD:
                if self.asking:
                    return IGNORE  # WebView2 repeats this while the page stays stuck
                self.asking = True
                return ASK_RELOAD
            now = self.clock()
            while self.recoveries and now - self.recoveries[0] > self.window:
                self.recoveries.popleft()
            if len(self.recoveries) >= self.limit:
                self.gave_up = True
                return GIVE_UP
            self.recoveries.append(now)
            return action

    def answered(self) -> None:
        """The "stopped responding" question was closed."""
        with self._lock:
            self.asking = False

    def log(self, message: str) -> None:
        """Add one timestamped line to webview.log; logging never breaks recovery."""
        if self.log_path is None:
            return
        line = f"{datetime.now().isoformat(timespec='seconds')}  {message}\n"
        try:
            path = self.log_path
            if path.exists() and path.stat().st_size > LOG_LIMIT:
                text = path.read_text(encoding="utf-8", errors="replace")
                path.write_text(text[len(text) // 2:], encoding="utf-8")
            with path.open("a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass


def _is_temp(folder: str) -> bool:
    """True for a folder inside the temp folder (pywebview's private-mode profiles live there)."""
    try:
        return Path(folder).resolve().is_relative_to(Path(tempfile.gettempdir()).resolve())
    except (OSError, ValueError):
        return False


def describe(args: Any) -> tuple[str, str]:
    """``(kind, details)`` for a ProcessFailed event; the details are for the log only."""
    kind = dotnet_name(getattr(args, "ProcessFailedKind", "Unknown"))
    parts = [kind]
    for name in ("Reason", "ExitCode", "ProcessDescription"):
        try:
            value = getattr(args, name)
        except Exception:  # noqa: BLE001 - older runtimes lack some of these
            continue
        if value not in (None, ""):
            parts.append(f"{name}={dotnet_name(value)}")
    return kind, " ".join(parts)


class _WinForms:
    """The WinForms / WebView2 calls the guard makes, kept apart so tests can fake them."""

    def __init__(self) -> None:
        import System.Windows.Forms as WinForms  # type: ignore[import-not-found]
        from System import Action  # type: ignore[import-not-found]

        self.WinForms = WinForms
        self.Action = Action

    def on_ui(self, form: Any, func: Callable[[], None], *, wait: bool) -> None:
        """Run ``func`` on the window's UI thread (WebView2 may only be touched there)."""
        if wait:
            form.Invoke(self.Action(func))
        else:
            form.BeginInvoke(self.Action(func))

    def new_browser(self, form: Any, window: Any, folder: str) -> Any:
        """A fresh pywebview EdgeChrome (a new WebView2 control) that loads the page when ready."""
        from webview.platforms.edgechromium import EdgeChrome  # type: ignore[import-not-found]

        return EdgeChrome(form, window, folder)

    def drop_control(self, form: Any, control: Any) -> None:
        try:
            form.Controls.Remove(control)
        except Exception:  # noqa: BLE001 - a dead control may refuse
            pass
        try:
            control.Dispose()
        except Exception:  # noqa: BLE001
            pass

    def ask(self, form: Any, text: str) -> bool:
        WF = self.WinForms
        answer = WF.MessageBox.Show(form, text, "VaultNotes", WF.MessageBoxButtons.YesNo,
                                    WF.MessageBoxIcon.Warning)
        return answer == WF.DialogResult.Yes

    def tell(self, form: Any, text: str) -> None:
        WF = self.WinForms
        WF.MessageBox.Show(form, text, "VaultNotes", WF.MessageBoxButtons.OK, WF.MessageBoxIcon.Error)


class WindowGuard:
    """Watches one pywebview window and brings its page back.

    ``install`` hooks the window's ``loaded`` event: every time a page loads,
    the current CoreWebView2 gets a ProcessFailed handler (once per CoreWebView2,
    so a new control after a recovery gets one too).
    """

    def __init__(self, window: Any, guard: Guard, ui: Any = None) -> None:
        self.window = window
        self.guard = guard
        self.ui = ui
        self._watched: set[int] = set()
        self._lock = threading.Lock()

    # -- hooking ------------------------------------------------------
    def install(self) -> None:
        self.window.events.loaded += self._on_loaded

    def _on_loaded(self) -> None:
        # pywebview runs this on a worker thread after each page load.
        PAGE_DOWN.clear()
        form = getattr(self.window, "native", None)
        if form is None or getattr(form, "browser", None) is None:
            return
        try:
            self._ui().on_ui(form, lambda: self._watch(form), wait=True)
        except Exception as e:  # noqa: BLE001 - the guard must never break the app
            self.guard.log(f"could not watch the page: {e!r}")

    def _watch(self, form: Any) -> None:
        core = getattr(form.browser.webview, "CoreWebView2", None)
        if core is None:
            return
        with self._lock:
            if id(core) in self._watched:
                return
            self._watched.add(id(core))
        core.ProcessFailed += lambda sender, args: self._on_failed(form, sender, args)

    def _ui(self) -> Any:
        if self.ui is None:
            self.ui = _WinForms()
        return self.ui

    # -- failures (WebView2 raises these on the UI thread) ------------
    def _on_failed(self, form: Any, sender: Any, args: Any) -> None:
        kind, details = describe(args)
        action = self.guard.decide(kind)
        self.guard.log(f"{details} -> {action}")
        if action in (RECREATE, RELOAD, GIVE_UP):
            PAGE_DOWN.set()
        # Never rebuild inside the dying control's own event: post it instead.
        if action == RECREATE:
            self._ui().on_ui(form, lambda: self._recreate(form), wait=False)
        elif action == RELOAD:
            self._ui().on_ui(form, lambda: self._reload(form, sender), wait=False)
        elif action == ASK_RELOAD:
            self._ui().on_ui(form, lambda: self._ask_reload(form, sender), wait=False)
        elif action == GIVE_UP:
            self._ui().on_ui(form, lambda: self._ui().tell(form, GIVE_UP_TEXT), wait=False)

    def _reload(self, form: Any, core: Any) -> None:
        try:
            core.Reload()
        except Exception as e:  # noqa: BLE001 - the renderer and the browser went together
            self.guard.log(f"reload failed ({e!r}), replacing the browser")
            self._recreate(form)

    def _ask_reload(self, form: Any, core: Any) -> None:
        try:
            if self._ui().ask(form, UNRESPONSIVE_TEXT):
                self.guard.log("user chose to reload the unresponsive page")
                PAGE_DOWN.set()
                self._reload(form, core)
            else:
                self.guard.log("user kept the unresponsive page")
        finally:
            self.guard.answered()

    def _recreate(self, form: Any) -> None:
        """Put a new WebView2 control in the window, on the same profile folder if it is free."""
        ui = self._ui()
        old = form.browser
        folder = getattr(old, "user_data_folder", None)
        ui.drop_control(form, old.webview)
        try:
            new = ui.new_browser(form, self.window, folder or tempfile.mkdtemp(prefix="vaultnotes-webview-"))
        except Exception as e:  # noqa: BLE001
            self.guard.log(f"new browser failed: {e!r}")
            self._fresh_folder(form, old)
            return
        self._swap(form, new)
        # The profile folder may still be locked by what is left of the old
        # browser: if the new control cannot start there, use a fresh folder.
        new.webview.CoreWebView2InitializationCompleted += (
            lambda sender, args: self._started(form, new, old, args)
        )

    def _started(self, form: Any, new: Any, old: Any, args: Any) -> None:
        if getattr(args, "IsSuccess", True):
            self.guard.log("page restored")
            return
        self.guard.log(f"new browser could not start: {getattr(args, 'InitializationException', '')!r}")
        self._ui().drop_control(form, new.webview)
        self._fresh_folder(form, old)

    def _fresh_folder(self, form: Any, old: Any) -> None:
        ui = self._ui()
        stale = getattr(old, "user_data_folder", None)
        folder = tempfile.mkdtemp(prefix="vaultnotes-webview-")
        try:
            new = ui.new_browser(form, self.window, folder)
        except Exception as e:  # noqa: BLE001
            self.guard.log(f"new browser failed again: {e!r}")
            ui.tell(form, FAILED_TEXT)
            return
        self._swap(form, new)

        def started(sender: Any, args: Any) -> None:
            if getattr(args, "IsSuccess", True):
                self.guard.log("page restored on a fresh profile folder")
                if stale and _is_temp(stale):  # private mode: throwaway cache, nothing else
                    shutil.rmtree(stale, ignore_errors=True)
            else:
                self.guard.log(f"fresh browser could not start: {getattr(args, 'InitializationException', '')!r}")
                ui.tell(form, FAILED_TEXT)

        new.webview.CoreWebView2InitializationCompleted += started

    @staticmethod
    def _swap(form: Any, new: Any) -> None:
        # pywebview's form reaches the page only through these two attributes.
        form.browser = new
        form.webview = new.webview


def install(window: Any, app_dir: Path | None) -> WindowGuard | None:
    """Watch ``window``'s page and bring it back if WebView2 dies (Windows only)."""
    if sys.platform != "win32":
        return None
    watcher = WindowGuard(window, Guard(app_dir / LOG_NAME if app_dir else None))
    watcher.install()
    return watcher
