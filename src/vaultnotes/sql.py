"""The SQL space: query SQL Server and SQLite databases from VaultNotes.

Connections are saved in ``sql.db``, a SQLite file in
``%LOCALAPPDATA%\\VaultNotes``: outside the notes folder, so the Google Drive
backup never sees it.  A SQL login's password is never in that file; it lives
in the Windows Credential Manager (``keyring`` service ``VaultNotes SQL``).

Each query tab in the page owns one :class:`_Session`, a live connection, so a
``#temp`` table made in one run is still there for the next.  A run happens on
its own thread: the Bridge call returns at once, progress and the result come
back as ``sql_progress`` and ``sql_done`` events, and the rows stay here in
memory, where the page reads them a page at a time with :meth:`SqlManager.rows`.
"""

from __future__ import annotations

import datetime as dt
import decimal
import math
import re
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any, Callable

#: Engine ids in the order the page offers them.
ENGINES = ("mssql", "sqlite")
ENGINE_NAMES = {"mssql": "SQL Server", "sqlite": "SQLite"}
AUTH_MODES = ("windows", "sql")

#: SQL Server ODBC drivers, best first.  "SQL Server" ships with Windows.
MSSQL_DRIVERS = (
    "ODBC Driver 18 for SQL Server",
    "ODBC Driver 17 for SQL Server",
    "ODBC Driver 13 for SQL Server",
    "SQL Server Native Client 11.0",
    "SQL Server",
)
#: The driver built into Windows: old, but always there.
LEGACY_DRIVER = "SQL Server"

#: Query tabs open at once (one connection each).
MAX_SESSIONS = 8
#: Saved connections.
MAX_CONNECTIONS = 200
MAX_NAME_LENGTH = 100
MAX_FIELD_LENGTH = 256
#: The most SQL one run may send (a long script).
MAX_QUERY_LENGTH = 1_000_000
#: Rows the page may ask for in one call, and rows sent with ``sql_done``.
MAX_PAGE_ROWS = 2000
FIRST_PAGE_ROWS = 200
#: Rows read from the driver at a time, and how often progress is reported.
FETCH_BATCH = 2000
PROGRESS_SECONDS = 0.25
#: Seconds to wait for a server to answer a login.
CONNECT_TIMEOUT = 15
#: What one cell may carry to the page; the full value stays here.
MAX_CELL_CHARS = 10_000
MAX_BINARY_PREVIEW = 64
#: Server messages kept for the Messages tab of one run.
MAX_MESSAGES = 500
#: "GO 5" runs a batch five times; more than this is refused.
MAX_GO_REPEAT = 1000

KEYRING_SERVICE = "VaultNotes SQL"

Emit = Callable[[str, Any], None]


