"""Google sign-in for VaultNotes' Drive backup (Build Plan section 8.2).

Three rules shape this module:

* **Narrowest scope.**  Only ``drive.file`` is requested, so Google hands the
  app visibility over files VaultNotes created itself and nothing else
  (security rule 10).
* **Only the refresh token is stored, and only in the OS credential store**
  (``keyring`` service ``VaultNotes`` / user ``gdrive``).  Tokens, access keys
  and note text never land in a plain file, and nothing about them is ever
  logged (security rules 5 and 12).
* **The import is lazy.**  ``google-auth``/``google-api-python-client`` are
  imported inside the functions that need them so the rest of the app - and the
  backup manifest tests - never depend on a network library being present.

Set up once per machine with ``client_secret.json`` next to ``settings.json``;
see section 8.1 of the build plan.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

#: The one scope VaultNotes asks for (security rule 10).
SCOPES = ["https://www.googleapis.com/auth/drive.file"]

#: Name of the Drive folder that holds the backup (section 8.2).
BACKUP_FOLDER_NAME = "VaultNotes Backup"

#: ``keyring`` coordinates for the stored refresh token.
KEYRING_SERVICE = "VaultNotes"
KEYRING_USER = "gdrive"

#: File name of the OAuth client, inside the app-settings folder.
CLIENT_SECRET_FILE = "client_secret.json"


class KeyringLike(Protocol):
    """The two ``keyring`` functions this module uses, so tests can fake them."""

    def get_password(self, service: str, username: str) -> str | None: ...

    def set_password(self, service: str, username: str, password: str) -> None: ...

    def delete_password(self, service: str, username: str) -> None: ...


class DriveAuthError(RuntimeError):
    """A sign-in problem with a Bridge-friendly ``code`` and user text."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _app_dir() -> Path:
    """The app-settings folder (``%APPDATA%\\VaultNotes``)."""
    from vaultnotes.config import get_app_dir

    return get_app_dir()


def client_secret_path(app_dir: Path | str | None = None) -> Path:
    """Return where ``client_secret.json`` must live (section 8.1)."""
    base = Path(app_dir) if app_dir is not None else _app_dir()
    return base / CLIENT_SECRET_FILE


def read_client_secret(app_dir: Path | str | None = None) -> dict[str, Any]:
    """Load and sanity-check the OAuth client file.

    Raises :class:`DriveAuthError` with a message that tells the user what to
    do next instead of exposing a traceback.
    """
    path = client_secret_path(app_dir)
    if not path.is_file():
        raise DriveAuthError(
            "client_secret_missing",
            "Google sign-in needs client_secret.json in "
            f"{path.parent}. Copy it from the Google Cloud console (see the "
            "build plan, section 8.1), then try again.",
        )
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception as exc:  # noqa: BLE001 - damaged file, not a crash
        raise DriveAuthError(
            "client_secret_damaged",
            f"client_secret.json could not be read ({type(exc).__name__}).",
        ) from exc

    if not isinstance(data, dict):
        raise DriveAuthError(
            "client_secret_damaged",
            "client_secret.json must hold the JSON object Google downloaded.",
        )

    # Desktop-app clients nest their fields under "installed"; web clients
    # under "web".  Only the desktop shape is supported (section 8.1).
    client = data.get("installed") or data.get("web")
    if not isinstance(client, dict) or not client.get("client_id"):
        raise DriveAuthError(
            "client_secret_damaged",
            "client_secret.json has no client id. Download an OAuth client of "
            "type 'Desktop app' from Google Cloud Console.",
        )
    return client


def _keyring_backend(keyring: KeyringLike | None = None) -> KeyringLike:
    """Return the credential store, or explain why it is unavailable."""
    if keyring is not None:
        return keyring
    try:
        import keyring  # noqa: PLC0415 - optional at import time by design
    except Exception as exc:  # noqa: BLE001
        raise DriveAuthError(
            "keyring_unavailable",
            "The Windows Credential Manager library (keyring) is not installed, "
            "so VaultNotes has no safe place to keep the Google token.",
        ) from exc
    return keyring.get_keyring() if hasattr(keyring, "get_keyring") else keyring


