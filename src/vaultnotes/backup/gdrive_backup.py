"""One-way backup of the notes folder to Google Drive (Build Plan section 8.2).

The rules that matter here:

* **Included:** ``*.md``, ``*.vnote`` and ``vault.json``.  **Skipped:** ``*.vnkey``,
  ``*.tmp`` and everything else - key files are never uploaded even if someone
  puts one inside the notes folder by hand (security rule 6).
* **The manifest decides the work.**  Each file's SHA-256 is compared with
  ``backup_manifest.json``: unchanged → skipped, changed → ``files().update``,
  new → ``files().create``.  A 404 from Drive means the entry is created again,
  and a missing manifest makes the code look files up by name and parent folder
  before creating, so no duplicates appear (section 8.2).
* **Deleted locally is not deleted from Drive.**  v1 only adds and updates;
  Drive keeps older versions of updated files (section 8.2, step 5).
* **No decrypted text touches this code.**  Uploads read the file already on
  disk (plaintext for Plain notes, ciphertext for vault notes), so a vault note
  is never materialised for the network (security rule 3).
* **Errors are data.**  A failed file is counted and reported; nothing here
  raises into the app or kills the worker thread (section 8.2).
"""

from __future__ import annotations

import hashlib
import io
import os
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from vaultnotes.storage.atomic import atomic_write

#: Name of the Drive folder created on the first backup (section 8.2).
BACKUP_FOLDER_NAME = "VaultNotes Backup"

#: The only extensions the backup ever reads (section 8.2, step 1).
INCLUDE_SUFFIXES = frozenset({".md", ".vnote"})

#: Extra file names included regardless of extension.
INCLUDE_FILE_NAMES = frozenset({"vault.json"})

#: Refused even when they would otherwise match (security rule 6).
NEVER_SUFFIXES = frozenset({".vnkey", ".tmp"})

#: Where the manifest lives inside the app-settings folder.
MANIFEST_NAME = "backup_manifest.json"

#: Save the manifest after this many successful files, and at the end.
MANIFEST_FLUSH_EVERY = 25

#: How many example file names a report may mention.
MAX_REPORTED_ERRORS = 3


class BackupError(RuntimeError):
    """A backup problem with a Bridge-friendly code and user-facing text."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class DriveFileGone(FileNotFoundError):
    """The Drive side answer was HTTP 404: the file is no longer there."""


def _now_iso() -> str:
    """Timestamp in the same format the note files use (section 4.4)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ----------------------------------------------------------------------
# Which files, and what changed
# ----------------------------------------------------------------------
def should_backup(path: Path | str) -> bool:
    """Whether one file belongs in the backup (section 8.2, step 1)."""
    name = Path(path).name
    suffix = Path(name).suffix.lower()
    if suffix in NEVER_SUFFIXES:
        return False
    if name.endswith(".tmp"):
        return False
    if suffix in INCLUDE_SUFFIXES:
        return True
    return name in INCLUDE_FILE_NAMES


def iter_backup_files(root: Path | str) -> list[tuple[str, Path]]:
    """Return ``[(relative/key.md, absolute path), ...]`` for a notes root.

    Keys always use ``/`` because they are also the manifest's keys and the
    folder names recreated in Drive.  Hidden folders are skipped apart from
    ``.trash``, whose entries stay backed up (vault notes in it are still
    encrypted).  The result is sorted, so runs are reproducible.
    """
    base = Path(root)
    found: list[tuple[str, Path]] = []
    if not base.is_dir():
        return found

    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        relative_parts = path.relative_to(base).parts
        if any(
            part.startswith(".") and part != ".trash"
            for part in relative_parts[:-1]
        ):
            continue
        if not should_backup(path):
            continue
        found.append(("/".join(relative_parts), path))
    return found