class SqlError(Exception):
    """A SQL space problem with a user-facing code and message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _now_stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ----------------------------------------------------------------------
# Drivers
# ----------------------------------------------------------------------
def _pyodbc() -> Any:
    try:
        import pyodbc  # type: ignore  # noqa: PLC0415 - optional until SQL Server is used
    except ImportError as exc:
        raise SqlError(
            "no_driver", "SQL Server needs the pyodbc package (pip install -r requirements.txt)."
        ) from exc
    return pyodbc


def pick_mssql_driver(installed: list[str] | tuple[str, ...]) -> str | None:
    """The best SQL Server ODBC driver among ``installed``, or None."""
    have = set(installed)
    for name in MSSQL_DRIVERS:
        if name in have:
            return name
    # A newer "ODBC Driver NN for SQL Server" than this list knows.
    newer = sorted(
        (name for name in installed if re.fullmatch(r"ODBC Driver \d+ for SQL Server", name)),
        key=lambda name: int(re.findall(r"\d+", name)[0]),
        reverse=True,
    )
    return newer[0] if newer else None


def mssql_driver() -> str | None:
    """The SQL Server driver this PC would use, or None when it has none."""
    try:
        return pick_mssql_driver(_pyodbc().drivers())
    except SqlError:
        return None
    except Exception:  # noqa: BLE001 - a broken ODBC install
        return None


# ----------------------------------------------------------------------
# Saved connections
# ----------------------------------------------------------------------
def _clean_text(value: Any, field: str, *, required: bool, max_length: int = MAX_FIELD_LENGTH) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise SqlError("invalid_input", f"{field} must be text.")
    text = value.strip()
    if required and not text:
        raise SqlError("invalid_input", f"{field} is required.")
    if len(text) > max_length:
        raise SqlError("invalid_input", f"{field} is too long (at most {max_length} characters).")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise SqlError("invalid_input", f"{field} must be one line.")
    return text


def _flag(value: Any, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def clean_profile(data: Any) -> dict[str, Any]:
    """A connection as the page described it, checked field by field.

    The page never names a file: a SQLite connection's file is chosen in a
    native dialog, so ``file`` is ignored here.
    """
    if not isinstance(data, dict):
        raise SqlError("invalid_input", "A connection must be an object.")
    engine = data.get("engine")
    if engine not in ENGINES:
        raise SqlError("invalid_input", "Unknown database type.")
    profile: dict[str, Any] = {
        "engine": engine,
        "name": _clean_text(data.get("name"), "Name", required=True, max_length=MAX_NAME_LENGTH),
        "server": "",
        "database": "",
        "auth": "windows",
        "username": "",
        "encrypt": True,
        "trust_cert": False,
    }
    if engine == "mssql":
        auth = data.get("auth", "windows")
        if auth not in AUTH_MODES:
            raise SqlError("invalid_input", "Unknown sign-in method.")
        profile.update(
            server=_clean_text(data.get("server"), "Server", required=True),
            database=_clean_text(data.get("database"), "Database", required=False),
            auth=auth,
            username=_clean_text(data.get("username"), "User name", required=auth == "sql"),
            encrypt=_flag(data.get("encrypt"), True),
            trust_cert=_flag(data.get("trust_cert"), False),
        )
        if auth == "windows":
            profile["username"] = ""
    return profile


class SqlStore:
    """``sql.db``: saved connections (and, later, saved queries)."""

    SCHEMA_VERSION = 1

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def _open(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        if db.execute("PRAGMA user_version").fetchone()[0] < self.SCHEMA_VERSION:
            with db:
                db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS connections (
                        id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        engine TEXT NOT NULL,
                        server TEXT NOT NULL DEFAULT '',
                        database TEXT NOT NULL DEFAULT '',
                        auth TEXT NOT NULL DEFAULT 'windows',
                        username TEXT NOT NULL DEFAULT '',
                        encrypt INTEGER NOT NULL DEFAULT 1,
                        trust_cert INTEGER NOT NULL DEFAULT 0,
                        file TEXT NOT NULL DEFAULT '',
                        created TEXT NOT NULL,
                        modified TEXT NOT NULL
                    )
                    """
                )
                db.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")
        return db

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        out = dict(row)
        out["encrypt"] = bool(out["encrypt"])
        out["trust_cert"] = bool(out["trust_cert"])
        return out

    def list_connections(self) -> list[dict[str, Any]]:
        with closing(self._open()) as db:
            rows = db.execute("SELECT * FROM connections ORDER BY name COLLATE NOCASE, created").fetchall()
        return [self._row(row) for row in rows]

    def get_connection(self, connection_id: Any) -> dict[str, Any]:
        if not isinstance(connection_id, str):
            raise SqlError("not_found", "That connection does not exist any more.")
        with closing(self._open()) as db:
            row = db.execute("SELECT * FROM connections WHERE id = ?", (connection_id,)).fetchone()
        if row is None:
            raise SqlError("not_found", "That connection does not exist any more.")
        return self._row(row)

    def save_connection(self, profile: dict[str, Any], connection_id: str | None = None) -> dict[str, Any]:
        """Add a connection, or replace the fields of ``connection_id``."""
        now = _now_stamp()
        fields = ("name", "engine", "server", "database", "auth", "username", "encrypt", "trust_cert", "file")
        values = [profile.get(key, "") for key in fields]
        values[fields.index("encrypt")] = int(bool(profile.get("encrypt", True)))
        values[fields.index("trust_cert")] = int(bool(profile.get("trust_cert", False)))
        with closing(self._open()) as db, db:
            if connection_id is None:
                count = db.execute("SELECT COUNT(*) FROM connections").fetchone()[0]
                if count >= MAX_CONNECTIONS:
                    raise SqlError("too_many", f"You can keep up to {MAX_CONNECTIONS} connections.")
                connection_id = uuid.uuid4().hex
                db.execute(
                    f"INSERT INTO connections (id, {', '.join(fields)}, created, modified) "
                    f"VALUES (?, {', '.join('?' for _ in fields)}, ?, ?)",
                    (connection_id, *values, now, now),
                )
            else:
                updated = db.execute(
                    f"UPDATE connections SET {', '.join(f'{key} = ?' for key in fields)}, modified = ? WHERE id = ?",
                    (*values, now, connection_id),
                ).rowcount
                if not updated:
                    raise SqlError("not_found", "That connection does not exist any more.")
        return self.get_connection(connection_id)

    def delete_connection(self, connection_id: Any) -> bool:
        if not isinstance(connection_id, str):
            return False
        with closing(self._open()) as db, db:
            return db.execute("DELETE FROM connections WHERE id = ?", (connection_id,)).rowcount > 0


