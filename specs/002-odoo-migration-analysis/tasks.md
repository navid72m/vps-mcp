---

description: "Task list for Odoo Migration Analysis & Rehearsal"
---

# Tasks: Odoo Migration Analysis & Rehearsal

**Input**: Design documents from `/specs/002-odoo-migration-analysis/`

**Prerequisites**: [plan.md](./plan.md), [spec.md](./spec.md), [research.md](./research.md) (M1–M10), [data-model.md](./data-model.md), [contracts/migration-tools.md](./contracts/migration-tools.md), [contracts/migration-config.md](./contracts/migration-config.md), [quickstart.md](./quickstart.md)

**Tests**: Included. The spec requires a planted-defect detection suite (SC-003) and a negative suite of at least 30 cases (SC-004). Write each story's tests first.

**Organization**: Grouped by user story US1–US6 from spec.md. All paths are relative to the repo root (`src/vps_mcp/`, `tests/`). This builds on feature 001: the executor, `core/guard.run_action`, `AuditStore`, `remote/redact`, `tests/fakes.FakeVps`, and `tests/conftest`'s `SessionPerCall` and `call`.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: US1–US6

---

## Phase 1: Setup

- [X] T001 Create the package `src/vps_mcp/migration/__init__.py` and the test dir `tests/unit/migration/__init__.py`. Add the marker `migration` to `[tool.pytest.ini_options].markers` in `pyproject.toml`.
- [X] T002 [P] Add a commented `[migration]` example section to `examples/vps-mcp.toml`, copied from `specs/002-odoo-migration-analysis/contracts/migration-config.md`. Use placeholder paths and no real hostnames.

---

## Phase 2: Foundational (Blocking Prerequisites)

**⚠️ CRITICAL**: No user story can start until this phase is complete.

### Tests for Foundation

- [X] T003 [P] Config validation tests in `tests/unit/migration/test_config.py`. Cover each of the 8 rules in `contracts/migration-config.md` §Validation rules:
  - **Target names**: `^[a-z_][a-z0-9_]{0,62}$`, no `vmcp_` prefix, unique.
  - **Paths**: files must resolve inside `approved_dirs` (realpath, no `..`).
  - **Placeholders**: only `{db}` and `{port}`.
  - **`odoo_upgrade` steps**: require `[migration.odoo]`; `rounds` only with `modules = "stale"` (1–5).
  - **`run_as`**: an allowed user.
  - **Caps**: ≤ 30 steps, ≤ 10 pipelines, ≤ 20 checks.
  - **Thresholds**: `0 < row_drop_warn_pct < row_drop_block_pct ≤ 100`.
  - **Optional section**: a config without `[migration]` still loads (feature 001 unchanged).
- [X] T004 [P] Catalogue and read-only runner tests in `tests/unit/migration/test_db.py`:
  - Golden argv is `["psql", "-X", "-p", "5435", "-d", <db>, "-v", "ON_ERROR_STOP=1", "-At", "-v", "name=<value>", "-c", <CATALOG_SQL>]`. It runs **without** sudo, with `env={"PGOPTIONS": "-c default_transaction_read_only=on -c statement_timeout=<ms>"}`.
  - The identifier validator rejects anything not matching `^[a-z_][a-z0-9_]{0,62}$` (or `schema.table` made of two such parts).
  - JSON output parses, and a psql error maps to `VpsMcpError(failed)`.
  - Target resolution: unknown or non-allowlisted databases give `not_permitted`, with the same wording whether or not they exist.

### Implementation for Foundation

- [X] T005 Implement the config models in `src/vps_mcp/migration/config.py`:
  - **Models**: `MigrationSettings`, `RiskThresholds`, `AnalysisTarget`, `OdooProfile`, `StepDefinition`, `PipelineDefinition`, `CheckDefinition`, all pydantic with `extra="forbid"`.
  - **Fields and defaults**: exactly as data-model.md §1 — `port`, `min_free_gb` = 10, `min_free_pct` = 10, `copy_growth_factor` = 1.3, `copy_retention_h` = 24, `analysis_timeout_s` = 300, `step_timeout_s` = 21600, `approved_dirs`, and `thresholds` (warn 5, block 20, exact trigger 2).
  - **Validators**: the static rules 1–7 from the contract.
  - **Wiring**: add an optional `migration: MigrationSettings | None = None` to `AppConfig` in `src/vps_mcp/config/policy.py`.
