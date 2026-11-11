---

description: "Task list for Declared Jobs"
---

# Tasks: Declared Jobs

**Input**: Design documents from `/specs/003-declared-jobs/`

**Prerequisites**: [plan.md](./plan.md), [spec.md](./spec.md), [research.md](./research.md) (J1–J10), [data-model.md](./data-model.md), [contracts/job-tools.md](./contracts/job-tools.md), [contracts/job-config.md](./contracts/job-config.md), [quickstart.md](./quickstart.md)

**Tests**: Included. The spec requires a test that plants side effects (SC-003) and a negative suite of at least 20 cases (SC-004). Write each story's tests first.

**Organization**: Tasks are grouped by user story (US1–US6). The two P2 stories are ordered US4 (guard), then US3 (report), because the report includes guard findings. Paths are relative to the repo root.

This feature reuses existing parts of features 001 and 002:
- `vps_mcp.migration.runtime` (`MigrationRuntime`, `run_db_action`, `mask`)
- `migration.db.MigrationDb` (`write_counters`, `query`, `free_and_total`)
- `migration.compare.compare_snapshots` and `migration.risk`
- `migration.understand.resolve_db`
- feature 001's backends (for service states)
- `tests/fakes.FakeVps`, `tests/fakepg.FakePg`, and `tests/unit/migration/conftest`

**Privacy**: all fixtures and docs use generic names (`full_rehearsal.sh`, `shop_*`, `odoo-test`).

## Format: `[ID] [P?] [Story] Description`

---

## Phase 1: Setup

- [X] T001 Create the package `src/vps_mcp/jobs/__init__.py` and the test package `tests/unit/jobs/__init__.py`.
- [X] T002 [P] Add a commented `[[migration.job]]` example, with a `[[migration.job.params]]` entry, to `examples/vps-mcp.toml`. Copy it from `specs/003-declared-jobs/contracts/job-config.md` and keep the generic names.

---

## Phase 2: Foundational (Blocking Prerequisites)

**⚠️ CRITICAL**: No user story can start until this phase is complete.

### Tests for Foundation

- [X] T003 [P] Write config validation tests in `tests/unit/jobs/test_job_config.py`, one case per rule in `contracts/job-config.md` §Validation:
  1. Job names are unique and match `^[a-z0-9-]{1,40}$`.
  2. Paths are absolute with no `..`, and globs may only have wildcards in the last component.
  3. `may_modify`, `source` and `result` must be targets, and `result` must be in `may_modify`.
  4. Every entry in `services` must be a declared `[[service]]`, and remote-access units are refused.
  5. `run_as` must be in the allowed set.
  6. Parameter names match `^[A-Z][A-Z0-9_]{0,31}$`; names on the deny list are rejected (`PATH`, `LD_PRELOAD`, `PGPASSWORD`, `VPSMCP_X`, `BASH_ENV`); patterns must compile; a default without `{date:` must match its own pattern.
  7. `outcome_patterns` must compile and may only use the named groups `step` and `status`.
  8. `max_runtime_h` is within 0.1–168 and `min_free_gb` is at least 0.

  Also test that a configuration with no jobs still loads.

### Implementation for Foundation

- [X] T004 Add `ParamDecl` and `JobDeclaration` to `src/vps_mcp/migration/config.py`, and add `job: list[JobDeclaration]` (at most 10) to `MigrationSettings`.
  - Field defaults come from data-model.md §1: `run_as="root"`, `min_free_gb=5`, `max_runtime_h=24`, `success_marker="ALL DONE"`, and `params` with at most 10 entries.
  - The deny list from research J7 is a module constant.
  - Rules 1–3 and 5–8 are validated inside the model.
  - Rule 4 (services must be declared) needs feature 001's `[[service]]` list, so it goes in `AppConfig._cross_checks` in `src/vps_mcp/config/policy.py`. That check also refuses `remote_access_units`.
