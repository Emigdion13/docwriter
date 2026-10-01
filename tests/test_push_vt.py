"""Pushing a virtual table into a query tab's connection: #name on SQL Server,
temp.name on SQLite, so the tab can join it with that database's own tables.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

from test_sql import FakeWindow, make_api, sample_db
from vaultnotes.sql import SqlError, SqlManager, clean_profile
from vaultnotes.vt import _ColumnScan, mssql_converter, push_plan, VtStore


def run(api: Any, window: FakeWindow, sid: str, text: str) -> dict[str, Any]:
    before = len(window.events("sql_done"))
    assert api.sql_run(sid, text)["ok"] is True
    return window.wait_for("sql_done", before + 1)[before]


def owners_db(path: Path) -> Path:
    db = sqlite3.connect(path)
    with db:
        db.execute("CREATE TABLE owners (batch_id INTEGER, owner TEXT)")
        db.executemany("INSERT INTO owners VALUES (?, ?)", [(i, f"team {i % 3}") for i in range(1, 21)])
    db.close()
    return path


def two_databases(tmp_path: Path) -> tuple[Any, FakeWindow, str, str]:
    """A tab on ingest.db with a virtual table kept from it, and a tab on owners.db."""
    window = FakeWindow()
    api = make_api(tmp_path)
    api.config.data["sql"]["enabled"] = True
    ingest = api.sql_add_sqlite(str(sample_db(tmp_path / "ingest.db")))["connection"]
    owners = api.sql_add_sqlite(str(owners_db(tmp_path / "owners.db")))["connection"]
    api.window = window
    source = api.sql_open(ingest["id"])["id"]
    run(api, window, source, "SELECT id AS batch_id, name, size FROM batches WHERE id <= 10")
    assert api.sql_save_vt(source, 0, "first_ten")["ok"] is True
    target = api.sql_open(owners["id"])["id"]
    return api, window, source, target


# ----------------------------------------------------------------------
# SQLite end to end
# ----------------------------------------------------------------------
def test_a_virtual_table_joins_another_databases_tables(tmp_path: Path) -> None:
    api, window, source, target = two_databases(tmp_path)
    api.sql_close(source)  # the database it came from is not needed any more

    res = api.sql_push_vt(target, "first_ten")
    assert res["ok"] is True and res["target"] == "temp.first_ten" and res["rows"] == 10
    assert [c["name"] for c in res["columns"]] == ["batch_id", "name", "size"]

    done = run(api, window, target,
               "SELECT f.batch_id, f.name, o.owner FROM first_ten f JOIN owners o ON o.batch_id = f.batch_id ORDER BY 1")
    rows = done["results"][0]["rows"]
    assert len(rows) == 10 and rows[0] == [1, "batch 1", "team 1"]

    # It lives in the tab's connection only: not in the file, not in other tabs.
    other = api.sql_open(api.sql_state()["connections"][1]["id"])["id"]
    assert "no such table" in run(api, window, other, "SELECT * FROM first_ten")["error"]
    check = sqlite3.connect(tmp_path / "owners.db")
    assert check.execute("SELECT name FROM sqlite_master WHERE name = 'first_ten'").fetchall() == []
    check.close()


def test_pushing_again_replaces_it(tmp_path: Path) -> None:
    api, window, source, target = two_databases(tmp_path)
    api.sql_push_vt(target, "first_ten")
    run(api, window, source, "SELECT id AS batch_id FROM batches WHERE id <= 3")
    api.sql_save_vt(source, 0, "first_ten", True)
    assert api.sql_push_vt(target, "first_ten")["rows"] == 3
    assert run(api, window, target, "SELECT COUNT(*) FROM first_ten")["results"][0]["rows"] == [[3]]


def test_pushes_are_refused_when_they_make_no_sense(tmp_path: Path) -> None:
    api, window, source, target = two_databases(tmp_path)
    vt = api.sql_open("vt")["id"]
    assert api.sql_push_vt(vt, "first_ten")["error"] == "invalid_input"
    assert api.sql_push_vt(target, "missing")["error"] == "not_found"
    assert api.sql_push_vt(target, "bad name")["error"] == "invalid_input"
    assert api.sql_push_vt("not-a-tab", "first_ten")["error"] == "not_open"

    slow = "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 300000000) SELECT COUNT(*) FROM n"
    api.sql_run(target, slow)
    assert api.sql_push_vt(target, "first_ten")["error"] == "busy"
    api.sql_cancel(target)
    window.wait_for("sql_done", 2)


def test_a_big_push_reports_progress_and_can_be_stopped(tmp_path: Path) -> None:
    api, window, source, target = two_databases(tmp_path)
    store = api.vt_store
    store.save("big", ["n"], ["number"], ((i,) for i in range(400_000)), source=None, query="", replace=False)

    results: list[dict[str, Any]] = []
    thread = threading.Thread(target=lambda: results.append(api.sql_push_vt(target, "big")))
    thread.start()
    window.wait_for("sql_push_progress", timeout=20)
    api.sql_cancel(target)
    thread.join(20)
    assert results[0]["error"] == "cancelled"
    # Nothing half-filled is left behind, and the tab still works.
    assert "no such table" in run(api, window, target, "SELECT COUNT(*) FROM big")["error"]


# ----------------------------------------------------------------------
# SQL Server: choosing types and the statements sent
# ----------------------------------------------------------------------
def scan(*values: Any) -> str:
    column = _ColumnScan()
    for value in values:
        column.add(value)
    return column.mssql_type()


def test_sql_server_types_follow_the_values() -> None:
    assert scan(1, 2, None) == "BIGINT"
    assert scan(1, 2.5) == "FLOAT"
    assert scan("2026-10-01", None, "2026-09-30") == "DATE"
    assert scan("2026-10-01 08:30:00", "2026-10-01T09:00:00.123456") == "DATETIME2(7)"
    assert scan("2026-10-01", "2026-10-01 08:30:00") == "NVARCHAR(19)"
    assert scan("2026-13-45") == "NVARCHAR(10)"
    assert scan("abc", "abcdef") == "NVARCHAR(6)"
    assert scan("x" * 4001) == "NVARCHAR(MAX)"
    assert scan(b"\x00", None) == "VARBINARY(MAX)"
    assert scan("a", 12345) == "NVARCHAR(5)"
    assert scan(None, None) == "NVARCHAR(50)"


def test_sql_server_values_are_sent_as_their_type() -> None:
    assert mssql_converter("DATE")("2026-10-01") == dt.date(2026, 10, 1)
    assert mssql_converter("DATETIME2(7)")("2026-10-01 08:30:00") == dt.datetime(2026, 10, 1, 8, 30)
    assert mssql_converter("NVARCHAR(5)")(12345) == "12345"
    assert mssql_converter("NVARCHAR(MAX)")(b"\x01") == "01"
    assert mssql_converter("BIGINT")(None) is None


def test_push_plan_reads_the_table(tmp_path: Path) -> None:
    store = VtStore(tmp_path / "vt.db")
    store.save("t", ["id", "at", "note"], ["number", "date", "text"],
               iter([(1, "2026-10-01 08:00:00", "a"), (2, "2026-10-02 09:00:00", None)]), source=None, query="", replace=False)
    plan = push_plan(store, "T", "mssql")
    assert plan == {"table": "t", "columns": ["id", "at", "note"], "types": ["BIGINT", "DATETIME2(7)", "NVARCHAR(1)"], "rows": 2}
    assert push_plan(store, "t", "sqlite")["types"] == ["NUMERIC", "TEXT", "TEXT"]


class RecordingCursor:
    def __init__(self, fail_on_insert: int = 0) -> None:
        self.statements: list[tuple[str, list[Any] | None]] = []
        self.fail_on_insert = fail_on_insert
        self.inserts = 0

    def execute(self, sql: str, params: list[Any] | None = None) -> None:
        self.statements.append((sql, params))
        if sql.startswith("INSERT"):
            self.inserts += 1
            if self.inserts == self.fail_on_insert:
                raise RuntimeError("[42000] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]Boom. (50000) (SQLExecDirectW)")

    def cancel(self) -> None:
        pass

    def close(self) -> None:
        pass


class RecordingConnection:
    def __init__(self, cursor: RecordingCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> RecordingCursor:
        return self._cursor

    def close(self) -> None:
        pass


def mssql_tab(cursor: RecordingCursor) -> tuple[SqlManager, str]:
    manager = SqlManager(emit=lambda *_: None, connector=lambda *_: RecordingConnection(cursor))
    profile = {**clean_profile({"engine": "mssql", "name": "Dev", "server": "s"}), "id": "c1"}
    return manager, manager.open(profile, None)["id"]


def test_sql_server_push_makes_a_hash_temp_table_in_batches() -> None:
    cursor = RecordingCursor()
    manager, sid = mssql_tab(cursor)
    columns = [f"c{i}" for i in range(5)] + ["raw"]
    types = ["BIGINT"] * 5 + ["VARBINARY(MAX)"]
    rows = [(i, i, i, i, i, None) for i in range(900)]
    res = manager.push(sid, "pending", columns, types, iter(rows), [mssql_converter(t) for t in types])
    assert res == {"target": "#pending", "rows": 900}

    sqls = [sql for sql, _ in cursor.statements]
    assert sqls[0] == "IF OBJECT_ID(N'tempdb..#pending') IS NOT NULL DROP TABLE [#pending]"
    assert sqls[1].startswith("CREATE TABLE [#pending] ([c0] BIGINT NULL") and "[raw] VARBINARY(MAX) NULL" in sqls[1]
    inserts = [(sql, params) for sql, params in cursor.statements if sql.startswith("INSERT")]
    # 6 columns: 333 rows (1998 parameters) per statement, under SQL Server's 2100.
    assert [len(params) // 6 for _, params in inserts] == [333, 333, 234]
    assert "CAST(? AS VARBINARY(MAX))" in inserts[0][0]


def test_a_failed_sql_server_push_drops_the_half_filled_table() -> None:
    cursor = RecordingCursor(fail_on_insert=2)
    manager, sid = mssql_tab(cursor)
    with pytest.raises(SqlError) as info:
        manager.push(sid, "t", ["n"], ["BIGINT"], ((i,) for i in range(5000)))
    assert info.value.code == "push_failed" and info.value.message == "Boom."
    assert cursor.statements[-1][0] == "IF OBJECT_ID(N'tempdb..#t') IS NOT NULL DROP TABLE [#t]"
    # The tab is free again.
    assert manager.describe(sid)["running"] is False