- [X] T006 [P] Implement the fixed SQL catalogue in `src/vps_mcp/migration/catalog.py`:
  - **Constants**: `Q_OVERVIEW`, `Q_TABLES` (all tables with columns, indexes, constraints, `reltuples`, total bytes; one JSON doc), `Q_TABLE` (one table, using psql var `:'tbl'` and `:'sch'`), `Q_MODULES` (or `null` when `ir_module_module` is absent), `Q_VIEWS`, `Q_ORPHAN_VIEWS`, `Q_WRITE_COUNTERS` (`pg_stat_database` for all databases), `Q_EXACT_COUNT` (`:'sch'`/`:'tbl'` via `format('%I.%I')` in a DO-free `SELECT` using `query_to_xml` count), `Q_DB_SIZE`, `Q_CONNECTIONS` (`pg_stat_activity` count for one database), `Q_DB_COMMENT` (`shobj_description`).
  - **Identifiers**: `check_identifier()` and `split_table()` helpers.
  - **No string formatting** of caller values into SQL anywhere.
- [X] T007 Implement the read-only query runner in `src/vps_mcp/migration/db.py`. Depends on T005 and T006.
  - `MigrationDb(ctx)` with `async query(db, sql, vars: dict[str, str] = {}) -> Any` builds the T004 golden argv and runs it via `ctx.executor` with sudo=False and the `PGOPTIONS` env.
  - `resolve_target(name)` accepts allowlisted targets or registered copies (the copies registry from T008, filled by T028) and returns `not_permitted` otherwise.
  - `write_counters(dbs)` returns `{db: (ins, upd, del, commits)}`.
  - The `PGOPTIONS` env key needs adding to the executor's env-key allowlist regex if needed (`src/vps_mcp/remote/executor.py` already accepts `^[A-Z_][A-Z0-9_]{0,63}$`).
- [X] T008 [P] Store schema v2 in `src/vps_mcp/store/db.py`:
  - Bump `SCHEMA_VERSION` to 2, with an idempotent migration from v1.
  - Create the tables `migration_snapshots(id, database, is_copy, taken_at, json)`, `migration_reports(id, created_at, kind, verdict, inputs_json, json)`, `migration_copies` (columns per data-model §4) and `migration_rehearsals` (columns per data-model §5).
  - `purge_audit` also purges reports and snapshots older than the retention period.
  - Create `src/vps_mcp/migration/store.py` with typed accessors: `save_snapshot`, `get_snapshot`, `save_report`, `get_report`, `list_reports`, `copies_*`, `rehearsals_*`.
- [X] T009 Extend `tests/fakes.py` with a `FakePostgres` attached to `FakeVps` (`vps.pg`):
  - **Databases**: a dict of databases, each with tables (columns, rows), modules, views, comment, connections and write counters.
  - **Commands**: handlers for `psql` (dispatch on the catalogue constant identity: return JSON built from the fake state; apply `-f <file>` steps by recording them and mutating via a test hook; refuse writes when `PGOPTIONS` contains `default_transaction_read_only=on` and the SQL is a write), `createdb -T`, `dropdb`, `pg_dump`/`pg_restore`, `systemd-run` (records the unit and runs the runner synchronously via a test hook), `sha256sum`, `grep`.
  - **Mutation detection**: add `pg_mutations(db)` so tests can assert that no non-copy database changed.
- [X] T010 Register migration tools conditionally in `src/vps_mcp/server.py`:
  - When `config.migration` is set, call `vps_mcp.migration.tools.register(mcp, app, wrap)`.
  - Create `src/vps_mcp/migration/tools.py` with an empty `register()`, which each story fills in.
  - `AppContext` gains `migration: MigrationRuntime | None` (holding `MigrationDb` and the store).
- [X] T011 [P] Setup material in `src/vps_mcp/cli.py`:
  - `bootstrap --print-sudoers --with-migration` appends the M10 rules: `(postgres) NOPASSWD: /usr/bin/psql, /usr/bin/createdb, /usr/bin/dropdb, /usr/bin/pg_dump, /usr/bin/pg_restore` and `(root) NOPASSWD: /usr/bin/systemd-run, /usr/bin/grep, /usr/bin/sha256sum`, plus one rule per check `run_as` interpreter.
  - New `bootstrap --print-migration-sql` prints `CREATE ROLE vpsmcp LOGIN; GRANT pg_read_all_data, pg_read_all_stats TO vpsmcp;` (guarded with `DO $$ … IF NOT EXISTS … $$`).
  - Tests go in `tests/unit/test_cli.py`.

