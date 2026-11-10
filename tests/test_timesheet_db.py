from __future__ import annotations

import sqlite3

import pytest

from lpdev.db import (
    SCHEMA_VERSION,
    connect,
    init_schema,
    timesheet_cell_set,
    timesheet_cells,
    timesheet_cells_clear,
    timesheet_task_add,
    timesheet_tasks,
    worklog_submission_create,
    worklog_submission_finish,
    worklog_submissions,
)


def test_migration_v13_from_v12_keeps_data_and_is_idempotent(tmp_path):
    conn = connect(tmp_path / "db.sqlite")
    init_schema(conn)
    conn.execute("INSERT INTO poll_state (key, value) VALUES ('k', 'v')")
    for table in ("timesheet_cell", "timesheet_task", "worklog_submission"):
        conn.execute(f"DROP TABLE {table}")
    conn.execute("PRAGMA user_version = 12")
    init_schema(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 14
    assert conn.execute("SELECT value FROM poll_state WHERE key = 'k'").fetchone()[0] == "v"
    timesheet_cell_set(conn, "SUPERAPP-1", "2026-11-09", 60)
    conn.execute("PRAGMA user_version = 12")
    init_schema(conn)
    assert timesheet_cells(conn, ["2026-11-09"]) == {("SUPERAPP-1", "2026-11-09"): 60}
    conn.close()


def test_timesheet_cells_crud(conn):
    timesheet_cell_set(conn, "A-1", "2026-11-09", 60)
    timesheet_cell_set(conn, "A-1", "2026-11-09", 90)
    timesheet_cell_set(conn, "A-2", "2026-11-10", 0)
    timesheet_cell_set(conn, "A-3", "2026-11-17", 30)
    assert timesheet_cells(conn, ["2026-11-09", "2026-11-10"]) == {
        ("A-1", "2026-11-09"): 90,
        ("A-2", "2026-11-10"): 0,
    }
    assert timesheet_cells(conn, []) == {}
    timesheet_cell_set(conn, "A-1", "2026-11-09", None)
    assert timesheet_cells(conn, ["2026-11-09"]) == {}
    assert timesheet_cells_clear(conn, ["2026-11-10", "2026-11-11"]) == 1
    assert timesheet_cells_clear(conn, []) == 0
    assert timesheet_cells(conn, ["2026-11-17"]) == {("A-3", "2026-11-17"): 30}


def test_timesheet_cell_rejects_negative_minutes(conn):
    with pytest.raises(sqlite3.IntegrityError):
        timesheet_cell_set(conn, "A-1", "2026-11-09", -30)


def test_timesheet_tasks_are_per_week_and_unique(conn):
    timesheet_task_add(conn, "A-1", "2026-11-09")
    timesheet_task_add(conn, "A-1", "2026-11-09")
    timesheet_task_add(conn, "A-2", "2026-11-09")
    timesheet_task_add(conn, "A-3", "2026-11-16")
    assert timesheet_tasks(conn, "2026-11-09") == ["A-1", "A-2"]
    assert timesheet_tasks(conn, "2026-11-02") == []


def test_worklog_submission_journal(conn):
    first = worklog_submission_create(conn, "A-1", "2026-11-09", 60)
    second = worklog_submission_create(conn, "A-1", "2026-11-10", 30)
    worklog_submission_finish(conn, first, "done", tracker_id="777")
    worklog_submission_finish(conn, second, "failed", error="boom")
    rows = worklog_submissions(conn, ["2026-11-09", "2026-11-10"])
    assert [(row["id"], row["status"], row["tracker_id"], row["error"]) for row in rows] == [
        (first, "done", "777", None),
        (second, "failed", None, "boom"),
    ]
    assert rows[0]["updated_at"] is not None
    assert worklog_submissions(conn, []) == []


@pytest.mark.parametrize(("minutes", "status"), [(0, "pending"), (30, "weird")])
def test_worklog_submission_constraints(conn, minutes, status):
    with pytest.raises(sqlite3.IntegrityError):
        submission_id = worklog_submission_create(conn, "A-1", "2026-11-09", minutes)
        worklog_submission_finish(conn, submission_id, status)


def test_migration_v14_adds_reconcile_attempts_to_existing_v13_journal(tmp_path):
    from lpdev.db import worklog_submission_attempted

    conn = connect(tmp_path / "db.sqlite")
    init_schema(conn)
    conn.execute("DROP TABLE worklog_submission")
    conn.execute(
        """
        CREATE TABLE worklog_submission (
            id INTEGER PRIMARY KEY AUTOINCREMENT, issue_key TEXT NOT NULL, day TEXT NOT NULL,
            minutes INTEGER NOT NULL CHECK(minutes > 0),
            status TEXT NOT NULL CHECK(status IN ('pending','done','failed','unknown')),
            tracker_id TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO worklog_submission (issue_key, day, minutes, status, created_at) VALUES ('A-1', '2026-11-09', 30, 'unknown', 'x')"
    )
    conn.execute("PRAGMA user_version = 13")
    init_schema(conn)
    init_schema(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 14
    row = worklog_submissions(conn, ["2026-11-09"])[0]
    assert (row["status"], row["reconcile_attempts"]) == ("unknown", 0)
    worklog_submission_attempted(conn, row["id"])
    worklog_submission_attempted(conn, row["id"])
    row = worklog_submissions(conn, ["2026-11-09"])[0]
    assert row["reconcile_attempts"] == 2
    assert row["updated_at"] is not None
    conn.close()