- [X] T005 [P] Store schema v3 in `src/vps_mcp/store/db.py`:
  - Set `SCHEMA_VERSION=3` and add `_SCHEMA_V3`, creating the table `job_runs` with the columns in data-model.md §2: `run_id` primary key, `job`, `plan_hash`, `plan_json`, `status`, `unit`, `rundir`, `progress_offset`, `baseline_json`, `last_guard_json`, `started_at`, `ended_at`, `exit_code`, `systemd_result`, `cancel_requested`, `report_id`, `audit_id`, `created_at`, `updated_at`.
  - The migration must be idempotent from v1 and v2.
  - Add `src/vps_mcp/jobs/store.py` with `JobStore`: `add_plan`, `by_plan`, `get`, `latest`, `running`, `update(**allowed)` with an allowlisted set of columns, and `recent(limit)`.
- [X] T006 Add the action names `run_job` and `cancel_job` to `ACTIONS` in `src/vps_mcp/store/audit.py`.
- [X] T007 [P] Add `bootstrap --with-jobs` to `src/vps_mcp/cli.py`. It appends `Cmnd_Alias VPSMCP_JOBS = /usr/bin/stat` and `{user} ALL=(root) NOPASSWD: VPSMCP_JOBS`. Test it in `tests/unit/test_cli.py`.
- [X] T008 Extend the fakes in `tests/fakepg.py`:
  - `FakePg.job_units`: `systemd-run` with `--unit vps-mcp-job-*` records the full argv, `setenv`, `uid` and `RuntimeMaxSec`. A test hook `job_effect(run)` mutates the fake state and appends to the fake progress file (in `vps.files`), so tests can simulate a job.
  - New handlers:
    - `stat -c '%U %a %n'` and `stat -c %s`, answered from `vps.file_meta`;
    - `pgrep -f -- <script>`, answered from `pg.processes`;
    - `tail -c +N -- <file>`;
    - `systemctl show -p ActiveState,SubState,Result,ExecMainStartTimestamp,ExecMainExitTimestamp --timestamp=utc <unit>`;
    - `systemctl reset-failed` and `systemctl stop vps-mcp-job-*`.
  - The exit code file is written to `vps.files[<rundir>/exit_code]`.
- [X] T009 Create `tests/unit/jobs/conftest.py` with:
  - `job_config()`: the migration section plus one job `full-rehearsal` using generic names. It has script `/root/full_rehearsal.sh`, progress `/root/rehearsal_logs/STEPS.txt`, report `/root/rehearsal_logs/compare.md`, `may_modify=["shop_19"]`, `services=["worker"]`, `source="shop_17"`, `result="shop_19"`, and param `RUN_NAME` (pattern `^[a-z0-9_]{1,40}$`, default `rehearsal_{date:%Y%m%d_%H%M}`).
  - Fixtures `jvps`, `japp` and `jclient`. The script file is root-owned with mode 755 and its directory mode 700. `STEPS.txt` is pre-filled with lines from an earlier run.
- [X] T010 Register the job tools conditionally in `src/vps_mcp/server.py`: when `app.migration` is set and `config.migration.job` is non-empty, call `vps_mcp.jobs.tools.register(mcp, app.migration, wrap, _session_id)`. Create `src/vps_mcp/jobs/tools.py` with an empty `register()`.

**Checkpoint**: All 502 existing tests still pass, and configurations without jobs register no job tools.

---

## Phase 3: User Story 1 - Start a declared job after reviewing its plan (Priority: P1) 🎯 MVP

**Goal**: plan, then confirm with the plan id, then a detached systemd unit runs the declared script.

**Independent Test**: An unconfirmed request runs nothing and its plan names the database to be dropped. A confirmed request creates a `vps-mcp-job-*` unit whose argv exactly matches research J1.

### Tests for User Story 1