**Checkpoint**: The config loads, the catalogue runs against FakePostgres, and server startup is unchanged when `[migration]` is absent (all 361 existing tests still pass).

---

## Phase 3: User Story 1 - Understand a database before migrating (Priority: P1) 🎯 MVP

**Goal**: An overview, the module list with code presence, table detail and problem indicators for any allowlisted database.

**Independent Test**: Quickstart scenarios 1–3. Fake: a database with 3 modules at 17.x (one without code), 2 inactive views and 1 orphaned view.

### Tests for User Story 1

- [X] T012 [P] [US1] Write `tests/unit/migration/test_understand.py`:
  - **Overview**: `db_overview` returns `largest` (max 20, sorted by bytes), `modules.by_state` and `by_major`, and `indicators` (transitional, code_missing, inactive_views, orphaned_views).
  - **Modules**: `db_modules` filters (`state`, `major`, `code_missing`) and sets `code` from the addons directory listing.
  - **Table detail**: `db_table` gives columns, indexes and constraints. A bad identifier gives `invalid_input`.
  - **Non-Odoo databases**: `modules` is null, and indicators are reported as skipped.
- [X] T013 [P] [US1] Write contract tests in `tests/contract/test_migration_tools.py`:
  - all 13 tool names appear when `[migration]` is configured, and none when it isn't;
  - `readOnlyHint` is true exactly on the 9 read-only tools;
  - input schemas match `contracts/migration-tools.md`;
  - `not_permitted` gives identical wording for nonexistent and non-allowlisted databases.

### Implementation for User Story 1

- [X] T014 [US1] Implement `src/vps_mcp/migration/understand.py`:
  - `overview(rt, db)`, `modules(rt, db, state, major, code_missing)`, `table(rt, db, table)`.
  - **Code presence**: list each `odoo.addons_paths` dir with `ls -1 -- <dir>` (no sudo; relies on the ACL from the quickstart) and cache the listing per call.
  - **Timing**: everything bounded by `analysis_timeout_s`; output compact (data-model §2 shapes).
- [X] T015 [US1] Register `db_list`, `db_overview`, `db_modules` and `db_table` in `src/vps_mcp/migration/tools.py`, with `readOnlyHint` and plain-language descriptions. `db_list` sizes come from `Q_DB_SIZE`.

**Checkpoint**: Quickstart 1–3 pass on the VPS read-only.

---

## Phase 4: User Story 2 - Compare two databases and get a risk report (Priority: P1)

**Goal**: A deterministic diff with classified findings, a verdict, and stored, pageable reports.

**Independent Test**: The planted-defect fixture: a dropped populated column, `varchar(64→16)`, a 40% row loss, a module left at `17.0.1.0`, a module in `to upgrade`, a deactivated view, and a removed FK. Every defect must be found with the expected severity, the verdict must be `do_not_promote`, and running it twice must give identical findings.

### Tests for User Story 2

- [X] T016 [P] [US2] Write `tests/unit/migration/test_diff_risk.py` with pure-function tests over hand-built `Snapshot` dicts:
  - **Every category** in the data-model §3 table, with its default severity. Include the empty-table info downgrade for `table_removed`/`column_removed`.
  - **Narrowing rules**: `varchar` n→m (m < n), numeric precision/scale decrease, `text→varchar(n)`, `bigint→integer`, `timestamptz→timestamp`, cross-family. Each reverse is info only.
  - **Thresholds**: `row_drop` warn above 5% and block above 20%, using only `exact_rows` for blocking.
  - **Verdict**: derived from the most severe finding.
  - **Sorting**: by `(severity, category, object)`.
  - **Determinism**: same inputs give the same output.
- [X] T017 [P] [US2] Write `tests/unit/migration/test_planted_defects.py` (SC-003). It builds the fixture in FakePostgres, calls `db_compare`, asserts 100% detection, the expected severities and the `do_not_promote` verdict, and that `get_report` paging returns all findings.

### Implementation for User Story 2

