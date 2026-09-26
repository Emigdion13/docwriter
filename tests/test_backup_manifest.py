"""Tests for the Drive backup decisions (Build Plan M8, section 8.2).

Everything here runs against :class:`FakeDrive`, an in-memory stand-in for the
Drive API: no network, no Google account, no ``keyring``.  What is under test is
the set of decisions VaultNotes makes - which files are picked, when something
is uploaded, updated, skipped, recreated or left alone.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from vaultnotes.backup.gdrive_auth import DriveAuthError
from vaultnotes.backup.gdrive_backup import (
    BACKUP_FOLDER_NAME,
    BackupError,
    BackupManifest,
    BackupReport,
    BackupRunner,
    DriveFileGone,
    IntervalTimer,
    decide,
    iter_backup_files,
    prune_deleted,
    restore,
    sha256_file,
    should_backup,
    sync_files,
)

FOLDER_MIME = "application/vnd.google-apps.folder"


# ----------------------------------------------------------------------
# The fake Drive
# ----------------------------------------------------------------------
class FakeDrive:
    """A tiny Drive: folders hold children, files hold bytes."""

    def __init__(self) -> None:
        self.nodes: dict[str, dict] = {
            "root": {"id": "root", "name": "root", "mimeType": FOLDER_MIME, "children": []}
        }
        self._next = 0
        self.calls: list[str] = []

    # -- test helpers ----------------------------------------------------
    def _add(self, parent_id: str, name: str, mime: str, data: bytes = b"") -> str:
        self._next += 1
        node_id = f"drive-{self._next}"
        self.nodes[node_id] = {
            "id": node_id,
            "name": name,
            "mimeType": mime,
            "children": [],
            "data": data,
            "parent": parent_id,
        }
        self.nodes[parent_id]["children"].append(node_id)
        return node_id

    def add_raw(self, parent_relative: str, name: str, data: bytes) -> str:
        """Put a file directly into Drive, bypassing VaultNotes' own rules."""
        return self._add(self.path_of(parent_relative)["id"], name, "", data)

    def path_of(self, relative: str) -> dict:
        """Follow ``VaultNotes Backup/plain/Note.md`` through the fake tree."""
        node = self.nodes["root"]
        for part in relative.split("/"):
            for child in self.list_children(node["id"]):
                if child["name"] == part:
                    node = self.nodes[child["id"]]
                    break
            else:
                raise AssertionError(f"{relative} is not in Drive (missing {part!r})")
        return node

    def data_of(self, relative: str) -> bytes:
        return self.path_of(relative)["data"]

    def exists(self, relative: str) -> bool:
        try:
            self.path_of(relative)
        except AssertionError:
            return False
        return True

    # -- the DriveApi surface VaultNotes uses ---------------------------
    def list_children(self, folder_id: str) -> list[dict]:
        if folder_id not in self.nodes:
            raise DriveFileGone(folder_id)
        return [
            self.nodes[child_id] | {"id": child_id}
            for child_id in self.nodes[folder_id]["children"]
        ]

    def find_child(self, name: str, folder_id: str) -> dict | None:
        for child in self.list_children(folder_id):
            if child["name"] == name:
                return {"id": child["id"], "name": name, "mimeType": child.get("mimeType", "")}
        return None

    def create_folder(self, name: str, parent_id: str) -> str:
        self.calls.append(f"mkdir:{name}")
        return self._add(parent_id, name, FOLDER_MIME)

    def upload(self, name: str, parent_id: str, path: Path) -> str:
        self.calls.append(f"create:{name}")
        return self._add(parent_id, name, "", Path(path).read_bytes())

    def replace(self, file_id: str, path: Path) -> None:
        self.calls.append(f"update:{file_id}")
        if file_id not in self.nodes:
            raise DriveFileGone(file_id)
        self.nodes[file_id]["data"] = Path(path).read_bytes()

    def download(self, file_id: str, dest: Path) -> None:
        if file_id not in self.nodes:
            raise DriveFileGone(file_id)
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(self.nodes[file_id]["data"])

    def delete(self, file_id: str) -> None:
        self.calls.append(f"delete:{file_id}")
        if file_id not in self.nodes:
            raise DriveFileGone(file_id)
        node = self.nodes.pop(file_id)
        parent = self.nodes.get(node.get("parent", "root"))
        if parent is not None and file_id in parent["children"]:
            parent["children"].remove(file_id)


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------
@pytest.fixture()
def notes_root(tmp_path: Path) -> Path:
    """A notes folder with plain notes, a trash note, two vaults and junk."""
    root = tmp_path / "VaultNotes"
    for folder in ("plain/.trash", "vaults/encrypted/.trash", "vaults/personal"):
        (root / folder).mkdir(parents=True, exist_ok=True)

    (root / "plain" / "Shopping list.md").write_text("# Shopping list\n- coffee\n", encoding="utf-8")
    (root / "plain" / "Home lab.md").write_text("# Home lab\n", encoding="utf-8")
    (root / "plain" / ".trash" / "Old note.md").write_text("old\n", encoding="utf-8")
    (root / "vaults" / "encrypted" / "vault.json").write_text(
        '{"format":"vaultnotes-vault","version":1}', encoding="utf-8"
    )
    (root / "vaults" / "encrypted" / "3f2a9ce1.vnote").write_bytes(b"VNT1\x01" + bytes(range(64)))
    (root / "vaults" / "personal" / "vault.json").write_text(
        '{"format":"vaultnotes-vault","version":1}', encoding="utf-8"
    )
    (root / "vaults" / "personal" / "8b01d47a.vnote").write_bytes(b"VNT1\x01" + bytes(range(40)))
    return root


