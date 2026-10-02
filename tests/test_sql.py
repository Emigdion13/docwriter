"""The SQL space: off until the user allows it natively, connections saved
outside the notes folder, passwords only in the credential store, and queries
that run on their own thread and report back as events.
"""

from __future__ import annotations

import datetime as dt
import inspect
import decimal
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from vaultnotes.ai_share import load_snapshot
from vaultnotes.api import Api
from vaultnotes.backup.gdrive_auth import TokenStore
from vaultnotes.config import Config
from vaultnotes.sql import (
    MAX_SESSIONS,
    SqlError,
    SqlManager,
    SqlPasswords,
    cell,
    clean_driver_message,
    clean_profile,
    mssql_connection_string,
    pick_mssql_driver,
    split_mssql,
    split_sqlite,
)


class FakeWindow:
    """Answers the native question and records the events sent to the page."""

    def __init__(self, allow: bool = True) -> None:
        self.allow = allow
        self.asked = 0
        self.scripts: list[str] = []
        self._lock = threading.Lock()

    def create_confirmation_dialog(self, title: str, message: str) -> bool:
        self.asked += 1
        return self.allow

    def evaluate_js(self, script: str) -> None:
        with self._lock:
            self.scripts.append(script)

    def events(self, name: str) -> list[dict[str, Any]]:
        prefix = f'window.vn && window.vn.emit("{name}", '
        with self._lock:
            scripts = list(self.scripts)
        return [json.loads(script[len(prefix):-2]) for script in scripts if script.startswith(prefix)]

    def wait_for(self, name: str, count: int = 1, timeout: float = 10) -> list[dict[str, Any]]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = self.events(name)
            if len(found) >= count:
                return found
            time.sleep(0.01)
        raise AssertionError(f"no {name} event")


def make_api(tmp_path: Path, window: Any = None) -> Api:
    cfg = Config(settings_path=tmp_path / "settings.json")
    cfg.data["notes_root"] = str(tmp_path / "notes")
    cfg.save()
    cfg.ensure_folders()
    return Api(
        config=cfg,
        window=window,
        app_dir=tmp_path / "appdata",
        sql_dir=tmp_path / "local",
        sql_passwords=SqlPasswords(memory=True),
        drive_store=TokenStore(memory=True),
    )


def turned_on(tmp_path: Path) -> tuple[Api, FakeWindow]:
    window = FakeWindow()
    api = make_api(tmp_path, window)
    assert api.sql_enable() == {"ok": True, "enabled": True}
    return api, window


def sample_db(path: Path) -> Path:
    db = sqlite3.connect(path)
    with db:
        db.execute("CREATE TABLE batches (id INTEGER PRIMARY KEY, name TEXT, size REAL, started TEXT)")
        db.executemany(
            "INSERT INTO batches (name, size, started) VALUES (?, ?, ?)",
            [(f"batch {i}", i * 1.5, "2026-10-01") for i in range(1, 501)],
        )
    db.close()
    return path


# ----------------------------------------------------------------------
# Pure helpers
# ----------------------------------------------------------------------
def test_split_mssql_on_go_lines() -> None:
    text = "SELECT 1\nGO\nSELECT 2\n  go  \n\nGO\nSELECT 3 -- not GO\nGO 3\n"
    assert [b.strip() for b in split_mssql(text)] == ["SELECT 1", "SELECT 2", *["SELECT 3 -- not GO"] * 3]
    assert split_mssql("SELECT 'GO'") == ["SELECT 'GO'"]
    assert split_mssql("\nGO\n") == []
    with pytest.raises(SqlError):
        split_mssql("SELECT 1\nGO 100000")


def test_split_sqlite_one_statement_each() -> None:
    text = "SELECT 1; SELECT ';' AS x;\n-- just a comment\nCREATE TRIGGER t AFTER INSERT ON a BEGIN SELECT 1; END;\nSELECT 4"
    parts = [p.strip() for p in split_sqlite(text)]
    assert parts == [
        "SELECT 1;",
        "SELECT ';' AS x;",
        "-- just a comment\nCREATE TRIGGER t AFTER INSERT ON a BEGIN SELECT 1; END;",
        "SELECT 4",
    ]
    assert split_sqlite("-- nothing\n/* here */ ;") == []


def test_pick_driver_prefers_the_newest() -> None:
    assert pick_mssql_driver(["SQL Server", "ODBC Driver 17 for SQL Server"]) == "ODBC Driver 17 for SQL Server"
    assert pick_mssql_driver(["SQL Server"]) == "SQL Server"
    assert pick_mssql_driver(["ODBC Driver 19 for SQL Server", "Excel"]) == "ODBC Driver 19 for SQL Server"
    assert pick_mssql_driver(["Microsoft Access Driver (*.mdb)"]) is None


