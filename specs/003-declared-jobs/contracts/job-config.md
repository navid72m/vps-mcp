# Contract: `[[migration.job]]` (TOML)

The field rules are in [data-model.md §1](../data-model.md#1-configuration-migrationjob). This is an example, with generic names:

```toml
[[migration.job]]
name = "full-rehearsal"
script = "/root/full_rehearsal.sh"
run_as = "root"
progress_file = "/root/rehearsal_logs/STEPS.txt"
log_globs = ["/root/rehearsal_logs/*.log", "/root/rehearsal_logs/*.out"]
report_file = "/root/rehearsal_logs/compare_migration.md"
may_modify = ["shop_19_test"]            # will be dropped and rebuilt
services = ["odoo-test"]                 # may be stopped/started by the job
source = "shop_17"                       # compared against result when the job succeeds
result = "shop_19_test"
min_free_gb = 5
max_runtime_h = 24
success_marker = "ALL DONE"

[[migration.job.params]]
name = "RUN_NAME"
pattern = "^[a-z0-9_]{1,40}$"
default = "rehearsal_{date:%Y%m%d_%H%M}"
description = "Pipeline run name (needs the script to read ${RUN_NAME})"
```

## Validation (`vps-mcp check-config`)

1. Job names must be unique and match `^[a-z0-9-]{1,40}$`.
2. `script`, `progress_file`, `report_file` and `log_globs` must be absolute paths with no `..`. In globs, wildcards are allowed only in the last path component.
3. `may_modify`, `source` and `result` must be `[[migration.target]]` names, and `result` must be in `may_modify`.
4. Each entry in `services` must name a declared `[[service]]`. The remote-access units can never be listed.
5. `run_as` must be in the allowed set (feature 002 rule 5).
6. Parameter names must match `^[A-Z][A-Z0-9_]{0,31}$` and not be on the deny list (research J7). Patterns must compile. If a default contains no `{date:…}`, it must match its own pattern.
7. `outcome_patterns` must compile, and may only use the named groups `step` and `status`.
8. `max_runtime_h` must be between 0.1 and 168, and `min_free_gb` must be at least 0.

**Server-side checks**: run at plan time and again at start: the script exists, its owner and mode are acceptable (FR-005), and its sha256 is recorded.
