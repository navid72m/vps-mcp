# Quickstart & Validation: Odoo Migration Analysis & Rehearsal

Tools are defined in [contracts/migration-tools.md](./contracts/migration-tools.md), and config in [contracts/migration-config.md](./contracts/migration-config.md).

## Prerequisites

- Feature 001 is set up (the `vpsmcp` user, the pinned host key, `~/.config/vps-mcp/<vps>.toml`).
- **Disk**: a rehearsal of an 8.5 GB main DB needs about 11 GB plus the minimum free (default 10 GB). With today's 14 GB free, **free space first**. For example, drop obsolete test databases yourself, or set `min_free_gb` lower knowingly.

## One-time VPS setup (as root, after reviewing)

```bash
uv run vps-mcp bootstrap --print-sudoers --with-migration > vps-mcp.sudoers   # review, then install + visudo -c
uv run vps-mcp bootstrap --print-migration-sql                                # CREATE ROLE vpsmcp … GRANT pg_read_all_data …
# on the VPS:
sudo -u postgres psql -p 5435 -f vps-mcp-migration.sql
setfacl -m u:vpsmcp:rx /opt/odoo
```

Then add a `[migration]` section to `<vps>.toml` (start from the contract example) and run `vps-mcp check-config`.

## Automated checks

```bash
uv run pytest tests/unit/migration tests/contract tests/negative   # no VPS needed
```

The expected results include:
- the planted-defect set → 100% detected, verdict `do_not_promote` (SC-003);
- the migration negative suite (≥ 30 cases) → zero modifications to non-copy databases (SC-004).

## Manual scenarios (on the VPS, through Claude Code)

| # | Story | Ask | Expected |
|---|---|---|---|
| 1 | US1 | "Overview of shop_19_initial" | Under 30 s: size ≈ 8.8 GB, largest tables, module counts by major version (SC-001) |
| 2 | US1 | "Which modules are still at 17.x in shop_19_repaired, and do they have code?" | A list with `code: true/false` |
| 3 | US1 | "Overview of shop_19" (not allowlisted) | `not_permitted` |
| 4 | US2 | "Compare shop_17 with shop_19_initial" | A report under 5 min, with a verdict and top findings, retrievable via `get_report` (SC-002) |
| 5 | US2 | Same comparison again | Identical finding list (FR-015) |
| 6 | US4 | "How did the last deploy-v19-core run go?" | Steps from `STEPS.txt`, plus the top errors from the `upgrade_v19_*` logs |
| 7 | US3 | "Rehearse deploy-v19-core on shop_17" with too little disk | `insufficient_disk`, and nothing created |
| 8 | US3 | Same, after freeing space: preview | A plan with method, step sha256s, runner sha256 and disk before/after |
| 9 | US3 | Confirm, then close Claude Code, reopen later and ask "status of the rehearsal" | It kept running, and status/steps are shown (FR-021) |
| 10 | US3 | A second rehearsal while one is running | `conflict` |
| 11 | US3 | After it finishes | A report with step results and verdict; the `shop_17` write counters are unchanged |
| 12 | US5 | "Run post-upgrade-checks on the copy" | Pass/fail with output tail, added to the report |
| 13 | US6 | "List copies" / "drop expired copies" | Only `vmcp_*` copies with a valid marker; the reclaimed space is reported |
| 14 | US6 | Try to drop `shop_19_initial` | Refused: not a copy |
| 15 | Safety | Edit `data_cleanup.sql` between preview and confirm | `conflict`, and a new plan with the changed sha256 |