def public_connection(profile: dict[str, Any], has_password: bool = False) -> dict[str, Any]:
    """What the page may see of a connection: never a password."""
    engine = profile["engine"]
    if engine == "sqlite":
        where = Path(profile["file"]).name if profile.get("file") else "No file"
    else:
        where = profile["server"] + (f" / {profile['database']}" if profile.get("database") else "")
    return {
        "id": profile["id"],
        "name": profile["name"],
        "engine": engine,
        "engineName": ENGINE_NAMES[engine],
        "server": profile.get("server", ""),
        "database": profile.get("database", ""),
        "auth": profile.get("auth", "windows"),
        "username": profile.get("username", ""),
        "encrypt": bool(profile.get("encrypt", True)),
        "trust_cert": bool(profile.get("trust_cert", False)),
        "file": profile.get("file", ""),
        "where": where,
        "hasPassword": has_password,
    }


class SqlPasswords:
    """SQL login passwords, in the Windows Credential Manager only.

    Tests pass ``memory=True`` so no real credential store is touched.
    """

    def __init__(self, keyring: Any = None, *, memory: bool = False) -> None:
        self._injected = keyring
        self._memory: dict[str, str] | None = {} if memory and keyring is None else None

    def _backend(self) -> Any:
        if self._injected is not None:
            return self._injected
        try:
            import keyring  # noqa: PLC0415
        except Exception as exc:  # noqa: BLE001
            raise SqlError(
                "keyring_unavailable", "The Windows Credential Manager library (keyring) is not installed."
            ) from exc
        return keyring

    def get(self, connection_id: str) -> str | None:
        if self._memory is not None:
            return self._memory.get(connection_id)
        try:
            return self._backend().get_password(KEYRING_SERVICE, connection_id) or None
        except SqlError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise SqlError(
                "keyring_unavailable", f"The saved password could not be read ({type(exc).__name__})."
            ) from exc

    def set(self, connection_id: str, password: str) -> None:
        if self._memory is not None:
            self._memory[connection_id] = password
            return
        try:
            self._backend().set_password(KEYRING_SERVICE, connection_id, password)
        except SqlError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise SqlError(
                "keyring_unavailable", f"The Credential Manager refused the password ({type(exc).__name__})."
            ) from exc

    def delete(self, connection_id: str) -> None:
        if self._memory is not None:
            self._memory.pop(connection_id, None)
            return
        try:
            self._backend().delete_password(KEYRING_SERVICE, connection_id)
        except Exception:  # noqa: BLE001 - nothing saved is fine
            pass


# ----------------------------------------------------------------------
# Opening a connection
# ----------------------------------------------------------------------
def _odbc_value(value: str) -> str:
    """One connection-string value, braced so ``;`` or ``}`` cannot end it."""
    return "{" + value.replace("}", "}}") + "}"


def mssql_connection_string(profile: dict[str, Any], password: str | None, driver: str) -> str:
    parts = [
        f"DRIVER={_odbc_value(driver)}",
        f"SERVER={_odbc_value(profile['server'])}",
    ]
    if profile.get("database"):
        parts.append(f"DATABASE={_odbc_value(profile['database'])}")
    if profile.get("auth") == "sql":
        parts.append(f"UID={_odbc_value(profile.get('username', ''))}")
        parts.append(f"PWD={_odbc_value(password or '')}")
    else:
        parts.append("Trusted_Connection=yes")
    parts.append(f"Encrypt={'yes' if profile.get('encrypt', True) else 'no'}")
    # Windows' own "SQL Server" driver does not know this option and says so.
    if driver != LEGACY_DRIVER:
        parts.append(f"TrustServerCertificate={'yes' if profile.get('trust_cert') else 'no'}")
    parts.append("APP={VaultNotes}")
    return ";".join(parts)