- [X] T011 [P] [US1] Write `tests/unit/jobs/test_integrity.py`, covering `check_script` against research J3:
  - an owner of `root` or the `run_as` user is accepted;
  - modes 755, 750 and 700 are accepted, while 775, 757 and 777 are refused (group or other write bit);
  - a script directory with mode 777 is refused;
  - a missing file gives `invalid_input`;
  - the sha comes from the `sha256sum` output.
- [X] T012 [P] [US1] Write `tests/unit/jobs/test_launcher_argv.py`, a golden test of the `systemd-run` argv against research J1:
  - `--unit vps-mcp-job-<id>`;
  - `--property=RuntimeMaxSec=<h*3600>`;
  - `--property=KillMode=control-group`;
  - `StandardOutput=append:` and `StandardError=append:` pointing at `<rundir>/console.log`;
  - `--working-directory=<dirname>`;
  - `--setenv=VPSMCP_JOB_RUN=<id>` and one `--setenv=NAME=value` per parameter;
  - `--uid` only when `run_as` is not root;
  - the fixed wrapper `/bin/bash -c 'bash "$1"; rc=$?; printf "%s\n" "$rc" > "$2"; exit $rc' vps-mcp-job <script> <rundir>/exit_code`;
  - no `--collect`.

  Also test that a parameter value containing a quote, `$` or space stays a single argv element.
- [X] T013 [P] [US1] Write `tests/unit/jobs/test_plan_start.py`:
  - **Preview**: the plan has `script_sha256`, `may_modify` with `exists`/`size_bytes`, `services` with state, `guarded` and `notes`, and the resolved `RUN_NAME`. There are zero mutating calls and no units.
  - **Confirm**: a matching `plan_id` gives `running`, records the unit, and records `progress_offset` equal to the file size at start.
  - **Conflicts**: `conflict` when the plan is older than 30 minutes, the sha changed, the mode became 777, a job is running, a feature-002 rehearsal is active, or `pgrep` finds the script.
  - **Disk**: `insufficient_disk` below `min_free_gb`.
  - **Read-only**: `read_only`.
  - **Audit**: one record per attempt, with action `run_job`.

### Implementation for User Story 1

- [X] T014 [P] [US1] Implement `src/vps_mcp/jobs/integrity.py`:
  - `async check_script(rt, job) -> {sha256, owner, mode, mtime, dir_mode}` uses `sudo sha256sum -- <script>` and `sudo stat -c '%U %a %Y %n' -- <script> <dir>`.
  - It raises `not_permitted` if the owner is not in {root, run_as} or `(mode & 0o022)` is set on the script or its directory.
- [X] T015 [P] [US1] Implement `src/vps_mcp/jobs/params.py` (pure):
  - `resolve(job, supplied: dict, now) -> dict[str, str]`.
  - Undeclared names and values that don't fully match the pattern give `invalid_input`. Values over 256 characters, or containing NUL or newline, also give `invalid_input`.
  - `{date:<fmt>}` defaults are rendered with `now.strftime`.
- [X] T016 [US1] Implement `src/vps_mcp/jobs/launcher.py`. Depends on T014.
  - `rundir(run_id)`;
  - `argv(job, run_id, params, settings) -> list[str]`, exactly as research J1 describes;
  - `async start(rt, job, run_id, params)`: run `sudo install -d -m 0700 <rundir>`, then `sudo systemd-run …`, raising `failed` if it doesn't succeed;
  - `async progress_size(rt, path)` via `sudo stat -c %s`, returning 0 if the file is missing;
  - `async running_elsewhere(rt, script) -> pid|None` via `pgrep -f -- <script>`.