def test_connection_string_braces_every_value() -> None:
    profile = clean_profile(
        {"engine": "mssql", "name": "Prod", "server": "db;x}", "database": "Ingest", "auth": "sql", "username": "me"}
    )
    text = mssql_connection_string(profile, "p;w}d", "SQL Server")
    assert "SERVER={db;x}}}" in text
    assert "PWD={p;w}}d}" in text
    assert "Trusted_Connection" not in text
    windows = mssql_connection_string(clean_profile({"engine": "mssql", "name": "a", "server": "s"}), None, "SQL Server")
    assert "Trusted_Connection=yes" in windows and "UID" not in windows and "DATABASE" not in windows
    # Windows' own driver rejects TrustServerCertificate; the newer ones get it.
    assert "TrustServerCertificate" not in windows
    trusted = clean_profile({"engine": "mssql", "name": "a", "server": "s", "trust_cert": True})
    assert "TrustServerCertificate=yes" in mssql_connection_string(trusted, None, "ODBC Driver 18 for SQL Server")


@pytest.mark.parametrize(
    "data",
    [
        None,
        {"engine": "oracle", "name": "x"},
        {"engine": "mssql", "name": "", "server": "s"},
        {"engine": "mssql", "name": "x", "server": ""},
        {"engine": "mssql", "name": "x", "server": "s", "auth": "sql"},
        {"engine": "mssql", "name": "x", "server": "s\nDROP"},
        {"engine": "mssql", "name": "x" * 101, "server": "s"},
    ],
)
def test_bad_profiles_are_refused(data: Any) -> None:
    with pytest.raises(SqlError):
        clean_profile(data)


def test_cells_are_json_safe() -> None:
    assert cell(None) is None
    assert cell(2**60) == str(2**60)
    assert cell(float("nan")) == "nan"
    assert cell(decimal.Decimal("12.3400")) == "12.3400"
    assert cell(dt.datetime(2026, 10, 1, 9, 5)) == "2026-10-01 09:05:00"
    assert cell(b"\x01\xff") == "0x01FF"
    assert cell(b"x" * 100).endswith("…")
    assert len(cell("y" * 20_000)) == 10_001


def test_driver_messages_lose_their_prefix() -> None:
    exc = Exception(
        "42S02",
        "[42S02] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]Invalid object name 'nope'. (208) (SQLExecDirectW)",
    )
    assert clean_driver_message(exc) == "Invalid object name 'nope'."
    joined = Exception(
        "08001",
        "[08001] [Microsoft][ODBC SQL Server Driver][DBNETLIB]SQL Server does not exist or access denied. (17) "
        "(SQLDriverConnect); [08001] [Microsoft][ODBC SQL Server Driver][DBNETLIB]ConnectionOpen (Connect()). (53)",
    )
    assert clean_driver_message(joined) == "SQL Server does not exist or access denied."


