# Implementation Plan: Declared Jobs

**Branch**: `003-declared-jobs` | **Date**: 2026-11-11 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/003-declared-jobs/spec.md`

## Summary

This adds four tools (`list_jobs`, `run_job`, `job_status`, `cancel_job`). They let the assistant run scripts the operator declared in the policy file, such as a full-rehearsal script, and nothing else.

- **Starting a job**:
  - The script is fingerprinted, and its owner and mode are checked.
  - A plan lists what the job will rebuild and stop, and must be confirmed.
  - The job then runs as a **transient systemd unit** (runtime cap, kills the whole process tree on cancel, parameters passed via `--setenv`, so no shell is involved).
- **Status**: read from the job's own progress file, **from the byte offset recorded at start**, because the reference file is appended to across runs.
- **Guard**: feature 002's write counters and database list, plus feature 001's service states, flag any undeclared change.
- **At the end**: one report combines step outcomes, the job's own report file and feature 002's source-vs-result comparison into a verdict.

Decisions J1–J10 are in [research.md](./research.md).

## Technical Context

**Language/Version**: Python 3.12+ (same package).

**Primary Dependencies**: none new. On the VPS: systemd ≥ 240 (`StandardOutput=append:`), plus `stat` and `pgrep`.

**Storage**: SQLite schema v3 adds the `job_runs` table. On the VPS, `/var/lib/vps-mcp/jobs/<run_id>/` holds `console.log` and `exit_code`.

**Testing**: pytest.
- `FakeVps` gains `systemd-run` job units, `stat`, `pgrep` and `tail -c`.
- Progress-parser fixtures come from the reference script's real line format.
- Guard tests plant side effects.
- A negative suite of at least 20 cases.

**Target Platform**: the same VPS as features 001 and 002.

**Project Type**: an extension of the single package: a new `vps_mcp.jobs` subpackage.

**Performance Goals**: `job_status` responds in under 10 s (SC-002), using ≤ 6 remote calls: unit state, progress tail, console tail, write counters, database list, service states.

**Constraints**:
- no arbitrary commands or parameters;
- one job at a time, never alongside a rehearsal;
- no automatic repair;
- output masked and capped (50 lines, a 200-line excerpt).

**Scale/Scope**: about 1–5 declared jobs, 1 running at a time, runs lasting up to 24 h, progress files of up to about 1 MB.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

The constitution is still the unfilled template, so there are no ratified gates. **PASS**. The engineering defaults from features 001 and 002 apply:

- **Least privilege**: the only new sudo right is `stat`.
- **Testability**: parsing and verdict logic are pure functions.
- **Simplicity**: there is no generated runner. The operator's script runs as-is under systemd.
- **Observability**: every run and cancel is audited, and status persists.

**Post-design re-check**: PASS. The documented residual risks are:
- the script cannot be confined, but undeclared changes are detected;
- work done outside the job's process group (database backends, `docker exec`) is not killed by cancel.

## Project Structure

### Documentation (this feature)

```text
specs/003-declared-jobs/
├── plan.md  research.md  data-model.md  quickstart.md
├── contracts/ (job-tools.md, job-config.md)
├── checklists/requirements.md
└── tasks.md                  # /speckit-tasks
```

### Source Code (repository root)

```text
src/vps_mcp/
├── migration/config.py       # + JobDeclaration, ParamDecl in MigrationSettings.job (validation rules 1-8)
├── jobs/
│   ├── __init__.py
│   ├── progress.py           # line grammar + outcome rules (J4), pure
│   ├── integrity.py          # sha256 / stat owner+mode checks (J3)
│   ├── params.py             # param validation, {date:} defaults, deny-list (J7), pure
│   ├── launcher.py           # systemd-run argv (J1), unit state + exit code (J2), cancel (J9)
│   ├── guard.py              # baseline/compare: write counters, db existence, service states (J5)
│   ├── report.py             # final report assembly + verdict (J8)
│   ├── runs.py               # plan / confirm / status / cancel orchestration, concurrency (J6)
│   ├── store.py              # job_runs table accessors
│   └── tools.py              # 4 MCP tools
├── store/db.py               # schema v3 (job_runs)
├── server.py                 # register job tools when jobs are declared
└── cli.py                    # bootstrap --with-jobs (adds /usr/bin/stat)

tests/
├── fakes.py / fakepg.py      # + job units, stat, pgrep, tail -c
├── unit/jobs/                # progress, params, integrity, launcher argv, guard, report, flow
└── negative/test_jobs_disallowed.py
```

**Structure Decision**: Jobs live in their own subpackage, `vps_mcp.jobs`. They reuse feature 002's `MigrationRuntime` (database queries, store, `compare_snapshots`, `risk`) and feature 001's backends (for service states). Registration is conditional on `migration.job`, so existing configurations are unaffected.

## Implementation Phasing (for /speckit-tasks)

1. **Foundation**: configuration models and validation, schema v3, fake support, sudoers `--with-jobs`.
2. **US1 (P1)**: integrity checks, parameters, plan/plan_id, launch, concurrency, `list_jobs`, `run_job`.
3. **US2 (P1)**: progress parser (offset reads), unit state and exit code, `job_status`.
4. **US4 (P2)**: guard baseline, re-reads and findings, shown in `job_status`.
5. **US3 (P2)**: final report and verdict, built once on reaching a final state.
6. **US5 (P3)**: `cancel_job`, including `left_behind`.
7. **US6 (P3)**: parameter defaults with `{date:}`. This is partly covered in US1; here it gets full tests.
8. **Polish**: negative suite, README section, `examples/vps-mcp.toml` job example, and live validation (plan only, read-only) on the operator's VPS after the setup.

## Complexity Tracking

There are no constitution violations. The two residual risks above are documented and shown in every plan.
