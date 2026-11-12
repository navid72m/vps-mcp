"""Local state: SQLite (WAL, 0600) for conversations, approvals, schedules, budgets, audit (R10, R16)."""

from __future__ import annotations

import json
import os
import secrets as _secrets
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY, chat_id INTEGER, started TEXT, last_activity TEXT, status TEXT,
  summary TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS turns (
  id INTEGER PRIMARY KEY AUTOINCREMENT, conversation TEXT, ts TEXT, role TEXT, text TEXT,
  tool_summary TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY, conversation TEXT, server TEXT, tool TEXT, args_hash TEXT,
  args_json TEXT, preview TEXT, created TEXT, expires TEXT, message_id INTEGER, status TEXT
);
CREATE TABLE IF NOT EXISTS schedules (
  name TEXT PRIMARY KEY, cadence TEXT, prompt TEXT, enabled INTEGER, last_run TEXT,
  next_run TEXT, source TEXT
);
CREATE TABLE IF NOT EXISTS budgets (
  day TEXT PRIMARY KEY, tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0,
  tool_calls INTEGER DEFAULT 0, model_calls INTEGER DEFAULT 0, cost REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS proposals (
  id TEXT PRIMARY KEY, conversation TEXT, payload_json TEXT, message_id INTEGER, status TEXT
);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, conversation TEXT, origin TEXT, kind TEXT,
  server TEXT, tool TEXT, target TEXT, outcome TEXT, tokens_in INTEGER, tokens_out INTEGER,
  cost REAL
);
CREATE TABLE IF NOT EXISTS undo (
  token TEXT PRIMARY KEY, action TEXT, memory_id TEXT, previous_id TEXT,
  previous_content TEXT, expires TEXT, used INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS toolsets_active (
  conversation TEXT, name TEXT, loaded_turn INTEGER, last_used_turn INTEGER, reason TEXT,
  PRIMARY KEY (conversation, name)
);
CREATE INDEX IF NOT EXISTS turns_conv ON turns (conversation, id);
CREATE INDEX IF NOT EXISTS audit_ts ON audit (ts);
"""
TEXT_CAP = 32 * 1024  # long pastes (a profile, a log) must survive whole
AUDIT_KINDS = (
    "model_call",
    "tool_call",
    "approval_request",
    "approval_outcome",
    "refusal",
    "schedule_run",
    "command",
    "access_denied",
    "error",
    "memory_save",
    "system",
    "toolset",
)


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}{_secrets.token_hex(5)}"


class State:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        new = not path.exists()
        self.conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        if new:
            os.chmod(path, 0o600)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)

    def x(self, sql: str, args: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, args)

    # ------------------------------------------------------------------- kv

    def kv_get(self, key: str, default: str | None = None) -> str | None:
        row = self.x("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def kv_set(self, key: str, value: str) -> None:
        self.x(
            "INSERT INTO kv VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # -------------------------------------------------------- conversations

    def conversation_current(self, chat_id: int) -> dict[str, Any] | None:
        row = self.x(
            "SELECT * FROM conversations WHERE chat_id = ? AND status = 'active' "
            "ORDER BY started DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
        return dict(row) if row else None

    def conversation_new(self, chat_id: int, ts: str) -> dict[str, Any]:
        cid = new_id("c_")
        self.x(
            "INSERT INTO conversations VALUES (?,?,?,?,?,?)", (cid, chat_id, ts, ts, "active", "")
        )
        return {
            "id": cid,
            "chat_id": chat_id,
            "started": ts,
            "last_activity": ts,
            "status": "active",
            "summary": "",
        }

    def conversation_end(self, cid: str) -> None:
        self.x("UPDATE conversations SET status = 'ended' WHERE id = ?", (cid,))

    def conversation_touch(self, cid: str, ts: str) -> None:
        self.x("UPDATE conversations SET last_activity = ? WHERE id = ?", (ts, cid))

    def conversation_get(self, cid: str) -> dict[str, Any] | None:
        row = self.x("SELECT * FROM conversations WHERE id = ?", (cid,)).fetchone()
        return dict(row) if row else None

    def idle_conversations(self, before: str) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.x(
                "SELECT * FROM conversations WHERE status = 'active' AND last_activity < ?",
                (before,),
            )
        ]

    def set_summary(self, cid: str, summary: str) -> None:
        self.x("UPDATE conversations SET summary = ? WHERE id = ?", (summary[:TEXT_CAP], cid))

    def add_turn(self, cid: str, ts: str, role: str, text: str, tool_summary: str = "") -> None:
        self.x(
            "INSERT INTO turns (conversation, ts, role, text, tool_summary) VALUES (?,?,?,?,?)",
            (cid, ts, role, text[:TEXT_CAP], tool_summary[:1000]),
        )

    def operator_turn(self, cid: str, *, advance: bool = False) -> int:
        """Monotonic operator-turn number of a conversation (old turns get folded away)."""
        key = f"turn_no:{cid}"
        n = int(self.kv_get(key, "0") or 0) + (1 if advance else 0)
        if advance:
            self.kv_set(key, str(n))
        return n

    # undo of remembered facts (feature 009)
    def undo_add(
        self,
        action: str,
        memory_id: str,
        expires: str,
        previous_id: str = "",
        previous_content: str = "",
    ) -> str:
        token = new_id("u")
        self.x(
            "INSERT INTO undo VALUES (?,?,?,?,?,?,0)",
            (token, action, memory_id, previous_id, previous_content, expires),
        )
        return token

    def undo_take(self, token: str, now: str) -> dict[str, Any] | None:
        """The undo entry, once: None if unknown, used or expired."""
        row = self.x("SELECT * FROM undo WHERE token = ?", (token,)).fetchone()
        if row is None or row["used"] or row["expires"] <= now:
            return None
        self.x("UPDATE undo SET used = 1 WHERE token = ?", (token,))
        return dict(row)

    def kv_json(self, key: str, default: dict[str, Any]) -> dict[str, Any]:
        raw = self.kv_get(key)
        try:
            val = json.loads(raw) if raw else None
        except ValueError:
            val = None
        return {**default, **val} if isinstance(val, dict) else dict(default)

    def kv_set_json(self, key: str, value: dict[str, Any]) -> None:
        self.kv_set(key, json.dumps(value))

    def kv_del(self, key: str) -> None:
        self.x("DELETE FROM kv WHERE key = ?", (key,))

    def kv_prefix(self, prefix: str) -> dict[str, str]:
        rows = self.x("SELECT key, value FROM kv WHERE key LIKE ?", (prefix + "%",)).fetchall()
        return {str(r["key"]): str(r["value"]) for r in rows}

    # loaded toolsets per conversation (feature 008)
    def toolsets_active(self, cid: str) -> dict[str, dict[str, Any]]:
        rows = self.x("SELECT * FROM toolsets_active WHERE conversation = ?", (cid,)).fetchall()
        return {str(r["name"]): dict(r) for r in rows}

    def toolset_load(self, cid: str, name: str, turn: int, reason: str) -> None:
        self.x(
            "INSERT INTO toolsets_active VALUES (?,?,?,?,?) ON CONFLICT(conversation, name) "
            "DO UPDATE SET loaded_turn = excluded.loaded_turn, reason = excluded.reason",
            (cid, name, turn, 0, reason[:200]),  # last_used_turn 0: not used yet
        )

    def toolset_used(self, cid: str, name: str, turn: int) -> None:
        self.x(
            "UPDATE toolsets_active SET last_used_turn = ? WHERE conversation = ? AND name = ?",
            (turn, cid, name),
        )

    def toolset_unload(self, cid: str, name: str) -> None:
        self.x("DELETE FROM toolsets_active WHERE conversation = ? AND name = ?", (cid, name))

    def recent_turns(self, cid: str, n: int) -> list[dict[str, Any]]:
        rows = self.x(
            "SELECT * FROM turns WHERE conversation = ? ORDER BY id DESC LIMIT ?", (cid, n)
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def turn_count(self, cid: str) -> int:
        return int(
            self.x("SELECT COUNT(*) FROM turns WHERE conversation = ?", (cid,)).fetchone()[0]
        )

    def drop_old_turns(self, cid: str, keep: int) -> list[dict[str, Any]]:
        rows = [
            dict(r)
            for r in self.x(
                "SELECT * FROM turns WHERE conversation = ? ORDER BY id DESC LIMIT -1 OFFSET ?",
                (cid, keep),
            )
        ]
        if rows:
            self.x(
                f"DELETE FROM turns WHERE id IN ({','.join('?' * len(rows))})",  # noqa: S608
                tuple(r["id"] for r in rows),
            )
        return list(reversed(rows))

    def forget_all(self) -> int:
        n = int(self.x("SELECT COUNT(*) FROM conversations").fetchone()[0])
        self.x("DELETE FROM turns")
        self.x("DELETE FROM conversations")
        self.x("DELETE FROM proposals")
        return n

    # ------------------------------------------------------------ approvals

    def approval_add(self, **row: Any) -> None:
        cols = (
            "id",
            "conversation",
            "server",
            "tool",
            "args_hash",
            "args_json",
            "preview",
            "created",
            "expires",
            "message_id",
            "status",
        )
        self.x(
            f"INSERT INTO approvals ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",  # noqa: S608
            tuple(row.get(c) for c in cols),
        )

    def approval_get(self, aid: str) -> dict[str, Any] | None:
        row = self.x("SELECT * FROM approvals WHERE id = ?", (aid,)).fetchone()
        return dict(row) if row else None

    def approval_set(self, aid: str, **fields: Any) -> None:
        for k, v in fields.items():
            assert k in ("status", "message_id")
            self.x(f"UPDATE approvals SET {k} = ? WHERE id = ?", (v, aid))  # noqa: S608

    def approvals_pending(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.x("SELECT * FROM approvals WHERE status = 'pending'")]

    # ------------------------------------------------------------ schedules

    def schedule_upsert(
        self, name: str, cadence: str, prompt: str, enabled: bool, next_run: str | None, source: str
    ) -> None:
        self.x(
            "INSERT INTO schedules (name, cadence, prompt, enabled, next_run, source) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET cadence = excluded.cadence,"
            " prompt = excluded.prompt, source = excluded.source",
            (name, cadence, prompt, int(enabled), next_run, source),
        )

    def schedules(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.x("SELECT * FROM schedules ORDER BY name")]

    def schedule_set(self, name: str, **fields: Any) -> None:
        for k, v in fields.items():
            assert k in ("enabled", "last_run", "next_run")
            self.x(f"UPDATE schedules SET {k} = ? WHERE name = ?", (v, name))  # noqa: S608

    def schedule_remove(self, name: str) -> bool:
        return self.x("DELETE FROM schedules WHERE name = ?", (name,)).rowcount > 0

    # -------------------------------------------------------------- budgets

    def budget_get(self, day: str) -> dict[str, Any]:
        row = self.x("SELECT * FROM budgets WHERE day = ?", (day,)).fetchone()
        return (
            dict(row)
            if row
            else {
                "day": day,
                "tokens_in": 0,
                "tokens_out": 0,
                "tool_calls": 0,
                "model_calls": 0,
                "cost": 0.0,
            }
        )

    def budget_add(self, day: str, **inc: float) -> None:
        self.x("INSERT OR IGNORE INTO budgets (day) VALUES (?)", (day,))
        for k, v in inc.items():
            assert k in ("tokens_in", "tokens_out", "tool_calls", "model_calls", "cost")
            self.x(f"UPDATE budgets SET {k} = {k} + ? WHERE day = ?", (v, day))  # noqa: S608

    # ------------------------------------------------------------ proposals

    def proposal_add(self, pid: str, cid: str, payload: dict[str, Any]) -> None:
        self.x(
            "INSERT INTO proposals VALUES (?,?,?,?,?)",
            (pid, cid, json.dumps(payload), None, "pending"),
        )

    def proposal_get(self, pid: str) -> dict[str, Any] | None:
        row = self.x("SELECT * FROM proposals WHERE id = ?", (pid,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["payload"] = json.loads(d.pop("payload_json"))
        return d

    def proposals_pending(self, cid: str) -> list[dict[str, Any]]:
        rows = self.x(
            "SELECT id FROM proposals WHERE conversation = ? AND status = 'pending'", (cid,)
        ).fetchall()
        return [p for p in (self.proposal_get(str(r["id"])) for r in rows) if p]

    def proposal_set(self, pid: str, status: str) -> None:
        self.x("UPDATE proposals SET status = ? WHERE id = ?", (status, pid))

    # ---------------------------------------------------------------- audit

    def audit(
        self,
        kind: str,
        *,
        conversation: str | None = None,
        origin: str = "chat",
        server: str | None = None,
        tool: str | None = None,
        target: str | None = None,
        outcome: str = "ok",
        tokens_in: int = 0,
        tokens_out: int = 0,
        cost: float = 0.0,
    ) -> None:
        assert kind in AUDIT_KINDS, kind
        self.x(
            "INSERT INTO audit (ts, conversation, origin, kind, server, tool, target, outcome,"
            " tokens_in, tokens_out, cost) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                now_iso(),
                conversation,
                origin,
                kind,
                server,
                tool,
                (target or "")[:200] or None,
                outcome[:200],
                tokens_in,
                tokens_out,
                cost,
            ),
        )

    def audit_since(self, since: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.x("SELECT * FROM audit WHERE ts >= ? ORDER BY id", (since,))]