- [X] T017 [US1] Implement plan and confirm in `src/vps_mcp/jobs/runs.py`. Depends on T005, T014–T016.
  - **`plan(rt, name, params)`**: resolve the declaration (or `not_found`), resolve parameters, check the script, collect `may_modify` existence and size (`db_info`), service states (feature 001 backend `list_status` of the declared `services`), the guarded scope, disk (`free_and_total` against `min_free_gb`) and anything running (research J6), plus notes. Build `plan_hash` (16 hex) over the stable fields, excluding live disk and size numbers. Store it as `planned` and return `{plan_id, plan}`.
  - **`start(rt, name, params, plan_id, confirm, session_id)`**: go through `run_db_action(action="run_job", preview=plan)`. The body re-plans and compares the hash (`conflict` plus a fresh plan on mismatch), re-checks concurrency, records `progress_offset` and the guard baseline (T026, a no-op until US4), starts the unit, and updates the row to `running`.
- [X] T018 [US1] Register `list_jobs` and `run_job` in `src/vps_mcp/jobs/tools.py`, with input schemas and annotations per `contracts/job-tools.md`. The `run_job` description must say that the job may drop and rebuild its declared databases.

**Checkpoint**: Jobs can be planned and started. Quickstart scenarios 1–4 and 6 pass on fakes.

---

## Phase 4: User Story 2 - Watch a running job (Priority: P1)

**Goal**: State, elapsed time and progress lines since this run's start, masked and capped.

**Independent Test**: A progress file holding an earlier run's lines plus this run's lines (including `DEPLOY EXIT 1` and `STOP: …`) shows only this run's lines, with the right outcomes, and the right final state.

### Tests for User Story 2

- [X] T019 [P] [US2] Write `tests/unit/jobs/test_progress.py`, testing the parser on the reference format `YYYY-MM-DD HH:MM:SS <text> | free <N>`. The lines below must parse to:

  | Line | Step | Outcome | Other |
  |---|---|---|---|
  | `PIPELINE EXIT 0: skip_8a=1 == DONE` | `PIPELINE` | ok | |
  | `RESTORE psql exit 0 , ERROR lines 3` | `RESTORE psql` | ok | |
  | `DEPLOY EXIT 1 (log …)` | | failed | |
  | `STOP: no dump …` | | failed | |
  | `COMPARE SUMMARY 6 PASS, 8 FAIL, 1 INFO` | | | `reported_failures=8` |
  | `ALL DONE` | | done | |
  | `freed: old … dropped` | | none | kept as info |

  Also test that `| free 14G` sets `disk_free=14G`, that declared `outcome_patterns` are tried first, and that a line without a timestamp parses with `ts=None`.
- [X] T020 [P] [US2] Write `tests/unit/jobs/test_status.py`:
  - **Offset reads**: with `progress_offset` past the earlier run's lines, only new lines appear. A file that shrank is read from 0.
  - **States**: an active unit gives `running`. `exit_code` 0 plus `ALL DONE` gives `succeeded`. `exit_code` 0 without the marker gives `failed`. `Result=timeout` gives `timed_out`. An inactive unit with no exit file gives `failed`.
  - **Output limits**: `recent` holds at most 50 lines, `steps` holds every outcome line, and the console tail holds at most 20 masked lines.
  - **Masking**: `password=…` in progress is masked.
  - **Reconnect**: status works from a fresh `SessionPerCall`.

### Implementation for User Story 2

- [X] T021 [P] [US2] Implement `src/vps_mcp/jobs/progress.py` (pure):
  - `parse_line(text, marker, patterns) -> ProgressLine` and `parse(text, marker, patterns) -> list[ProgressLine]`.
  - Apply the rules from research J4 in order, with the `ProgressLine` fields from data-model.md §3.
- [X] T022 [US2] Add `async unit_state(rt, run) -> {active, result, exit_code, started, ended}` to `src/vps_mcp/jobs/launcher.py`. It uses `systemctl show … --timestamp=utc <unit>` and `sudo cat <rundir>/exit_code`. Also add `async console_tail(rt, run, n=20)` via `sudo tail -n`.
- [X] T023 [US2] Implement `status(rt, run_id=None)` in `src/vps_mcp/jobs/runs.py`:
  - Read progress with `sudo tail -c +<offset+1> -- <progress_file>`, falling back to offset 0 if `stat %s` is smaller than the offset.
  - Parse the lines and derive the state with the research J2 table.
  - When the run reaches a final state, persist the state, `exit_code`, `systemd_result` and `ended_at`, run `systemctl reset-failed`, and call `finalize()` (T028, a stub returning `None` until US3).
  - Output is masked with `rt.mask`, with `recent` capped at 50 lines.
