"""Bridge calls arrive on many threads at once; none may corrupt or lose a note.

pywebview runs every call from the page on its own thread, and the auto-lock
timer and Drive backup call back from theirs.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from vaultnotes.api import Api
from vaultnotes.backup.gdrive_auth import TokenStore
from vaultnotes.calllock import CallLock
from vaultnotes.config import Config
from vaultnotes.storage.atomic import atomic_write


# ----------------------------------------------------------------------
# CallLock
# ----------------------------------------------------------------------
def test_call_lock_is_reentrant_and_exclusive() -> None:
    lock = CallLock()
    inside: list[str] = []

    def other() -> None:
        with lock:
            inside.append("other")

    with lock:
        with lock:  # a Bridge method calling another one
            thread = threading.Thread(target=other)
            thread.start()
            thread.join(timeout=0.2)
            assert thread.is_alive(), "another thread got in while the lock was held"
    thread.join(timeout=2)
    assert inside == ["other"]


def test_call_lock_lets_others_in_while_waiting_on_the_user() -> None:
    lock = CallLock()
    ran: list[str] = []

    def autosave() -> None:
        with lock:
            ran.append("autosave")

    with lock:
        with lock.released():  # e.g. a file dialog is open
            thread = threading.Thread(target=autosave)
            thread.start()
            thread.join(timeout=2)
        assert ran == ["autosave"]
        # Still held afterwards, at the same depth.
        blocked = threading.Thread(target=autosave)
        blocked.start()
        blocked.join(timeout=0.2)
        assert blocked.is_alive()
    blocked.join(timeout=2)
    assert ran == ["autosave", "autosave"]


# ----------------------------------------------------------------------
# atomic_write
# ----------------------------------------------------------------------
def test_concurrent_atomic_writes_never_lose_the_file(tmp_path: Path) -> None:
    dest = tmp_path / "note.md"
    atomic_write(dest, b"start")
    payloads = [f"version {n}\n".encode() * 200 for n in range(12)]
    errors: list[BaseException] = []

    def write(payload: bytes) -> None:
        for _ in range(15):
            try:
                atomic_write(dest, payload)
            except PermissionError:
                # Windows refuses a replace while another replace is mid-way;
                # that save reports an error and the file keeps a whole version.
                pass
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

    threads = [threading.Thread(target=write, args=(p,)) for p in payloads]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert dest.read_bytes() in payloads, "the note must end as one whole version"
    assert list(tmp_path.glob("*.tmp")) == [], "no temp file may be left behind"


# ----------------------------------------------------------------------
# Through the Bridge API
# ----------------------------------------------------------------------
@pytest.fixture
def api(tmp_path: Path) -> Api:
    cfg = Config(settings_path=tmp_path / "settings.json")
    cfg.data["notes_root"] = str(tmp_path / "notes")
    cfg.save()
    cfg.ensure_folders()
    api = Api(config=cfg, app_dir=tmp_path / "appdata", drive_store=TokenStore(memory=True))
    created = api.initialize_vaults(
        key_paths={
            "encrypted": tmp_path / "keys" / "encrypted.vnkey",
            "personal": tmp_path / "keys" / "personal.vnkey",
        }
    )
    assert created.get("ok") is True
    return api


def test_simultaneous_saves_of_one_note_all_succeed_and_leave_one_version(api: Api) -> None:
    note_id = api.create_note("plain", "Busy note")["id"]
    bodies = [f"# Busy note\n\nversion {n}\n" for n in range(16)]
    results: list[dict] = []

    def save(body: str) -> None:
        results.append(api.save_note("plain", note_id, body))

    threads = [threading.Thread(target=save, args=(body,)) for body in bodies]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert all("error" not in result for result in results), results
    assert api.open_note("plain", note_id)["body"] in bodies


def test_auto_lock_waits_for_a_vault_save_in_flight(api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
    """A lock arriving mid-save once encrypted the note under an all-zero key."""
    assert api.unlock_vault("encrypted").get("ok") is True
    note_id = api.create_note("encrypted", "Bank stuff")["id"]
    store = api.vault_stores["encrypted"]
    real_write = store._write_note
    writing = threading.Event()

    def slow_write(*args, **kwargs):
        writing.set()
        time.sleep(0.3)  # the auto-lock timer fires right now
        return real_write(*args, **kwargs)

    monkeypatch.setattr(store, "_write_note", slow_write)
    saver = threading.Thread(target=api.save_note, args=("encrypted", note_id, "# Bank stuff\n\nsafe\n"))
    saver.start()
    assert writing.wait(timeout=2)
    api._auto_lock_expired()  # blocks until the save is done
    saver.join(timeout=5)

    assert store.locked
    assert api.unlock_vault("encrypted").get("ok") is True
    assert api.open_note("encrypted", note_id)["body"] == "# Bank stuff\n\nsafe\n"