#: The ``[42S02] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]`` in front of a message.
_BRACKETS = re.compile(r"^(?:\[[^\]]*\]\s*)+")


def clean_driver_message(exc: BaseException) -> str:
    """A driver error without the ``[Microsoft][ODBC …]`` noise."""
    args = getattr(exc, "args", ())
    text = str(args[1] if len(args) > 1 and isinstance(args[1], str) else (args[0] if args else exc))
    text = _BRACKETS.sub("", text)
    # pyodbc joins several messages with "; ", each with its own bracket
    # prefix, native error number and ODBC function name.
    text = re.sub(r";\s*(?:\[[^\]]*\]\s*)+", "; ", text)
    text = re.sub(r"\s*\(SQL\w+\)(?=\s*(?:;|$))", "", text)
    text = re.sub(r"\s*\(\d+\)(?=\s*(?:;|$))", "", text)
    # The old driver adds the network call that failed: noise to the reader.
    text = re.sub(r";?\s*ConnectionOpen\b[^;]*", "", text)
    return text.strip(" ;") or type(exc).__name__


def connect(profile: dict[str, Any], password: str | None) -> Any:
    """A DB-API connection for ``profile``, in autocommit mode like SSMS."""
    engine = profile["engine"]
    if engine == "sqlite":
        path = Path(profile.get("file") or "")
        if not profile.get("file") or not path.is_file():
            raise SqlError("not_found", "The SQLite file was not found. Choose it again in the connection.")
        try:
            # mode=rw: a moved file is an error, never a new empty database.
            uri = path.resolve().as_uri() + "?mode=rw"
            return sqlite3.connect(uri, uri=True, timeout=5, check_same_thread=False, isolation_level=None)
        except sqlite3.Error as exc:
            raise SqlError("connect_failed", f"The SQLite file could not be opened: {exc}") from exc
    pyodbc = _pyodbc()
    driver = pick_mssql_driver(pyodbc.drivers())
    if driver is None:
        raise SqlError(
            "no_driver",
            "No SQL Server ODBC driver is installed. Install Microsoft's ODBC Driver 18 for SQL Server.",
        )
    try:
        return pyodbc.connect(
            mssql_connection_string(profile, password, driver), timeout=CONNECT_TIMEOUT, autocommit=True
        )
    except pyodbc.Error as exc:
        message = clean_driver_message(exc)
        if re.search(r"certificate|SSL|TLS|encrypt", message, re.IGNORECASE):
            message += (
                " If the server uses a self-signed certificate, tick “Trust the server certificate”"
                + (" (needs ODBC Driver 17 or 18 for SQL Server)" if driver == LEGACY_DRIVER else "")
                + ", or untick “Encrypt the connection”."
            )
        raise SqlError("connect_failed", message) from exc


# ----------------------------------------------------------------------
# Splitting a script into what the driver runs
# ----------------------------------------------------------------------
_GO_LINE = re.compile(r"^[ \t]*GO(?:[ \t]+(\d+))?[ \t]*(?:--[^\r\n]*)?\r?$\n?", re.IGNORECASE | re.MULTILINE)


def split_mssql(text: str) -> list[str]:
    """SSMS-style batches: a line holding only ``GO`` (or ``GO 5``) ends one."""
    batches: list[str] = []
    start = 0
    for match in _GO_LINE.finditer(text):
        batch = text[start:match.start()]
        repeat = int(match.group(1)) if match.group(1) else 1
        if repeat > MAX_GO_REPEAT:
            raise SqlError("invalid_input", f"GO can repeat a batch at most {MAX_GO_REPEAT} times.")
        if batch.strip():
            batches.extend([batch] * repeat)
        start = match.end()
    if text[start:].strip():
        batches.append(text[start:])
    return batches


def _only_comments(text: str) -> bool:
    stripped = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    stripped = re.sub(r"--[^\n]*", "", stripped)
    return not stripped.strip(" \t\r\n;")


