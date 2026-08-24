"""
engine/db.py
Thin pyodbc wrapper backing the Queries tab's Run button -- executes a
query's SQL against a named connection from config/connections.json and
returns plain columns/rows, or raises with pyodbc's own error text (the
caller turns that into the Results panel's error state).

Each connection in connections.json is just {"name", "connection_string"}
-- a raw ODBC connection string rather than a modeled set of
driver/server/database/uid/pwd fields, since that's the one shape that
works unmodified for whatever ODBC driver/DB the user already has
configured (SQL Server, Postgres, etc.) without SCRAML needing to know
its dialect. Stored in plaintext -- see config/connections.json's own
note on why, and keep that file out of version control.
"""

from __future__ import annotations

import time

import pyodbc


def run_query(connection_string: str, sql: str, params: list | None = None) -> dict:
    """Runs one query to completion and returns
    {"columns": [...], "rows": [[...], ...], "row_count", "duration_ms"}.
    Raises pyodbc.Error (or any other exception the driver raises) on
    failure -- the caller is responsible for catching it and turning it
    into a user-facing message; this stays a thin pass-through so that
    message is the driver's own, not something reworded/lossy here.
    """
    started = time.monotonic()
    with pyodbc.connect(connection_string, timeout=10) as conn:
        cursor = conn.cursor()
        cursor.execute(sql, params or [])
        if cursor.description:
            columns = [col[0] for col in cursor.description]
            rows = [list(row) for row in cursor.fetchall()]
        else:
            columns, rows = [], []
        conn.commit()
    duration_ms = round((time.monotonic() - started) * 1000, 1)
    return {"columns": columns, "rows": rows, "row_count": len(rows), "duration_ms": duration_ms}


def test_connection(connection_string: str) -> None:
    """Just opens and immediately closes a connection -- raises on
    failure. Used by the Connections modal's own quick "Test" action, to
    validate a connection string that hasn't run any query yet."""
    with pyodbc.connect(connection_string, timeout=10):
        pass
