"""Read-only access to the migration PostgreSQL cluster (research M1).

Analysis connects as the OS user running the server (``vpsmcp``) over the local socket
with peer auth, i.e. as the database role ``vpsmcp`` that only holds pg_read_all_data /
pg_read_all_stats. Every session is additionally forced read-only via PGOPTIONS.
"""

from __future__ import annotations

import json
from typing import Any

from vps_mcp.errors import ErrorCategory, VpsMcpError, invalid
from vps_mcp.migration import catalog
from vps_mcp.migration.config import COPY_PREFIX, DB_NAME_RE, MigrationSettings
from vps_mcp.remote.executor import RemoteExecutor

COPY_NAME_RE_PATTERN = r"^vmcp_[a-z0-9_]{1,30}_\d{12}_[0-9a-f]{4}$"


def not_permitted_db(name: str) -> VpsMcpError:
    # Identical wording whether or not the database exists (FR-001).
    return VpsMcpError(
        ErrorCategory.NOT_PERMITTED, f"database '{name}' is unknown or not permitted"
    )


class MigrationDb:
    def __init__(self, executor: RemoteExecutor, settings: MigrationSettings) -> None:
        self.ex = executor
        self.settings = settings

    def psql_argv(self, db: str, variables: dict[str, str] | None = None) -> list[str]:
        argv = [
            "psql",
            "-X",
            "-p",
            str(self.settings.port),
            "-d",
            db,
            "-v",
            "ON_ERROR_STOP=1",
            "-At",
        ]
        for k, v in (variables or {}).items():
            if not catalog.IDENT_RE.fullmatch(k) or "\n" in v or "\x00" in v:
                raise invalid(f"bad query variable {k!r}")
            argv += ["-v", f"{k}={v}"]
        return argv + ["-f", "-"]

    def pgoptions(self, timeout_s: float | None = None) -> str:
        ms = int((timeout_s or self.settings.analysis_timeout_s) * 1000)
        return f"-c default_transaction_read_only=on -c statement_timeout={ms}"

    async def query(
        self,
        db: str,
        name: str,
        variables: dict[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> Any:
        """Run catalogue query ``name`` read-only against ``db`` and return parsed JSON."""
        if not DB_NAME_RE.fullmatch(db) and not db.startswith(COPY_PREFIX):
            raise invalid(f"database: {db!r} is not a valid database name")
        sql = catalog.CATALOG[name]
        t = float(timeout_s or self.settings.analysis_timeout_s)
        r = await self.ex.run(
            self.psql_argv(db, variables),
            timeout=t + 5,
            stdin=sql,
            env={"PGOPTIONS": self.pgoptions(t)},
        )
        if not r.ok:
            detail = (r.stderr or r.stdout).strip().splitlines()[-1:] or ["psql failed"]
            raise VpsMcpError(ErrorCategory.FAILED, f"query '{name}' on '{db}' failed: {detail[0]}")
        text = r.stdout.strip()
        try:
            return json.loads(text) if text else None
        except json.JSONDecodeError:
            raise VpsMcpError(
                ErrorCategory.FAILED, f"query '{name}' returned unparseable output"
            ) from None

    async def db_info(self, db: str) -> dict[str, Any]:
        info = await self.query("postgres", "db_info", {"db": db})
        return dict(info or {})

    async def write_counters(self) -> dict[str, dict[str, int]]:
        data = await self.query("postgres", "write_counters")
        return {
            k: {"writes": int(v["writes"]), "sessions": int(v["sessions"])}
            for k, v in (data or {}).items()
        }

    async def free_and_total(self) -> tuple[int, int]:
        r = await self.ex.run(
            ["df", "-B1", "--output=size,avail", self.settings.data_dir],
            timeout=float(self.settings.analysis_timeout_s),
        )
        lines = [ln.split() for ln in r.stdout.splitlines()[1:] if ln.strip()]
        if not r.ok or not lines or len(lines[0]) < 2:
            raise VpsMcpError(ErrorCategory.FAILED, "could not read free disk space")
        return int(lines[0][1]), int(lines[0][0])

    def min_free_bytes(self, total: int) -> int:
        s = self.settings
        return int(max(s.min_free_gb * 1024**3, total * s.min_free_pct / 100))