- [X] T018 [P] [US2] Implement `src/vps_mcp/migration/snapshot.py`:
  - `capture(rt, db) -> Snapshot` uses `Q_TABLES`, `Q_MODULES`, `Q_VIEWS` and `Q_WRITE_COUNTERS`, with the data-model §2 shape and `format_type`-normalised types. It stores the snapshot via the store and returns `snp_<12hex>`.
  - `exact_counts(rt, db, tables)` runs `Q_EXACT_COUNT` per table, bounded by `analysis_timeout_s`.
- [X] T019 [P] [US2] Implement `src/vps_mcp/migration/diff.py`: `diff(before, after) -> list[RawDiff]`, covering tables, columns, types, indexes, constraints, rows, modules and views. It is a pure function.
- [X] T020 [US2] Implement `src/vps_mcp/migration/risk.py`. Depends on T019.
  - `classify(raw, thresholds, before_major) -> list[Finding]` follows the data-model §3 table exactly, with the narrowing rules from research M3.
  - `verdict(findings)`.
  - `build_report(kind, inputs, findings, skipped, step_results=None, check_results=None) -> Report` with id `rpt_<12hex>`.
- [X] T021 [US2] Implement `src/vps_mcp/migration/compare.py`:
  - `compare(rt, before, after, exact_counts=True)` captures both snapshots and diffs them.
  - For tables whose estimated row change exceeds `exact_count_trigger_pct`, it fetches exact counts on both sides and re-diffs.
  - It then classifies, saves the report and returns a `ReportSummary` (top 20 findings).
- [X] T022 [US2] Register `db_compare`, `get_report` (50 findings per page, filters `severity`/`category`) and `list_reports` (`limit` 1..100, default 20) in `src/vps_mcp/migration/tools.py`.

**Checkpoint**: Quickstart 4–5 pass. US1 and US2 together form the MVP (pure analysis, zero writes).

---

## Phase 5: User Story 3 - Rehearse the pipeline on a disposable copy (Priority: P2)

**Goal**: plan → confirm → a copy is made → declared steps run detached on the VPS → results and a report are produced.

**Independent Test**: A fake pipeline with an `sql` step that succeeds and a `script` step that fails. Check that the copy received the changes, the source is unchanged (`pg_mutations`), the failure is reported with its log tail, and the remaining steps are `not_run`.

### Tests for User Story 3

- [X] T023 [P] [US3] Write `tests/unit/migration/test_copies.py`:
  - **Name**: matches `^vmcp_[a-z0-9_]{1,30}_\d{12}_[0-9a-f]{4}$`.
  - **Marker**: `vps-mcp-copy:<copy_id>:<hmac[:32]>`, and verification fails if the name or comment is changed.
  - **Method**: `auto` picks `template` with 0 connections and `dump` otherwise.
  - **Disk**: the estimate is `size × copy_growth_factor`; `insufficient_disk` when `free - est < max(min_free_gb GB, min_free_pct% of total)`.
  - **Expiry timer**: argv `systemd-run --on-active=<h>h --unit vps-mcp-expire-<copy_id> …`.
  - **Drop**: refused unless both the local row and the live comment match.
- [X] T024 [P] [US3] Write `tests/unit/migration/test_sqlscan.py`. It refuses `\c`, `\connect`, `dblink`, `postgres_fdw`, `COPY … PROGRAM`, `ALTER SYSTEM`, `CREATE EXTENSION dblink`, case-insensitively, including inside comments with care: comments are stripped first and string literals ignored. It accepts the real-world shapes of `data_cleanup.sql` (DELETE/UPDATE).
- [X] T025 [P] [US3] Write `tests/unit/migration/test_runner.py`:
  - **Golden render**: each step kind produces the exact command from research M5; the copy name appears as the only `-d` target; `timeout <step_timeout_s>` wraps each step; the disk check runs between steps; the write detector runs before and after each step; `status.json` writes happen.
  - **sha256**: the runner hash is stable for identical input.
  - **Local bash run**: execute the rendered script with stub `psql`, `sudo`, `df`, `timeout` and `sha256sum` on `PATH`. Assert the status.json transitions (`ok`, `failed`, then `not_run`), the disk abort (`aborted_disk`), and the fingerprint mismatch (`failed` with reason `fingerprint_changed`).
