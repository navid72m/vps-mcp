"""Rebuildable SQLite index over the memory files (research A4, data-model §2).

The files are the source of truth. ``sync()`` re-indexes only files whose (mtime, size)
changed and drops entries whose file vanished, so hand edits show up on the next search.
"""

from __future__ import annotations

import contextlib
import logging
import re
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from agent_memory import frontmatter
from agent_memory.files import Files, Malformed
from agent_memory.model import MemoryRecord

log = logging.getLogger(__name__)
SCHEMA_VERSION = "1"
COLUMNS = (
    "id",
    "path",
    "mtime_ns",
    "size",
    "type",
    "scope",
    "subject",
    "title",
    "content",
    "confidence",
    "importance",
    "status",
    "pinned",
    "live_state",
    "state",
    "source_kind",
    "source_ref",
    "source_client",
    "created",
    "updated",
    "last_confirmed",
    "confirmations",
    "expires",
    "fresh_days",
    "needs_verification",
    "supersedes",
    "superseded_by",
    "generated_key",
    "tags",
    "conflicts_with",
    "related",
    "alternatives",
)
_DDL = f"""
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS mem ({", ".join(c + (" TEXT PRIMARY KEY" if c == "id" else "") for c in COLUMNS)});
CREATE INDEX IF NOT EXISTS mem_scope_subject ON mem (scope, subject, status);
CREATE INDEX IF NOT EXISTS mem_path ON mem (path);
CREATE INDEX IF NOT EXISTS mem_generated ON mem (generated_key);
"""
_FTS = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS mem_fts USING fts5(id UNINDEXED, subject, title, "
    "content, tags, alternatives, tokenize='unicode61 remove_diacritics 2')"
)
_TOKEN = re.compile(r"[A-Za-z0-9_]+")