- [X] T024 [US2] Register `job_status` in `src/vps_mcp/jobs/tools.py` (read-only).

**Checkpoint**: US1 and US2 together form the MVP: start a job and watch it.

---

## Phase 5: User Story 4 - Know if a job touched anything it shouldn't have (Priority: P2)

**Goal**: A guard baseline at the start, compared on every status call and at the end.

**Independent Test**: In a test that plants side effects, the job also writes to an undeclared target, drops an undeclared database, and stops an undeclared service. All three must appear as blocking findings.

### Tests for User Story 4

- [X] T025 [P] [US4] Write `tests/unit/jobs/test_guard.py`:
  - a write to `shop_17`, which is undeclared and has 0 sessions, gives a blocking `undeclared_db_write`;
  - the same write with `sessions=2` gives a warning `undeclared_db_write_unattributed`;
  - `plain_db` disappearing gives a blocking `undeclared_db_dropped`;
  - `nginx` going from running to stopped gives a blocking `undeclared_service_change`;
  - changes to the declared `shop_19` or `worker` give no findings;
  - findings are deduplicated across repeated status calls.

### Implementation for User Story 4

- [X] T026 [US4] Implement `src/vps_mcp/jobs/guard.py`:
  - `async baseline(rt, job) -> dict`: `write_counters` for targets not in `may_modify`, existence from `db_sizes` for every target, and states of managed services not in `job.services` via feature 001's `list_status`.
  - `async compare(rt, job, baseline) -> (readings, findings)`, applying the research J5 table.
  - Wire `baseline()` into `start()`, and `compare()` into `status()`. Persist both in `baseline_json` and `last_guard_json`.
  - Include `guard_findings` in the `job_status` output.

---

## Phase 6: User Story 3 - Get one report and a verdict when the job ends (Priority: P2)

**Goal**: One stored report (`kind="job"`) with a verdict.

**Independent Test**: A job that succeeds but whose result lost 40% of a table's rows gives `do_not_promote`, with that row drop as a blocking finding and the job's own summary excerpt included.

### Tests for User Story 3

- [X] T027 [P] [US3] Write `tests/unit/jobs/test_report.py`:
  - **Success with row loss**: a succeeded job with `result` 40% smaller in one table gives a blocking `row_drop` from the feature-002 comparison and the verdict `do_not_promote`.
  - **Clean success**: a succeeded, clean job gives `safe_to_promote`, or `review_warnings` when `COMPARE … 8 FAIL` produces `job_reported_failures`.
  - **Failed job**: gives a blocking `step_failed` and `job_not_succeeded`, and the comparison is skipped with a reason.
  - **Excerpt**: the report file is masked and cut at 200 lines with `truncated` set.
  - **Built once**: the report is built only once, and `get_report` returns it.
  - **Script changed during the run**: gives a warning `script_changed_during_run`.

### Implementation for User Story 3

- [X] T028 [US3] Implement `finalize(rt, run, job, parsed_lines, guard_findings, state)` in `src/vps_mcp/jobs/report.py`:
  - Add a `step_failed` finding per failed outcome line, `job_not_succeeded` when the state is not `succeeded`, guard findings, and `job_reported_failures`.
  - Re-check the script sha and add `script_changed_during_run` if it changed.
  - Add the report excerpt via `sudo cat` (masked, ≤ 200 lines).
  - If the run `succeeded` and `source`/`result` are declared, call `compare_snapshots(capture(source), capture(result), extra_findings=…, kind="job", step_results=…, report_excerpt=…, job_state=state)`. Otherwise use `risk.build_report("job", …)` with a skipped note.
  - Force the verdict to `do_not_promote` if the state isn't `succeeded` (FR-015).
  - Save the report, then set `job_runs.report_id`.
