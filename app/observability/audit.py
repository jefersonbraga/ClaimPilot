"""Append-only decision audit (SQLite) + structured JSON logging.

SQLite keeps the MVP dependency-free. In production this becomes an immutable, retention-managed store
(e.g. Postgres with WORM backups or an event log), with PHI access itself audited.
"""

import json
import logging
import sqlite3
import sys
import threading
from datetime import datetime, timezone

from app.models.domain import ExecutionRecord

_logger = logging.getLogger("claimpilot")
if not _logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(_handler)
    _logger.setLevel(logging.INFO)
    _logger.propagate = False


def log_event(event: str, **fields) -> None:
    _logger.info(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "event": event, **fields}, default=str))


def connect(path: str) -> sqlite3.Connection:
    """SQLite connection shared by the audit store and usage analytics (same file, separate connections).

    * autocommit (isolation_level=None): no statement can leave a transaction open and hold the write lock;
      a forgotten commit once locked every audit INSERT with "database is locked" (see tests/test_sqlite_concurrency.py)
    * WAL: readers never block the writer and vice versa
    * busy_timeout: brief waits instead of immediate errors under concurrent writes
    """
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=10)
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


class AuditStore:
    def __init__(self, path: str):
        self._lock = threading.Lock()
        self._conn = connect(path)
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS executions (
                   execution_id TEXT PRIMARY KEY,
                   claim_id     TEXT NOT NULL,
                   timestamp    TEXT NOT NULL,
                   record       TEXT NOT NULL)"""
        )
        # `visitor` scopes history in the public demo: a hash of an anonymous browser cookie, never the cookie itself.
        # Production would scope by authenticated user + role (RBAC) instead.
        if "visitor" not in {row[1] for row in self._conn.execute("PRAGMA table_info(executions)")}:
            self._conn.execute("ALTER TABLE executions ADD COLUMN visitor TEXT")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_exec_claim ON executions(claim_id)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_exec_visitor ON executions(visitor, timestamp)")
        self._conn.commit()

    def save(self, record: ExecutionRecord, visitor: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO executions (execution_id, claim_id, timestamp, record, visitor) VALUES (?, ?, ?, ?, ?)",
                (record.execution_id, record.claim_id, record.timestamp.isoformat(), record.model_dump_json(), visitor),
            )
            self._conn.commit()

    def get(self, execution_id: str) -> ExecutionRecord | None:
        row = self._conn.execute("SELECT record FROM executions WHERE execution_id = ?", (execution_id,)).fetchone()
        return ExecutionRecord.model_validate_json(row[0]) if row else None

    def for_claim(self, claim_id: str, visitor: str | None = None) -> list[ExecutionRecord]:
        """All executions of a claim; limited to one visitor's executions when `visitor` is given."""
        sql, args = "SELECT record FROM executions WHERE claim_id = ?", [claim_id]
        if visitor is not None:
            sql, args = sql + " AND visitor = ?", args + [visitor]
        rows = self._conn.execute(sql + " ORDER BY timestamp", args).fetchall()
        return [ExecutionRecord.model_validate_json(r[0]) for r in rows]

    def recent(self, visitor: str, limit: int = 20, claim_id: str | None = None) -> list[ExecutionRecord]:
        """A visitor's most recent executions, newest first."""
        sql, args = "SELECT record FROM executions WHERE visitor = ?", [visitor]
        if claim_id:
            sql, args = sql + " AND claim_id = ?", args + [claim_id]
        rows = self._conn.execute(sql + " ORDER BY timestamp DESC LIMIT ?", args + [limit]).fetchall()
        return [ExecutionRecord.model_validate_json(r[0]) for r in rows]

    def all(self) -> list[ExecutionRecord]:
        return [ExecutionRecord.model_validate_json(r[0]) for r in self._conn.execute("SELECT record FROM executions")]
