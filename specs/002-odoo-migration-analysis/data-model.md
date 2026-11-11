# Data Model: Odoo Migration Analysis & Rehearsal

**Feature**: [spec.md](./spec.md) | **Research**: [research.md](./research.md)

This extends feature 001's data model ([../001-vps-service-mcp/data-model.md](../001-vps-service-mcp/data-model.md)). The existing policy, audit and secret-masking rules apply unchanged.

## 1. Configuration (`[migration]` in the policy TOML)

The full schema is in [contracts/migration-config.md](./contracts/migration-config.md).

| Entity | Key fields | Rules |
|---|---|---|
| **MigrationSettings** | `port`, `min_free_gb`, `min_free_pct`, `copy_growth_factor`, `copy_retention_h`, `analysis_timeout_s`, `step_timeout_s`, `thresholds` | `port` is 1–65535. Defaults: `min_free_gb` 10, `min_free_pct` 10, `copy_growth_factor` 1.3, `copy_retention_h` 24, `analysis_timeout_s` 300, `step_timeout_s` 21600 (6 h). |
| **RiskThresholds** | `row_drop_warn_pct` (5), `row_drop_block_pct` (20), `exact_count_trigger_pct` (2) | `0 < warn < block ≤ 100` |
| **AnalysisTarget** | `name`, `purpose` (`dump`/`test`/`reference`) | `name` matches `^[a-z_][a-z0-9_]{0,62}$`, must not start with `vmcp_`, and must be unique. |
| **OdooProfile** | `python`, `odoo_bin`, `config`, `pythonpath[]`, `addons_paths[]`, `run_as` | Absolute paths only. `run_as` is a valid unix user. |
| **PipelineDefinition** | `name`, `steps[]`, `progress_file?`, `log_globs[]`, `progress_patterns?` | `name` matches `^[a-z0-9-]{1,40}$`, and there are 1–30 steps. |
| **StepDefinition** | `name`, `kind` (`sql`/`script`/`odoo_upgrade`), `file?`, `modules?` (`all`/`stale`), `rounds?` (1–5), `run_as?`, `timeout_s?` | `file` is required for `sql`/`script`, and must be under one of `approved_dirs`. `modules` is required for `odoo_upgrade`. |
| **CheckDefinition** | `name`, `command[]`, `run_as`, `access` (`read_only`/`may_modify`), `blocking` (false), `timeout_s` | `command[0]` is under `approved_dirs` or is a declared interpreter. The only placeholders allowed are `{db}` and `{port}`. |
| **approved_dirs** | list of absolute dirs | Step and check files must resolve (realpath) inside one of them. |

## 2. Snapshot (captured, stored locally)

```text
Snapshot {
  id: "snp_<12hex>", database: str, is_copy: bool, taken_at: datetime,
  size_bytes: int,
  tables: { "<schema>.<table>": Table },
  modules: { "<name>": {state, version} } | null      # null if not an Odoo DB
  views: { "<xmlid or id>": {active, inherit_xmlid, module} } | null
  stats: {tup_inserted, tup_updated, tup_deleted, xact_commit}
}
Table { columns: {name: Column}, indexes: [str], constraints: [str],
        est_rows: int, exact_rows: int|null, total_bytes: int }
Column { type: str, nullable: bool, default: str|null }
```

Snapshots are immutable. A table's identity is `schema.table`. Column types are normalised using `format_type()`, so comparisons are textual.

## 3. Finding and Report

```text
Finding {
  severity: "blocking" | "warning" | "info",
  category: one of CATEGORIES,
  object: str,            # e.g. "public.res_partner.vat", "module:hr_payroll"
  message: str,           # plain language
  values: {..}            # measured numbers: before/after rows, old/new type…
}
Report {
  id: "rpt_<12hex>", created_at, kind: "compare" | "rehearsal" | "single",
  inputs: {before: snapshot_id, after: snapshot_id, rehearsal_id?},
  verdict: "safe_to_promote" | "review_warnings" | "do_not_promote",
  counts: {blocking, warning, info},
  findings: [Finding]     # sorted (severity, category, object)
  skipped_checks: [str]   # e.g. "modules: before is not an Odoo database"
  step_results: [StepResult]? , check_results: [CheckResult]?
}
```

