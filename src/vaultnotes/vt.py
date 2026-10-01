"""The SQL - VT space: virtual tables, results kept after the connection is gone.

A virtual table is an ordinary table in ``vt.db``, a SQLite file in
``%LOCALAPPDATA%\\VaultNotes`` next to ``sql.db``: outside the notes folder, so
the Google Drive backup never sees it.  Saving a result copies its rows there
under a name the user picks; the SQL - VT space then queries that file like any
SQLite database, so virtual tables join each other with plain SQL, whatever
server each one came from.

``_vt_meta`` remembers where a table came from.  A table made with SQL in the
space itself (``CREATE TABLE x AS SELECT ...``) is a virtual table too; it just
has no source.
"""

from __future__ import annotations

import datetime as dt
import decimal
import json
import re
import sqlite3
import threading
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable

from vaultnotes.sql import SqlError, query_preview

#: The connection id the page uses for the virtual tables' own database.
VT_CONNECTION_ID = "vt"
VT_NAME = "Virtual tables"

#: A table name that needs no quoting in SQL: what the user will type.
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}")
#: Rows written per statement while saving.
INSERT_BATCH = 5000
#: Virtual tables kept at once.
MAX_TABLES = 500

_META = "_vt_meta"
_SQLITE_TYPES = {"number": "NUMERIC", "bool": "INTEGER", "binary": "BLOB", "date": "TEXT", "text": "TEXT"}


def clean_table_name(value: Any) -> str:
    """A virtual table's name: letters, digits and ``_``, not starting with a digit."""
    if not isinstance(value, str) or not _NAME.fullmatch(value.strip()):
        raise SqlError(
            "invalid_input",
            "A virtual table's name uses letters, digits and _ only, starts with a letter or _, "
            "and has at most 63 characters (like pending_batches).",
        )
    name = value.strip()
    if name.lower().startswith(("sqlite_", "_vt_")):
        raise SqlError("invalid_input", "Names starting with sqlite_ or _vt_ are reserved.")
    return name


def column_names(names: list[str]) -> list[str]:
    """Result column names made usable as table columns: none empty, none twice."""
    out: list[str] = []
    seen: set[str] = set()
    for index, raw in enumerate(names, start=1):
        name = raw.strip() if isinstance(raw, str) else ""
        if not name or name.startswith("(No column name"):
            name = f"column_{index}"
        base, n = name, 2
        while name.lower() in seen:
            name = f"{base}_{n}"
            n += 1
        seen.add(name.lower())
        out.append(name)
    return out


def quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def sqlite_value(value: Any) -> Any:
    """A driver's value as SQLite keeps it."""
    if value is None or isinstance(value, (str, float)):
        return value
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value if -(2**63) <= value < 2**63 else str(value)
    if isinstance(value, decimal.Decimal):
        if not value.is_finite():
            return str(value)
        if value == value.to_integral_value() and abs(value) < 2**63:
            return int(value)
        return float(value)
    if isinstance(value, dt.datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, (dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    if isinstance(value, uuid.UUID):
        return str(value).upper()
    return str(value)


class VtStore:
    """``vt.db``: the virtual tables and where each came from."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def _open(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_META} (
                name TEXT PRIMARY KEY COLLATE NOCASE,
                source_connection TEXT,
                source_name TEXT,
                source_where TEXT,
                query TEXT,
                columns TEXT,
                rows INTEGER,
                created TEXT NOT NULL
            )
            """
        )
        return db

    def profile(self) -> dict[str, Any]:
        """``vt.db`` as a SQLite connection, for the space's query tabs."""
        self._open().close()  # make sure the file is there
        return {
            "id": VT_CONNECTION_ID,
            "name": VT_NAME,
            "engine": "sqlite",
            "file": str(self.path),
            "server": "",
            "database": "",
            "auth": "windows",
        }

    @staticmethod
    def _tables(db: sqlite3.Connection) -> list[str]:
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' "
            f"AND name <> '{_META}' ORDER BY name COLLATE NOCASE"
        ).fetchall()
        return [row["name"] for row in rows]

    def exists(self, name: str) -> bool:
        with closing(self._open()) as db:
            return any(t.lower() == name.lower() for t in self._tables(db))

    def list(self) -> list[dict[str, Any]]:
        """Every virtual table: name, size, columns and source."""
        out = []
        with closing(self._open()) as db:
            meta = {row["name"].lower(): row for row in db.execute(f"SELECT * FROM {_META}")}
            for name in self._tables(db):
                info = meta.get(name.lower())
                columns = [row["name"] for row in db.execute(f"PRAGMA table_info({quote(name)})")]
                # Counted, not remembered: SQL in the space may have changed the rows.
                rows = db.execute(f"SELECT COUNT(*) FROM {quote(name)}").fetchone()[0]
                out.append(
                    {
                        "name": name,
                        "rows": rows,
                        "columns": columns,
                        "source": None if info is None or not info["source_name"] else {
                            "connection": info["source_connection"],
                            "name": info["source_name"],
                            "where": info["source_where"],
                        },
                        "query": query_preview(info["query"] or "") if info is not None else "",
                        "created": info["created"] if info is not None else None,
                    }
                )
        return out

    def save(
        self,
        name: str,
        names: list[str],
        kinds: list[str],
        rows: Iterable[tuple[Any, ...]],
        *,
        source: dict[str, Any] | None,
        query: str,
        replace: bool,
    ) -> dict[str, Any]:
        """Write ``rows`` as table ``name``, in one transaction."""
        columns = column_names(names)
        if not columns:
            raise SqlError("invalid_input", "That result has no columns to keep.")
        declared = ", ".join(f"{quote(c)} {_SQLITE_TYPES.get(k, 'TEXT')}" for c, k in zip(columns, kinds))
        placeholders = ", ".join("?" for _ in columns)
        count = 0
        with self._lock, closing(self._open()) as db:
            existing = [t for t in self._tables(db) if t.lower() == name.lower()]
            if existing and not replace:
                raise SqlError("exists", f"A virtual table named {existing[0]} already exists.")
            if not existing and len(self._tables(db)) >= MAX_TABLES:
                raise SqlError("too_many", f"You can keep up to {MAX_TABLES} virtual tables.")
            db.execute("BEGIN IMMEDIATE")
            try:
                for old in existing:
                    db.execute(f"DROP TABLE {quote(old)}")
                db.execute(f"CREATE TABLE {quote(name)} ({declared})")
                batch: list[tuple[Any, ...]] = []
                insert = f"INSERT INTO {quote(name)} VALUES ({placeholders})"
                for row in rows:
                    batch.append(tuple(sqlite_value(v) for v in row))
                    if len(batch) >= INSERT_BATCH:
                        db.executemany(insert, batch)
                        count += len(batch)
                        batch = []
                if batch:
                    db.executemany(insert, batch)
                    count += len(batch)
                db.execute(f"DELETE FROM {_META} WHERE name = ?", (name,))
                db.execute(
                    f"INSERT INTO {_META} (name, source_connection, source_name, source_where, query, columns, rows, created) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        name,
                        (source or {}).get("connection"),
                        (source or {}).get("name"),
                        (source or {}).get("where"),
                        query,
                        json.dumps(columns),
                        count,
                        dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    ),
                )
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        return {"name": name, "rows": count, "columns": columns}

    def rename(self, name: str, new_name: str) -> None:
        with self._lock, closing(self._open()) as db:
            tables = self._tables(db)
            current = next((t for t in tables if t.lower() == name.lower()), None)
            if current is None:
                raise SqlError("not_found", "That virtual table does not exist any more.")
            if new_name.lower() != current.lower() and any(t.lower() == new_name.lower() for t in tables):
                raise SqlError("exists", f"A virtual table named {new_name} already exists.")
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(f"ALTER TABLE {quote(current)} RENAME TO {quote(new_name)}")
                db.execute(f"UPDATE {_META} SET name = ? WHERE name = ?", (new_name, current))
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def delete(self, name: str) -> bool:
        with self._lock, closing(self._open()) as db:
            current = next((t for t in self._tables(db) if t.lower() == name.lower()), None)
            db.execute(f"DELETE FROM {_META} WHERE name = ?", (name,))
            if current is None:
                return False
            db.execute(f"DROP TABLE {quote(current)}")
            return True

    def forget_stale_meta(self) -> None:
        """Drop source notes of tables dropped with SQL in the space itself."""
        with self._lock, closing(self._open()) as db:
            tables = {t.lower() for t in self._tables(db)}
            for row in db.execute(f"SELECT name FROM {_META}").fetchall():
                if row["name"].lower() not in tables:
                    db.execute(f"DELETE FROM {_META} WHERE name = ?", (row["name"],))
