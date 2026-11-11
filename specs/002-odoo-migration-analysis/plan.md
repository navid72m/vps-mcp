# Implementation Plan: Odoo Migration Analysis & Rehearsal

**Branch**: `002-odoo-migration-analysis` | **Date**: 2026-11-11 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/002-odoo-migration-analysis/spec.md`

## Summary

Add 13 migration tools to the existing `vps` MCP server. They analyse Odoo/PostgreSQL databases, compare them with deterministic risk classification, and rehearse the operator's declared upgrade pipeline on disposable copies.

- **Analysis**: a dedicated **read-only database role** (`vpsmcp`, peer authentication, `pg_read_all_data`) runs a **fixed catalogue of JSON-returning queries**.
- **Comparison**: pure-Python diffs over locally stored **snapshots**, ending in a verdict.
- **Rehearsals** follow three rules:
  - **Disposable copies** made with `createdb -T` or `pg_dump|pg_restore`, marked with an HMAC comment.
  - **One detached runner per rehearsal**: a generated, fingerprinted bash script started with `systemd-run`, so it survives the MCP client closing.
  - **Steps bound to the copy**: SQL files and Odoo upgrades are confined to it; operator scripts are watched by a **write detector** on every other allowlisted database.
- **Disk**: guarded before copying and between steps. Copies expire through transient timers plus a lazy sweep.

Decisions M1–M10 are in [research.md](./research.md).

## Technical Context

**Language/Version**: Python 3.12+ (same package as feature 001).

**Primary Dependencies**: the existing ones (`mcp` 2.x, `asyncssh`, `pydantic` v2). No new Python dependencies. On the VPS, the stock PostgreSQL 18 client tools, `systemd-run` and coreutils.

**Storage**: the existing local SQLite state DB gets four new tables: `migration_snapshots`, `migration_reports`, `migration_copies`, `migration_rehearsals`. On the VPS, `/var/lib/vps-mcp/rehearsals/<id>/` holds the runner, status and logs.

**Testing**: pytest. The diff and risk engine is tested on synthetic snapshots, including the planted-defect set. The runner script is tested with golden renders and executed locally under bash against a fake `psql`. `FakeVps` is extended with a fake PostgreSQL catalogue, copies and `systemd-run`. A migration negative suite of at least 30 cases is added. Integration tests stay opt-in against a disposable VM with PostgreSQL 18.

**Target Platform**: the same as feature 001. The VPS needs PostgreSQL ≥ 14 (for `pg_read_all_data`) and systemd.

**Project Type**: an extension of the single Python package (a new `vps_mcp.migration` subpackage).

**Performance Goals**: database overview under 30 s (SC-001); comparison of two ~9 GB databases under 5 min (SC-002). Snapshots use catalogue statistics, with exact counts only for flagged tables.

**Constraints**:
- no caller-supplied SQL or commands;
- no writes outside system-created copies;
- the disk minimum is enforced;
- rehearsals keep running if the client disconnects;
- responses stay within the compact-output budget.

**Scale/Scope**: about 5 targets, 1–10 pipelines, one rehearsal at a time, copies of 8–9 GB, and about 4–6k tables per Odoo database.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

`.specify/memory/constitution.md` is still the unfilled template, so there are no ratified gates. **PASS.**

Engineering defaults, kept from feature 001:

| Default | How this plan meets it |
|---|---|
| Least privilege | Analysis runs as a database role that can't write. Copy, drop and steps go through fixed templates. The extra sudo rights (`psql`/`createdb` as postgres, `systemd-run`) are documented as root-equivalent. |
| Testability | The diff and risk engine is pure functions. The runner is rendered from templates and golden-tested. |
| Simplicity | No new services on the VPS: transient units only. No new Python dependencies. |
| Observability | Each state change gets an audit record. Rehearsal status and logs persist on the VPS and locally. |

**Post-design re-check**: PASS. **Known residual risk**: `script` steps are *detected*, not *confined* (research M5). This is shown in every plan as `guarantee: detected`, and any foreign write is a blocking finding.

## Project Structure

### Documentation (this feature)

```text
specs/002-odoo-migration-analysis/
├── plan.md  research.md  data-model.md  quickstart.md
├── contracts/
│   ├── migration-tools.md
│   └── migration-config.md
├── checklists/requirements.md
└── tasks.md                      # /speckit-tasks
```

### Source Code (repository root)

```text
src/vps_mcp/
├── config/policy.py              # + optional `migration: MigrationSettings | None`
├── migration/
│   ├── __init__.py
│   ├── config.py                 # MigrationSettings, targets, OdooProfile, pipelines, checks (contract validation)
│   ├── catalog.py                # fixed SQL catalogue (JSON-returning queries), identifier validation
│   ├── db.py                     # run catalogue queries as read-only role; write-counter probe
│   ├── snapshot.py               # capture + store snapshots; exact-count second pass
│   ├── diff.py                   # pure snapshot diff → raw differences
│   ├── risk.py                   # classification rules, thresholds, verdict (data-model §3)
│   ├── copies.py                 # naming, HMAC marker, create (template|dump), drop, expiry timer, sweep
│   ├── sqlscan.py                # forbidden-construct scan for sql steps
│   ├── runner.py                 # render rehearsal runner script (+ sha256), status.json parsing
│   ├── rehearsal.py              # plan/plan_hash, start via systemd-run, poll, cancel, finalize → report
│   ├── pipeline_logs.py          # progress-file + Odoo log parsing/normalisation (RunSummary)
│   ├── checks.py                 # declared checks, read-only enforcement, write detection
│   ├── store.py                  # SQLite tables + report storage/paging
│   └── tools.py                  # registers the 13 MCP tools on the existing server
├── server.py                     # + register migration tools when [migration] present
├── cli.py                        # + bootstrap --with-migration, --print-migration-sql
└── store/db.py                   # + schema v2 migration (new tables)

tests/
├── fakes.py                      # + FakePostgres catalogue/copies/systemd-run in FakeVps
├── unit/migration/               # catalog, diff, risk (planted defects), copies, sqlscan, runner, logs, checks
├── contract/test_migration_tools.py
├── negative/test_migration_disallowed.py   # ≥30 cases (SC-004)
└── integration/test_migration.py           # opt-in, VM with PostgreSQL 18
```

**Structure Decision**: The feature is a self-contained `vps_mcp.migration` subpackage. It reuses feature 001's executor, guard/audit pipeline, secret masking, locks and SQLite store. `server.py` only gains a conditional registration call, so the existing tools and tests are unaffected when `[migration]` is absent.

## Implementation Phasing (for /speckit-tasks)

1. **Foundation**: config section and validation, store schema v2, catalogue and read-only query runner, fake PostgreSQL in `FakeVps`, setup material (`--with-migration`, `--print-migration-sql`).
2. **US1 (P1)**: `db_list`, `db_overview`, `db_modules`, `db_table`.
3. **US2 (P1)**: snapshots, diff, risk and verdict, `db_compare`, `get_report`, `list_reports`. The planted-defect suite is the gate.
4. **US3 (P2)**: copies (create, marker, expiry), sqlscan, runner rendering, `rehearse` (plan/plan_hash/confirm), `rehearsal_status`, `cancel_rehearsal`, finalize to a report.
5. **US4 (P2)**: `pipeline_last_run`.
6. **US5 (P3)**: `run_check`.
7. **US6 (P3)**: `drop_copies`, sweep.
8. **Hardening**: negative suite, docs and README security note, integration tests, live read-only validation on the VPS (quickstart #1–6).

## Complexity Tracking

No constitution violations. One accepted residual risk is documented above: `script` steps are detected, not confined.
