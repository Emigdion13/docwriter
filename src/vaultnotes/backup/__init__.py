"""Google Drive backup for VaultNotes (Build Plan section 8).

The two modules in here stay free of pywebview, so the backup decisions can be
unit-tested with a fake Drive client and no network (section 11, "keep the
engine separate from the look").
"""

from vaultnotes.backup.gdrive_auth import (
    BACKUP_FOLDER_NAME,
    DriveAuthError,
    SCOPES,
    client_secret_path,
    disconnect,
    is_connected,
    sign_in,
)
from vaultnotes.backup.gdrive_backup import (
    BackupManifest,
    BackupReport,
    BackupRunner,
    DriveFileGone,
    GoogleDriveClient,
    decide,
    ensure_backup_root,
    iter_backup_files,
    prune_deleted,
    restore,
    sha256_file,
    should_backup,
    sync_files,
)

__all__ = [
    "BACKUP_FOLDER_NAME",
    "SCOPES",
    "BackupManifest",
    "BackupReport",
    "BackupRunner",
    "DriveAuthError",
    "DriveFileGone",
    "GoogleDriveClient",
    "client_secret_path",
    "decide",
    "ensure_backup_root",
    "iter_backup_files",
    "disconnect",
    "is_connected",
    "prune_deleted",
    "restore",
    "sha256_file",
    "should_backup",
    "sign_in",
    "sync_files",
]
