# Contract: Job Tools

These are 4 tools, registered only when at least one `[[migration.job]]` is declared. Envelopes, errors and audit behaviour are the same as in [feature 001](../../001-vps-service-mcp/contracts/mcp-tools.md) and [feature 002](../../002-odoo-migration-analysis/contracts/migration-tools.md).

| Tool | Annotations | Input | Output |
|---|---|---|---|
| `list_jobs` | read-only | `{}` | `{jobs: [{name, script, may_modify, services, params:[{name, pattern, default, description}], source?, result?}], runs: [RunSummary] (latest 10)}` |
| `run_job` | write, not destructive-hinted* | `{job, params?: {NAME: value}, confirm: bool, plan_id?: str}` | Preview: `{plan_id, plan}`. Applied: `{run_id, status: "running", unit}`. |
| `job_status` | read-only | `{run_id?}` (default: the latest run) | `{run_id, job, status, elapsed_s, steps: [ProgressLine], recent: [ProgressLine] (≤50), disk_free?, guard_findings, console_tail (≤20, masked), report?: ReportSummary}` |
| `cancel_job` | destructive | `{run_id, confirm: bool}` | Preview: `{will_stop: unit, current_step, warning}`. Applied: `{status: "cancelled", left_behind: {services: {name: state}, databases: {name: exists/size}}}` |

\* `run_job` changes data that the operator declared, so its description says so explicitly. The confirmation step is the actual safeguard.

## `run_job` plan

```text
plan = {
  job, script, script_sha256, script_mode, script_owner, script_mtime,
  run_as, params: {NAME: value},                       # resolved, validated
  may_modify: [{db, exists, size_bytes}],               # "will be dropped/rebuilt"
  services: [{name, state}],                            # "may be stopped/started"
  guarded: {databases: [...], services: [...]},         # watched for undeclared changes
  disk: {free_bytes, min_free_bytes},
  max_runtime_h, success_marker,
  running: null | {kind: job|rehearsal|hand_started, id_or_pid},
  notes: ["the script runs as <user> and cannot be confined; undeclared changes are detected, not prevented", …]
}
```

**Refusals**:
- `not_found`: the job is not declared.
- `invalid_input`: an undeclared parameter, or a value that doesn't match its pattern.
- `insufficient_disk`: free disk is below the declared minimum.
- `not_permitted`: the script or its directory is writable by group/others, or the owner is wrong.
- `conflict`: something is already running (research J6), or the `plan_id` is unknown, older than 30 minutes, or no longer matches (the script's sha, owner or mode changed). In that case a fresh plan is returned.
- `read_only`: the server is in read-only mode.

## Audit

The audit actions are `run_job` and `cancel_job` (one record per attempt). Status reads are not audited.
