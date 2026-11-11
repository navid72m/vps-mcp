# Contract: Migration MCP Tools

These are 13 new tools on the existing `vps` MCP server. The conventions are the same as in [feature 001 contracts](../../001-vps-service-mcp/contracts/mcp-tools.md):
- the success envelope `{ok, …}`, and the error envelope with categories;
- compact JSON output;
- an audit record for every state change.

New error categories: none. `conflict`, `insufficient_disk`, `confirmation_required`, `not_permitted`, `not_found` and `failed` are reused.

`Db` = an allowlisted target name, or the name of a copy created by this server.

## Read-only tools (`readOnlyHint: true`)

| Tool | Input | Output | Spec |
|---|---|---|---|
| `db_list` | `{}` | `{targets:[{name, purpose, size_bytes}], copies:[CopySummary]}` | US1, US6 |
| `db_overview` | `{db}` | `{size_bytes, tables, largest:[{table, est_rows, bytes}] (20), modules:{by_state:{}, by_major:{}}, indicators:{transitional:[], code_missing:[], inactive_views:n, orphaned_views:n}}` | FR-007, FR-010 |
| `db_modules` | `{db, state?, major?, code_missing?: bool}` | `{modules:[{name, state, version, code: bool}]}` | FR-008 |
| `db_table` | `{db, table}` (`schema.table` or `table`) | `{columns:[{name, type, nullable, default}], indexes:[], constraints:[], est_rows, bytes}` | FR-009 |
| `db_compare` | `{before, after, exact_counts?: bool = true}` | `ReportSummary` | FR-011–FR-015 |
| `get_report` | `{report_id, severity?, category?, page?: int = 1}` | `{summary: ReportSummary, findings: Finding[] (50 per page), page, pages}` | FR-014 |
| `list_reports` | `{limit?: 1..100 = 20}` | `{reports:[{id, kind, created_at, verdict, inputs}]}` | FR-014 |
| `rehearsal_status` | `{rehearsal_id?}` (default: the latest) | `{rehearsal_id, status, step_results, copy, report_id?}` | FR-021 |
| `pipeline_last_run` | `{pipeline}` | `RunSummary` (data-model §6) | FR-024 |

`ReportSummary` = `{report_id, verdict, counts, top_findings: Finding[] (≤20), skipped_checks, step_results?, check_results?}`

## State-changing tools

All of these are refused in read-only mode. They need `confirm=true`, and without it they return a plan or preview. Each attempt writes one audit record. The audit action names are new: `copy`, `rehearse`, `run_check`, `drop_copy`, `cancel_rehearsal`.

| Tool | Input | Preview (confirm=false) | Applied (confirm=true) | Spec |
|---|---|---|---|---|
| `rehearse` | `{pipeline, source, method?: auto\|template\|dump, plan_hash?, confirm}` | `{plan_hash, plan:{source, copy_name, method, steps:[{name, kind, file, sha256, guarantee: bound\|detected}], runner_sha256, est_copy_bytes, free_bytes_before, free_bytes_after, min_free_bytes}}` | `{rehearsal_id, status:"copying", unit}` | FR-016–FR-023 |
| `cancel_rehearsal` | `{rehearsal_id, confirm}` | `{will_stop: unit, current_step}` | `{status:"cancelled"}` | US3 |
| `run_check` | `{check, db, confirm?}` | (read-only checks run immediately on any Db; `may_modify` checks need a copy plus confirm) | `{check, status: pass\|fail\|timeout, exit_code, duration_s, output_tail, report_id?}` | FR-025 |
| `drop_copies` | `{copy_ids?: [], expired_only?: bool, confirm}` | `{will_drop:[CopySummary], reclaim_bytes}` | `{dropped:[], reclaimed_bytes, refused:[{name, reason}]}` | FR-026, FR-027 |

## Rules for `rehearse`

- **Plan hash**: `confirm=true` requires the `plan_hash` from a preview made within the last 30 minutes. If anything changed since (a file sha, free disk going below the minimum, or the source's connection state flipping the method), the request is refused with `conflict` and a fresh plan is returned.
- **Refusals**:
  - `insufficient_disk` when `free_bytes_after < min_free_bytes` (FR-018);
  - `conflict` when another rehearsal is active (FR-022);
  - `not_permitted` when the source isn't an allowlisted target (copies of copies are not allowed);
  - `invalid_input` when an `sql` step contains a forbidden construct (research M5).
- **Results**: when the rehearsal ends, `rehearsal_status` and `get_report` show the results. The report is created lazily, on the first status call after the rehearsal reaches a terminal state.

## Rules for `run_check`

- The `db` must be an allowlisted target or a copy.
- `access=read_only` checks always run read-only. If the write detector sees any change, the result is a blocking `foreign_write` finding.
- `access=may_modify` checks on a non-copy database fail with `not_permitted`.
