"""A simulated PostgreSQL cluster (plus rehearsal runner) for the migration tests.

Attach with ``vps.pg = FakePg(vps)``: FakeVps then routes psql/createdb/dropdb/systemd-run/
sha256sum/ls/tail/grep through here. Catalogue queries are recognised by identity
(vps_mcp.migration.catalog.name_of) and answered from the in-memory model.
"""

from __future__ import annotations

import copy as copymod
import fnmatch
import hashlib
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from vps_mcp.migration import catalog
from vps_mcp.remote.executor import CommandResult


def ok(out: str = "", err: str = "") -> CommandResult:
    return CommandResult(0, out, err)


def fail(err: str, code: int = 1) -> CommandResult:
    return CommandResult(code, "", err)


@dataclass
class Table:
    columns: dict[str, dict[str, Any]]
    rows: int
    est: int | None = None
    indexes: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)

    def estimate(self) -> int | None:
        return self.rows if self.est is None else (None if self.est < 0 else self.est)


@dataclass
class Db:
    tables: dict[str, Table] = field(default_factory=dict)
    modules: dict[str, dict[str, Any]] | None = None
    views: dict[str, dict[str, Any]] | None = None
    owner: str = "odoo"
    comment: str | None = None
    sessions: int = 0
    writes: int = 0
    size: int = 8 * 1024**3


def col(t: str, nullable: bool = True, default: str | None = None) -> dict[str, Any]:
    return {"type": t, "nullable": nullable, "default": default}


def odoo_db(major: str = "17", rows: int = 1000) -> Db:
    return Db(
        tables={
            "public.ir_module_module": Table(
                {
                    "id": col("integer", False),
                    "name": col("character varying"),
                    "state": col("character varying"),
                    "latest_version": col("character varying"),
                },
                4,
            ),
            "public.ir_ui_view": Table({"id": col("integer", False), "active": col("boolean")}, 3),
            "public.res_partner": Table(
                {
                    "id": col("integer", False),
                    "name": col("character varying(64)", False),
                    "vat": col("character varying(32)"),
                    "credit": col("numeric(12,2)"),
                },
                rows,
                indexes=["res_partner_pkey", "res_partner_name_idx"],
                constraints=["res_partner_pkey:p", "res_partner_company_fk:f"],
            ),
            "public.account_move": Table(
                {"id": col("integer", False), "name": col("text")}, rows * 5
            ),
        },
        modules={
            "base": {"state": "installed", "version": f"{major}.0.1.3"},
            "sale": {"state": "installed", "version": f"{major}.0.1.2"},
            "old_mod": {"state": "uninstalled", "version": None},
        },
        views={
            "base.v1": {"active": True, "inherit": None, "module": "base"},
            "sale.v2": {"active": True, "inherit": "base.v1", "module": "sale"},
        },
    )


