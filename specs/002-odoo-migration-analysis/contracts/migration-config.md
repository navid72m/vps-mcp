# Contract: `[migration]` Policy Section (TOML)

This section is optional. Without it, no migration tools are registered. It is validated by `vps-mcp check-config`, and unknown keys are errors (the same rule as feature 001).

```toml
[migration]
port = 5435
min_free_gb = 10            # refuse/abort below max(min_free_gb, min_free_pct% of disk)
min_free_pct = 10
copy_growth_factor = 1.3    # -u all rewrites tables
copy_retention_h = 24
analysis_timeout_s = 300
step_timeout_s = 21600
approved_dirs = ["/opt/odoo/custom-addons/_migration"]

[migration.thresholds]
row_drop_warn_pct = 5
row_drop_block_pct = 20
exact_count_trigger_pct = 2

[[migration.target]]
name = "shop_17"
purpose = "dump"
[[migration.target]]
name = "shop_19_initial"
purpose = "test"
[[migration.target]]
name = "shop_19_repaired"
purpose = "test"

[migration.odoo]
python = "/opt/odoo/venv312/bin/python3.12"
odoo_bin = "/opt/odoo/odoo19/odoo-bin"
config = "/etc/odoo.conf"
pythonpath = ["/opt/odoo/custom-addons/lib"]
addons_paths = ["/opt/odoo/custom-addons", "/opt/odoo/odoo19/addons", "/opt/odoo/odoo19/odoo/addons"]
run_as = "odoo"

[[migration.pipeline]]
name = "deploy-v19-core"
progress_file = "/root/upgrade_logs/STEPS.txt"
log_globs = ["/var/log/odoo/upgrade_v19_*.log"]
steps = [
  { name = "data-cleanup",  kind = "sql",          file = "/opt/odoo/custom-addons/_migration/data_cleanup.sql" },
  { name = "orphan-views",  kind = "script",       file = "/opt/odoo/custom-addons/_migration/drop_orphaned_view_children.sh", run_as = "root" },
  { name = "upgrade-all",   kind = "odoo_upgrade", modules = "all" },
  { name = "re-upgrade-17", kind = "odoo_upgrade", modules = "stale", rounds = 3 },
]

[[migration.check]]
name = "post-upgrade-checks"
command = ["/opt/odoo/venv312/bin/python3.12", "/opt/odoo/custom-addons/_migration/post_upgrade_checks.py", "--db", "{db}", "--port", "{port}"]
run_as = "odoo"
access = "read_only"
blocking = true
timeout_s = 1800
```

## Validation rules

1. Target names must match `^[a-z_][a-z0-9_]{0,62}$`, must not start with `vmcp_`, and must be unique.
2. Every `file`, and `command[1:]` entries that are paths, must be absolute, contain no `..`, and resolve inside `approved_dirs`. `command[0]` must be `odoo.python` or be inside `approved_dirs`.
3. Check placeholders: only `{db}` and `{port}`, and only as whole argv elements, or as suffixes such as `--db={db}`.
4. `odoo_upgrade` steps need `[migration.odoo]`. `rounds` is only allowed with `modules = "stale"`.
5. `run_as` must be one of `root`, `postgres`, `odoo`, `vpsmcp`, or a user listed in `allowed_run_as`.
6. Steps and pipelines are capped (≤ 30 steps per pipeline, ≤ 10 pipelines, ≤ 20 checks).
7. `0 < row_drop_warn_pct < row_drop_block_pct ≤ 100`.
8. Server-side checks (they need the VPS, so they run at startup and in `rehearse` previews): files exist, and step SQL files contain no forbidden constructs (research M5).

## Mapping from the operator's current scripts

| `upgrade_v19.sh` | Declared as |
|---|---|
| step 5 `data_cleanup.sql` | `sql` step |
| step 5 `drop_orphaned_view_children.sh` (reads `$PSQL`, `$REPO`, `$OUT`) | `script` step. The runner provides `PSQL`, `DB`, `PGDATABASE`, `REPO` (from `odoo.addons_paths[0]`) and `OUT` (inside the rehearsal dir). |
| step 6 `-u all` | `odoo_upgrade` with `modules = "all"` |
| step 7 re-upgrade loop | `odoo_upgrade` with `modules = "stale"`, `rounds = 3` |
| final "start test server" | not included (out of scope for v1) |