def split_sqlite(text: str) -> list[str]:
    """One statement per item: Python's sqlite3 runs a single statement per call."""
    statements: list[str] = []
    start = 0
    position = text.find(";")
    while position != -1:
        candidate = text[start:position + 1]
        if sqlite3.complete_statement(candidate):
            if not _only_comments(candidate):
                statements.append(candidate)
            start = position + 1
        position = text.find(";", position + 1)
    if not _only_comments(text[start:]):
        statements.append(text[start:])
    return statements


# ----------------------------------------------------------------------
# Values on their way to the page
# ----------------------------------------------------------------------
_NUMBER_TYPES = (int, float, decimal.Decimal)
_DATE_TYPES = (dt.datetime, dt.date, dt.time)
_BINARY_TYPES = (bytes, bytearray, memoryview)


def value_kind(value: Any) -> str:
    """``number``, ``bool``, ``date``, ``binary`` or ``text``: how the grid aligns it."""
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, _NUMBER_TYPES):
        return "number"
    if isinstance(value, _DATE_TYPES):
        return "date"
    if isinstance(value, _BINARY_TYPES):
        return "binary"
    return "text"


def _type_kind(type_code: Any) -> str | None:
    """A column's kind from the driver's type, when the driver says (pyodbc does)."""
    if not isinstance(type_code, type):
        return None
    if issubclass(type_code, bool):
        return "bool"
    if issubclass(type_code, _NUMBER_TYPES):
        return "number"
    if issubclass(type_code, _DATE_TYPES):
        return "date"
    if issubclass(type_code, _BINARY_TYPES):
        return "binary"
    return "text"


def cell(value: Any) -> Any:
    """A value the page can show: JSON-safe and not too long."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value if abs(value) <= 2**53 else str(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, dt.datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, (dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, _BINARY_TYPES):
        data = bytes(value)
        text = "0x" + data[:MAX_BINARY_PREVIEW].hex().upper()
        return text + "…" if len(data) > MAX_BINARY_PREVIEW else text
    text = value if isinstance(value, str) else str(value)
    return text if len(text) <= MAX_CELL_CHARS else text[:MAX_CELL_CHARS] + "…"


def full_text(value: Any) -> str | None:
    """A value as copied to the clipboard: complete, unlike :func:`cell`."""
    if value is None:
        return None
    if isinstance(value, _BINARY_TYPES):
        return "0x" + bytes(value).hex().upper()
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, dt.datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, (dt.date, dt.time)):
        return value.isoformat()
    return str(value)


# ----------------------------------------------------------------------
# Query tabs
# ----------------------------------------------------------------------
class _Result:
    """One result set of a run: its columns and every row, as the driver gave them."""

    def __init__(self, description: Any) -> None:
        names: list[str] = []
        kinds: list[str | None] = []
        for index, column in enumerate(description or ()):
            name = column[0] if column and column[0] not in (None, "") else f"(No column name {index + 1})"
            names.append(str(name))
            kinds.append(_type_kind(column[1] if len(column) > 1 else None))
        self.names = names
        self.kinds = kinds
        self.rows: list[tuple[Any, ...]] = []

    def finish(self) -> None:
        """Fill in the kinds the driver did not give (SQLite) from the values."""
        for index, kind in enumerate(self.kinds):
            if kind is not None:
                continue
            seen = [row[index] for row in self.rows[:200] if row[index] is not None]
            found = {value_kind(value) for value in seen}
            self.kinds[index] = found.pop() if len(found) == 1 else "text"

    def columns(self) -> list[dict[str, str]]:
        return [{"name": name, "kind": kind or "text"} for name, kind in zip(self.names, self.kinds)]

    def page(self, offset: int, limit: int) -> list[list[Any]]:
        return [[cell(value) for value in row] for row in self.rows[offset:offset + limit]]


class _Session:
    """One query tab's connection and its latest results."""

    def __init__(self, session_id: str, profile: dict[str, Any], conn: Any) -> None:
        self.id = session_id
        self.connection_id = profile["id"]
        self.name = profile["name"]
        self.engine = profile["engine"]
        self.conn = conn
        self.results: list[_Result] = []
        self.run_id: str | None = None
        self.cursor: Any = None
        self.cancelled = threading.Event()
        self.closed = False
        self.lock = threading.Lock()

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "connection": self.connection_id,
            "name": self.name,
            "engine": self.engine,
            "running": self.run_id is not None,
        }