def sha256_file(path: Path | str, *, chunk_size: int = 1 << 20) -> str:
    """Hash a file without holding it in memory (notes can be large)."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class FileDecision:
    """What the manifest says must happen to one file."""

    key: str
    path: Path
    action: str  # "upload" | "update" | "skip"
    sha256: str
    drive_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """A JSON-safe shape for tests and debugging (no file contents)."""
        return {
            "key": self.key,
            "action": self.action,
            "sha256": self.sha256,
            "drive_id": self.drive_id,
        }


class BackupManifest:
    """``backup_manifest.json``: what has already been uploaded (section 8.2).

    ``{"plain/Shopping list.md": {"drive_id": ..., "sha256": ..., "uploaded": ...}}``
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.entries: dict[str, dict[str, Any]] = {}
        #: ``True`` once the file has been read, even if it was missing or
        #: damaged.  A damaged manifest must not delete Drive's history; it
        # only means every file has to be re-checked (step 4).
        self.loaded = False
        self.damaged = False

    def load(self) -> dict[str, dict[str, Any]]:
        """Read the manifest, tolerating a missing or damaged file."""
        self.entries = {}
        self.loaded = True
        self.damaged = False
        if not self.path.is_file():
            return self.entries
        try:
            import json  # noqa: PLC0415 - only needed when the file exists

            with open(self.path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except Exception:  # noqa: BLE001 - a damaged manifest is not fatal
            self.damaged = True
            return self.entries
        if not isinstance(raw, Mapping):
            self.damaged = True
            return self.entries
        for key, value in raw.items():
            if isinstance(value, Mapping) and value.get("drive_id"):
                self.entries[str(key)] = {
                    "drive_id": str(value["drive_id"]),
                    "sha256": str(value.get("sha256", "") or ""),
                    "uploaded": str(value.get("uploaded", "") or ""),
                }
        return self.entries

    def save(self) -> None:
        """Write the manifest atomically (it is app data, not a note)."""
        import json  # noqa: PLC0415 - keeps module import cheap

        body = json.dumps(self.entries, indent=2, sort_keys=True) + "\n"
        atomic_write(self.path, body.encode("utf-8"))

    def get(self, key: str) -> dict[str, Any] | None:
        return self.entries.get(key)

    def record(self, key: str, drive_id: str, sha256: str, *, uploaded: str | None = None) -> None:
        """Remember one finished upload."""
        self.entries[key] = {
            "drive_id": drive_id,
            "sha256": sha256,
            "uploaded": uploaded or _now_iso(),
        }

    def drop(self, key: str) -> None:
        self.entries.pop(key, None)

    def keys(self) -> Iterable[str]:
        return tuple(self.entries.keys())

    def __len__(self) -> int:
        return len(self.entries)

    def to_dict(self) -> dict[str, dict[str, Any]]:
        return {key: dict(value) for key, value in self.entries.items()}


def decide(
    root: Path | str,
    manifest: BackupManifest,
    *,
    files: Iterable[tuple[str, Path]] | None = None,
    hash_fn: Callable[[Path], str] = sha256_file,
) -> list[FileDecision]:
    """Compare every candidate file with the manifest (section 8.2, step 2)."""
    base = Path(root)
    decisions: list[FileDecision] = []
    for key, path in files if files is not None else iter_backup_files(base):
        try:
            digest = hash_fn(path)
        except OSError:
            # Unreadable (locked, vanished mid-scan): reported, never fatal.
            decisions.append(FileDecision(key=key, path=path, action="unreadable", sha256=""))
            continue
        entry = manifest.get(key)
        if entry and entry.get("drive_id") and entry.get("sha256") == digest:
            action = "skip"
        elif entry and entry.get("drive_id"):
            action = "update"
        else:
            action = "upload"
        decisions.append(
            FileDecision(
                key=key,
                path=path,
                action=action,
                sha256=digest,
                drive_id=entry.get("drive_id") if entry else None,
            )
        )
    return decisions


# ----------------------------------------------------------------------
# The Drive side
# ----------------------------------------------------------------------
class DriveApi(Protocol):
    """The narrow Drive interface VaultNotes needs.

    The fake used in ``tests/test_backup_manifest.py`` implements exactly this,
    so the decisions above are testable with no network at all.
    """

    def list_children(self, folder_id: str) -> list[dict[str, str]]: ...

    def find_child(self, name: str, folder_id: str) -> dict[str, str] | None: ...

    def create_folder(self, name: str, parent_id: str | None) -> str: ...

    def upload(self, name: str, parent_id: str, path: Path) -> str: ...

    def replace(self, file_id: str, path: Path) -> None: ...

    def download(self, file_id: str, dest: Path) -> None: ...

    def delete(self, file_id: str) -> None: ...


class GoogleDriveClient:
    """:class:`DriveApi` on top of ``google-api-python-client``.

    Only ``drive.file`` scope is requested, so every call sees only files this
    app created (security rule 10).
    """

    FOLDER_MIME = "application/vnd.google-apps.folder"

    def __init__(self, service: Any) -> None:
        self.service = service

    @staticmethod
    def _escape(name: str) -> str:
        """Escape a name for a Drive ``q`` filter."""
        return name.replace("\\", "\\\\").replace("'", "\\'")

    def list_children(self, folder_id: str) -> list[dict[str, str]]:
        """Every non-trashed child of a folder, paging as needed."""
        children: list[dict[str, str]] = []
        page_token: str | None = None
        query = f"'{folder_id}' in parents and trashed = false"
        while True:
            request = self.service.files().list(
                q=query,
                fields="nextPageToken, files(id, name, mimeType, size, modifiedTime)",
                pageSize=1000,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                **({"pageToken": page_token} if page_token else {}),
            )
            response = request.execute()
            for item in response.get("files", []):
                children.append(
                    {
                        "id": item["id"],
                        "name": item.get("name", ""),
                        "mimeType": item.get("mimeType", ""),
                    }
                )
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return children

    def find_child(self, name: str, folder_id: str) -> dict[str, str] | None:
        query = (
            f"name = '{self._escape(name)}' and '{folder_id}' in parents "
            "and trashed = false"
        )
        response = (
            self.service.files()
            .list(
                q=query,
                fields="files(id, name, mimeType)",
                pageSize=10,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        for item in response.get("files", []):
            return {"id": item["id"], "name": item.get("name", ""), "mimeType": item.get("mimeType", "")}
        return None

    def create_folder(self, name: str, parent_id: str | None) -> str:
        body: dict[str, Any] = {"name": name, "mimeType": self.FOLDER_MIME}
        if parent_id:
            body["parents"] = [parent_id]
        created = (
            self.service.files()
            .create(body=body, fields="id", supportsAllDrives=True)
            .execute()
        )
        return str(created["id"])

    @staticmethod
    def _media(path: Path) -> Any:
        """The file's bytes, read up front.

        A ``MediaFileUpload`` keeps the note open for the whole upload, and on
        Windows an open file cannot be replaced -- so every save of that note
        failed with "Access is denied" until the upload finished.
        """
        from googleapiclient.http import MediaIoBaseUpload  # noqa: PLC0415

        return MediaIoBaseUpload(
            io.BytesIO(path.read_bytes()), mimetype="application/octet-stream", resumable=False
        )

    def upload(self, name: str, parent_id: str, path: Path) -> str:
        """``files().create`` with the file's bytes (section 8.2)."""
        body: dict[str, Any] = {"name": name, "parents": [parent_id]}
        media = self._media(path)
        created = (
            self.service.files()
            .create(body=body, media_body=media, fields="id", supportsAllDrives=True)
            .execute()
        )
        return str(created["id"])

    def replace(self, file_id: str, path: Path) -> None:
        """``files().update(fileId=..., media_body=...)`` (section 8.2)."""
        media = self._media(path)
        try:
            self.service.files().update(fileId=file_id, media_body=media, supportsAllDrives=True).execute()
        except Exception as exc:  # noqa: BLE001 - only 404 is reinterpreted
            if _is_not_found(exc):
                raise DriveFileGone(file_id) from exc
            raise

    def download(self, file_id: str, dest: Path) -> None:
        """Stream one file to *dest*, which the caller has already chosen."""
        from googleapiclient.http import MediaIoBaseDownload  # noqa: PLC0415

        request = self.service.files().get_media(fileId=file_id, supportsAllDrives=True)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as handle:
            try:
                downloader = MediaIoBaseDownload(handle, request)
                while True:
                    _, done = downloader.next_chunk()
                    if done:
                        break
                handle.flush()
                os.fsync(handle.fileno())
            except Exception as exc:  # noqa: BLE001
                if _is_not_found(exc):
                    raise DriveFileGone(file_id) from exc
                raise

    def delete(self, file_id: str) -> None:
        try:
            self.service.files().delete(fileId=file_id, supportsAllDrives=True).execute()
        except Exception as exc:  # noqa: BLE001
            if _is_not_found(exc):
                raise DriveFileGone(file_id) from exc
            raise


def _is_not_found(exc: BaseException) -> bool:
    """True for the Drive API's HTTP 404 error object."""
    status = getattr(getattr(exc, "resp", None), "status", None)
    if status == 404:
        return True
    return "404" in str(getattr(exc, "status_code", "") or "")


# ----------------------------------------------------------------------
# Backup run
# ----------------------------------------------------------------------
ProgressFn = Callable[[int, int, str], None]


@dataclass
class BackupReport:
    """The outcome of one sync or restore (fed to ``backup_done``)."""

    kind: str = "backup"
    ok: bool = True
    scanned: int = 0
    uploaded: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0
    restored: int = 0
    errors: list[str] = field(default_factory=list)
    started: str = ""
    finished: str = ""
    message: str = ""
    drive_folder_id: str | None = None
    cancelled: bool = False

    def to_dict(self) -> dict[str, Any]:
        """JSON data for the Bridge API / the ``backup_done`` event."""
        return {
            "ok": self.ok,
            "kind": self.kind,
            "scanned": self.scanned,
            "uploaded": self.uploaded,
            "updated": self.updated,
            "skipped": self.skipped,
            "failed": self.failed,
            "restored": self.restored,
            "errors": list(self.errors),
            "started": self.started,
            "finished": self.finished,
            "last_backup": self.finished or None,
            "message": self.message,
            "drive_folder_id": self.drive_folder_id,
            "cancelled": self.cancelled,
        }


def ensure_backup_root(
    client: DriveApi,
    *,
    folder_id: str | None = None,
    on_created: Callable[[str], None] | None = None,
    create: bool = True,
) -> str:
    """Find or create ``VaultNotes Backup`` and return its id (section 8.2).

    ``create=False`` is used by a restore: reading must never leave an empty
    backup folder behind in the user's Drive.
    """
    if folder_id:
        try:
            client.list_children(folder_id)
            return folder_id
        except DriveFileGone:
            pass  # deleted in Drive since the last run: look it up by name
    existing = client.find_child(BACKUP_FOLDER_NAME, "root")
    if existing and existing.get("mimeType") == GoogleDriveClient.FOLDER_MIME:
        if on_created is not None and not folder_id:
            on_created(existing["id"])
        return existing["id"]
    if not create:
        raise BackupError(
            "no_backup_folder",
            f"Your Drive has no '{BACKUP_FOLDER_NAME}' folder to restore from.",
        )
    created = client.create_folder(BACKUP_FOLDER_NAME, "root")
    if on_created is not None and not folder_id:
        on_created(created)
    return created


def _folder_for(
    client: DriveApi,
    root_id: str,
    relative_key: str,
    cache: dict[str, str],
) -> str:
    """Return the Drive folder id mirroring a local file's directory.

    Subfolders are created lazily, so a notes folder that has no ``.trash``
    yet does not gain an empty one in Drive.
    """
    parent_parts = Path(relative_key).parent.parts
    cache_key = "/".join(parent_parts)
    if cache_key in cache:
        return cache[cache_key]
    current = root_id
    walked: list[str] = []
    for part in parent_parts:
        walked.append(part)
        walked_key = "/".join(walked)
        if walked_key in cache:
            current = cache[walked_key]
            continue
        found = client.find_child(part, current)
        if found and found.get("mimeType") == GoogleDriveClient.FOLDER_MIME:
            current = found["id"]
        else:
            current = client.create_folder(part, current)
        cache[walked_key] = current
    cache[cache_key] = current
    return current


def _label(kind: str, done: int, total: int) -> str:
    """Status-bar text, e.g. ``Backing up 3/12…`` (section 8.2)."""
    word = "Backing up" if kind == "backup" else "Restoring"
    return f"{word} {done}/{total}…"


def sync_files(
    notes_root: Path | str,
    manifest: BackupManifest,
    client: DriveApi,
    *,
    root_folder_id: str | None = None,
    on_folder: Callable[[str], None] | None = None,
    progress: ProgressFn | None = None,
    decisions: Iterable[FileDecision] | None = None,
    report: BackupReport | None = None,
    kind: str = "backup",
) -> BackupReport:
    """Push changed files to Drive (section 8.2, "Each backup run")."""
    root = Path(notes_root)
    if not manifest.loaded:
        # Loading is the caller's job in api.py, and a forgotten load must not
        # turn into a pointless re-upload of every file in the notes folder.
        manifest.load()
    result = report or BackupReport(kind=kind)
    result.started = result.started or _now_iso()
    if root_folder_id is None:
        root_folder_id = ensure_backup_root(client, folder_id=None, on_created=on_folder)
    else:
        root_folder_id = ensure_backup_root(
            client, folder_id=root_folder_id, on_created=on_folder
        )
    result.drive_folder_id = root_folder_id

    if decisions is None:
        decisions = decide(root, manifest)
    steps = list(decisions)
    total = len(steps)
    result.scanned = total

    folder_cache: dict[str, str] = {}
    handled = 0
    for decision in steps:
        handled += 1
        if progress is not None:
            progress(handled - 1, total, _label(kind, handled, total))
        if decision.action == "skip":
            result.skipped += 1
            continue
        if decision.action == "unreadable":
            result.failed += 1
            if len(result.errors) < MAX_REPORTED_ERRORS:
                result.errors.append(f"{decision.key} could not be read")
            continue

        try:
            folder_id = _folder_for(client, root_folder_id, decision.key, folder_cache)
            name = Path(decision.key).name
            drive_id = decision.drive_id
            if drive_id is None:
                # No manifest entry: look it up by name and parent first so a
                # missing manifest never duplicates a file (step 4).
                found = client.find_child(name, folder_id)
                if found and found.get("mimeType") != GoogleDriveClient.FOLDER_MIME:
                    drive_id = found["id"]
            if drive_id is None:
                drive_id = client.upload(name, folder_id, decision.path)
                result.uploaded += 1
            else:
                try:
                    client.replace(drive_id, decision.path)
                    result.updated += 1
                except DriveFileGone:
                    # Step 3: the Drive file is gone, so create it again.
                    drive_id = client.upload(name, folder_id, decision.path)
                    result.uploaded += 1
            manifest.record(decision.key, drive_id, decision.sha256)
            if (result.uploaded + result.updated) % MANIFEST_FLUSH_EVERY == 0:
                manifest.save()
        except DriveFileGone:
            result.failed += 1
            if len(result.errors) < MAX_REPORTED_ERRORS:
                result.errors.append(f"{decision.key} could not be written to Drive")
        except OSError:
            result.failed += 1
            if len(result.errors) < MAX_REPORTED_ERRORS:
                result.errors.append(f"{decision.key} could not be read from disk")
        except Exception:  # noqa: BLE001 - a backup error must never crash (8.2)
            result.failed += 1
            if len(result.errors) < MAX_REPORTED_ERRORS:
                result.errors.append(f"{decision.key} could not be uploaded")

    manifest.save()
    result.finished = _now_iso()
    result.ok = result.failed == 0
    if result.failed:
        extra = f" ({', '.join(result.errors)})" if result.errors else ""
        result.message = f"{result.failed} of {total} files were not uploaded{extra}."
    elif result.uploaded or result.updated:
        result.message = (
            f"Uploaded {result.uploaded} new and {result.updated} changed files."
        )
    else:
        result.message = "Everything was already up to date."
    if progress is not None:
        progress(total, total, _label(kind, total, total))
    return result


# ----------------------------------------------------------------------
# Restore
# ----------------------------------------------------------------------
def _safe_drive_name(name: object) -> bool:
    """Whether a Drive file or folder name is safe to use as one path part."""
    return (
        isinstance(name, str)
        and name not in {"", ".", ".."}
        and not any(char in name for char in '/\\:')
        and not any(ord(char) < 32 for char in name)
        and name == name.rstrip(" .")
    )


def restore(
    client: DriveApi,
    target_dir: Path | str,
    *,
    root_folder_id: str | None = None,
    progress: ProgressFn | None = None,
    kind: str = "restore",
) -> BackupReport:
    """Download the backup into an empty folder (section 8.2, "Restore").

    Only the file names the backup is allowed to upload are written, so a stray
    ``*.vnkey`` in Drive could never be pulled down over a local one.
    """
    dest = Path(target_dir)
    if dest.exists() and any(dest.iterdir()):
        raise BackupError(
            "target_not_empty",
            "Restore needs an empty folder so nothing of yours is overwritten.",
        )
    dest.mkdir(parents=True, exist_ok=True)

    result = BackupReport(kind=kind, started=_now_iso())
    root_id = ensure_backup_root(client, folder_id=root_folder_id, create=False)
    result.drive_folder_id = root_id

    collected: list[tuple[str, str]] = []
    unsafe: list[str] = []

    def walk(folder_id: str, prefix: str) -> None:
        for child in client.list_children(folder_id):
            name = child.get("name", "")
            key = f"{prefix}{name}"
            if not _safe_drive_name(name):
                # Drive allows "..", "/" and "\\" in names; followed blindly, a
                # renamed file could be written over live notes elsewhere.
                unsafe.append(key)
                continue
            if child.get("mimeType") == GoogleDriveClient.FOLDER_MIME:
                walk(child["id"], f"{key}/")
            elif should_backup(name):
                collected.append((key, child["id"]))

    walk(root_id, "")
    total = len(collected)
    for index, (key, file_id) in enumerate(collected, start=1):
        if progress is not None:
            progress(index - 1, total, _label(kind, index, total))
        final = dest / Path(key)
        if not final.resolve().is_relative_to(dest.resolve()):
            unsafe.append(key)
            continue
        # Written under a staging name and renamed, so a half downloaded file
        # is never mistaken for a note (and never picked up by a backup).
        staging = final.with_name(final.name + ".vnrestore")
        try:
            final.parent.mkdir(parents=True, exist_ok=True)
            client.download(file_id, staging)
            os.replace(staging, final)
            result.restored += 1
        except Exception:  # noqa: BLE001 - one bad file must not stop the rest
            try:
                staging.unlink()
            except OSError:
                pass
            result.failed += 1
            if len(result.errors) < MAX_REPORTED_ERRORS:
                result.errors.append(f"{key} could not be downloaded")

    for key in unsafe:
        result.failed += 1
        if len(result.errors) < MAX_REPORTED_ERRORS:
            result.errors.append(f"{key!r} was skipped: its name could point outside the restore folder")
    result.scanned = total
    result.finished = _now_iso()
    result.ok = result.failed == 0
    result.message = (
        f"Restored {result.restored} files. Vaults still need their key files to open."
        if result.restored
        else "That Drive folder has no VaultNotes backup yet."
    )
    if progress is not None:
        progress(total, total, _label(kind, total, total))
    return result


# ----------------------------------------------------------------------
# Prune (M10): remove Drive copies of files deleted long ago
# ----------------------------------------------------------------------
def prune_deleted(
    notes_root: Path | str,
    manifest: BackupManifest,
    client: DriveApi,
    *,
    older_than_days: int = 30,
    now: datetime | None = None,
    progress: ProgressFn | None = None,
) -> list[str]:
    """Delete Drive copies of files gone from disk for over 30 days.

    The manifest keeps entries after a local delete, which is what makes this
    possible: an entry whose file no longer exists and whose ``uploaded`` time
    is older than the cut-off is the file the user deleted long ago.
    """
    root = Path(notes_root)
    moment = now or datetime.now(timezone.utc)
    entries = [
        (key, entry)
        for key, entry in manifest.to_dict().items()
        if not (root / key).exists()
    ]
    if progress is not None:
        progress(0, len(entries), f"Pruning 0/{len(entries)}…")
    removed: list[str] = []
    for index, (key, entry) in enumerate(entries, start=1):
        if progress is not None:
            progress(index - 1, len(entries), f"Pruning {index}/{len(entries)}…")
        if not _older_than(entry.get("uploaded", ""), moment, older_than_days):
            continue
        try:
            client.delete(entry["drive_id"])
        except DriveFileGone:
            pass  # already gone in Drive: the local entry can go too
        except Exception:  # noqa: BLE001 - never crash on one file
            continue
        manifest.drop(key)
        removed.append(key)
    if removed:
        manifest.save()
    return removed


def _older_than(stamp: str, moment: datetime, days: int) -> bool:
    """Whether an ISO timestamp is more than ``days`` old (unknown = yes-safe)."""
    try:
        uploaded = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        # No usable timestamp: treat it as old enough to prune, because the
        # file is also gone from the computer.
        return True
    if uploaded.tzinfo is None:
        uploaded = uploaded.replace(tzinfo=timezone.utc)
    return (moment - uploaded).days >= max(0, int(days))


# ----------------------------------------------------------------------
# The background worker (section 8.2: "Backups run in a background thread")
# ----------------------------------------------------------------------
class IntervalTimer:
    """Fires ``callback`` every ``minutes`` in a daemon thread.

    Mirrors :class:`vaultnotes.autolock.AutoLock` so the app has one familiar
    pattern for background timing, and can be driven deterministically in
    tests with :meth:`fire` and a fake clock.
    """

    def __init__(
        self,
        minutes: float,
        callback: Callable[[], None],
        *,
        clock: Callable[[], float] = time.monotonic,
        start: bool = False,
    ) -> None:
        self.callback = callback
        self._clock = clock
        self.minutes = max(0.001, float(minutes))
        self._condition = threading.Condition()
        self._deadline: float | None = None
        self._stopped = True
        self._thread: threading.Thread | None = None
        if start:
            self.start()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stopped)

    def set_minutes(self, minutes: float) -> None:
        """Change the interval and re-arm if the timer is running."""
        with self._condition:
            self.minutes = max(0.001, float(minutes))
            if self._deadline is not None:
                self._deadline = self._clock() + self.minutes * 60.0
            self._condition.notify_all()

    def start(self) -> None:
        """Arm the timer (no-op when already armed)."""
        with self._condition:
            if self.running:
                return
            self._stopped = False
            self._deadline = self._clock() + self.minutes * 60.0
            self._thread = threading.Thread(
                target=self._run, name="VaultNotes-auto-backup", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        """Disarm and stop the worker."""
        with self._condition:
            self._stopped = True
            self._deadline = None
            self._condition.notify_all()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        self._thread = None

    def fire(self) -> None:
        """Run the callback once, right now (tests and "back up on close")."""
        try:
            self.callback()
        except Exception:  # noqa: BLE001 - a timer thread must survive
            pass

    def _run(self) -> None:
        while True:
            with self._condition:
                if self._stopped:
                    return
                remaining = (self._deadline or 0.0) - self._clock()
                if remaining > 0:
                    self._condition.wait(timeout=remaining)
                    continue
                self._deadline = self._clock() + self.minutes * 60.0
            self.fire()


class BackupRunner:
    """Runs syncs and restores one at a time, in a thread, reporting by event.

    Everything the engine needs from the outside is injected, which is what
    lets ``tests/test_backup_manifest.py`` drive a whole run with a fake
    client and no network:

    ``service_factory``
        Returns a fresh :class:`DriveApi` (raising :class:`BackupError` or
        ``DriveAuthError`` when the user is not connected).
    ``context_provider``
        Returns ``{"notes_root", "manifest", "folder_id"}`` for the next run.
    ``on_result``
        Called with the finished :class:`BackupReport` on the worker thread;
        ``api.py`` persists timestamps here.
    ``emit``
        ``emit(event_name, data)`` - used for ``backup_progress`` and
        ``backup_done``.
    """

    def __init__(
        self,
        *,
        context_provider: Callable[[], dict[str, Any]],
        service_factory: Callable[[], DriveApi],
        on_result: Callable[[BackupReport], None] | None = None,
        emit: Callable[[str, Any], None] | None = None,
        auto_callback: Callable[[], None] | None = None,
    ) -> None:
        self._context_provider = context_provider
        self._service_factory = service_factory
        self._on_result = on_result
        self._emit = emit
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._kind: str | None = None
        self.auto = IntervalTimer(60.0, auto_callback or (lambda: None))
        #: Whether a final sync should be attempted when the app closes; the
        #: bridge turns it on with the auto-backup setting.
        self._close_backup_enabled = False

    # -- state ---------------------------------------------------------
    @property
    def running(self) -> bool:
        """Whether a sync or restore is in flight."""
        thread = self._thread
        return bool(thread and thread.is_alive())

    @property
    def kind(self) -> str | None:
        return self._kind

    # -- entry points --------------------------------------------------
    def start(self, kind: str = "backup", target_dir: Path | str | None = None) -> dict[str, Any]:
        """Kick off a run in the background.  ``{"started": False}`` if busy."""
        if self.running:
            return {"started": False, "reason": "busy"}
        thread = threading.Thread(
            target=self._run_guarded, args=(kind, target_dir), name=f"VaultNotes-{kind}", daemon=True
        )
        self._kind = kind
        self._thread = thread
        thread.start()
        return {"started": True, "kind": kind}

    def run_once(
        self, kind: str = "backup", target_dir: Path | str | None = None
    ) -> BackupReport:
        """Do the work on the calling thread (used by tests and app close)."""
        return self._execute(kind, target_dir)

    def wait(self, timeout: float = 20.0) -> bool:
        """Join the worker; ``False`` when it is still running afterwards."""
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout=timeout)
        return not thread.is_alive()

    def stop(self) -> None:
        """Stop the auto-backup timer (the worker thread is a daemon)."""
        self.auto.stop()

    def configure(self, *, enabled: bool, interval_minutes: float) -> None:
        """Arm or disarm the auto-backup timer from ``settings.json``."""
        self.auto.set_minutes(max(1.0, float(interval_minutes)))
        self._close_backup_enabled = bool(enabled)
        if enabled:
            self.auto.start()
        else:
            self.auto.stop()

    def finish_on_close(self, timeout: float = 25.0) -> bool:
        """One last sync when the app closes, bounded by ``timeout``.

        Section 8.2 asks for a backup "when it closes".  It runs in a thread
        that is abandoned if Google is slow, so closing the app never hangs on
        the network.
        """
        if self.running:
            return self.wait(timeout=timeout)
        if not self._close_backup_enabled:
            return True
        done = threading.Event()

        def worker() -> None:
            try:
                self._execute("backup", None)
            finally:
                done.set()

        thread = threading.Thread(target=worker, name="VaultNotes-close-backup", daemon=True)
        thread.start()
        return done.wait(timeout=timeout)

    # -- internals -----------------------------------------------------
    def _run_guarded(self, kind: str, target_dir: Path | str | None) -> None:
        try:
            self._execute(kind, target_dir)
        except Exception as exc:  # noqa: BLE001 - never let a thread die loudly
            report = BackupReport(kind=kind, ok=False, finished=_now_iso())
            report.message = f"Backup failed ({type(exc).__name__})."
            self._emit_event("backup_done", report.to_dict())
        finally:
            self._kind = None

    def _execute(self, kind: str, target_dir: Path | str | None) -> BackupReport:
        with self._lock:  # one run at a time, section 8.2
            context = self._context_provider()
            try:
                client = self._service_factory()
            except BackupError as exc:
                return self._fail(kind, exc.code, exc.message)
            except Exception as exc:  # noqa: BLE001 - google/keyring flavours
                from vaultnotes.backup.gdrive_auth import DriveAuthError

                if isinstance(exc, DriveAuthError):
                    return self._fail(kind, exc.code, exc.message)
                return self._fail(
                    kind,
                    "network",
                    "Google Drive could not be reached. VaultNotes will try "
                    "again at the next scheduled time.",
                )

            progress = self._progress(kind)
            try:
                if kind == "restore":
                    report = restore(
                        client,
                        target_dir or context["notes_root"],
                        root_folder_id=context.get("folder_id"),
                        progress=progress,
                    )
                else:
                    report = sync_files(
                        context["notes_root"],
                        context["manifest"],
                        client,
                        root_folder_id=context.get("folder_id"),
                        on_folder=context.get("on_folder"),
                        progress=progress,
                    )
            except BackupError as exc:
                return self._fail(kind, exc.code, exc.message)
            except Exception as exc:  # noqa: BLE001
                return self._fail(
                    kind,
                    "network",
                    f"The {'restore' if kind == 'restore' else 'backup'} stopped early "
                    f"({type(exc).__name__}). Your notes on disk are unchanged.",
                )

            if self._on_result is not None:
                try:
                    self._on_result(report)
                except Exception:  # noqa: BLE001 - settings write is best effort
                    pass
            self._emit_event("backup_done", report.to_dict())
            return report

    def _fail(self, kind: str, code: str, message: str) -> BackupReport:
        report = BackupReport(kind=kind, ok=False, message=message, finished=_now_iso())
        report.drive_folder_id = None
        report.errors = [message]
        self._emit_event("backup_done", report.to_dict())
        return report

    def _progress(self, kind: str) -> ProgressFn:
        def report_progress(done: int, total: int, label: str) -> None:
            self._emit_event(
                "backup_progress",
                {"done": done, "total": total, "label": label, "kind": kind},
            )

        return report_progress

    def _emit_event(self, name: str, data: Any) -> None:
        if self._emit is None:
            return
        try:
            self._emit(name, data)
        except Exception:  # noqa: BLE001 - the UI is never a reason to stop
            pass


__all__ = [
    "BACKUP_FOLDER_NAME",
    "BackupError",
    "BackupManifest",
    "BackupReport",
    "BackupRunner",
    "DriveApi",
    "DriveFileGone",
    "FileDecision",
    "GoogleDriveClient",
    "INCLUDE_SUFFIXES",
    "IntervalTimer",
    "MANIFEST_NAME",
    "NEVER_SUFFIXES",
    "decide",
    "ensure_backup_root",
    "iter_backup_files",
    "prune_deleted",
    "restore",
    "sha256_file",
    "should_backup",
    "sync_files",
]
