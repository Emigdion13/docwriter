"""The window's page comes back when WebView2 dies (it used to stay black)."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from vaultnotes import app as app_module
from vaultnotes import webview_guard as wg
from vaultnotes.events import PAGE_DOWN, emit_event


@pytest.fixture(autouse=True)
def _page_up() -> Any:
    PAGE_DOWN.clear()
    yield
    PAGE_DOWN.clear()


class FakeEvent(list):
    """pywebview and pythonnet events both take handlers with +=."""

    def __iadd__(self, handler):  # type: ignore[override]
        self.append(handler)
        return self

    def fire(self, *args: Any) -> None:
        for handler in list(self):
            handler(*args)


class FakeCore:
    def __init__(self) -> None:
        self.ProcessFailed = FakeEvent()
        self.reloads = 0
        self.reload_error: Exception | None = None

    def Reload(self) -> None:
        if self.reload_error:
            raise self.reload_error
        self.reloads += 1


class FakeControl:
    def __init__(self) -> None:
        self.CoreWebView2: FakeCore | None = FakeCore()
        self.CoreWebView2InitializationCompleted = FakeEvent()
        self.disposed = False


class FakeBrowser:
    def __init__(self, folder: str) -> None:
        self.user_data_folder = folder
        self.webview = FakeControl()


class FakeForm:
    def __init__(self, browser: FakeBrowser) -> None:
        self.browser = browser
        self.webview = browser.webview


class FakeWindow:
    def __init__(self, form: FakeForm | None) -> None:
        class Events:
            def __init__(self) -> None:
                self.loaded = FakeEvent()

        self.events = Events()
        self.native = form


class FakeUI:
    """Stands in for WinForms: runs UI work right away and records what happened."""

    def __init__(self, *, answer: bool = True) -> None:
        self.answer = answer
        self.created: list[tuple[str, FakeBrowser]] = []
        self.dropped: list[FakeControl] = []
        self.asked: list[str] = []
        self.told: list[str] = []
        self.fail_new = False

    def on_ui(self, form: Any, func: Any, *, wait: bool) -> None:
        func()

    def new_browser(self, form: Any, window: Any, folder: str) -> FakeBrowser:
        if self.fail_new:
            raise RuntimeError("no WebView2")
        browser = FakeBrowser(folder)
        self.created.append((folder, browser))
        return browser

    def drop_control(self, form: Any, control: FakeControl) -> None:
        control.disposed = True
        self.dropped.append(control)

    def ask(self, form: Any, text: str) -> bool:
        self.asked.append(text)
        return self.answer

    def tell(self, form: Any, text: str) -> None:
        self.told.append(text)


class Failed:
    """CoreWebView2ProcessFailedEventArgs as pythonnet hands it over."""

    def __init__(self, kind: str, **extra: Any) -> None:
        self.ProcessFailedKind = kind
        for name, value in extra.items():
            setattr(self, name, value)


class Started:
    def __init__(self, ok: bool) -> None:
        self.IsSuccess = ok
        self.InitializationException = None if ok else "0x80070020"


def watched(tmp_path: Path, ui: FakeUI | None = None) -> tuple[wg.WindowGuard, FakeWindow, FakeForm, FakeUI]:
    ui = ui or FakeUI()
    form = FakeForm(FakeBrowser(str(tmp_path / "profile")))
    window = FakeWindow(form)
    guard = wg.WindowGuard(window, wg.Guard(tmp_path / wg.LOG_NAME), ui)
    guard.install()
    window.events.loaded.fire()  # the page loaded
    return guard, window, form, ui


def fail(form: FakeForm, kind: str, **extra: Any) -> None:
    core = form.browser.webview.CoreWebView2
    assert core is not None
    core.ProcessFailed.fire(core, Failed(kind, **extra))


def log_text(tmp_path: Path) -> str:
    return (tmp_path / wg.LOG_NAME).read_text(encoding="utf-8")


# -- what each failure needs ------------------------------------------
@pytest.mark.parametrize("kind, action", [
    ("BrowserProcessExited", wg.RECREATE),
    ("RenderProcessExited", wg.RELOAD),
    ("RenderProcessUnresponsive", wg.ASK_RELOAD),
    ("GpuProcessExited", wg.IGNORE),
    ("UtilityProcessExited", wg.IGNORE),
    ("FrameRenderProcessExited", wg.IGNORE),
    ("SomethingNew", wg.IGNORE),
])
def test_each_failure_kind_gets_its_action(kind: str, action: str) -> None:
    assert wg.Guard(None).decide(kind) == action


def test_the_guard_gives_up_when_the_page_keeps_dying() -> None:
    now = [0.0]
    guard = wg.Guard(None, clock=lambda: now[0], limit=3, window=300)
    for _ in range(3):
        assert guard.decide("BrowserProcessExited") == wg.RECREATE
        now[0] += 10
    assert guard.decide("BrowserProcessExited") == wg.GIVE_UP
    assert guard.decide("BrowserProcessExited") == wg.IGNORE, "it says so once, then stays quiet"


def test_old_recoveries_stop_counting() -> None:
    now = [0.0]
    guard = wg.Guard(None, clock=lambda: now[0], limit=3, window=300)
    for _ in range(3):
        guard.decide("BrowserProcessExited")
    now[0] += 301
    assert guard.decide("RenderProcessExited") == wg.RELOAD


def test_unresponsive_is_asked_once_until_answered() -> None:
    guard = wg.Guard(None)
    assert guard.decide("RenderProcessUnresponsive") == wg.ASK_RELOAD
    assert guard.decide("RenderProcessUnresponsive") == wg.IGNORE
    guard.answered()
    assert guard.decide("RenderProcessUnresponsive") == wg.ASK_RELOAD


def test_the_log_keeps_details_and_stays_small(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    kind, details = wg.describe(Failed("BrowserProcessExited", Reason="Crashed", ExitCode=-1073741819,
                                       ProcessDescription=""))
    assert kind == "BrowserProcessExited"
    assert details == "BrowserProcessExited Reason=Crashed ExitCode=-1073741819"

    monkeypatch.setattr(wg, "LOG_LIMIT", 200)
    guard = wg.Guard(tmp_path / wg.LOG_NAME)
    for i in range(50):
        guard.log(f"line {i}")
    text = log_text(tmp_path)
    assert "line 49" in text and "line 0 " not in text
    assert len(text.encode()) < 400


def test_logging_into_a_missing_folder_does_not_raise(tmp_path: Path) -> None:
    wg.Guard(tmp_path / "gone" / wg.LOG_NAME).log("still fine")


# -- the window -------------------------------------------------------
def test_each_page_load_watches_the_current_browser_once(tmp_path: Path) -> None:
    _, window, form, _ = watched(tmp_path)
    window.events.loaded.fire()  # navigating again
    core = form.browser.webview.CoreWebView2
    assert core is not None and len(core.ProcessFailed) == 1


def test_no_form_yet_is_fine(tmp_path: Path) -> None:
    window = FakeWindow(None)
    wg.WindowGuard(window, wg.Guard(None), FakeUI()).install()
    window.events.loaded.fire()


def test_a_dead_browser_is_replaced_and_the_page_comes_back(tmp_path: Path) -> None:
    _, window, form, ui = watched(tmp_path)
    old = form.browser

    fail(form, "BrowserProcessExited", Reason="Crashed")

    assert PAGE_DOWN.is_set(), "nothing is sent to the dead page"
    assert old.webview.disposed
    (folder, new), = ui.created
    assert folder == old.user_data_folder, "the same profile folder is tried first"
    assert form.browser is new and form.webview is new.webview

    new.webview.CoreWebView2InitializationCompleted.fire(new.webview, Started(True))
    window.events.loaded.fire()  # pywebview loads the page into the new control
    assert not PAGE_DOWN.is_set()
    assert new.webview.CoreWebView2 is not None and len(new.webview.CoreWebView2.ProcessFailed) == 1, \
        "the new browser is watched too"
    text = log_text(tmp_path)
    assert "BrowserProcessExited Reason=Crashed -> recreate" in text and "page restored" in text


def test_a_locked_profile_folder_falls_back_to_a_fresh_one(tmp_path: Path) -> None:
    _, _, form, ui = watched(tmp_path)
    stale = tmp_path / "profile"
    stale.mkdir()
    fail(form, "BrowserProcessExited")
    (_, first), = ui.created

    first.webview.CoreWebView2InitializationCompleted.fire(first.webview, Started(False))

    assert first.webview.disposed
    assert len(ui.created) == 2
    folder, second = ui.created[1]
    assert folder != str(stale) and "vaultnotes-webview-" in folder
    assert form.browser is second
    second.webview.CoreWebView2InitializationCompleted.fire(second.webview, Started(True))
    assert not stale.exists(), "the old throwaway profile is cleaned up"
    assert ui.told == []


def test_when_no_browser_can_start_the_user_is_told(tmp_path: Path) -> None:
    ui = FakeUI()
    _, _, form, _ = watched(tmp_path, ui)
    ui.fail_new = True
    fail(form, "BrowserProcessExited")
    assert ui.told == [wg.FAILED_TEXT]


def test_a_dead_renderer_reloads_the_page(tmp_path: Path) -> None:
    _, _, form, ui = watched(tmp_path)
    core = form.browser.webview.CoreWebView2
    fail(form, "RenderProcessExited")
    assert core is not None and core.reloads == 1
    assert ui.created == [] and PAGE_DOWN.is_set()


def test_a_reload_that_fails_replaces_the_browser(tmp_path: Path) -> None:
    _, _, form, ui = watched(tmp_path)
    core = form.browser.webview.CoreWebView2
    assert core is not None
    core.reload_error = OSError("browser gone too")
    fail(form, "RenderProcessExited")
    assert len(ui.created) == 1


@pytest.mark.parametrize("answer, reloads", [(True, 1), (False, 0)])
def test_an_unresponsive_page_is_reloaded_only_if_the_user_says_so(
        tmp_path: Path, answer: bool, reloads: int) -> None:
    guard, _, form, ui = watched(tmp_path, FakeUI(answer=answer))
    core = form.browser.webview.CoreWebView2
    fail(form, "RenderProcessUnresponsive")
    assert ui.asked == [wg.UNRESPONSIVE_TEXT]
    assert core is not None and core.reloads == reloads
    assert PAGE_DOWN.is_set() is answer
    assert not guard.guard.asking


def test_gpu_crashes_are_only_logged(tmp_path: Path) -> None:
    _, _, form, ui = watched(tmp_path)
    fail(form, "GpuProcessExited", ExitCode=1)
    assert ui.created == [] and not PAGE_DOWN.is_set()
    assert "GpuProcessExited ExitCode=1 -> ignore" in log_text(tmp_path)


def test_giving_up_tells_the_user(tmp_path: Path) -> None:
    guard, window, form, ui = watched(tmp_path)
    guard.guard.limit = 1
    fail(form, "BrowserProcessExited")
    window.events.loaded.fire()  # the new browser loads the page, then dies too
    fail(form, "BrowserProcessExited")
    assert ui.told == [wg.GIVE_UP_TEXT]


def test_install_does_nothing_off_windows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(wg.sys, "platform", "linux")
    assert wg.install(FakeWindow(None), tmp_path) is None


# -- nothing waits on a dead page --------------------------------------
def test_events_are_not_sent_while_the_page_is_down() -> None:
    class Window:
        scripts: list[str] = []

        def evaluate_js(self, script: str) -> None:
            self.scripts.append(script)

    window = Window()
    PAGE_DOWN.set()
    emit_event(window, "claude_event", {"kind": "permission"})
    assert window.scripts == []
    PAGE_DOWN.clear()
    emit_event(window, "claude_event", {"kind": "permission"})
    assert len(window.scripts) == 1


class ClosingWindow:
    def __init__(self, evaluate: Any) -> None:
        class Events:
            def __init__(self) -> None:
                self.closing = FakeEvent()

        self.events = Events()
        self.destroyed = threading.Event()
        self.evaluate = evaluate

    def evaluate_js(self, script: str) -> None:
        self.evaluate(script)

    def destroy(self) -> None:
        self.destroyed.set()


class SaveSignal:
    def __init__(self) -> None:
        self.page_saved_for_close = threading.Event()


def test_a_hung_page_cannot_keep_the_window_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_module, "CLOSE_SAVE_TIMEOUT", 0.2)
    never = threading.Event()
    window = ClosingWindow(lambda script: never.wait())  # evaluate_js that never returns
    app_module.hold_close_until_saved(window, SaveSignal())
    (on_closing,) = window.events.closing
    assert on_closing() is False
    assert window.destroyed.wait(timeout=3)
    never.set()


def test_a_dead_page_closes_at_once_without_asking_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_module, "CLOSE_SAVE_TIMEOUT", 30)
    asked: list[str] = []
    window = ClosingWindow(asked.append)
    app_module.hold_close_until_saved(window, SaveSignal())
    PAGE_DOWN.set()
    (on_closing,) = window.events.closing
    on_closing()
    assert window.destroyed.wait(timeout=3)
    assert asked == []