class TokenStore:
    """The refresh token's only home: ``keyring``.

    A missing or unreadable credential store is reported as a clear error; the
    token is never written to disk as a fallback (security rule 5).  Tests pass
    ``memory=True`` (or their own fake backend) so no real credential store is
    touched.
    """

    def __init__(
        self,
        keyring: KeyringLike | None = None,
        *,
        memory: bool = False,
    ) -> None:
        self._injected = keyring
        self._memory: str | None = None
        self._memory_only = keyring is None and memory

    @property
    def backend(self) -> KeyringLike:
        if self._memory_only:
            return _MemoryKeyring(self)
        if self._injected is not None:
            return self._injected
        return _keyring_backend(None)

    def load(self) -> str | None:
        """Return the stored refresh token, or ``None`` when not connected."""
        if self._memory_only:
            return self._memory
        try:
            token = self.backend.get_password(KEYRING_SERVICE, KEYRING_USER)
        except Exception as exc:  # noqa: BLE001 - no backend is a normal state
            if _is_missing_keyring(exc):
                return None
            raise DriveAuthError(
                "keyring_unavailable",
                "VaultNotes could not read the saved Google token from the "
                f"credential store ({type(exc).__name__}).",
            ) from exc
        return token or None

    def save(self, refresh_token: str) -> None:
        """Store *only* the refresh token."""
        if self._memory_only:
            self._memory = refresh_token
            return
        try:
            self.backend.set_password(KEYRING_SERVICE, KEYRING_USER, refresh_token)
        except Exception as exc:  # noqa: BLE001
            raise DriveAuthError(
                "keyring_unavailable",
                "The credential store refused the Google token "
                f"({type(exc).__name__}), so sign-in cannot be completed.",
            ) from exc

    def clear(self) -> None:
        """Forget the token (used by "Disconnect" and on ``invalid_grant``)."""
        if self._memory_only:
            self._memory = None
            return
        try:
            self.backend.delete_password(KEYRING_SERVICE, KEYRING_USER)
        except Exception as exc:  # noqa: BLE001 - already gone is fine
            if not _is_missing_credential(exc):
                raise DriveAuthError(
                    "keyring_unavailable",
                    f"The token could not be removed from the credential store ({type(exc).__name__}).",
                ) from exc


class _MemoryKeyring:
    """``keyring``-shaped view of :class:`TokenStore`'s in-memory slot."""

    def __init__(self, store: TokenStore) -> None:
        self._store = store

    def get_password(self, service: str, username: str) -> str | None:  # noqa: ARG002
        return self._store._memory

    def set_password(self, service: str, username: str, password: str) -> None:  # noqa: ARG002
        self._store._memory = password

    def delete_password(self, service: str, username: str) -> None:  # noqa: ARG002
        self._store._memory = None


def _is_missing_keyring(exc: BaseException) -> bool:
    """True when the platform simply has no credential store configured."""
    name = type(exc).__name__.lower()
    return "nokeyring" in name or "keyringraised" in name or "initerror" in name


def _is_missing_credential(exc: BaseException) -> bool:
    """True when there is nothing to delete (already disconnected)."""
    return "notfound" in type(exc).__name__.lower()


def is_connected(store: TokenStore | None = None) -> bool:
    """Whether Google sign-in is stored, without touching the network."""
    try:
        return bool((store or TokenStore()).load())
    except DriveAuthError:
        return False