class Index:
    def __init__(self, files: Files) -> None:
        self.files = files
        self.path = files.root / "index.sqlite"
        self.malformed: list[Malformed] = []
        self.fts = True
        self.conn = self._open()

    # ---------------------------------------------------------------- schema

    def _open(self) -> sqlite3.Connection:
        try:
            conn = self._connect()
            ver = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if ver is None or ver[0] != SCHEMA_VERSION:
                raise sqlite3.DatabaseError("schema version mismatch")
            conn.execute("SELECT COUNT(*) FROM mem").fetchone()
            return conn
        except sqlite3.DatabaseError as exc:
            log.info("rebuilding memory index (%s)", exc)
            with contextlib.suppress(OSError):
                self.path.unlink()
            for suffix in ("-wal", "-shm"):
                with contextlib.suppress(OSError):
                    Path(str(self.path) + suffix).unlink()
            conn = self._connect()
            conn.execute(
                "INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)", (SCHEMA_VERSION,)
            )
            return conn

    def _connect(self) -> sqlite3.Connection:
        self.files.ensure()
        new = not self.path.exists()
        conn = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False)
        if new:
            self.path.chmod(0o600)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(_DDL)
        try:
            conn.execute(_FTS)
        except sqlite3.OperationalError:
            self.fts = False
            log.warning("SQLite has no FTS5; memory search falls back to a LIKE scan")
        return conn

    def rebuild(self) -> None:
        self.conn.execute("DELETE FROM mem")
        if self.fts:
            self.conn.execute("DELETE FROM mem_fts")
        self.sync()

    # ------------------------------------------------------------------ sync

    def sync(self) -> int:
        known = {
            r["path"]: (r["mtime_ns"], r["size"])
            for r in self.conn.execute("SELECT path, mtime_ns, size FROM mem")
        }
        seen: set[str] = set()
        changed = 0
        self.malformed = []
        for rel, path, st in self.files.iter_files():
            seen.add(rel)
            if known.get(rel) == (st.st_mtime_ns, st.st_size):
                continue
            try:
                rec = MemoryRecord.from_text(rel, self.files.read(path), st.st_mtime)
            except (OSError, UnicodeDecodeError, frontmatter.FrontMatterError) as exc:
                self.malformed.append(Malformed(rel, str(exc)[:200]))
                self._delete_path(rel)
                continue
            except Exception as exc:  # noqa: BLE001 - bad field values in hand-made files
                self.malformed.append(Malformed(rel, getattr(exc, "message", str(exc))[:200]))
                self._delete_path(rel)
                continue
            self.upsert(rec, st.st_mtime_ns, st.st_size)
            changed += 1
        for rel in set(known) - seen:
            self._delete_path(rel)
            changed += 1
        return changed

    def upsert(self, rec: MemoryRecord, mtime_ns: int, size: int) -> None:
        row = self._row(rec, mtime_ns, size)
        self.conn.execute("BEGIN")
        try:
            old = self.conn.execute(
                "SELECT id FROM mem WHERE path = ? OR id = ?", (rec.path, rec.id)
            ).fetchall()
            for o in old:
                self._delete_id(o["id"])
            marks = ",".join("?" * len(COLUMNS))
            self.conn.execute(
                f"INSERT INTO mem ({','.join(COLUMNS)}) VALUES ({marks})", [row[c] for c in COLUMNS]
            )
            if self.fts:
                self.conn.execute(
                    "INSERT INTO mem_fts (id, subject, title, content, tags, alternatives) "
                    "VALUES (?,?,?,?,?,?)",
                    (
                        rec.id,
                        rec.subject.replace(".", " ") + " " + rec.subject,
                        rec.title,
                        rec.content,
                        " ".join(rec.tags),
                        " ".join(rec.alternatives),
                    ),
                )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def _delete_id(self, mem_id: str) -> None:
        self.conn.execute("DELETE FROM mem WHERE id = ?", (mem_id,))
        if self.fts:
            self.conn.execute("DELETE FROM mem_fts WHERE id = ?", (mem_id,))

    def _delete_path(self, rel: str) -> None:
        for r in self.conn.execute("SELECT id FROM mem WHERE path = ?", (rel,)).fetchall():
            self._delete_id(r["id"])

    @staticmethod
    def _row(rec: MemoryRecord, mtime_ns: int, size: int) -> dict[str, Any]:
        d: dict[str, Any] = {c: getattr(rec, c, None) for c in COLUMNS}
        d.update(mtime_ns=mtime_ns, size=size)
        for k in ("tags", "conflicts_with", "related", "alternatives"):
            d[k] = "\x1f".join(getattr(rec, k))
        for k in ("pinned", "live_state", "needs_verification"):
            d[k] = int(bool(getattr(rec, k)))
        return d

    # --------------------------------------------------------------- queries

    def all_rows(self, where: str = "1=1", params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(f"SELECT * FROM mem WHERE {where}", tuple(params)))

    def by_id(self, mem_id: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self.conn.execute(
            "SELECT * FROM mem WHERE id = ?", (mem_id,)
        ).fetchone()
        return row

    def by_subject(self, scope: str, subject: str) -> list[sqlite3.Row]:
        return self.all_rows("scope = ? AND subject = ?", (scope, subject))

    def by_generated_key(self, key: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self.conn.execute(
            "SELECT * FROM mem WHERE generated_key = ? ORDER BY updated DESC", (key,)
        ).fetchone()
        return row

    def keyword(self, query: str, scopes: list[str]) -> dict[str, float]:
        """Return {id: relevance > 0} for memories in ``scopes`` that match the query."""
        from agent_memory.similarity import STOPWORDS, stem

        tokens = list(
            dict.fromkeys(
                stem(t.lower())
                for t in _TOKEN.findall(query)
                if len(t) > 2 and t.lower() not in STOPWORDS
            )
        )[:24]
        if not tokens:
            return {}
        marks = ",".join("?" * len(scopes))
        if self.fts:
            match = " OR ".join(f'"{t}"*' for t in tokens)
            rows = self.conn.execute(
                f"SELECT f.id AS id, bm25(mem_fts, 0, 4.0, 3.0, 1.0, 2.0, 1.0) AS rank "
                f"FROM mem_fts f JOIN mem m ON m.id = f.id "
                f"WHERE mem_fts MATCH ? AND m.scope IN ({marks})",
                (match, *scopes),
            ).fetchall()
            return {r["id"]: -float(r["rank"]) for r in rows if -float(r["rank"]) > 0}
        out: dict[str, float] = {}
        for r in self.all_rows(f"scope IN ({marks})", scopes):
            text = f"{r['subject']} {r['title']} {r['content']} {r['tags']}".lower()
            hits = sum(text.count(t) for t in tokens)
            if hits:
                out[r["id"]] = float(hits)
        return out


def row_to_record(row: sqlite3.Row) -> MemoryRecord:
    d = dict(row)
    for k in ("tags", "conflicts_with", "related", "alternatives"):
        d[k] = [x for x in (d.get(k) or "").split("\x1f") if x]
    for k in ("pinned", "live_state", "needs_verification"):
        d[k] = bool(d.get(k))
    d.pop("mtime_ns", None)
    d.pop("size", None)
    return MemoryRecord(**d)
