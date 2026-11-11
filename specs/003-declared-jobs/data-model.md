# Data Model: Declared Jobs

**Feature**: [spec.md](./spec.md) | **Research**: [research.md](./research.md)

This builds on [feature 002's data model](../002-odoo-migration-analysis/data-model.md) (reports, findings, write counters) and [feature 001's](../001-vps-service-mcp/data-model.md) (managed services, audit).

## 1. Configuration (`[[migration.job]]`)

| Field | Type | Rules / default |
|---|---|---|
| `name` | str | `^[a-z0-9-]{1,40}$`, unique among jobs |
| `script` | abs path | required; no `..` |
| `run_as` | user | default `root`; must be in the allowed run-as set |
| `progress_file` | abs path | required |
| `log_globs` | list[abs path] | optional; wildcards only in the file name |
| `report_file` | abs path? | optional |
| `may_modify` | list[db] | each must be a `[[migration.target]]` name |
| `services` | list[name] | each must be a declared `[[service]]` name (feature 001) |
| `source` / `result` | db? | both must be targets; `result` ∈ `may_modify` |
| `params` | list[ParamDecl] | at most 10 |
| `min_free_gb` | float | default 5 |
| `max_runtime_h` | float | default 24, range 0.1–168 |
| `success_marker` | str | default `ALL DONE`, 1–80 printable characters |
| `outcome_patterns` | list[regex] | optional; named groups `step`, `status` (`ok`/`failed`/`done`) |

**ParamDecl**: `name` (`^[A-Z][A-Z0-9_]{0,31}$`, not on the deny list in research J7), `pattern` (a regex that must compile and is matched in full), `default` (str, may contain `{date:<strftime>}`), `description` (≤ 200 characters).

## 2. Job run (local table `job_runs`, schema v3)

| Column | Notes |
|---|---|
| `run_id` | `job_<12hex>`, primary key |
| `job` | the declaration name |
| `plan_hash` | 16-hex hash of the plan that was approved |
| `plan_json` | script sha256, mode/owner, params, declared databases (sizes) and services (states), disk, guard scope, creation time |
| `status` | see the state machine below |
| `unit` | `vps-mcp-job-<run_id>` |
| `rundir` | `/var/lib/vps-mcp/jobs/<run_id>` |
| `progress_offset` | byte offset in `progress_file` at start (J4) |
| `baseline_json` | guard baseline (J5) |
| `last_guard_json` | latest guard readings |
| `started_at`, `ended_at`, `exit_code`, `systemd_result` | |
| `cancel_requested` | bool |
| `report_id`, `audit_id` | |

**State machine**:

```text
planned ─(confirm, plan ≤30 min, re-checks pass)─▶ running ─┬─▶ succeeded
   │                                                        ├─▶ failed
   └─(expires / superseded)                                 ├─▶ timed_out   (RuntimeMaxSec)
                                                            └─▶ cancelled   (cancel_job)
final states ──▶ report built once (report_id set)
```

At most one row may be `running`. This is enforced together with feature 002's rehearsals and the VPS unit/process checks (research J6).

## 3. Progress line (computed)

```text
ProgressLine { ts: "YYYY-MM-DD HH:MM:SS" | null, text: str, step: str | null,
               outcome: "ok" | "failed" | "done" | null, disk_free: str | null,
               reported_failures: int | null }
```

## 4. Findings added by this feature (on top of feature 002's categories)

| Category | Severity |
|---|---|
| `step_failed` (an outcome line with failed) | blocking |
| `job_not_succeeded` (the final state is not `succeeded`) | blocking |
| `undeclared_db_write` | blocking |
| `undeclared_db_write_unattributed` | warning |
| `undeclared_db_dropped` | blocking |
| `undeclared_service_change` | blocking |
| `job_reported_failures` (`N FAIL`, N > 0) | warning |
| `script_changed_during_run` | warning |

The report's `kind` is `"job"`, and its `inputs` are `{job, run_id, source?, result?}`. It adds `step_results`, `report_excerpt` and `job_state`.
