# Quickstart & Validation: Declared Jobs

The tools are described in [contracts/job-tools.md](./contracts/job-tools.md) and the configuration in [contracts/job-config.md](./contracts/job-config.md).

## Prerequisites

- **Feature 002 VPS setup is done**: the read-only role, plus the migration sudoers additions.
- **Jobs sudoers block installed**: `vps-mcp bootstrap --print-sudoers --with-migration --with-jobs` adds `/usr/bin/stat`.
- **Configuration declares**:
  - the job's `may_modify`, `source` and `result` databases as `[[migration.target]]`s;
  - its `services` as `[[service]]`s.
- **Optional**: to use parameters, make the script read them, e.g. `R=${RUN_NAME:-rehearsal_$(date +%Y%m%d)}`.

## Automated

```bash
uv run pytest tests/unit/jobs tests/negative/test_jobs_disallowed.py
```

**Expected**:
- progress-parsing fixtures match exactly, including appended-across-runs files;
- the guard planted-side-effects test produces 3 blocking findings (SC-003);
- the negative suite (≥ 20 cases) starts no unit (SC-004).

## Manual scenarios (through Claude Code, on the VPS)

| # | Story | Ask | Expected |
|---|---|---|---|
| 1 | US1 | "Plan the full rehearsal" | A plan that names `shop_19_test` (will be rebuilt), `odoo-test`, the script sha and free disk. Nothing runs. |
| 2 | US1 | Edit the script, then confirm the old plan | `conflict`, plus a fresh plan with the new sha |
| 3 | US1 | `chmod o+w` the script, then plan | `not_permitted`. Undo the chmod afterwards. |
| 4 | US1 | Confirm a valid plan | `running`, with a `vps-mcp-job-…` unit on the VPS |
| 5 | US2 | Close Claude, reopen it, ask "how's the rehearsal going?" | Steps since this start only (no lines from earlier runs), disk free, elapsed time |
| 6 | US1 | Plan again while it runs | `conflict` |
| 7 | US3 | After `ALL DONE`: "is the result safe to promote?" | One report: step outcomes, `job_reported_failures` from `COMPARE SUMMARY … FAIL`, an excerpt of `compare_migration.md`, the source-vs-result comparison and a verdict |
| 8 | US4 | (test VM only) a job that writes to an undeclared target | blocking `undeclared_db_write` |
| 9 | US5 | Start, then "stop the rehearsal" and confirm | `cancelled`; no `vps-mcp-job` unit left and `pgrep` empty; `left_behind` shows `odoo-test` stopped |
| 10 | US6 | "Run it with RUN_NAME=bad name!" | `invalid_input` |