def get_credentials(
    app_dir: Path | str | None = None,
    store: TokenStore | None = None,
    *,
    force_refresh: bool = False,
) -> Any:
    """Rebuild authorized credentials from the stored refresh token.

    Section 8.2: later runs never re-run the browser flow - the credentials are
    recreated from ``client_secret.json`` plus the saved refresh token.  When
    Google rejects that token (``invalid_grant``, e.g. the consent was revoked
    or the 7-day test-mode window expired) the stored token is dropped and the
    user is asked to connect again.
    """
    from google.auth.transport.requests import Request  # noqa: PLC0415
    from google.oauth2.credentials import Credentials  # noqa: PLC0415

    store = store or TokenStore()
    refresh_token = store.load()
    if not refresh_token:
        raise DriveAuthError(
            "not_connected",
            "Connect Google Drive first (Settings, or Ctrl+B in the app).",
        )

    client = read_client_secret(app_dir)
    credentials = Credentials(
        token=None,
        refresh_token=refresh_token,
        client_id=client["client_id"],
        client_secret=client.get("client_secret"),
        token_uri=client.get("token_uri", "https://oauth2.googleapis.com/token"),
        scopes=SCOPES,
    )

    try:
        if force_refresh or not credentials.valid:
            credentials.refresh(Request())
    except Exception as exc:  # noqa: BLE001 - google raises many flavours
        if _is_invalid_grant(exc):
            store.clear()
            raise DriveAuthError(
                "reconnect_required",
                "Google refused the saved sign-in, so it was forgotten. Open "
                "Settings and connect Google Drive again.",
            ) from exc
        raise DriveAuthError(
            "network",
            "Google could not be reached, so the backup did not start. VaultNotes "
            "will try again at the next scheduled time.",
        ) from exc
    return credentials


def sign_in(
    app_dir: Path | str | None = None,
    store: TokenStore | None = None,
    *,
    open_browser: bool = True,
    flow_factory: Any | None = None,
) -> dict[str, Any]:
    """Run the one-time desktop OAuth flow and store the refresh token.

    ``flow_factory`` exists for tests; production uses
    :meth:`InstalledAppFlow.from_client_secrets_file` with
    ``run_local_server(port=0)`` exactly as section 8.2 prescribes.
    """
    client_secret = client_secret_path(app_dir)
    read_client_secret(app_dir)  # validates before a browser window opens

    if flow_factory is None:
        from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: PLC0415

        flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), SCOPES)
        credentials = flow.run_local_server(port=0, open_browser=open_browser)
    else:
        credentials = flow_factory(str(client_secret), SCOPES)

    refresh_token = getattr(credentials, "refresh_token", None)
    if not refresh_token:
        raise DriveAuthError(
            "declined",
            "Google did not return a refresh token, so VaultNotes cannot back up "
            "later without opening the browser again.",
        )

    (store or TokenStore()).save(refresh_token)
    return {"ok": True, "connected": True, "folder": BACKUP_FOLDER_NAME}


def disconnect(store: TokenStore | None = None) -> dict[str, Any]:
    """Forget the stored token (the Drive files themselves are left alone)."""
    (store or TokenStore()).clear()
    return {"ok": True, "connected": False}


def build_drive_service(credentials: Any) -> Any:
    """Create the Drive API client for already-refreshed credentials."""
    from googleapiclient.discovery import build  # noqa: PLC0415

    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def _is_invalid_grant(exc: BaseException) -> bool:
    """Detect the ``invalid_grant`` family of refusals from Google."""
    text = f"{type(exc).__name__} {exc}".lower()
    if "invalid_grant" in text:
        return True
    status = getattr(getattr(exc, "resp", None), "status", None) or getattr(exc, "status_code", None)
    if status in (400, 401, 403):
        return any(word in text for word in ("invalid", "revoked", "expired", "unauthorized", "consent"))
    return False


__all__ = [
    "BACKUP_FOLDER_NAME",
    "CLIENT_SECRET_FILE",
    "KEYRING_SERVICE",
    "KEYRING_USER",
    "SCOPES",
    "DriveAuthError",
    "TokenStore",
    "build_drive_service",
    "client_secret_path",
    "disconnect",
    "get_credentials",
    "is_connected",
    "read_client_secret",
    "sign_in",
]