- [X] T026 [P] [US3] Write `tests/unit/migration/test_rehearsal.py`:
  - **Preview**: returns a plan and `plan_hash`; nothing is created (no mutating calls).
  - **Confirm**: needs a matching `plan_hash` less than 30 min old, else `conflict` with a fresh plan.
  - **Concurrency**: a second rehearsal while one is active gives `conflict`, checked against both the local table and `systemctl is-active`.
  - **Finalisation**: after FakePostgres runs the runner, `rehearsal_status` finalises the report with step results.
  - **Foreign writes**: a `script` step that writes to a non-copy database gives a blocking `foreign_write` finding and aborts.
  - **Cancel**: `cancel_rehearsal` stops the unit and keeps the copy.
  - **Audit**: one audit record per state change, with the actions `copy` and `rehearse`.

### Implementation for User Story 3

- [X] T027 [P] [US3] Implement `src/vps_mcp/migration/sqlscan.py`: `scan(sql_text) -> list[str]` returns the forbidden constructs found, per research M5.
- [X] T028 [P] [US3] Implement `src/vps_mcp/migration/copies.py`:
  - Copy naming, the HMAC marker (key from `~/.local/state/vps-mcp/<vps>.migration.key`, mode 0600, created on first use), disk estimate and check, and method selection.
  - `create()` uses `sudo -u postgres createdb -p <port> -T <src> -O <owner> <copy>` followed by `psql … -c "COMMENT ON DATABASE :\"copy\" IS :'marker'"` with psql vars.
  - **Dump mode** runs inside the runner unit.
  - `schedule_expiry()`, `drop()` (verify the marker, then `dropdb`), and `sweep()` (lazy expiry).
- [X] T029 [US3] Implement `src/vps_mcp/migration/runner.py`. Depends on T027 and T028.
  - `render(rehearsal, pipeline, copy, settings, odoo) -> (script_text, sha256)` uses only fixed templates and `shlex.quote` for every interpolated value.
  - **Step commands** per research M5. `script` steps get the env `DB`, `PGDATABASE`, `PSQL`, `REPO` and `OUT`.
  - **Each step**: re-check the sha256, run the write detector before and after, `timeout`, write the log to `<dir>/<n>-<name>.log`, and update the atomic `status.json` (tmp file + mv).
  - **Between steps**: a disk check that aborts with `aborted_disk`.
  - `parse_status(text) -> dict`.
- [X] T030 [US3] Implement `src/vps_mcp/migration/rehearsal.py`. Depends on T029.
  - **Plan**: `plan(rt, pipeline, source, method) -> (plan, plan_hash)`. It reads file sha256s with `sudo sha256sum`, runs `sqlscan` on the sql step files (read via `sudo cat`), estimates disk, and picks the method.
  - **Start**: `start(...)` re-plans and compares hashes, creates the copy (template mode), uploads the runner (`tee` + `install -m 0700` to `/var/lib/vps-mcp/rehearsals/<id>/run.sh`), runs `systemd-run --unit vps-mcp-rehearsal-<id> --property=TimeoutStopSec=60 /bin/bash <run.sh>`, and records the row.
  - **Status**: `status(id)` reads `status.json` and log tails. On a terminal state it runs `finalize()`: capture the copy snapshot, compare source → copy, add `step_failed`, `foreign_write` and `disk_abort` findings, save the report.
  - **Cancel**: `cancel(id)` runs `systemctl stop`.
  - **Guard**: all state changes go through `core/guard.run_action` with the actions `copy`/`rehearse`/`cancel_rehearsal`.
- [X] T031 [US3] Register `rehearse`, `rehearsal_status` and `cancel_rehearsal` in `src/vps_mcp/migration/tools.py`, per the contract, with `destructiveHint: false` for rehearse and `true` for cancel.

**Checkpoint**: The rehearsal flow passes against FakeVps. On the real VPS it is blocked until disk space is freed (quickstart 7 shows `insufficient_disk`).

---

## Phase 6: User Story 4 - Inspect results of the operator's own pipeline runs (Priority: P2)

**Goal**: Summarise the declared progress file and logs of runs the operator started by hand.

**Independent Test**: A known `STEPS.txt` (START, `STEP5 cleanup ok`, `STEP5 orphan views rc=0`, `STEP6 -u all EXIT 1`) plus a log with 3 distinct repeated errors. The summary must show the steps and statuses and the top errors with correct counts.

### Tests for User Story 4