@pytest.fixture()
def manifest(tmp_path: Path) -> BackupManifest:
    """A manifest in the app-settings folder, not next to the notes."""
    return BackupManifest(tmp_path / "appdata" / "backup_manifest.json")


# ----------------------------------------------------------------------
# Which files are picked
# ----------------------------------------------------------------------
def test_only_the_documented_files_are_included(notes_root: Path) -> None:
    keys = {key for key, _ in iter_backup_files(notes_root)}
    assert keys == {
        "plain/.trash/Old note.md",
        "plain/Home lab.md",
        "plain/Shopping list.md",
        "vaults/encrypted/3f2a9ce1.vnote",
        "vaults/encrypted/vault.json",
        "vaults/personal/8b01d47a.vnote",
        "vaults/personal/vault.json",
    }


def test_key_files_and_temp_files_are_never_picked(notes_root: Path) -> None:
    """Security rule 6: skip ``*.vnkey`` even if one is found inside the root."""
    stray_key = notes_root / "plain" / "personal.vnkey"
    stray_key.write_text('{"key_b64": "SECRET-KEY-TEXT"}', encoding="utf-8")
    (notes_root / "plain" / "Shopping list.md.tmp").write_text("half written\n", encoding="utf-8")
    (notes_root / "plain" / "index.db").write_bytes(b"sqlite\n")
    (notes_root / "plain" / "photo.png").write_bytes(b"png\n")

    keys = {key for key, _ in iter_backup_files(notes_root)}
    assert keys.isdisjoint(
        {"plain/personal.vnkey", "plain/Shopping list.md.tmp", "plain/index.db", "plain/photo.png"}
    )
    assert should_backup(stray_key) is False
    assert should_backup("vaults/personal/x.vnote") is True
    assert should_backup("vaults/personal/vault.json") is True


def test_a_run_never_uploads_or_reads_a_key_file(notes_root: Path, manifest: BackupManifest) -> None:
    secret = "SECRET-KEY-TEXT"
    (notes_root / "vaults" / "encrypted" / "encrypted.vnkey").write_text(
        json.dumps({"key_b64": secret}), encoding="utf-8"
    )
    drive = FakeDrive()
    sync_files(notes_root, manifest, drive)

    assert not drive.exists(f"{BACKUP_FOLDER_NAME}/vaults/encrypted/encrypted.vnkey")
    uploaded = b"".join(node.get("data", b"") for node in drive.nodes.values())
    assert secret.encode() not in uploaded
    # The encrypted note still travels, still as ciphertext.
    assert drive.data_of(f"{BACKUP_FOLDER_NAME}/vaults/encrypted/3f2a9ce1.vnote").startswith(b"VNT1")


def test_hidden_folders_are_skipped_apart_from_trash(notes_root: Path) -> None:
    (notes_root / ".cache").mkdir()
    (notes_root / ".cache" / "cached note.md").write_text("nope\n", encoding="utf-8")
    keys = {key for key, _ in iter_backup_files(notes_root)}
    assert ".cache" not in " ".join(keys)
    assert "plain/.trash/Old note.md" in keys