- [X] T029 [US3] Wire `finalize` into `status()` in `src/vps_mcp/jobs/runs.py`, calling it once when `report_id` is null on reaching a final state. Return `report` (`risk.summary`) in `job_status`.

---

## Phase 7: User Story 5 - Stop a running job (Priority: P3)

**Goal**: A confirmed cancel kills the whole process tree and reports what was left behind.

**Independent Test**: Start a job, cancel it with confirmation: the unit is inactive, `pgrep` is empty, the state is `cancelled`, and `left_behind` lists the declared services and databases.

### Tests for User Story 5

- [X] T030 [P] [US5] Write `tests/unit/jobs/test_cancel.py`:
  - **Preview**: shows the current step and a warning.
  - **Confirmed cancel**: runs `systemctl stop vps-mcp-job-<id>` as root, then checks the unit is inactive and `pgrep` is empty, giving `cancelled` with `left_behind`. A declared service the job stopped is still reported as stopped, and nothing is restarted.
  - **Not running**: an unknown or finished run gives `not_found`.
  - **Read-only**: refused.
  - **Audit**: action `cancel_job`.

### Implementation for User Story 5

- [X] T031 [US5] Implement `cancel(rt, run_id, confirm, session_id)` in `src/vps_mcp/jobs/runs.py`, and `async stop(rt, run)` in `src/vps_mcp/jobs/launcher.py`, through `run_db_action(action="cancel_job")`:
  1. Set `cancel_requested`.
  2. Stop the unit.
  3. Verify it is inactive and that no process is left (otherwise `failed`, with the remaining pids).
  4. Compute `left_behind` from service states and `db_info`.
  5. Set the state to `cancelled`, then finalize.
- [X] T032 [US5] Register `cancel_job` in `src/vps_mcp/jobs/tools.py` with `destructiveHint`.

---

## Phase 8: User Story 6 - Pass declared parameters (Priority: P3)

**Goal**: Declared, pattern-validated parameters reach the script as environment values. Most of the mechanism is in T015 and T016; this phase adds tool-level tests.

**Independent Test**: A valid `RUN_NAME` reaches the unit's `--setenv`; an invalid or undeclared parameter gives `invalid_input`; the default renders the date.

- [X] T033 [P] [US6] Write `tests/unit/jobs/test_params.py`:
  - a value matching the pattern is accepted;
  - values like `bad name!`, `../x`, `$(id)`, 300 characters long, or containing a newline give `invalid_input`;
  - an undeclared parameter `FOO` gives `invalid_input`;
  - the default `rehearsal_{date:%Y%m%d_%H%M}` renders with a fixed `now`;
  - through `run_job` with `confirm=true`, the systemd-run argv contains `--setenv=RUN_NAME=<value>` as exactly one element.

---

## Phase 9: Polish & Cross-Cutting Concerns

- [X] T034 [P] Write the negative suite `tests/negative/test_jobs_disallowed.py` (≥ 20 cases, SC-004). Every case asserts that no `vps-mcp-job-*` unit was created and the expected error category. Cases:
  - an undeclared job name;
  - injection strings in `job` and `run_id`;
  - undeclared, invalid, oversized and newline parameters;
  - confirm without `plan_id`, with a stale `plan_id`, or with a wrong `plan_id`;
  - sha changed, script mode 777, directory mode 777, wrong owner;
  - low disk;
  - a job already running, an active rehearsal, a hand-started process;
  - read-only mode;
  - `cancel_job` without confirm, or on an unknown run.
