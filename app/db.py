"""Read-only query execution as analyst_ro. The role's grants are the control; this adds belt and braces."""

from __future__ import annotations

from dataclasses import dataclass

import psycopg

from app.config import settings

PERMISSION_STATES = {"42501", "25006"}  # insufficient_privilege, read_only_sql_transaction


class DBError(Exception):
    def __init__(self, message: str, sqlstate: str | None = None):
        super().__init__(message)
        self.message = message
        self.sqlstate = sqlstate

    @property
    def is_permission(self) -> bool:
        return self.sqlstate in PERMISSION_STATES

    @property
    def is_timeout(self) -> bool:
        return self.sqlstate == "57014"


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[tuple]
    truncated: bool = False


def _message(exc: psycopg.Error) -> str:
    diag = exc.diag
    msg = diag.message_primary or str(exc).splitlines()[0]
    if diag.message_hint:
        msg += f" (hint: {diag.message_hint})"
    return msg


def fetch(conn: psycopg.Connection, sql: str, max_rows: int | None = None) -> QueryResult:
    """Run one query on an open connection inside a read-only transaction, then roll back.

    Uses a server-side cursor so a huge result is never pulled into memory: at most max_rows + 1 rows
    cross the wire.
    """
    max_rows = max_rows or settings.max_rows
    try:
        conn.rollback()
        conn.read_only = True
        conn.execute(f"SET LOCAL statement_timeout = {int(settings.statement_timeout_ms)}")
        with conn.cursor(name="ask_q", scrollable=False) as cur:
            cur.execute(sql)
            rows = cur.fetchmany(max_rows + 1)
            cols = [d.name for d in cur.description or []]
        return QueryResult(cols, rows[:max_rows], truncated=len(rows) > max_rows)
    except psycopg.Error as exc:
        raise DBError(_message(exc), exc.sqlstate) from exc
    finally:
        conn.rollback()


def run_query(sql: str, dsn: str | None = None, max_rows: int | None = None) -> QueryResult:
    """Connect as analyst_ro (default DSN), run the query read-only with a 5 s timeout, disconnect."""
    try:
        with psycopg.connect(dsn or settings.analyst_dsn, connect_timeout=5) as conn:
            return fetch(conn, sql, max_rows)
    except psycopg.OperationalError as exc:  # connection-level failure, not a query error
        raise DBError(_message(exc), exc.sqlstate) from exc