# ----------------------------------------------------------------------
# The manifest's decisions
# ----------------------------------------------------------------------
def test_new_files_upload_then_a_second_run_skips_everything(
    notes_root: Path, manifest: BackupManifest
) -> None:
    drive = FakeDrive()
    first = sync_files(notes_root, manifest, drive)
    assert (first.uploaded, first.updated, first.skipped, first.failed) == (7, 0, 0, 0)
    assert first.ok is True
    assert drive.data_of(f"{BACKUP_FOLDER_NAME}/plain/Shopping list.md") == (
        notes_root / "plain" / "Shopping list.md"
    ).read_bytes()

    # A brand-new Drive: the backup folder gets created, but not one file is
    # sent, because the manifest still says everything matches.
    untouched = FakeDrive()
    second = sync_files(notes_root, manifest, untouched)
    assert (second.uploaded, second.updated, second.skipped) == (0, 0, 7)
    assert untouched.calls == [f"mkdir:{BACKUP_FOLDER_NAME}"]


def test_editing_one_note_uploads_only_that_note(notes_root: Path, manifest: BackupManifest) -> None:
    drive = FakeDrive()
    sync_files(notes_root, manifest, drive)
    drive.calls.clear()

    (notes_root / "plain" / "Shopping list.md").write_text("# Shopping list\n- tea\n", encoding="utf-8")
    report = sync_files(notes_root, manifest, drive)

    assert (report.updated, report.uploaded, report.skipped) == (1, 0, 6)
    assert [call for call in drive.calls if call.startswith("update:")]
    assert not any(call.startswith("create:") for call in drive.calls)
    assert drive.data_of(f"{BACKUP_FOLDER_NAME}/plain/Shopping list.md").decode().endswith("- tea\n")


def test_manifest_records_hash_and_time_and_reloads(notes_root: Path, manifest: BackupManifest) -> None:
    drive = FakeDrive()
    sync_files(notes_root, manifest, drive)
    assert manifest.loaded and not manifest.damaged

    payload = json.loads(manifest.path.read_text(encoding="utf-8"))
    entry = payload["plain/Shopping list.md"]
    assert set(entry) == {"drive_id", "sha256", "uploaded"}
    assert entry["sha256"] == sha256_file(notes_root / "plain" / "Shopping list.md")
    assert datetime.fromisoformat(entry["uploaded"].replace("Z", "+00:00"))

    reloaded = BackupManifest(manifest.path)
    reloaded.load()
    report = sync_files(notes_root, reloaded, drive)
    assert (report.uploaded, report.updated, report.skipped) == (0, 0, 7)


def test_missing_manifest_reuses_drive_files_instead_of_duplicating(
    notes_root: Path, manifest: BackupManifest
) -> None:
    """Section 8.2 step 4: look up by name and parent before creating."""
    drive = FakeDrive()
    sync_files(notes_root, manifest, drive)
    drive.calls.clear()

    # A manifest that knows nothing: not loaded from disk, so every file looks
    # like it has to be created.
    fresh = BackupManifest(manifest.path.parent / "no-such-manifest.json")
    decisions = decide(notes_root, fresh)
    report = sync_files(notes_root, fresh, drive, decisions=decisions)

    assert report.uploaded == 0, "an existing Drive file must be reused, never duplicated"
    assert report.updated == 7
    plain_id = drive.path_of(f"{BACKUP_FOLDER_NAME}/plain")["id"]
    names = [child["name"] for child in drive.list_children(plain_id)]
    assert names.count("Shopping list.md") == 1


def test_a_drive_file_that_vanishes_is_recreated_when_the_note_next_changes(
    notes_root: Path, manifest: BackupManifest
) -> None:
    """An unchanged note is skipped (v1 rule), so recreation happens on update."""
    drive = FakeDrive()
    sync_files(notes_root, manifest, drive)
    drive.delete(manifest.get("plain/Home lab.md")["drive_id"])
    drive.calls.clear()

    unchanged = sync_files(notes_root, manifest, drive)
    assert (unchanged.updated, unchanged.uploaded) == (0, 0), "no local change, no call"

    (notes_root / "plain" / "Home lab.md").write_text("# Home lab\nmoved\n", encoding="utf-8")
    report = sync_files(notes_root, manifest, drive)
    assert report.failed == 0
    assert report.uploaded == 1, "the 404 on update fell back to a create"
    assert drive.data_of(f"{BACKUP_FOLDER_NAME}/plain/Home lab.md").decode().endswith("moved\n")