- [X] T035 [P] Add a "Declared jobs" section to `README.md`: what a job is, an example declaration (generic names), the plan, confirm and status flow, the guard, the report, cancel limits, the `--with-jobs` sudoers addition, and the residual risks (no confinement; work outside the process group).
- [X] T036 [P] Write opt-in integration tests in `tests/integration/test_jobs.py`. Declare a tiny job on the VM that writes 4 progress lines, sleeps, then prints `ALL DONE`. Check plan, start, status, report, and cancel of a second run.
- [X] T037 Run `uv run ruff check`, `uv run ruff format --check`, `uv run mypy` and `uv run pytest`, and fix all failures. All earlier tests must still pass. Run a privacy scan of `specs/003-declared-jobs`, `tests/` and `README.md` for operator-specific names.
- [ ] T038 Live, **plan-only** validation on the operator's VPS. This waits for the VPS setup: the feature 002 role and sudoers plus `--with-jobs`.
  1. Add the operator's real job to their local `~/.config/vps-mcp/<vps>.toml`.
  2. Run `run_job` with `confirm=false` and check that the plan shows the right sha, owner and mode, databases and services.
  3. Record the outcome in `specs/003-declared-jobs/checklists/live-validation.md`.

  Starting a real full rehearsal needs the operator's explicit go-ahead.

---

## Dependencies & Execution Order

- **Setup, then Foundational**, which blocks everything.
- **US1 (P1)**: needs Foundational.
- **US2 (P1)**: needs US1 (a started run).
- **US4 (P2)**: needs US1 and US2. The baseline is taken at start and compared during status.
- **US3 (P2)**: needs US2 (final state) and US4 (guard findings), and reuses feature 002's compare.
- **US5 (P3)**: needs US1. It is independent of US3 and US4, apart from calling finalize.
- **US6 (P3)**: its mechanism is in US1 (T015, T016). The test phase can run right after US1.
- **Polish**: after the stories. T038 needs the VPS setup.

```text
Setup → Foundational → US1 → US2 ─┬─ US4 → US3 ─┐
                        ├── US6 ──┤             ├─ Polish
                        └── US5 ──┴─────────────┘
```

Within each story, write tests first, then the pure modules (progress, params), then the remote ones (integrity, launcher, guard), then orchestration (`runs.py`), then the tools. `runs.py` and `tools.py` are shared files, so tasks touching them run one after another.

### Parallel Opportunities

- **Phase 2**: T003, T005 and T007 in parallel, then T004, T006, T008, T009 and T010.
- **US1**: tests T011, T012 and T013 together. T014 (integrity) and T015 (params) together.
- **US2**: T019 and T020 together. T021 (progress parser) alongside T022 (launcher state).
- **Across stories**: after US1, US5 and US6 tests can be written in parallel with US2.

## Parallel Example: User Story 1

```bash
Task: "T011 integrity tests in tests/unit/jobs/test_integrity.py"
Task: "T012 launcher argv golden tests in tests/unit/jobs/test_launcher_argv.py"
Task: "T014 integrity in src/vps_mcp/jobs/integrity.py"
Task: "T015 params in src/vps_mcp/jobs/params.py"
```

## Parallel Example: User Story 2

```bash
Task: "T019 progress parser tests in tests/unit/jobs/test_progress.py"
Task: "T021 progress parser in src/vps_mcp/jobs/progress.py"
```

---

## Implementation Strategy

### MVP (US1 + US2): start a job and watch it

1. Setup and Foundational.
2. US1, then **validate** quickstart 1–4 on fakes.
3. US2, then **validate** quickstart 5. The operator can now run the full rehearsal from Claude and follow it without SSH.

### Then

4. US4 (guard), then US3 (one report with a verdict).
5. US5 (cancel) and US6 (parameter tests).
6. Polish, including the negative suite and the README. Live validation is plan-only until the operator explicitly approves a real run.

## Notes

- **No raw input**: no task may add a tool or path that accepts a script, command or free-form argument from the caller.
- **Starting jobs**: every start goes through plan, confirm and `run_db_action`, so it is audited, refused in read-only mode, and checked for concurrency.
- **Privacy**: use generic names everywhere in the repo. Real names belong only in the operator's local config.