def _int(value: Any, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SqlError("invalid_input", f"{name} must be a whole number.")
    return max(low, min(high, value))


class SqlManager:
    """The SQL space's open connections, one per query tab."""

    def __init__(self, emit: Emit, connector: Callable[[dict[str, Any], str | None], Any] | None = None) -> None:
        self._emit = emit
        self._connect = connector or connect
        self._lock = threading.Lock()
        self._sessions: dict[str, _Session] = {}

    # -- tabs ----------------------------------------------------------
    def check_room(self) -> None:
        with self._lock:
            if len(self._sessions) >= MAX_SESSIONS:
                raise SqlError("too_many", f"Up to {MAX_SESSIONS} query tabs can be open at once. Close one first.")

    def open(self, profile: dict[str, Any], password: str | None) -> dict[str, Any]:
        """Connect for a new tab.  Slow on a far server, so call it outside the call lock."""
        self.check_room()
        conn = self._connect(profile, password)
        session = _Session(secrets.token_hex(16), profile, conn)
        with self._lock:
            if len(self._sessions) >= MAX_SESSIONS:
                _close_quietly(conn)
                raise SqlError("too_many", f"Up to {MAX_SESSIONS} query tabs can be open at once. Close one first.")
            self._sessions[session.id] = session
        return session.describe()

    def test(self, profile: dict[str, Any], password: str | None) -> None:
        """Connect and hang up at once."""
        _close_quietly(self._connect(profile, password))

    def _session(self, session_id: Any) -> _Session:
        with self._lock:
            session = self._sessions.get(session_id) if isinstance(session_id, str) else None
        if session is None:
            raise SqlError("not_open", "That query tab is not connected any more.")
        return session

    def sessions(self) -> list[dict[str, Any]]:
        with self._lock:
            return [session.describe() for session in self._sessions.values()]

    def close(self, session_id: Any = None) -> None:
        """Close one tab's connection, or every one when no id is given."""
        with self._lock:
            if session_id is None:
                ended, self._sessions = list(self._sessions.values()), {}
            else:
                one = self._sessions.pop(session_id, None) if isinstance(session_id, str) else None
                ended = [one] if one is not None else []
        for session in ended:
            session.closed = True
            self._interrupt(session)
            if session.run_id is None:
                _close_quietly(session.conn)
            # A running query closes its connection when its thread stops.

    def close_connection(self, connection_id: str) -> None:
        """Close every tab on a connection that was just deleted."""
        with self._lock:
            ids = [s.id for s in self._sessions.values() if s.connection_id == connection_id]
        for session_id in ids:
            self.close(session_id)

    # -- running -------------------------------------------------------
    def run(self, session_id: Any, text: Any) -> dict[str, Any]:
        if not isinstance(text, str):
            raise SqlError("invalid_input", "The query must be text.")
        if len(text) > MAX_QUERY_LENGTH:
            raise SqlError("invalid_input", "That query is too long.")
        session = self._session(session_id)
        pieces = split_mssql(text) if session.engine == "mssql" else split_sqlite(text)
        if not pieces:
            raise SqlError("empty", "There is no SQL to run.")
        with session.lock:
            if session.run_id is not None:
                raise SqlError("busy", "This tab is still running a query. Wait for it, or cancel it.")
            run_id = secrets.token_hex(8)
            session.run_id = run_id
            session.cancelled.clear()
            session.results = []
        threading.Thread(
            target=self._work, args=(session, run_id, pieces), name="VaultNotes-sql-run", daemon=True
        ).start()
        return {"run": run_id, "batches": len(pieces)}

    def cancel(self, session_id: Any) -> bool:
        session = self._session(session_id)
        if session.run_id is None:
            return False
        session.cancelled.set()
        self._interrupt(session)
        return True

    @staticmethod
    def _interrupt(session: _Session) -> None:
        """Ask the driver to stop the statement in flight, from another thread."""
        try:
            if session.engine == "sqlite":
                session.conn.interrupt()
            elif session.cursor is not None:
                session.cursor.cancel()
        except Exception:  # noqa: BLE001 - it may have finished already
            pass

    def _work(self, session: _Session, run_id: str, pieces: list[str]) -> None:
        started = time.monotonic()
        results: list[_Result] = []
        messages: list[str] = []
        error: str | None = None
        last_progress = started
        rows_read = 0

        def note(text: str) -> None:
            if len(messages) < MAX_MESSAGES:
                messages.append(text)

        try:
            cursor = session.conn.cursor()
            session.cursor = cursor
            try:
                for number, piece in enumerate(pieces, start=1):
                    if session.cancelled.is_set():
                        break
                    try:
                        cursor.execute(piece)
                    except Exception as exc:  # noqa: BLE001 - shown in the Messages tab
                        if session.cancelled.is_set():
                            break
                        where = f"Batch {number} of {len(pieces)}: " if len(pieces) > 1 else ""
                        error = where + clean_driver_message(exc)
                        break
                    while True:
                        for _kind, text in getattr(cursor, "messages", None) or []:
                            note(_BRACKETS.sub("", str(text)))
                        if cursor.description:
                            result = _Result(cursor.description)
                            results.append(result)
                            session.results = results
                            while not session.cancelled.is_set():
                                chunk = cursor.fetchmany(FETCH_BATCH)
                                if not chunk:
                                    break
                                result.rows.extend(tuple(row) for row in chunk)
                                rows_read += len(chunk)
                                now = time.monotonic()
                                if now - last_progress >= PROGRESS_SECONDS:
                                    last_progress = now
                                    self._emit("sql_progress", {"session": session.id, "run": run_id, "rows": rows_read})
                            result.finish()
                            note(f"({len(result.rows)} row{'' if len(result.rows) == 1 else 's'})")
                        elif isinstance(cursor.rowcount, int) and cursor.rowcount >= 0:
                            count = cursor.rowcount
                            note(f"({count} row{'' if count == 1 else 's'} affected)")
                        if session.cancelled.is_set() or not hasattr(cursor, "nextset"):
                            break
                        try:
                            more = cursor.nextset()
                        except Exception as exc:  # noqa: BLE001 - an error in a later statement
                            if not session.cancelled.is_set():
                                error = clean_driver_message(exc)
                            more = False
                        if not more:
                            break
                    if error:
                        break
            finally:
                session.cursor = None
                try:
                    cursor.close()
                except Exception:  # noqa: BLE001
                    pass
        except Exception as exc:  # noqa: BLE001 - a dropped connection
            if not session.cancelled.is_set():
                error = clean_driver_message(exc)

        cancelled = session.cancelled.is_set()
        session.results = results
        with session.lock:
            session.run_id = None
        if session.closed:
            _close_quietly(session.conn)
            return
        self._emit(
            "sql_done",
            {
                "session": session.id,
                "run": run_id,
                "ok": error is None and not cancelled,
                "cancelled": cancelled,
                "error": error,
                "messages": messages,
                "elapsedMs": int((time.monotonic() - started) * 1000),
                "results": [
                    {"columns": r.columns(), "total": len(r.rows), "rows": r.page(0, FIRST_PAGE_ROWS)} for r in results
                ],
            },
        )

    # -- reading results -----------------------------------------------
    def _result(self, session_id: Any, index: Any) -> _Result:
        session = self._session(session_id)
        results = session.results
        index = _int(index, "Result", 0, 10**6)
        if index >= len(results):
            raise SqlError("not_found", "That result is gone. Run the query again.")
        return results[index]

    def rows(self, session_id: Any, index: Any, offset: Any, limit: Any) -> dict[str, Any]:
        result = self._result(session_id, index)
        offset = _int(offset, "Offset", 0, 2**62)
        limit = _int(limit, "Limit", 1, MAX_PAGE_ROWS)
        return {"offset": offset, "total": len(result.rows), "rows": result.page(offset, limit)}

    def copy_text(self, session_id: Any, index: Any, with_header: bool = True) -> str:
        """A whole result as tab-separated text, for pasting into Excel."""
        result = self._result(session_id, index)

        def field(value: Any) -> str:
            text = full_text(value)
            if text is None:
                return "NULL"
            return text.replace("\t", " ").replace("\r\n", " ").replace("\n", " ")

        lines = ["\t".join(result.names)] if with_header else []
        lines.extend("\t".join(field(value) for value in row) for row in result.rows)
        return "\r\n".join(lines)


def _close_quietly(conn: Any) -> None:
    try:
        conn.close()
    except Exception:  # noqa: BLE001
        pass
