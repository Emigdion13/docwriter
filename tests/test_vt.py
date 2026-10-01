"""The SQL - VT space: results kept as tables in vt.db, queried after the
connection they came from is gone.
"""

from __future__ import annotations

import datetime as dt
import decimal
import sqlite3
import uuid
from pathlib import Path
from typing import Any

import pytest

from test_sql import FakeWindow, make_api, sample_db, sqlite_tab
from vaultnotes.sql import SqlError
from vaultnotes.vt import VtStore, clean_table_name, column_names, sqlite_value


def run(api: Any, window: FakeWindow, sid: str, text: str) -> dict[str, Any]:
    before = len(window.events("sql_done"))
    assert api.sql_run(sid, text)["ok"] is True
    return window.wait_for("sql_done", before + 1)[before]


# ----------------------------------------------------------------------
# Pure helpers
# ----------------------------------------------------------------------
@pytest.mark.parametrize("name", ["pending_batches", "_tmp", "T1", "a" * 63])
def test_good_names(name: str) -> None:
    assert clean_table_name(f" {name} ") == name


@pytest.mark.parametrize("name", ["", "1st", "has space", "semi;colon", "a" * 64, "sqlite_x", "_vt_meta", None, 5, "dbo.x"])
def test_bad_names(name: Any) -> None:
    with pytest.raises(SqlError):
        clean_table_name(name)


def test_column_names_are_made_usable() -> None:
    assert column_names(["id", "ID", "", "(No column name 4)", "id"]) == ["id", "ID_2", "column_3", "column_4", "id_3"]


def test_driver_values_become_sqlite_values() -> None:
    assert sqlite_value(True) == 1
    assert sqlite_value(decimal.Decimal("42.000")) == 42
    assert sqlite_value(decimal.Decimal("9.5")) == 9.5
    assert sqlite_value(decimal.Decimal("NaN")) == "NaN"
    assert sqlite_value(2**70) == str(2**70)
    assert sqlite_value(dt.datetime(2026, 10, 1, 8, 30)) == "2026-10-01 08:30:00"
    assert sqlite_value(dt.date(2026, 10, 1)) == "2026-10-01"
    assert sqlite_value(bytearray(b"\x01")) == b"\x01"
    assert sqlite_value(uuid.UUID(int=1)) == "00000000-0000-0000-0000-000000000001"


# ----------------------------------------------------------------------
# Saving and querying
# ----------------------------------------------------------------------
def test_a_result_becomes_a_virtual_table_that_outlives_its_connection(tmp_path: Path) -> None:
    api, window, sid = sqlite_tab(tmp_path)
    run(api, window, sid, "SELECT id, name, size FROM batches WHERE id <= 300; SELECT 1 AS one")

    res = api.sql_save_vt(sid, 0, "first_batches")
    assert res["ok"] is True and res["table"] == {"name": "first_batches", "rows": 300, "columns": ["id", "name", "size"]}
    table = res["tables"][0]
    assert table["name"] == "first_batches" and table["rows"] == 300
    assert table["source"] == {"connection": table["source"]["connection"], "name": "ingest", "where": "ingest.db"}
    assert table["query"].startswith("SELECT id, name, size")
    assert (tmp_path / "local" / "vt.db").is_file()

    # Disconnect and move the source file away: the virtual table stays.
    api.sql_close()
    (tmp_path / "ingest.db").rename(tmp_path / "gone.db")
    vt = api.sql_open("vt")
    assert vt["ok"] is True and vt["connection"]["name"] == "Virtual tables"
    done = run(api, window, vt["id"], "SELECT COUNT(*), SUM(size) FROM first_batches")
    assert done["results"][0]["rows"] == [[300, sum(i * 1.5 for i in range(1, 301))]]


def test_virtual_tables_join_across_sources(tmp_path: Path) -> None:
    api, window, sid = sqlite_tab(tmp_path)
    run(api, window, sid, "SELECT id, name FROM batches WHERE id <= 10")
    api.sql_save_vt(sid, 0, "a")
    run(api, window, sid, "SELECT id, size FROM batches WHERE id BETWEEN 6 AND 20")
    api.sql_save_vt(sid, 0, "b")

    vt = api.sql_open("vt")["id"]
    done = run(api, window, vt, "SELECT a.id, a.name, b.size FROM a JOIN b ON b.id = a.id ORDER BY a.id")
    assert [row[0] for row in done["results"][0]["rows"]] == [6, 7, 8, 9, 10]

    # SQL in the space makes (and changes) virtual tables too.
    run(api, window, vt, "CREATE TABLE c AS SELECT * FROM a WHERE id < 3")
    tables = {t["name"]: t for t in api.sql_vt_list()["tables"]}
    assert tables["c"]["rows"] == 2 and tables["c"]["source"] is None
    run(api, window, vt, "DELETE FROM a WHERE id > 5")
    assert {t["name"]: t["rows"] for t in api.sql_vt_list()["tables"]}["a"] == 5


