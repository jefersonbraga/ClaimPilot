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


class AuditStore:
    def __init__(self, path: str):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS executions (
                   execution_id TEXT PRIMARY KEY,
                   claim_id     TEXT NOT NULL,
                   timestamp    TEXT NOT NULL,
                   record       TEXT NOT NULL)"""
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_exec_claim ON executions(claim_id)")
        self._conn.commit()

    def save(self, record: ExecutionRecord) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO executions VALUES (?, ?, ?, ?)",
                (record.execution_id, record.claim_id, record.timestamp.isoformat(), record.model_dump_json()),
            )
            self._conn.commit()

    def get(self, execution_id: str) -> ExecutionRecord | None:
        row = self._conn.execute("SELECT record FROM executions WHERE execution_id = ?", (execution_id,)).fetchone()
        return ExecutionRecord.model_validate_json(row[0]) if row else None

    def for_claim(self, claim_id: str) -> list[ExecutionRecord]:
        rows = self._conn.execute(
            "SELECT record FROM executions WHERE claim_id = ? ORDER BY timestamp", (claim_id,)
        ).fetchall()
        return [ExecutionRecord.model_validate_json(r[0]) for r in rows]

    def all(self) -> list[ExecutionRecord]:
        return [ExecutionRecord.model_validate_json(r[0]) for r in self._conn.execute("SELECT record FROM executions")]
