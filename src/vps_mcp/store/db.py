"""Local SQLite store: audit trail, revisions, deployed-service registry (research R9)."""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS managed_services (
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    origin      TEXT NOT NULL CHECK (origin IN ('declared', 'deployed')),
    spec_json   TEXT,
    added_at    TEXT NOT NULL,
    removed_at  TEXT,
    PRIMARY KEY (name, kind)
);
CREATE TABLE IF NOT EXISTS audit_records (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ts               TEXT NOT NULL,
    session_id       TEXT,
    action           TEXT NOT NULL,
    service_name     TEXT,
    kind             TEXT,
    params_json      TEXT,
    confirmed        INTEGER NOT NULL DEFAULT 0,
    outcome          TEXT NOT NULL,
    error_category   TEXT,
    error_message    TEXT,
    resulting_state  TEXT,
    reconciled_state TEXT,
    data_deleted     INTEGER NOT NULL DEFAULT 0,
    duration_ms      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_records (ts DESC);
CREATE TABLE IF NOT EXISTS service_revisions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    service_name  TEXT NOT NULL,
    kind          TEXT NOT NULL,
    version       TEXT,
    content_json  TEXT NOT NULL,
    status        TEXT NOT NULL
        CHECK (status IN ('active', 'previous', 'superseded', 'rolled_back')),
    created_at    TEXT NOT NULL,
    audit_id      INTEGER REFERENCES audit_records (id)
);
CREATE INDEX IF NOT EXISTS idx_revisions_service ON service_revisions (service_name, kind);
"""


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def default_db_path(vps_name: str) -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / "vps-mcp" / f"{vps_name}.db"


def open_db(path: str | Path) -> sqlite3.Connection:
    p = Path(path)
    if str(p) != ":memory:":
        p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not p.exists():
            p.touch(mode=0o600)
        os.chmod(p, 0o600)  # revisions hold real secret values
    conn = sqlite3.connect(str(p), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < SCHEMA_VERSION:
        conn.executescript(_SCHEMA)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    return conn


def purge_audit(conn: sqlite3.Connection, retention_days: int) -> int:
    cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat(timespec="milliseconds")
    conn.execute("BEGIN")
    try:
        conn.execute(
            "UPDATE service_revisions SET audit_id = NULL WHERE audit_id IN "
            "(SELECT id FROM audit_records WHERE ts < ?)",
            (cutoff,),
        )
        cur = conn.execute("DELETE FROM audit_records WHERE ts < ?", (cutoff,))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return cur.rowcount