class FakePg:
    def __init__(self, vps: Any, port: int = 5435) -> None:
        self.vps = vps
        self.port = port
        self.dbs: dict[str, Db] = {"postgres": Db(tables={}, owner="postgres", size=8_000_000)}
        self.mutations: list[tuple[str, str]] = []
        self.units: dict[str, dict[str, Any]] = {}
        self.dirs: dict[str, list[str]] = {}
        self.disk_total = 200 * 1024**3
        self.disk_free = 100 * 1024**3
        # step effects: file path -> callable(db: Db, pg) -> exit code
        self.sql_effects: dict[str, Callable[[Db, FakePg], int]] = {}
        self.script_effects: dict[str, Callable[[Db, FakePg], int]] = {}
        self.odoo_effect: Callable[[Db, FakePg, str], int] | None = None
        self.run_runner_synchronously = True
        self.queries: list[tuple[str, str]] = []
        self.check_effects: dict[
            str, Callable[[list[str], dict[str, str], FakePg], tuple[int, str]]
        ] = {}
        self.check_calls: list[dict[str, Any]] = []
        # declared jobs (003)
        self.file_meta: dict[str, tuple[str, str, int]] = {}  # path -> (owner, mode, mtime)
        self.processes: dict[str, list[int]] = {}  # script path -> pids started by hand
        self.job_units: dict[str, dict[str, Any]] = {}
        self.job_effect: Callable[[dict[str, Any], FakePg], int | None] | None = None
        self.job_autorun = True

    # ------------------------------------------------------------------ helpers

    def mutate(self, db: str, what: str) -> None:
        self.mutations.append((db, what))
        if db in self.dbs:
            self.dbs[db].writes += 1

    def pg_mutations(self, *dbs: str) -> list[tuple[str, str]]:
        return [m for m in self.mutations if not dbs or m[0] in dbs]

    @staticmethod
    def _vars(argv: list[str]) -> dict[str, str]:
        out = {}
        for i, a in enumerate(argv):
            if a == "-v" and i + 1 < len(argv) and "=" in argv[i + 1]:
                k, _, v = argv[i + 1].partition("=")
                out[k] = v
        return out

    @staticmethod
    def _opt(argv: list[str], flag: str) -> str | None:
        return argv[argv.index(flag) + 1] if flag in argv else None

    # ----------------------------------------------------------------- catalogue

    def answer(self, name: str, db: Db, dbname: str, v: dict[str, str]) -> Any:
        if name == "db_sizes":
            return {k: d.size for k, d in self.dbs.items()}
        if name == "overview":
            largest = sorted(db.tables.items(), key=lambda kv: (-kv[1].rows, kv[0]))[:20]
            return {
                "size_bytes": db.size,
                "tables": len(db.tables),
                "largest": [
                    {"table": k, "est_rows": t.estimate(), "bytes": t.rows * 100}
                    for k, t in largest
                ],
                "has_odoo": db.modules is not None,
            }
        if name == "tables":
            return {
                k: {
                    "est_rows": t.estimate(),
                    "total_bytes": t.rows * 100,
                    "columns": t.columns,
                    "indexes": sorted(t.indexes),
                    "constraints": sorted(t.constraints),
                }
                for k, t in db.tables.items()
            }
        if name == "table":
            t = db.tables.get(f"{v['sch']}.{v['tbl']}")
            if t is None:
                return None
            return {
                "table": f"{v['sch']}.{v['tbl']}",
                "est_rows": t.estimate(),
                "bytes": t.rows * 100,
                "columns": [{"name": c, **d} for c, d in t.columns.items()],
                "indexes": t.indexes,
                "constraints": t.constraints,
            }
        if name == "modules":
            return db.modules
        if name == "views":
            return db.views
        if name == "view_stats":
            views, mods = db.views or {}, db.modules or {}
            inactive = sum(1 for x in views.values() if not x["active"])
            orphaned = sum(
                1
                for x in views.values()
                if x["active"]
                and x["inherit"]
                and (
                    mods.get(x["module"], {}).get("state") != "installed"
                    or mods.get(views.get(x["inherit"], {}).get("module"), {}).get("state")
                    != "installed"
                )
            )
            return {"inactive": inactive, "orphaned": orphaned}
        if name == "exact_counts":
            return {t: db.tables[t].rows for t in json.loads(v["tables"]) if t in db.tables}
        if name == "write_counters":
            return {k: {"writes": d.writes, "sessions": d.sessions} for k, d in self.dbs.items()}
        if name == "db_info":
            d = self.dbs.get(v["db"])
            if d is None:
                return {
                    "exists": False,
                    "owner": None,
                    "comment": None,
                    "size_bytes": None,
                    "sessions": 0,
                }
            return {
                "exists": True,
                "owner": d.owner,
                "comment": d.comment,
                "size_bytes": d.size,
                "sessions": d.sessions,
            }
        if name == "stale_modules":
            return sorted(
                k
                for k, m in (db.modules or {}).items()
                if m["state"] in ("installed", "to upgrade")
                and (m.get("version") or "").split(".")[0] == v["major"]
            )
        raise AssertionError(name)

    # ------------------------------------------------------------------ commands

    def _h_psql(
        self, argv: list[str], stdin: str | None, env: dict[str, str] | None, user: str | None
    ) -> CommandResult:
        args = argv[1:]
        dbname = self._opt(args, "-d") or "postgres"
        db = self.dbs.get(dbname)
        if db is None:
            return fail(f'psql: error: FATAL:  database "{dbname}" does not exist', 2)
        f = self._opt(args, "-f")
        if f == "-":
            name = catalog.name_of(stdin or "")
            if name is None:
                return fail("ERROR: unknown query (not in catalogue)")
            readonly = "default_transaction_read_only=on" in (env or {}).get("PGOPTIONS", "")
            if not readonly:
                return fail("test fake: catalogue queries must run read-only")
            self.queries.append((name, dbname))
            return ok(json.dumps(self.answer(name, db, dbname, self._vars(args))) + "\n")
        if f:
            effect = self.sql_effects.get(f)
            self.mutate(dbname, f"sql:{f}")
            code = effect(db, self) if effect else 0
            return CommandResult(code, "", "" if code == 0 else "ERROR: step failed")
        return fail("test fake: unsupported psql call")

    def _h_dropdb(
        self, argv: list[str], stdin: str | None, env: Any, user: str | None
    ) -> CommandResult:
        if user != "postgres":
            return fail("dropdb must run as postgres")
        name = argv[-1]
        if name in self.dbs:
            del self.dbs[name]
            self.mutations.append((name, "dropped"))
        return ok()

    def _h_sha256sum(
        self, argv: list[str], stdin: str | None, env: Any, user: str | None
    ) -> CommandResult:
        out = []
        for path in argv[argv.index("--") + 1 :]:
            if path in self.vps.files:
                out.append(f"{hashlib.sha256(self.vps.files[path].encode()).hexdigest()}  {path}")
        return CommandResult(0 if out else 1, "\n".join(out) + "\n", "")

    def _h_ls(
        self, argv: list[str], stdin: str | None, env: Any, user: str | None
    ) -> CommandResult:
        path = argv[-1]
        if path not in self.dirs:
            return fail(f"ls: cannot access '{path}': Permission denied", 2)
        return ok("\n".join(self.dirs[path]) + "\n")

    def _h_tail(
        self, argv: list[str], stdin: str | None, env: Any, user: str | None
    ) -> CommandResult:
        if "-c" in argv:
            start = int(argv[argv.index("-c") + 1].lstrip("+"))
            path = argv[-1]
            if path not in self.vps.files:
                return fail(f"tail: cannot open '{path}'")
            return ok(self.vps.files[path].encode()[start - 1 :].decode())
        n = int(argv[argv.index("-n") + 1])
        if argv[-1] not in self.vps.files:
            return fail(f"tail: cannot open '{argv[-1]}'")
        text = self.vps.files.get(argv[-1], "")
        return ok("\n".join(text.splitlines()[-n:]) + "\n")

    def _h_grep(
        self, argv: list[str], stdin: str | None, env: Any, user: str | None
    ) -> CommandResult:
        i = argv.index("--")
        pattern, files = argv[i + 1], argv[i + 2 :]
        rx = re.compile(pattern)
        lines = [
            ln for f in files for ln in self.vps.files.get(f, "").splitlines() if rx.search(ln)
        ]
        return CommandResult(0 if lines else 1, "\n".join(lines) + ("\n" if lines else ""), "")

    def _h_df(
        self, argv: list[str], stdin: str | None, env: Any, user: str | None
    ) -> CommandResult | None:
        if "--output=size,avail" in argv:
            return ok(f"1B-blocks Avail\n{self.disk_total} {self.disk_free}\n")
        return None  # fall back to FakeVps

    def _h_systemctl(
        self, argv: list[str], stdin: str | None, env: Any, user: str | None
    ) -> CommandResult | None:
        verb = argv[1]
        if verb == "list-units":
            pats = [a for a in argv[2:] if not a.startswith("-")]
            active = [u for u, st in self.units.items() if st["active"]]
            active += [u for u, st in self.job_units.items() if st["active"]]
            if pats:
                active = [u for u in active if any(fnmatch.fnmatchcase(u, p) for p in pats)]
            return ok("".join(f"{u}.service loaded active running\n" for u in active))
        if argv[-1] in self.job_units:
            ju = self.job_units[argv[-1]]
            if verb == "show":
                return ok(
                    f"ActiveState={'active' if ju['active'] else 'inactive'}\n"
                    f"SubState={'running' if ju['active'] else 'dead'}\n"
                    f"Result={ju.get('result') or 'success'}\n"
                    "ExecMainStartTimestamp=Wed 2026-11-11 10:00:00 UTC\n"
                    "ExecMainExitTimestamp=\n"
                )
            if verb == "stop":
                ju["active"] = False
                ju["result"] = "signal"
                self.processes.pop(ju["script"], None)
                return ok()
            if verb == "reset-failed":
                ju["reset"] = True
                return ok()
        if verb == "show" and "ActiveState" in argv:
            u = self.units.get(argv[-1])
            return ok(("active" if u and u["active"] else "inactive") + "\n")
        if verb == "stop" and argv[-1] in self.units:
            u = self.units[argv[-1]]
            u["active"] = False
            self.vps.files[u["events"]] = (
                self.vps.files.get(u["events"], "") + f"{int(time.time())} RUN cancelled\n"
            )
            return ok()
        if verb == "stop" and argv[-1].startswith("vps-mcp-expire-"):
            return ok()
        return None

    def _h_systemd_run(
        self, argv: list[str], stdin: str | None, env: Any, user: str | None
    ) -> CommandResult:
        if "--unit" not in argv:
            return fail("unit required")
        unit = argv[argv.index("--unit") + 1]
        if unit.startswith("vps-mcp-job-"):
            return self._start_job(unit, argv)
        if unit.startswith("vps-mcp-expire-"):
            self.units[unit] = {"active": False, "timer": argv}
            return ok()
        script = argv[-1]
        rdir = script.rsplit("/", 1)[0]
        self.units[unit] = {"active": True, "events": f"{rdir}/events.log", "dir": rdir}
        if self.run_runner_synchronously:
            self.simulate(unit)
        return ok(f"Running as unit: {unit}.service\n")

    def _start_job(self, unit: str, argv: list[str]) -> CommandResult:
        env = dict(
            a.removeprefix("--setenv=").split("=", 1) for a in argv if a.startswith("--setenv=")
        )
        uid = next((a.split("=", 1)[1] for a in argv if a.startswith("--uid=")), None)
        script, exit_file = argv[-2], argv[-1]
        run = {
            "unit": unit,
            "argv": argv,
            "env": env,
            "uid": uid,
            "script": script,
            "exit_file": exit_file,
            "active": True,
            "result": None,
        }
        self.job_units[unit] = run
        if self.job_autorun:
            self.finish_job(unit)
        return ok(f"Running as unit: {unit}.service\n")

    def finish_job(self, unit: str, code: int | None = None, result: str | None = None) -> None:
        run = self.job_units[unit]
        if code is None and self.job_effect is not None:
            code = self.job_effect(run, self)
        if result == "timeout":
            run.update(active=False, result="timeout")
            return
        code = 0 if code is None else code
        self.vps.files[run["exit_file"]] = f"{code}\n"
        run.update(active=False, result="success" if code == 0 else "exit-code")

    def _h_stat(
        self, argv: list[str], stdin: str | None, env: Any, user: str | None
    ) -> CommandResult:
        fmt = argv[argv.index("-c") + 1]
        paths = argv[argv.index("--") + 1 :]
        out = []
        for path in paths:
            if fmt == "%s":
                if path not in self.vps.files:
                    return fail(f"stat: cannot statx '{path}': No such file or directory")
                out.append(str(len(self.vps.files[path].encode())))
            else:
                meta = self.file_meta.get(path)
                if meta is None:
                    continue
                owner, mode, mtime = meta
                out.append(f"{owner} {mode} {mtime} {path}")
        code = 0 if len(out) == len(paths) else 1
        return CommandResult(code, "\n".join(out) + ("\n" if out else ""), "")

    def _h_pgrep(
        self, argv: list[str], stdin: str | None, env: Any, user: str | None
    ) -> CommandResult:
        script = argv[-1]
        pids = list(self.processes.get(script, []))
        pids += [4242 for ju in self.job_units.values() if ju["active"] and ju["script"] == script]
        return CommandResult(0 if pids else 1, "".join(f"{p}\n" for p in pids), "")

    def run_abs(
        self, argv: list[str], stdin: str | None, env: dict[str, str] | None, user: str | None
    ) -> CommandResult:
        """Absolute-path programs: declared check interpreters."""
        self.check_calls.append({"argv": argv, "env": env, "user": user})
        script = argv[1] if len(argv) > 1 else argv[0]
        eff = self.check_effects.get(script)
        if eff is None:
            return fail(f"{argv[0]}: not found", 127)
        code, out = eff(argv, env or {}, self)
        return CommandResult(code, out, "")

    # ---------------------------------------------------------------- runner sim

    def simulate(self, unit: str) -> None:
        """Emulate run.sh: copy, steps with effects, foreign-write detection, events."""
        u = self.units[unit]
        rdir = u["dir"]
        run = self.vps.files[f"{rdir}/run.sh"]
        ev_path = u["events"]
        events: list[str] = []

        def ev(msg: str) -> None:
            events.append(f"{int(time.time())} {msg}")

        copy = re.search(r"^COPY='?([^'\n]+)'?$", run, re.M)[1]  # type: ignore[index]
        marker = re.search(r"-v marker='?([^' ]+)'?", run)[1]  # type: ignore[index]
        m = re.search(r"-T '?([a-z0-9_]+)'?", run) or re.search(
            r"pg_dump -p \d+ -Fc '?([a-z0-9_]+)", run
        )
        source = m[1]  # type: ignore[index]
        watch = re.findall(r"'([a-z0-9_]+)'", re.search(r"IN \(([^)]*)\)", run)[1])  # type: ignore[index]
        ev("RUN started")
        if self.disk_free < int(re.search(r"^MIN_FREE=(\d+)", run, re.M)[1]):  # type: ignore[index]
            ev("RUN aborted_disk")
            self._finish(u, ev_path, events)
            return
        ev("COPY started")
        if source not in self.dbs:
            ev("COPY failed 1")
            ev("RUN copy_failed")
            self._finish(u, ev_path, events)
            return
        new = copymod.deepcopy(self.dbs[source])
        new.comment, new.sessions, new.writes = marker, 0, 0
        self.dbs[copy] = new
        self.mutations.append((copy, "created"))
        ev("COPY ok")
        steps = re.findall(r"^run_step (\d+) ('[^']*'|\S*) ('[^']*'|\S*) (\d+)", run, re.M)
        final = "succeeded"
        for n, file, sha, _tmo in steps:
            file, sha = file.strip("'"), sha.strip("'")
            if file:
                cur = hashlib.sha256(self.vps.files.get(file, "").encode()).hexdigest()
                if cur != sha:
                    ev(f"STEP {n} failed 1 fingerprint_changed")
                    final = "failed"
                    break
            before = {d: self.dbs[d].writes for d in watch if d in self.dbs}
            ev(f"STEP {n} running")
            step_sh = self.vps.files[f"{rdir}/step-{n}.sh"]
            code = self._run_step(step_sh, copy)
            self.vps.files[f"{rdir}/step-{n}.log"] = f"step {n} output\nexit {code}\n"
            st = "ok" if code == 0 else ("timeout" if code == 124 else "failed")
            ev(f"STEP {n} {st} {code}")
            foreign = False
            for d in watch:
                if d in self.dbs and self.dbs[d].writes > before.get(d, 0):
                    attributed = self.dbs[d].sessions == 0
                    ev(
                        f"FOREIGN {n} {d} {self.dbs[d].writes - before.get(d, 0)} "
                        + ("attributed" if attributed else "unattributed")
                    )
                    foreign = foreign or attributed
            if foreign:
                final = "foreign_write"
                break
            if st != "ok":
                final = "timed_out" if st == "timeout" else "failed"
                break
        ev(f"RUN {final}")
        self._finish(u, ev_path, events)

    def _finish(self, u: dict[str, Any], ev_path: str, events: list[str]) -> None:
        self.vps.files[ev_path] = "\n".join(events) + "\n"
        u["active"] = False

    def _run_step(self, step_sh: str, copy: str) -> int:
        db = self.dbs[copy]
        m = re.search(r"psql -X -p \d+ -d \S+ -v ON_ERROR_STOP=1 -f '?([^'\n]+)'?", step_sh)
        if m:
            self.mutate(copy, f"sql:{m[1]}")
            eff = self.sql_effects.get(m[1])
            return eff(db, self) if eff else 0
        m = re.search(r"bash '?(/[^'\n]+)'?\s*$", step_sh, re.M)
        if m and "odoo-bin" not in step_sh:
            self.mutate(copy, f"script:{m[1]}")
            eff = self.script_effects.get(m[1])
            return eff(db, self) if eff else 0
        if "odoo-bin" in step_sh:
            self.mutate(copy, "odoo")
            kind = "stale" if "for round in" in step_sh else "all"
            return self.odoo_effect(db, self, kind) if self.odoo_effect else 0
        return 0