def test_an_existing_name_is_replaced_only_when_asked(tmp_path: Path) -> None:
    api, window, sid = sqlite_tab(tmp_path)
    run(api, window, sid, "SELECT id FROM batches WHERE id <= 3")
    api.sql_save_vt(sid, 0, "keep")
    run(api, window, sid, "SELECT id, name FROM batches WHERE id <= 7")
    assert api.sql_save_vt(sid, 0, "KEEP")["error"] == "exists"
    res = api.sql_save_vt(sid, 0, "keep", True)
    assert [(t["name"], t["rows"], t["columns"]) for t in res["tables"]] == [("keep", 7, ["id", "name"])]


def test_rename_and_delete(tmp_path: Path) -> None:
    api, window, sid = sqlite_tab(tmp_path)
    run(api, window, sid, "SELECT 1 AS x")
    api.sql_save_vt(sid, 0, "one")
    api.sql_save_vt(sid, 0, "two")
    assert api.sql_rename_vt("one", "two")["error"] == "exists"
    assert [t["name"] for t in api.sql_rename_vt("one", "uno")["tables"]] == ["two", "uno"]
    assert api.sql_rename_vt("nope", "x")["error"] == "not_found"
    assert api.sql_rename_vt("uno", "bad name")["error"] == "invalid_input"
    assert [t["name"] for t in api.sql_delete_vt("two")["tables"]] == ["uno"]


def test_nothing_is_saved_while_the_tab_still_runs(tmp_path: Path) -> None:
    api, window, sid = sqlite_tab(tmp_path)
    run(api, window, sid, "SELECT 1")
    slow = "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 300000000) SELECT COUNT(*) FROM n"
    api.sql_run(sid, slow)
    assert api.sql_save_vt(sid, 0, "half")["error"] == "busy"
    api.sql_cancel(sid)
    window.wait_for("sql_done", 2)


def test_a_result_that_is_gone_cannot_be_saved(tmp_path: Path) -> None:
    api, window, sid = sqlite_tab(tmp_path)
    assert api.sql_save_vt(sid, 0, "x")["error"] == "not_found"
    assert api.sql_save_vt("not-a-tab", 0, "x")["error"] == "not_open"


def test_vt_is_off_with_the_sql_space(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    assert api.sql_open("vt")["error"] == "sql_off"
    assert api.sql_vt_list()["error"] == "sql_off"
    assert api.sql_state()["vtables"] == []


def test_vt_db_stays_outside_the_notes_folder(tmp_path: Path) -> None:
    api, window, sid = sqlite_tab(tmp_path)
    run(api, window, sid, "SELECT 1 AS x")
    api.sql_save_vt(sid, 0, "x")
    notes = Path(api.config.notes_root)
    assert not list(notes.rglob("vt.db")) and (tmp_path / "local" / "vt.db").is_file()


# ----------------------------------------------------------------------
# The store with SQL Server shaped values
# ----------------------------------------------------------------------
def test_store_keeps_sql_server_values(tmp_path: Path) -> None:
    store = VtStore(tmp_path / "vt.db")
    rows = [
        (1, decimal.Decimal("10.50"), dt.datetime(2026, 9, 30, 23, 59), True, b"\x00\xff", None),
        (2, decimal.Decimal("3"), dt.datetime(2026, 10, 1, 0, 0), False, b"", "x"),
    ]
    saved = store.save(
        "money", ["id", "amount", "at", "ok", "raw", "note"], ["number", "number", "date", "bool", "binary", "text"],
        iter(rows), source={"connection": "c1", "name": "Prod", "where": "prod / Billing"}, query="SELECT ...", replace=False,
    )
    assert saved["rows"] == 2
    db = sqlite3.connect(tmp_path / "vt.db")
    assert db.execute("SELECT * FROM money ORDER BY id").fetchall() == [
        (1, 10.5, "2026-09-30 23:59:00", 1, b"\x00\xff", None),
        (2, 3, "2026-10-01 00:00:00", 0, b"", "x"),
    ]
    assert db.execute("SELECT SUM(amount) FROM money WHERE at >= '2026-10-01'").fetchone() == (3,)
    db.close()


def test_a_failed_save_leaves_the_old_table(tmp_path: Path) -> None:
    store = VtStore(tmp_path / "vt.db")
    store.save("t", ["x"], ["number"], iter([(1,)]), source=None, query="", replace=False)

    def broken() -> Any:
        yield (2,)
        raise RuntimeError("driver hiccup")

    with pytest.raises(RuntimeError):
        store.save("t", ["x"], ["number"], broken(), source=None, query="", replace=True)
    assert [t["rows"] for t in store.list()] == [1]