def test_damaged_manifest_is_tolerated(tmp_path: Path) -> None:
    path = tmp_path / "backup_manifest.json"
    path.write_text("{ this is not json", encoding="utf-8")
    manifest = BackupManifest(path)
    manifest.load()
    assert manifest.damaged is True
    assert len(manifest) == 0

    manifest.record("plain/a.md", "drive-1", "abc")
    manifest.save()
    assert json.loads(path.read_text(encoding="utf-8"))["plain/a.md"]["drive_id"] == "drive-1"


def test_an_unreadable_local_file_is_reported_not_fatal(notes_root: Path, manifest: BackupManifest) -> None:
    drive = FakeDrive()
    (notes_root / "plain" / "Locked.md").write_text("hello\n", encoding="utf-8")

    def hash_except_locked(path: Path) -> str:
        if Path(path).name == "Locked.md":
            raise OSError("locked by another program")
        return sha256_file(path)

    decisions = decide(notes_root, manifest, hash_fn=hash_except_locked)
    report = sync_files(notes_root, manifest, drive, decisions=decisions)

    assert report.scanned == 8
    assert report.failed == 1 and report.uploaded == 7
    assert report.ok is False
    assert any("Locked.md could not be read" in error for error in report.errors)
    assert report.message.startswith("1 of 8 files were not uploaded")


# ----------------------------------------------------------------------
# Folders, deletions and progress
# ----------------------------------------------------------------------
def test_drive_mirrors_the_local_folder_structure(notes_root: Path, manifest: BackupManifest) -> None:
    drive = FakeDrive()
    sync_files(notes_root, manifest, drive)
    for relative in (
        BACKUP_FOLDER_NAME,
        f"{BACKUP_FOLDER_NAME}/plain",
        f"{BACKUP_FOLDER_NAME}/plain/.trash",
        f"{BACKUP_FOLDER_NAME}/vaults",
        f"{BACKUP_FOLDER_NAME}/vaults/encrypted",
        f"{BACKUP_FOLDER_NAME}/vaults/personal",
    ):
        assert drive.exists(relative), f"{relative} should exist in Drive"


def test_backup_root_folder_is_created_once_and_reported(
    notes_root: Path, manifest: BackupManifest
) -> None:
    drive = FakeDrive()
    created: list[str] = []
    report = sync_files(notes_root, manifest, drive, on_folder=created.append)

    assert created == [drive.path_of(BACKUP_FOLDER_NAME)["id"]]
    assert report.drive_folder_id == created[0]
    assert drive.calls.count(f"mkdir:{BACKUP_FOLDER_NAME}") == 1

    # The next run reuses the remembered id and creates nothing.
    created.clear()
    sync_files(notes_root, manifest, drive, root_folder_id=report.drive_folder_id, on_folder=created.append)
    assert created == []
    assert drive.calls.count(f"mkdir:{BACKUP_FOLDER_NAME}") == 1


def test_no_empty_trash_folders_are_created(notes_root: Path, manifest: BackupManifest) -> None:
    """Folders appear only where a file needs them (lazy mirroring)."""
    (notes_root / "vaults" / "encrypted" / ".trash").rmdir()
    drive = FakeDrive()
    sync_files(notes_root, manifest, drive)
    assert not drive.exists(f"{BACKUP_FOLDER_NAME}/vaults/encrypted/.trash")


def test_files_deleted_locally_stay_in_drive(notes_root: Path, manifest: BackupManifest) -> None:
    """v1 only adds and updates: a local delete must not reach Drive."""
    drive = FakeDrive()
    sync_files(notes_root, manifest, drive)
    (notes_root / "plain" / "Home lab.md").unlink()
    drive.calls.clear()

    sync_files(notes_root, manifest, drive)
    assert drive.exists(f"{BACKUP_FOLDER_NAME}/plain/Home lab.md")
    assert drive.calls == []


def test_progress_walks_the_files_with_design_text(notes_root: Path, manifest: BackupManifest) -> None:
    seen: list[tuple[int, int, str]] = []
    sync_files(notes_root, manifest, FakeDrive(), progress=lambda d, t, label: seen.append((d, t, label)))
    assert seen[0] == (0, 7, "Backing up 1/7…")
    assert seen[1] == (1, 7, "Backing up 2/7…")
    assert seen[-1] == (7, 7, "Backing up 7/7…")