The **verdict** is `do_not_promote` if any finding is blocking, `review_warnings` if any is a warning, and otherwise `safe_to_promote`.

### Finding categories and default severity (FR-012)

| Category | Condition | Severity |
|---|---|---|
| `table_removed` | the table existed before and is gone; before rows > 0 | **blocking** (info if it had 0 rows) |
| `column_removed` | the column is gone; the table had > 0 rows | **blocking** (info if empty) |
| `type_narrowed` | a narrowing rule matched (research M3) | warning |
| `type_changed` | another type change | info |
| `type_widened` | a widening rule matched | info |
| `row_drop` | exact rows fell by more than `row_drop_block_pct` | **blocking** |
| `row_drop` | exact rows fell by more than `row_drop_warn_pct` | warning |
| `table_added`, `column_added`, `index_*`, `constraint_*` | — | info |
| `constraint_removed` | a FK, unique or check constraint was removed | warning |
| `module_not_upgraded` | the after version still has the before major version prefix (e.g. `17.`) and the module is installed | **blocking** |
| `module_transitional` | the after state is `to upgrade`, `to install` or `to remove` | **blocking** |
| `module_removed` | installed before, absent or uninstalled after | warning |
| `module_code_missing` | installed but no directory in the addons paths | warning |
| `view_deactivated` | active before, inactive after | warning |
| `view_orphaned` | an inherited view whose parent or owning module isn't installed | warning |
| `step_failed` | a rehearsal step failed or timed out | **blocking** |
| `foreign_write` | the write detector saw a non-copy database change during a step | **blocking** |
| `check_failed` | a declared check failed | warning (**blocking** if the check has `blocking=true`) |
| `disk_abort` | the rehearsal was aborted for low disk | **blocking** |

## 4. Copy (local table `migration_copies`)

| Column | Notes |
|---|---|
| `copy_id` | `cpy_<12hex>`, primary key |
| `name` | `vmcp_<source[:30]>_<YYYYmmddHHMM>_<4hex>`, unique |
| `source` | the AnalysisTarget name |
| `method` | `template` \| `dump` |
| `marker` | `vps-mcp-copy:<copy_id>:<hmac-sha256(key, copy_id+name)[:32]>` |
| `created_at`, `expires_at`, `dropped_at?` | |
| `size_bytes?`, `rehearsal_id?`, `report_id?` | |

**Lifecycle**: `creating → ready → (in_use by a rehearsal) → ready → dropped`. A copy can also go `creating → failed (cleaned up)`. Only the `ready` and `failed` states may be dropped. A drop requires the stored marker to equal the live `obj_description` of the database.

## 5. Rehearsal (local table `migration_rehearsals`)

| Column | Notes |
|---|---|
| `rehearsal_id` | `rhs_<12hex>` |
| `pipeline`, `source`, `copy_id` | |
| `plan_json` | The exact plan shown to the operator: steps, file sha256s, runner sha256, method, size estimate, free disk before and after |
| `status` | see below |
| `unit` | `vps-mcp-rehearsal-<id>` |
| `step_results_json` | `[{name, kind, status: ok\|failed\|timeout\|skipped\|not_run, exit_code, started, ended, log_tail}]` |
| `report_id?`, `audit_id` | |

**State machine**:

```text
planned ──confirm──▶ copying ──▶ running ──▶ succeeded ─┐
   │                    │           ├──▶ failed ────────┤
   │                    │           ├──▶ timed_out ─────┼──▶ reported
   │                    │           ├──▶ aborted_disk ──┤
   │                    │           └──▶ cancelled ─────┘
   │                    └──▶ copy_failed (copy cleaned up)
   └── (plan expires after 30 min unconfirmed; the plan hash must match on confirm)
```

There can be at most one rehearsal in `copying` or `running` at a time. This is checked against the local table **and** against the VPS (`systemctl is-active vps-mcp-rehearsal-*`).

## 6. Pipeline run summary (computed, not stored)

```text
RunSummary { pipeline, progress_file, started_at?, steps: [{name, status, raw}],
             errors: {count_by_level, top: [{message, count, first_seen, last_seen}]},
             truncated: bool }
```