- [X] T032 [P] [US4] Write `tests/unit/migration/test_pipeline_logs.py`:
  - **Progress parsing**: the default patterns match `ok`, `FAILED`, `rc=<n>` and `EXIT <n>`.
  - **Odoo log parsing**: `YYYY-MM-DD HH:MM:SS,mmm PID LEVEL db logger: msg`.
  - **Normalisation**: numbers become `#` and quoted values become `'…'`, giving stable top-10 counts with first and last seen.
  - **Truncation**: output over 2 MB is cut and marked `truncated`.

### Implementation for User Story 4

- [X] T033 [US4] Implement `src/vps_mcp/migration/pipeline_logs.py`:
  - `last_run(rt, pipeline)` reads the progress file via `sudo -n cat -- <file>`.
  - It reads errors via `sudo -n grep -E -h -m 20000 ' (ERROR|CRITICAL|WARNING) ' -- <files>`. The files come from the declared `log_globs`, expanded **without a shell**: run `sudo -n ls -1 -- <dir>` for each glob's directory part, then match the file part locally with `fnmatch`.
  - It returns a `RunSummary` (data-model §6). Register `pipeline_last_run` in `src/vps_mcp/migration/tools.py`.

---

## Phase 7: User Story 5 - Run the operator's existing checks (Priority: P3)

**Goal**: Run declared checks with read-only enforcement and fold their results into reports.

**Independent Test**: One passing and one failing declared check. Both are recorded; the failing one becomes a finding; a `may_modify` check against a non-copy database is refused.

### Tests for User Story 5

- [X] T034 [P] [US5] Write `tests/unit/migration/test_checks.py`:
  - **Placeholders**: argv substitution only for whole elements and `--x={db}` suffixes.
  - **Read-only**: `read_only` checks run with `PGOPTIONS` read-only, and a write detected by the counters gives a blocking `foreign_write` finding.
  - **may_modify**: needs a copy and `confirm`, otherwise `not_permitted`.
  - **Results**: timeout gives `timeout`; output is masked by secret rules; `blocking=true` failures become blocking.

### Implementation for User Story 5

- [X] T035 [US5] Implement `src/vps_mcp/migration/checks.py`:
  - `run_check(rt, check, db, confirm, session_id)`.
  - `read_only` checks are not audited as state changes (they are reads).
  - `may_modify` checks go through `guard.run_action(action="run_check")`.
  - If the latest report concerns that database, the result is attached to it.
  - Register `run_check` in `src/vps_mcp/migration/tools.py`.

---

## Phase 8: User Story 6 - Clean up disposable copies (Priority: P3)

**Goal**: List and drop copies the server created, with expiry enforced.

**Independent Test**: Two copies, one expired, plus a non-copy database named `vmcp_fake`. Only the real expired copy is dropped by `expired_only`; `vmcp_fake` is refused with reason `marker_mismatch`.

### Tests for User Story 6

- [X] T036 [P] [US6] Write `tests/unit/migration/test_cleanup.py`:
  - **List**: `db_list` shows only marked copies.
  - **Drop**: `drop_copies` previews `will_drop` and `reclaim_bytes`; after confirm it drops and reports the reclaimed bytes.
  - **Refusals**: a forged name or a comment mismatch is refused.
  - **Sweep**: runs on startup.
  - **Expiry timer**: present for every copy created.

### Implementation for User Story 6

- [X] T037 [US6] Register `drop_copies` in `src/vps_mcp/migration/tools.py`. It uses `copies.drop` through `guard.run_action(action="drop_copy")`. Call `copies.sweep()` in the `build_context` startup path (`src/vps_mcp/server.py`) and at the start of every migration tool call (best effort, errors logged).

---

## Phase 9: Polish & Cross-Cutting Concerns

- [X] T038 [P] Negative suite in `tests/negative/test_migration_disallowed.py` (≥ 30 cases, SC-004). Every case asserts `vps.pg.pg_mutations(<non-copy dbs>) == []` and the expected error category. The cases:
  - injection strings in the `db`, `table`, `before`, `after`, `pipeline`, `source` and `check` fields;
  - non-allowlisted databases;
  - names starting `vmcp_` that aren't real copies;
  - copies of copies;
  - an sql step containing `\c other`;
  - a check `may_modify` on a target;
  - rehearse without confirm, and confirm with a stale or wrong `plan_hash`;
  - a concurrent rehearsal;
  - `drop_copies` on a target;
  - read-only mode for every state-changing migration tool.