# ----------------------------------------------------------------------
# Restore
# ----------------------------------------------------------------------
def test_restore_downloads_into_an_empty_folder(
    notes_root: Path, manifest: BackupManifest, tmp_path: Path
) -> None:
    drive = FakeDrive()
    sync_files(notes_root, manifest, drive)

    target = tmp_path / "restored"
    report = restore(drive, target)
    assert (report.restored, report.failed, report.ok) == (7, 0, True)
    assert (target / "plain" / "Shopping list.md").read_text(encoding="utf-8").endswith("- coffee\n")
    assert (target / "plain" / ".trash" / "Old note.md").is_file()
    assert (target / "vaults" / "encrypted" / "3f2a9ce1.vnote").read_bytes().startswith(b"VNT1")
    assert "key files" in report.message
    assert list(target.rglob("*.vnrestore")) == [], "no staging file is left behind"


def test_restore_refuses_a_non_empty_folder(notes_root: Path, manifest: BackupManifest, tmp_path: Path) -> None:
    drive = FakeDrive()
    sync_files(notes_root, manifest, drive)
    target = tmp_path / "not-empty"
    target.mkdir()
    (target / "my own note.md").write_text("mine\n", encoding="utf-8")

    with pytest.raises(BackupError) as exc:
        restore(drive, target)
    assert exc.value.code == "target_not_empty"
    assert (target / "my own note.md").read_text(encoding="utf-8") == "mine\n"


def test_restore_never_downloads_a_key_file(notes_root: Path, manifest: BackupManifest, tmp_path: Path) -> None:
    drive = FakeDrive()
    sync_files(notes_root, manifest, drive)
    drive.add_raw(
        f"{BACKUP_FOLDER_NAME}/vaults/encrypted", "personal.vnkey", b'{"key": "stolen"}'
    )

    target = tmp_path / "safe"
    report = restore(drive, target)
    assert report.restored == 7
    assert list(target.rglob("*.vnkey")) == []


def test_restore_without_a_backup_folder_says_so(manifest: BackupManifest, tmp_path: Path) -> None:
    drive = FakeDrive()
    with pytest.raises(BackupError) as exc:
        restore(drive, tmp_path / "empty")
    assert exc.value.code == "no_backup_folder"
    assert not drive.exists(BACKUP_FOLDER_NAME), "a failed restore creates nothing"


# ----------------------------------------------------------------------
# Prune (M10 extra)
# ----------------------------------------------------------------------
def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_prune_removes_only_old_locally_deleted_files(notes_root: Path, manifest: BackupManifest) -> None:
    drive = FakeDrive()
    sync_files(notes_root, manifest, drive)
    now = datetime.now(timezone.utc)

    (notes_root / "plain" / "Home lab.md").unlink()
    manifest.record(
        "plain/Home lab.md", manifest.get("plain/Home lab.md")["drive_id"], "x", uploaded=_stamp(now - timedelta(days=45))
    )
    (notes_root / "plain" / "Shopping list.md").unlink()
    manifest.record(
        "plain/Shopping list.md",
        manifest.get("plain/Shopping list.md")["drive_id"],
        "x",
        uploaded=_stamp(now - timedelta(days=2)),
    )

    removed = prune_deleted(notes_root, manifest, drive)
    assert removed == ["plain/Home lab.md"], "only files gone for more than 30 days"
    assert not drive.exists(f"{BACKUP_FOLDER_NAME}/plain/Home lab.md")
    assert drive.exists(f"{BACKUP_FOLDER_NAME}/plain/Shopping list.md")
    assert "plain/Home lab.md" not in manifest.to_dict()
    manifest.save()
    assert "plain/Home lab.md" not in json.loads(manifest.path.read_text(encoding="utf-8"))


def test_prune_keeps_files_that_still_exist_locally(notes_root: Path, manifest: BackupManifest) -> None:
    drive = FakeDrive()
    sync_files(notes_root, manifest, drive)
    for key in list(manifest.to_dict()):
        manifest.record(key, manifest.get(key)["drive_id"], "x", uploaded=_stamp(datetime(2020, 1, 1, tzinfo=timezone.utc)))

    old_drive = FakeDrive()
    assert prune_deleted(notes_root, manifest, old_drive) == []
    assert old_drive.calls == []


# ----------------------------------------------------------------------
# The background worker (section 8.2: never freeze the UI)
# ----------------------------------------------------------------------
def _runner_for(notes_root: Path, manifest_path: Path, drive: FakeDrive, **kwargs) -> BackupRunner:
    def context() -> dict:
        loaded = BackupManifest(manifest_path)
        loaded.load()
        return {"notes_root": notes_root, "manifest": loaded, "folder_id": None}

    return BackupRunner(context_provider=context, service_factory=lambda: drive, **kwargs)