# ----------------------------------------------------------------------
# Off until allowed
# ----------------------------------------------------------------------
def test_sql_is_off_by_default_and_refuses_everything(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    state = api.sql_state()
    assert state["enabled"] is False and state["connections"] == []
    for call in (
        lambda: api.sql_save_connection({"engine": "mssql", "name": "x", "server": "s"}),
        lambda: api.sql_open("x"),
        lambda: api.sql_run("x", "SELECT 1"),
        lambda: api.sql_add_sqlite(str(tmp_path / "a.db")),
    ):
        assert call()["error"] == "sql_off"


def test_saying_no_keeps_it_off(tmp_path: Path) -> None:
    window = FakeWindow(allow=False)
    api = make_api(tmp_path, window)
    assert api.sql_enable()["error"] == "cancelled"
    assert window.asked == 1
    assert api.sql_state()["enabled"] is False


def test_update_settings_cannot_switch_sql_on(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    api.update_settings({"sql": {"enabled": True}})
    assert api.sql_state()["enabled"] is False


def test_damaged_sql_settings_are_repaired(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    Config(settings_path=path).save()
    data = json.loads(path.read_text(encoding="utf-8"))
    data["sql"] = {"enabled": "yes"}
    path.write_text(json.dumps(data), encoding="utf-8")
    assert Config(settings_path=path).data["sql"] == {"enabled": False}


# ----------------------------------------------------------------------
# Saved connections
# ----------------------------------------------------------------------
def test_connections_are_saved_outside_settings_and_passwords_outside_both(tmp_path: Path) -> None:
    api, _ = turned_on(tmp_path)
    res = api.sql_save_connection(
        {"engine": "mssql", "name": "Dev ingest", "server": "dev-sql01", "database": "Ingest", "auth": "sql", "username": "svc"},
        "s3cret!",
    )
    conn = res["connection"]
    assert conn["hasPassword"] is True and "password" not in conn
    assert conn["where"] == "dev-sql01 / Ingest"

    db = tmp_path / "local" / "sql.db"
    assert db.is_file()
    assert b"s3cret!" not in db.read_bytes()
    assert "dev-sql01" not in (tmp_path / "settings.json").read_text(encoding="utf-8")

    # An empty password box keeps the saved password.
    res = api.sql_save_connection({**conn, "name": "Dev"}, "")
    assert res["connection"]["name"] == "Dev" and res["connection"]["hasPassword"] is True
    # Windows sign-in forgets it.
    res = api.sql_save_connection({**res["connection"], "auth": "windows"}, None)
    assert res["connection"]["hasPassword"] is False and res["connection"]["username"] == ""
    assert api.sql_passwords.get(conn["id"]) is None

    assert [c["name"] for c in api.sql_state()["connections"]] == ["Dev"]
    assert api.sql_delete_connection(conn["id"])["connections"] == []


def test_a_connection_cannot_change_type_or_take_a_path_from_the_page(tmp_path: Path) -> None:
    api, _ = turned_on(tmp_path)
    conn = api.sql_save_connection({"engine": "mssql", "name": "a", "server": "s"})["connection"]
    assert api.sql_save_connection({**conn, "engine": "sqlite"})["error"] == "invalid_input"
    assert api.sql_save_connection({"engine": "sqlite", "name": "x", "file": str(tmp_path)})["error"] == "invalid_input"


def test_sqlite_file_comes_from_the_dialog_only(tmp_path: Path) -> None:
    api, window = turned_on(tmp_path)
    # In the app the page cannot hand Python a path (rule 12f).
    from vaultnotes.api import expose_bridge

    class Exposer:
        exposed: dict[str, Any] = {}

        def expose(self, *functions: Any) -> None:
            for function in functions:
                self.exposed[function.__name__] = function

    exposer = Exposer()
    expose_bridge(exposer, api)
    assert exposer.exposed["sql_add_sqlite"](str(tmp_path / "x.db"))["error"] == "invalid_input"


def test_sqlite_connection_refuses_other_files(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    api.config.data["sql"]["enabled"] = True
    not_db = tmp_path / "notes.txt"
    not_db.write_text("hello, not a database", encoding="utf-8")
    assert api.sql_add_sqlite(str(not_db))["error"] == "invalid_input"
    assert api.sql_add_sqlite(str(tmp_path / "missing.db"))["error"] == "not_found"


# ----------------------------------------------------------------------
# Running queries (SQLite end to end)
# ----------------------------------------------------------------------
def sqlite_tab(tmp_path: Path) -> tuple[Api, FakeWindow, str]:
    window = FakeWindow()
    api = make_api(tmp_path)
    api.config.data["sql"]["enabled"] = True
    # No window yet, so the path stands in for the native dialog's answer.
    conn = api.sql_add_sqlite(str(sample_db(tmp_path / "ingest.db")))["connection"]
    assert conn["engine"] == "sqlite" and conn["where"] == "ingest.db"
    api.window = window  # from here on, events reach the fake page
    session = api.sql_open(conn["id"])
    assert session["ok"] is True
    return api, window, session["id"]


def test_run_reports_results_and_pages(tmp_path: Path) -> None:
    api, window, sid = sqlite_tab(tmp_path)
    started = api.sql_run(sid, "SELECT id, name, size FROM batches ORDER BY id; SELECT COUNT(*) AS n FROM batches")
    assert started["ok"] is True and started["batches"] == 2

    done = window.wait_for("sql_done")[0]
    assert done["ok"] is True and done["run"] == started["run"] and done["error"] is None
    first, second = done["results"]
    assert [c["name"] for c in first["columns"]] == ["id", "name", "size"]
    assert [c["kind"] for c in first["columns"]] == ["number", "text", "number"]
    assert first["total"] == 500 and len(first["rows"]) == 200
    assert first["rows"][0] == [1, "batch 1", 1.5]
    assert second["rows"] == [[500]]

    page = api.sql_rows(sid, 0, 450, 100)
    assert page["total"] == 500 and len(page["rows"]) == 50 and page["rows"][-1][0] == 500
    assert api.sql_rows(sid, 5, 0, 10)["error"] == "not_found"

    text = api.sql_copy(sid, 1)["text"]
    assert text == "n\r\n500"


def test_schema_lists_tables_views_and_columns_for_autocomplete(tmp_path: Path) -> None:
    api, window, sid = sqlite_tab(tmp_path)
    api.sql_run(sid, "CREATE VIEW big AS SELECT id, name FROM batches WHERE size > 100")
    window.wait_for("sql_done")

    res = api.sql_schema(sid)
    assert res["ok"] is True and res["truncated"] is False
    assert res["tables"] == [
        {"schema": "", "name": "batches", "columns": ["id", "name", "size", "started"]},
        {"schema": "", "name": "big", "columns": ["id", "name"]},
    ]
    assert api.sql_schema("nope")["error"] == "not_open"


def test_schema_waits_for_a_running_query(tmp_path: Path) -> None:
    api, window, sid = sqlite_tab(tmp_path)
    api.sql_run(sid, "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n) SELECT COUNT(*) FROM n")
    assert api.sql_schema(sid)["error"] == "busy"
    api.sql_cancel(sid)
    window.wait_for("sql_done")


class SchemaCursor:
    """What INFORMATION_SCHEMA.COLUMNS looks like through pyodbc."""

    def __init__(self, rows: list[tuple[str, str, str]]) -> None:
        self.rows = rows
        self.executed = ""

    def execute(self, text: str) -> None:
        self.executed = text

    def fetchmany(self, size: int) -> list[Any]:
        out, self.rows = self.rows[:size], self.rows[size:]
        return out

    def close(self) -> None:
        pass


def test_schema_on_sql_server_groups_columns_by_schema_and_table(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [("dbo", "Orders", "OrderId"), ("dbo", "Orders", "Total"), ("ops", "Batches", "BatchId")]
    cursor = SchemaCursor(rows)
    conn = FakeConnection()
    conn.cursor = lambda: cursor  # type: ignore[method-assign]
    manager = SqlManager(emit=lambda *_: None, connector=lambda *_: conn)
    profile = {**clean_profile({"engine": "mssql", "name": "Dev", "server": "s"}), "id": "c1"}
    session = manager.open(profile, None)

    assert manager.schema(session["id"]) == {
        "tables": [
            {"schema": "dbo", "name": "Orders", "columns": ["OrderId", "Total"]},
            {"schema": "ops", "name": "Batches", "columns": ["BatchId"]},
        ],
        "truncated": False,
    }
    assert "INFORMATION_SCHEMA.COLUMNS" in cursor.executed

    # A huge catalog is cut, and says so.
    monkeypatch.setattr("vaultnotes.sql.MAX_SCHEMA_COLUMNS", 2)
    cursor.rows = list(rows)
    cut = manager.schema(session["id"])
    assert cut["truncated"] is True and sum(len(t["columns"]) for t in cut["tables"]) == 2

    # A driver error is a message, not an exception.
    def broken() -> Any:
        raise RuntimeError("boom")

    conn.cursor = broken  # type: ignore[method-assign]
    with pytest.raises(SqlError) as info:
        manager.schema(session["id"])
    assert info.value.code == "schema_failed"


def test_writes_report_rows_affected_and_temp_tables_last_for_the_tab(tmp_path: Path) -> None:
    api, window, sid = sqlite_tab(tmp_path)
    api.sql_run(sid, "CREATE TEMP TABLE t (x); INSERT INTO t VALUES (1), (2), (3);")
    done = window.wait_for("sql_done")[0]
    assert done["ok"] is True and "(3 rows affected)" in done["messages"]

    api.sql_run(sid, "SELECT SUM(x) FROM t")
    done = window.wait_for("sql_done", 2)[1]
    assert done["results"][0]["rows"] == [[6]]


def test_errors_come_back_in_the_event(tmp_path: Path) -> None:
    api, window, sid = sqlite_tab(tmp_path)
    api.sql_run(sid, "SELECT 1; SELECT * FROM nope; SELECT 3")
    done = window.wait_for("sql_done")[0]
    assert done["ok"] is False
    assert "Batch 2 of 3" in done["error"] and "nope" in done["error"]
    assert len(done["results"]) == 1  # what ran before the error is kept


def test_empty_and_busy_runs_are_refused(tmp_path: Path) -> None:
    api, window, sid = sqlite_tab(tmp_path)
    assert api.sql_run(sid, "  -- nothing\n")["error"] == "empty"
    assert api.sql_run("not-a-tab", "SELECT 1")["error"] == "not_open"


def test_cancel_stops_a_long_query(tmp_path: Path) -> None:
    api, window, sid = sqlite_tab(tmp_path)
    slow = (
        "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 300000000) "
        "SELECT COUNT(*) FROM n"
    )
    api.sql_run(sid, slow)
    time.sleep(0.2)
    assert api.sql_run(sid, "SELECT 1")["error"] == "busy"
    assert api.sql_cancel(sid)["cancelled"] is True
    done = window.wait_for("sql_done")[0]
    assert done["cancelled"] is True and done["ok"] is False and done["error"] is None


def test_stop_right_after_run_is_not_lost(tmp_path: Path) -> None:
    """Stop pressed before SQLite started the statement still stops it."""
    api, window, sid = sqlite_tab(tmp_path)
    slow = (
        "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 300000000) "
        "SELECT COUNT(*) FROM n"
    )
    for attempt in range(5):
        api.sql_run(sid, slow)
        api.sql_cancel(sid)
        done = window.wait_for("sql_done", attempt + 1, timeout=5)[attempt]
        assert done["cancelled"] is True


def test_closing_and_turning_off_ends_the_tabs(tmp_path: Path) -> None:
    api, window, sid = sqlite_tab(tmp_path)
    assert [s["id"] for s in api.sql_state()["sessions"]] == [sid]
    assert api.sql_disable()["enabled"] is False
    assert api.sql_state()["sessions"] == []
    api.config.data["sql"]["enabled"] = True
    assert api.sql_run(sid, "SELECT 1")["error"] == "not_open"


def test_deleting_a_connection_closes_its_tabs(tmp_path: Path) -> None:
    api, window, sid = sqlite_tab(tmp_path)
    conn_id = api.sql_state()["connections"][0]["id"]
    api.sql_delete_connection(conn_id)
    assert api.sql_state()["sessions"] == []


def test_a_moved_sqlite_file_is_not_recreated(tmp_path: Path) -> None:
    api, window, sid = sqlite_tab(tmp_path)
    api.sql_close()
    conn_id = api.sql_state()["connections"][0]["id"]
    (tmp_path / "ingest.db").rename(tmp_path / "moved.db")
    assert api.sql_open(conn_id)["error"] == "not_found"
    assert not (tmp_path / "ingest.db").exists()


# ----------------------------------------------------------------------
# The manager with a fake SQL Server driver
# ----------------------------------------------------------------------
class FakeCursor:
    """Two result sets and a message, the way pyodbc hands them over."""

    def __init__(self) -> None:
        self.sets = [
            ([("id", int, None, None, None, None, None), ("total", decimal.Decimal, None, None, None, None, None)],
             [(1, decimal.Decimal("9.50")), (2, decimal.Decimal("1.25"))]),
            (None, 4),
        ]
        self.index = 0
        self.messages: list[tuple[str, str]] = []
        self.executed: list[str] = []
        self.rows: list[Any] = []

    @property
    def description(self) -> Any:
        return self.sets[self.index][0]

    @property
    def rowcount(self) -> int:
        current = self.sets[self.index]
        return -1 if current[0] else current[1]

    def execute(self, text: str) -> None:
        self.executed.append(text)
        self.index = 0
        self.messages = [("[01000] (0)", "[Microsoft][ODBC Driver 18 for SQL Server][SQL Server]hello from PRINT")]
        self.rows = list(self.sets[0][1])

    def fetchmany(self, size: int) -> list[Any]:
        out, self.rows = self.rows[:size], self.rows[size:]
        return out

    def nextset(self) -> bool:
        self.messages = []
        if self.index + 1 >= len(self.sets):
            return False
        self.index += 1
        return True

    def cancel(self) -> None:
        pass

    def close(self) -> None:
        pass


class FakeConnection:
    def __init__(self) -> None:
        self.cursors: list[FakeCursor] = []
        self.closed = False

    def cursor(self) -> FakeCursor:
        self.cursors.append(FakeCursor())
        return self.cursors[-1]

    def close(self) -> None:
        self.closed = True


def test_mssql_batches_result_sets_and_messages() -> None:
    events: list[tuple[str, Any]] = []
    conns: list[FakeConnection] = []

    def connector(profile: dict[str, Any], password: str | None) -> FakeConnection:
        conns.append(FakeConnection())
        return conns[-1]

    manager = SqlManager(emit=lambda name, data: events.append((name, data)), connector=connector)
    profile = {**clean_profile({"engine": "mssql", "name": "Dev", "server": "s"}), "id": "c1"}
    session = manager.open(profile, None)
    manager.run(session["id"], "SELECT 1\nGO\nUPDATE x SET y = 1")
    deadline = time.monotonic() + 5
    while not any(name == "sql_done" for name, _ in events) and time.monotonic() < deadline:
        time.sleep(0.01)
    done = next(data for name, data in events if name == "sql_done")
    assert conns[0].cursors[0].executed == ["SELECT 1\n", "UPDATE x SET y = 1"]
    assert done["ok"] is True
    assert done["results"][0]["columns"] == [{"name": "id", "kind": "number"}, {"name": "total", "kind": "number"}]
    assert done["results"][0]["rows"] == [[1, "9.50"], [2, "1.25"]]
    assert done["messages"][:3] == ["hello from PRINT", "(2 rows)", "(4 rows affected)"]

    manager.close(session["id"])
    assert conns[0].closed is True


def test_tab_limit() -> None:
    manager = SqlManager(emit=lambda *_: None, connector=lambda *_: FakeConnection())
    profile = {**clean_profile({"engine": "mssql", "name": "Dev", "server": "s"}), "id": "c1"}
    for _ in range(MAX_SESSIONS):
        manager.open(profile, None)
    with pytest.raises(SqlError) as info:
        manager.open(profile, None)
    assert info.value.code == "too_many"
    manager.close()
    assert manager.sessions() == []


def test_saved_password_is_used_to_sign_in(tmp_path: Path) -> None:
    api, _ = turned_on(tmp_path)
    seen: list[Any] = []
    api.sql = SqlManager(emit=lambda *_: None, connector=lambda profile, password: seen.append(password) or FakeConnection())
    conn = api.sql_save_connection(
        {"engine": "mssql", "name": "a", "server": "s", "auth": "sql", "username": "u"}, "pw1"
    )["connection"]
    assert api.sql_open(conn["id"])["ok"] is True
    assert api.sql_test_connection(conn, "typed")["ok"] is True
    assert api.sql_test_connection(conn)["ok"] is True
    assert seen == ["pw1", "typed", "pw1"]


def test_connect_failure_is_a_message(tmp_path: Path) -> None:
    api, _ = turned_on(tmp_path)

    def refuse(profile: dict[str, Any], password: str | None) -> Any:
        raise SqlError("connect_failed", "Login failed for user 'u'.")

    api.sql = SqlManager(emit=lambda *_: None, connector=refuse)
    conn = api.sql_save_connection({"engine": "mssql", "name": "a", "server": "s"})["connection"]
    assert api.sql_open(conn["id"]) == {"error": "connect_failed", "message": "Login failed for user 'u'."}


def test_no_sql_endpoint_raises_while_on(tmp_path: Path) -> None:
    """Hostile values from the page get an error back, never an exception."""
    api, _ = turned_on(tmp_path)
    api.window = None  # no native dialogs in a sweep
    names = [name for name in dir(Api) if name.startswith("sql_")]
    assert len(names) == 25
    for name in names:
        method = getattr(api, name)
        arity = len(inspect.signature(method).parameters)
        for hostile in (None, 0, "", [], {}, "../../x", "a" * 5000, {"engine": "mssql"}):
            api.config.data["sql"]["enabled"] = True  # sql_disable may have run
            result = method(*((hostile,) * arity))
            assert isinstance(result, dict), name
            assert {"ok", "error", "enabled"} & set(result), (name, result)
            assert result.get("error") != "internal_error", (name, hostile)


# ----------------------------------------------------------------------
# Sharing a result with AI helpers
# ----------------------------------------------------------------------
ROWS_1500 = (
    "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 1500) "
    "SELECT i, 'row ' || i AS label FROM n"
)


def run_done(api: Api, window: FakeWindow, sid: str, text: str) -> dict[str, Any]:
    """Run SQL in a tab and wait for its sql_done event."""
    before = len(window.events("sql_done"))
    assert api.sql_run(sid, text)["ok"] is True
    return window.wait_for("sql_done", before + 1)[before]


def shared(api: Api, window: FakeWindow, sid: str, text: str = "SELECT id, name FROM batches ORDER BY id") -> None:
    run_done(api, window, sid, text)
    assert api.sql_share_result(sid, 0, True)["ok"] is True
    assert api.ai_share.path.exists()


def test_sharing_needs_the_users_confirmation(tmp_path: Path) -> None:
    api, window, sid = sqlite_tab(tmp_path)
    run_done(api, window, sid, "SELECT id, name FROM batches")
    assert api.sql_share_result(sid, 0)["error"] == "not_confirmed"
    assert api.sql_share_result(sid, 0, "yes")["error"] == "not_confirmed"
    assert api.sql_share_result(sid, 0, 1)["error"] == "not_confirmed"
    assert not api.ai_share.path.exists()


def test_a_shared_result_is_capped_and_kept_outside_the_notes_folder(tmp_path: Path) -> None:
    api, window, sid = sqlite_tab(tmp_path)
    run_done(api, window, sid, ROWS_1500)
    res = api.sql_share_result(sid, 0, True)
    assert res["ok"] is True and res["rows"] == 1000 and res["total"] == 1500 and res["truncated"] is True

    snap = load_snapshot(api.ai_share.folder)
    assert snap is not None
    assert snap["shared_rows"] == 1000 and len(snap["rows"]) == 1000 and snap["total_rows"] == 1500
    assert snap["truncated"] is True and snap["query"] == ROWS_1500
    assert snap["rows"][0] == [1, "row 1"] and snap["rows"][-1] == [1000, "row 1000"]
    assert [c["name"] for c in snap["columns"]] == ["i", "label"]
    assert snap["connection"] == "ingest" and snap["engine"] == "SQLite"

    assert api.ai_share.folder.is_relative_to(tmp_path / "local")
    assert list((tmp_path / "notes").rglob("result.json")) == []


def test_a_small_result_is_shared_whole(tmp_path: Path) -> None:
    api, window, sid = sqlite_tab(tmp_path)
    shared(api, window, sid)
    snap = load_snapshot(api.ai_share.folder)
    assert snap is not None and snap["shared_rows"] == 500 and snap["truncated"] is False


def test_a_shared_file_expires_even_if_the_app_never_removed_it(tmp_path: Path) -> None:
    api, window, sid = sqlite_tab(tmp_path)
    shared(api, window, sid)
    assert load_snapshot(api.ai_share.folder, now=time.time() + 60) is not None
    assert load_snapshot(api.ai_share.folder, now=time.time() + 31 * 60) is None


def test_the_share_expires_in_the_app_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("vaultnotes.sql.SHARE_EXPIRY_SECONDS", 0.2)
    api, window, sid = sqlite_tab(tmp_path)
    shared(api, window, sid)
    event = window.wait_for("sql_share_changed")[-1]
    assert event == {"session": sid, "shared": False, "reason": "expired"}
    assert not api.ai_share.path.exists()


def test_a_new_run_takes_the_share_back(tmp_path: Path) -> None:
    api, window, sid = sqlite_tab(tmp_path)
    shared(api, window, sid)
    assert api.sql_run(sid, "SELECT 1")["ok"] is True
    assert not api.ai_share.path.exists()  # gone before the new result even arrives
    assert window.wait_for("sql_share_changed")[-1] == {"session": sid, "shared": False, "reason": "run"}
    window.wait_for("sql_done", 2)
    assert api.sql_share_result(sid, 0, True)["ok"] is True  # the new result can be shared afresh


def test_closing_the_tab_or_turning_sql_off_takes_the_share_back(tmp_path: Path) -> None:
    api, window, sid = sqlite_tab(tmp_path)
    shared(api, window, sid)
    api.sql_close(sid)
    assert not api.ai_share.path.exists()
    assert window.wait_for("sql_share_changed")[-1]["reason"] == "closed"

    session = api.sql_open(api.sql_state()["connections"][0]["id"])["id"]
    shared(api, window, session)
    assert api.sql_disable()["enabled"] is False
    assert not api.ai_share.path.exists()


def test_unsharing_by_hand(tmp_path: Path) -> None:
    api, window, sid = sqlite_tab(tmp_path)
    shared(api, window, sid)
    assert api.sql_unshare_result(sid) == {"ok": True}
    assert not api.ai_share.path.exists()
    assert window.wait_for("sql_share_changed")[-1] == {"session": sid, "shared": False, "reason": "user"}
    assert api.sql_unshare_result(sid) == {"ok": True}  # nothing shared any more: still fine


def test_sharing_another_result_replaces_the_first(tmp_path: Path) -> None:
    api, window, sid = sqlite_tab(tmp_path)
    other = api.sql_open(api.sql_state()["connections"][0]["id"])["id"]
    shared(api, window, sid, "SELECT 1 AS a")
    shared(api, window, other, "SELECT 2 AS b")
    snap = load_snapshot(api.ai_share.folder)
    assert snap is not None and snap["query"] == "SELECT 2 AS b"
    assert {"session": sid, "shared": False, "reason": "replaced"} in window.wait_for("sql_share_changed")

    api.sql_close(sid)  # the first tab no longer owns the file
    assert api.ai_share.path.exists()
    api.sql_close(other)
    assert not api.ai_share.path.exists()


def test_a_running_tab_cannot_share(tmp_path: Path) -> None:
    api, window, sid = sqlite_tab(tmp_path)
    run_done(api, window, sid, "SELECT 1")
    slow = (
        "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 300000000) "
        "SELECT COUNT(*) FROM n"
    )
    api.sql_run(sid, slow)
    assert api.sql_share_result(sid, 0, True)["error"] == "busy"
    api.sql_cancel(sid)
    window.wait_for("sql_done", 2)
    assert not api.ai_share.path.exists()


def test_a_missing_result_or_tab_cannot_share(tmp_path: Path) -> None:
    api, window, sid = sqlite_tab(tmp_path)
    run_done(api, window, sid, "SELECT 1")
    assert api.sql_share_result(sid, 5, True)["error"] == "not_found"
    assert api.sql_share_result("not-a-tab", 0, True)["error"] == "not_open"
    assert not api.ai_share.path.exists()


def test_a_leftover_shared_file_is_removed_at_start_up(tmp_path: Path) -> None:
    folder = tmp_path / "local" / "ai-share"
    folder.mkdir(parents=True)
    (folder / "result.json").write_text('{"left": "behind"}', encoding="utf-8")
    api = make_api(tmp_path)
    assert not (folder / "result.json").exists()
    api.close()


# ----------------------------------------------------------------------
# Saved queries
# ----------------------------------------------------------------------
def test_saved_queries_live_on_their_connection(tmp_path: Path) -> None:
    api, _ = turned_on(tmp_path)
    dev = api.sql_save_connection({"engine": "mssql", "name": "Dev", "server": "dev"})["connection"]
    qa = api.sql_save_connection({"engine": "mssql", "name": "QA", "server": "qa"})["connection"]

    res = api.sql_save_query({"connection": dev["id"], "name": "Pending batches", "text": "-- oldest first\nSELECT * FROM batches"})
    query = res["query"]
    assert query["connection"] == dev["id"] and query["text"].endswith("FROM batches")
    assert query["preview"] == "SELECT * FROM batches" and query["lastRun"] is None
    # The list carries no SQL; the tab asks for it when it opens.
    assert res["queries"] == [{k: v for k, v in query.items() if k != "text"}]
    assert api.sql_get_query(query["id"])["query"]["text"] == query["text"]

    # Saving again changes it in place; another connection moves it.
    moved = api.sql_save_query({**query, "connection": qa["id"], "name": "Pending", "text": "SELECT 1"})["query"]
    assert moved["id"] == query["id"] and moved["connection"] == qa["id"] and moved["name"] == "Pending"
    assert len(api.sql_state()["queries"]) == 1

    # They survive a restart: sql.db is on disk.
    again = make_api(tmp_path)
    again.config.data["sql"]["enabled"] = True
    assert [q["name"] for q in again.sql_state()["queries"]] == ["Pending"]

    assert api.sql_delete_query(query["id"])["queries"] == []
    assert api.sql_get_query(query["id"])["error"] == "not_found"


def test_deleting_a_connection_deletes_its_queries(tmp_path: Path) -> None:
    api, _ = turned_on(tmp_path)
    dev = api.sql_save_connection({"engine": "mssql", "name": "Dev", "server": "dev"})["connection"]
    qa = api.sql_save_connection({"engine": "mssql", "name": "QA", "server": "qa"})["connection"]
    api.sql_save_query({"connection": dev["id"], "name": "a", "text": "SELECT 1"})
    api.sql_save_query({"connection": qa["id"], "name": "b", "text": "SELECT 2"})
    res = api.sql_delete_connection(dev["id"])
    assert [q["name"] for q in res["queries"]] == ["b"]


@pytest.mark.parametrize(
    "query, code",
    [
        ({"connection": "nope", "name": "a", "text": "SELECT 1"}, "not_found"),
        ({"name": "a", "text": "SELECT 1"}, "invalid_input"),
        ({"connection": None, "name": "", "text": "SELECT 1"}, "invalid_input"),
        ({"connection": "CONN", "name": "", "text": "SELECT 1"}, "invalid_input"),
        ({"connection": "CONN", "name": "a\nb", "text": "SELECT 1"}, "invalid_input"),
        ({"connection": "CONN", "name": "a", "text": "   "}, "empty"),
        ({"connection": "CONN", "name": "a", "text": "x" * 1_000_001}, "invalid_input"),
        ({"connection": "CONN", "name": "a", "text": "SELECT 1", "id": "missing"}, "not_found"),
    ],
)
def test_bad_saved_queries_are_refused(tmp_path: Path, query: dict[str, Any], code: str) -> None:
    api, _ = turned_on(tmp_path)
    conn = api.sql_save_connection({"engine": "mssql", "name": "Dev", "server": "dev"})["connection"]
    query = {k: (conn["id"] if v == "CONN" else v) for k, v in query.items()}
    assert api.sql_save_query(query)["error"] == code


def test_running_a_saved_query_marks_when(tmp_path: Path) -> None:
    api, window, sid = sqlite_tab(tmp_path)
    conn_id = api.sql_state()["connections"][0]["id"]
    query = api.sql_save_query({"connection": conn_id, "name": "Count", "text": "SELECT COUNT(*) FROM batches"})["query"]
    assert api.sql_run(sid, query["text"], query["id"])["ok"] is True
    assert window.wait_for("sql_done")[0]["results"][0]["rows"] == [[500]]
    assert api.sql_get_query(query["id"])["query"]["lastRun"] is not None


def test_a_step_one_sql_db_is_upgraded_in_place(tmp_path: Path) -> None:
    path = tmp_path / "local" / "sql.db"
    path.parent.mkdir(parents=True)
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE connections (id TEXT PRIMARY KEY, name TEXT NOT NULL, engine TEXT NOT NULL, "
        "server TEXT NOT NULL DEFAULT '', database TEXT NOT NULL DEFAULT '', auth TEXT NOT NULL DEFAULT 'windows', "
        "username TEXT NOT NULL DEFAULT '', encrypt INTEGER NOT NULL DEFAULT 1, trust_cert INTEGER NOT NULL DEFAULT 0, "
        "file TEXT NOT NULL DEFAULT '', created TEXT NOT NULL, modified TEXT NOT NULL)"
    )
    old.execute("INSERT INTO connections (id, name, engine, server, created, modified) VALUES ('c1', 'Old', 'mssql', 's', 'x', 'x')")
    old.execute("PRAGMA user_version = 1")
    old.commit()
    old.close()

    api, _ = turned_on(tmp_path)
    assert [c["name"] for c in api.sql_state()["connections"]] == ["Old"]
    assert api.sql_save_query({"connection": "c1", "name": "q", "text": "SELECT 1"})["ok"] is True