- [X] T039 [P] Update `README.md`:
  - a "Migration analysis" section with the tool table, a setup summary (sudoers `--with-migration`, the SQL role, `setfacl`) and the disk caveat;
  - in the security section, the residual risk that `script` steps are detected, not confined (research M5).
- [X] T040 [P] Write opt-in integration tests in `tests/integration/test_migration.py`. Extend `tests/integration/provision.sh` to install PostgreSQL 18, create two small databases with a planted difference, and add a `[migration]` section to the test config. Cover quickstart scenarios 1–14 on the VM.
- [X] T041 Run `uv run ruff check`, `uv run ruff format --check`, `uv run mypy` and `uv run pytest`, and fix everything. Feature 001's 361 tests must still pass.
- [ ] T042 Live read-only validation on the operator's VPS:
  1. Install the migration sudoers additions, the SQL role and the ACL (as root, after showing the user).
  2. Add `[migration]` to `~/.config/vps-mcp/<vps>.toml` with targets `shop_17`, `shop_19_initial` and `shop_19_repaired`.
  3. Run quickstart scenarios 1–7.
  4. Record the results in `specs/002-odoo-migration-analysis/checklists/quickstart-results.md`.

  Rehearsal scenarios 8–15 stay pending until disk is freed.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup → Foundational**: Foundational blocks every story.
- **US1 (P1)**: needs Foundational only.
- **US2 (P1)**: needs Foundational. It reuses `catalog` and `db` but not US1 code, so it can run in parallel with US1.
- **US3 (P2)**: needs US2, because finalize calls `compare`.
- **US4 (P2)**: needs Foundational only (config plus executor). Independent.
- **US5 (P3)**: needs US2 to attach results to reports. The read-only part can start after Foundational.
- **US6 (P3)**: needs `copies.py` from US3 (T028).
- **Polish**: after the desired stories. T042 needs US1, US2 and US4 at minimum.

```text
Setup → Foundational ─┬─ US1 ─────────────┐
                      ├─ US2 ─┬─ US3 ─ US6 ├─ Polish
                      │       └─ US5 ──────┤
                      └─ US4 ──────────────┘
```

### Within Each Story

Tests first (they must fail), then pure modules, then remote orchestration, then tool registration. `tools.py` is touched by every story, so its registration tasks run sequentially, never in parallel with each other.

### Parallel Opportunities

- **Phase 2**: tests T003 and T004; implementation T006, T008 and T011 alongside T005.
- **US2**: T016 and T017 (tests); T018 and T019 (snapshot and diff are independent pure modules).
- **US3**: T023–T026 (tests); T027 and T028 (sqlscan and copies).
- **Across stories**: US1, US2 and US4 in parallel after Foundational.

## Parallel Example: User Story 2

```bash
Task: "T016 diff/risk unit tests in tests/unit/migration/test_diff_risk.py"
Task: "T017 planted-defect suite in tests/unit/migration/test_planted_defects.py"
Task: "T018 snapshot capture in src/vps_mcp/migration/snapshot.py"
Task: "T019 pure diff in src/vps_mcp/migration/diff.py"
```

## Parallel Example: User Story 3

```bash
Task: "T023 copies tests in tests/unit/migration/test_copies.py"
Task: "T024 sqlscan tests in tests/unit/migration/test_sqlscan.py"
Task: "T027 sqlscan in src/vps_mcp/migration/sqlscan.py"
Task: "T028 copies in src/vps_mcp/migration/copies.py"
```

---

## Implementation Strategy

### MVP (US1 + US2): pure analysis, zero writes

1. Phases 1–2.
2. US1, then **validate** quickstart 1–3 live, read-only.
3. US2, then **validate** quickstart 4–5. This alone answers "is this upgrade safe?" for databases your pipeline already produced (`shop_17` vs `shop_19_initial` / `shop_19_repaired`).
4. Optionally US4 (P2, independent and cheap) for summaries of your own runs.

### Then

5. US3 rehearsals. These need freed disk on the VPS to run live.
6. US5 checks.
7. US6 cleanup.
8. Polish: negative suite, docs, integration, live validation.

---

## Notes

- **No raw input**: no task may add a tool or code path that accepts SQL or a command string from the caller.
- **Writes**: every write path targets a system-created copy whose marker has been verified.
- **Audit**: all state changes go through `core/guard.run_action`, so audit, read-only mode and confirmation behave exactly as in feature 001.
- **Atomic commits**: commit after each task or logical group.