def test_runner_emits_progress_and_done_events(notes_root: Path, tmp_path: Path) -> None:
    events: list[tuple[str, dict]] = []
    persisted: list[dict] = []
    manifest_path = tmp_path / "backup_manifest.json"
    runner = _runner_for(
        notes_root,
        manifest_path,
        FakeDrive(),
        emit=lambda name, data: events.append((name, data)),
        on_result=lambda report: persisted.append(report.to_dict()),
    )
    runner.configure(enabled=False, interval_minutes=60)
    report = runner.run_once("backup")

    assert report.uploaded == 7
    assert [name for name, _ in events][0] == "backup_progress"
    assert events[-1][0] == "backup_done"
    assert events[-1][1]["uploaded"] == 7
    assert events[-1][1]["last_backup"], "the finished time doubles as last_backup"
    assert persisted[0]["ok"] is True
    assert manifest_path.is_file(), "the manifest lands in the app-settings folder"


def test_runner_turns_a_signin_problem_into_data(notes_root: Path, tmp_path: Path) -> None:
    events: list[tuple[str, dict]] = []

    def refused() -> FakeDrive:
        raise DriveAuthError("reconnect_required", "Google refused the saved sign-in.")

    runner = BackupRunner(
        context_provider=lambda: {
            "notes_root": notes_root,
            "manifest": BackupManifest(tmp_path / "backup_manifest.json"),
            "folder_id": None,
        },
        service_factory=refused,
        emit=lambda name, data: events.append((name, data)),
    )
    report = runner.run_once("backup")
    assert report.ok is False
    assert "refused" in report.message
    assert events[-1] == ("backup_done", report.to_dict())
    assert events[-1][1]["ok"] is False, "the UI shows a toast; the app does not crash"


def test_runner_runs_one_job_at_a_time(notes_root: Path, tmp_path: Path) -> None:
    gate = threading.Event()
    inside = threading.Event()
    drive = FakeDrive()
    real_upload = drive.upload

    def slow_upload(name: str, parent_id: str, path: Path) -> str:
        inside.set()
        gate.wait(timeout=10)
        return real_upload(name, parent_id, path)

    drive.upload = slow_upload  # type: ignore[method-assign]
    runner = _runner_for(notes_root, tmp_path / "backup_manifest.json", drive)

    assert runner.start("backup") == {"started": True, "kind": "backup"}
    assert inside.wait(timeout=5), "the worker should be uploading"
    assert runner.running is True
    assert runner.start("backup") == {"started": False, "reason": "busy"}
    gate.set()
    assert runner.wait(timeout=10) is True
    assert runner.running is False and runner.kind is None


def test_runner_finish_on_close_respects_the_setting(notes_root: Path, tmp_path: Path) -> None:
    done: list[int] = []
    runner = _runner_for(
        notes_root, tmp_path / "backup_manifest.json", FakeDrive(), on_result=lambda report: done.append(1)
    )
    runner.configure(enabled=False, interval_minutes=60)
    assert runner.finish_on_close(timeout=5) is True
    assert done == [], "closing without auto-backup sends nothing"

    runner.configure(enabled=True, interval_minutes=60)
    runner.auto.stop()  # the interval itself is not what this test is about
    assert runner.finish_on_close(timeout=20) is True
    assert done == [1]
    assert (tmp_path / "backup_manifest.json").is_file()


def test_interval_timer_arms_disarms_and_fires() -> None:
    fired: list[int] = []
    timer = IntervalTimer(0.001, lambda: fired.append(1))
    assert timer.running is False, "creating the timer must not start a thread"

    timer.start()
    assert timer.running is True
    deadline = time.monotonic() + 5
    while not fired and time.monotonic() < deadline:
        time.sleep(0.01)
    assert fired, "an interval of 0.06 seconds must fire"
    timer.stop()
    assert timer.running is False

    count = len(fired)
    timer.fire()
    assert len(fired) == count + 1
    timer.fire()  # a manual fire never leaves the timer in a broken state
    assert len(fired) == count + 2
    timer.set_minutes(30)
    assert timer.minutes == 30


def test_backup_report_shape_is_json_safe() -> None:
    report = BackupReport(kind="backup", ok=True, uploaded=2, finished="2026-09-26T10:00:00Z")
    payload = report.to_dict()
    assert json.loads(json.dumps(payload))["uploaded"] == 2
    assert payload["last_backup"] == "2026-09-26T10:00:00Z"
    assert payload["kind"] == "backup"
