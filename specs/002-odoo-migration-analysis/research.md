# Research: Odoo Migration Analysis & Rehearsal

**Feature**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md) | **Date**: 2026-11-11

Facts checked on the operator's VPS on 2026-11-11:
- PostgreSQL 18.6, cluster `18/main` on port 5435 (`postgresql@18-main`).
- `pg_hba.conf`: `local all all peer`.
- main databases are 8.5–9 GB each: `shop_17`, `shop_19`, `shop_19_initial`, `shop_19_repaired`, `shop_test19`.
- Root filesystem: 196 GB, 14 GB free.
- `/opt/odoo` is mode 0750 (owner `odoo`, with an ACL).
- The pipeline lives in `/opt/odoo/custom-addons/_migration/` (`deploy_v19.sh`, `data_cleanup.sql`, `drop_orphaned_view_children.sh`, `compare_migration.py`, `post_upgrade_checks.py`, `deep_smoke_test.py`, …) and in `a separate migration directory` (OpenUpgrade-based).
- Run progress is written to files like `/root/upgrade_logs/STEPS.txt` with lines such as `STEP5 cleanup ok`.
- `systemd-run`, `pg_dump`, `pg_restore`, `createdb` and `dropdb` are installed.

---

## M1. Read-only database access (FR-004)

**Decision**: Create a database role `vpsmcp` with `LOGIN`, granted `pg_read_all_data` and `pg_read_all_stats`. It has no `CREATEDB`, is not a superuser, and can write nothing.
- **Connection**: the OS user `vpsmcp` connects over the local Unix socket using **peer authentication**, so there's no password: `psql -X -p 5435 -d <db> -v ON_ERROR_STOP=1 -At -c <catalog query>`.
- **Session settings**: every analysis session also sets `default_transaction_read_only=on` and a `statement_timeout`, via `PGOPTIONS`.

**Rationale**:
- PostgreSQL itself enforces read-only access, at the privilege level and not just by convention. A bug in a query can't change data.
- Peer authentication matches the existing pg_hba rule, so no credentials are stored anywhere.

**Alternatives considered**:
- *`sudo -u postgres psql` for reads*: superuser for read-only work, with no database-level guarantee.
- *A password role over TCP*: adds a secret to manage.

## M2. Fixed query catalogue (FR-002)

**Decision**: All analysis SQL lives in code as named constants (`catalog.py`). Each query returns a single JSON document (`json_agg`/`jsonb_build_object`), so parsing is unambiguous.
- **No caller text in SQL**: parameters such as a table name are passed only as `psql -v` variables and referenced as `:'name'`, which psql quotes as a literal. They are also validated against the identifier regex `^[a-z_][a-z0-9_]{0,62}$`.
- **No raw SQL tool**: there is no tool that accepts SQL.

| Query | Source |
|---|---|
| overview | `pg_database_size`, `pg_class` (relkind `r`/`p`), `reltuples`, `pg_total_relation_size` |
| table detail | `information_schema.columns`, `pg_index`, `pg_constraint` |
| modules | `ir_module_module(name, state, latest_version)` |
| views | `ir_ui_view(active, inherit_id, mode)` joined with `ir_model_data` to find the owning module |
| row counts | `reltuples`; exact `count(*)` only for flagged tables, under `statement_timeout` |
| write detector | `pg_stat_database` (`tup_inserted`, `tup_updated`, `tup_deleted`, `xact_commit`) |

## M3. Snapshots and comparison engine (FR-011, FR-012, FR-015)

**Decision**: A **snapshot** is captured once per database and stored locally (SQLite, as JSON). It contains:
- tables, with columns, types and constraints;
- per-table row estimates;
- module states;
- view states.

The **diff** and **risk classification** are pure Python functions over two snapshots. Findings are sorted by `(severity, category, object)`.

**Exact counts**: when the estimated row change for a table crosses a threshold, a second pass counts exact rows for just those tables. Only these exact counts can trigger a row-loss blocking finding, which avoids false alarms from stale statistics.

**Type narrowing rules**:
- `varchar(n→m)` with `m < n`;
- `numeric` precision or scale decreasing;
- `text→varchar(n)`;
- `bigint→integer`;
- `timestamp tz→naive`;
- any change between type families.

Widening (the reverse of these) is info only.

**Rationale**: the diffing is easy to unit-test thoroughly and deterministic. Snapshots also make reports reproducible without re-querying.

**Alternatives considered**:
- *migra / pg-schema-diff*: these diff schema only, with no row or Odoo semantics, and would mean installing extra tools on the VPS.
- *The operator's `compare_migration.py`*: can be added as a declared check (M8), but it isn't the engine.

## M4. Disposable copies (FR-003, FR-006, FR-018, FR-026, FR-027)

**Decision**: Copies are created as the `postgres` OS user, through fixed command templates allowed by sudoers (`vpsmcp ALL=(postgres) NOPASSWD: /usr/bin/psql, /usr/bin/createdb, /usr/bin/dropdb, /usr/bin/pg_dump, /usr/bin/pg_restore`).

**Copy method**:
- **`template`** (fast, file-level): `createdb -p 5435 -T <source> -O <source owner> <copy>`. PostgreSQL requires the source to have **no other connections** while it copies.
- **`dump`** (slower, consistent snapshot, no locking): `pg_dump -Fc <source> | pg_restore -d <copy>`, run in one detached unit (M6).
- **`auto`** (the default): `template` if the source has zero active connections, otherwise `dump`. The plan states which method will be used. The server never terminates users' connections (spec edge case).

**Name**: `vmcp_<source[:30]>_<YYYYmmddHHMM>_<4 hex>`. The system generates it; callers never supply it.

**Ownership marker**: two independent proofs.
1. `COMMENT ON DATABASE <copy> IS 'vps-mcp-copy:<copy_id>:<hmac>'`. The HMAC key is generated locally and stored in the state directory (mode 0600).
2. A row in the local `migration_copies` table.

Drop and modify are allowed only when **both** match. A caller can't forge the comment, because no SQL is exposed. A human renaming a database can't make the server drop it, because the HMAC binds the name.

**Disk estimate**: `pg_database_size(source) × growth_factor`. The growth factor defaults to 1.3, because `-u all` rewrites tables. This is compared with `df` free space minus `min_free`, where `min_free = max(10%, 10 GB)` by default (FR-018).

> ⚠️ With today's 14 GB free and 8.5 GB sources, every rehearsal is refused under the defaults. The operator must free space (old `shop_*` test databases total about 35 GB) or lower `min_free_gb` knowingly. The quickstart shows how.

**Expiry (SC-005: no copy more than 1 h past expiry)**: the server isn't always running (stdio), so expiry can't rely on it alone.
- When a copy is created, the server also schedules a **transient systemd timer** on the VPS: `systemd-run --on-active=<retention> --unit vps-mcp-expire-<copy_id>`. The timer runs a fixed `dropdb` that first checks the marker comment (`psql … -c "SELECT obj_description(…)"`).
- A **lazy sweep** runs on every server start and every migration tool call, as a backstop for VPS reboots, which wipe transient timers.

**Alternatives considered**:
- *A ZFS/LVM snapshot*: not available on this disk layout.
- *A copy on another host*: out of scope (the assumption is one cluster).

## M5. Binding steps to the copy (FR-003)

**Decision**: each step kind gets the strongest guarantee it can have, and the plan says which one applies.

| Step kind | How it runs | Guarantee |
|---|---|---|
| `sql` (a file in an approved dir) | `psql -X -p 5435 -d <copy> -v ON_ERROR_STOP=1 -f <file>`, as postgres | **Bound**. The file is pre-scanned and refused if it contains `\c`/`\connect`, `dblink`, `postgres_fdw`, `COPY … PROGRAM`, or `ALTER SYSTEM`. |
| `odoo_upgrade` (`all` or `stale` modules, with a round limit) | `sudo -u odoo <python> <odoo-bin> -c <conf> -d <copy> --db-filter=^<copy>$ -u … --stop-after-init --no-http --workers 0 --max-cron-threads 0` | **Bound**. Built by the server; the database name is fixed. |
| `script` (a file in an approved dir) | Run with the environment `DB=<copy>`, `PGDATABASE=<copy>`, `PSQL="psql -p 5435 -d <copy>"`, `PGOPTIONS` unset, as the declared OS user | **Trusted + detected**. A script could ignore its environment, so the runner also applies the write detector (below). |

**Write detector** (for every step kind): before and after each step, the runner records `pg_stat_database` write counters for **every non-copy allowlisted database**. Any increase is a **blocking** finding: "step X wrote to database Y". The rehearsal then aborts.

**Rationale**: SQL files and Odoo upgrades can be truly confined. Arbitrary operator scripts can't be fully sandboxed without containers, so the next best thing is to detect writes immediately, and the plan says this honestly. The spec's assumption that operator pipeline files are trusted within a copy covers this.

**Alternatives considered**:
- *Running scripts in a container that can only reach the copy*: much stronger, but the operator's scripts use host paths and binaries. Left as a possible later hardening.

## M6. Detached, resumable rehearsals (FR-021, FR-022)

**Decision**: When a rehearsal is confirmed, the server does three things:
1. Renders a **runner script** from fixed templates. It contains each step's exact command, the disk check between steps, `timeout <step_timeout>` around each step, the write-detector queries, and status writes.
2. Uploads the runner (via `tee` + `install -m 0700`) to `/var/lib/vps-mcp/rehearsals/<id>/run.sh`. The runner's sha256 is part of the confirmed plan (FR-017/FR-020): confirming means confirming exactly this script.
3. Starts it with `systemd-run --unit vps-mcp-rehearsal-<id> --property=… /bin/bash …/run.sh`.

**While it runs**:
- **Status**: the runner writes `status.json` (`step i: running|ok|failed|timeout|skipped`, exit code, timestamps) and per-step logs to the rehearsal directory.
- **Polling**: the server polls `status.json` and reads log tails. Losing the SSH connection or stopping the MCP server doesn't affect the unit.
- **One at a time**: before starting, the server checks with `systemctl is-active 'vps-mcp-rehearsal-*'` and refuses a second rehearsal with `conflict`.
- **Fingerprint re-check**: the runner verifies each step file's sha256 right before running it, and stops if it changed (FR-020).
- **Cancel**: `systemctl stop vps-mcp-rehearsal-<id>` (needs confirmation). The current step is killed and the copy is kept.

**On completion**, the next migration tool call notices the terminal status. It then captures the copy snapshot, builds the source-vs-copy report (FR-023) and stores it.

**Rationale**: this is the only design that keeps running when the MCP server process is gone, and Claude Code and LM Studio start and stop it on demand. Nothing persistent is installed: the runner is generated per rehearsal, and the unit is transient.

**Alternatives considered**:
- *The server orchestrates step by step over SSH*: breaks when the client session ends during a multi-hour `-u all`.
- *`nohup`/`setsid`*: no reliable status, cancel or concurrency control.

## M7. Reading the operator's own pipeline runs (FR-024)

**Decision**: Each pipeline is declared with a `progress_file` and `log_globs`. The server reads them as follows:
- **Progress**: `sudo -n cat <progress_file>`. Lines are parsed with configurable regexes; the defaults match the `STEPn <name> ok|FAILED|rc=<n>|EXIT <n>` style.
- **Logs**: `sudo -n grep -E -h -m 20000 ' (ERROR|CRITICAL|WARNING) ' <files>`. The pattern is fixed; the files are declared paths only. The server parses the Odoo log line format (`date time,ms pid LEVEL db logger: message`), normalises messages (digits/ids → `#`, quoted values → `'…'`), and counts the top 10.
- **Size**: output is capped at 2 MB and marked as truncated.

`grep` needs adding to the sudoers profile, because the logs are root-owned.

## M8. Declared checks (FR-025)

**Decision**: A check is declared with `name`, `command` (an argv template that may only use the placeholders `{db}` and `{port}`), `run_as` (`odoo`|`postgres`|`vpsmcp`), `access` (`read_only`|`may_modify`) and `timeout_s`.
- **`read_only` checks** run with `PGOPTIONS='-c default_transaction_read_only=on'`, together with the write detector. A check that wrote anyway produces a blocking finding.
- **`may_modify` checks** may only target a copy.
- **Results**: exit 0 = pass. Anything else is a warning by default, or blocking if `blocking = true`. The output excerpt is masked with the existing secret rules.
- **sudoers**: one extra rule per `run_as` user for the declared interpreter.

## M9. Storage and reports (FR-014)

**Decision**: New tables in the existing SQLite state DB: `migration_snapshots`, `migration_reports`, `migration_copies`, `migration_rehearsals`. Reports are stored as JSON with a stable id (`rpt_<12 hex>`) and the same retention as the audit trail (90 days).
- **Size**: a snapshot of a production-sized database (~4–6k tables) is estimated at 2–5 MB of JSON, so the local state database grows by MBs per run, which is acceptable.
- **Tool output**: report tools return a **summary** (verdict, counts by severity, top 20 findings) by default. Full findings are paged, which keeps responses within the compact-output budget.

## M10. Sudoers additions

The setup material (`vps-mcp bootstrap --print-sudoers --with-migration`) adds:

```
vpsmcp ALL=(postgres) NOPASSWD: /usr/bin/psql, /usr/bin/createdb, /usr/bin/dropdb, /usr/bin/pg_dump, /usr/bin/pg_restore
vpsmcp ALL=(root)     NOPASSWD: /usr/bin/systemd-run, /usr/bin/grep, /usr/bin/tee, /usr/bin/sha256sum
```

It also runs this setup SQL:
- `CREATE ROLE vpsmcp LOGIN;`
- `GRANT pg_read_all_data, pg_read_all_stats TO vpsmcp;`

It also needs `setfacl -m u:vpsmcp:rx /opt/odoo`, so the module code check can list addons directories without sudo.

> `systemd-run` and `psql` as postgres are root-equivalent and DB-superuser-equivalent, just like docker in feature 001. The guarantees come from the server only issuing generated, fingerprinted commands, and this is stated in the README's security section.

## Implementation notes (recorded during /speckit-implement, 2026-11-11)

- **Query transport**: psql only interpolates `:'var'` for scripts read from a file or stdin,
  not for `-c`. Catalogue queries are therefore sent on **stdin** (`-f -`). All 11 queries
  were validated against a real PostgreSQL 18, including an injection attempt through a
  variable and a `DELETE` refused by the read-only session.
- **Runner status**: instead of a `status.json` file, the runner appends one line per event to
  `events.log` (`RUN|COPY|STEP|FOREIGN|ABORT …`). Appending is safe with no locking, and the
  server folds the events into the step results.
- **Stale statistics**: estimated row counts can be stale after an upgrade, which hid a planted
  40% row loss in testing. The **N largest tables are now always counted exactly**
  (`thresholds.exact_count_top_n`, default 50), in addition to tables flagged by their
  estimate.
- **SQL scanner**: the forbidden list grew beyond M5. Steps run as superuser, so it also
  refuses:
  - all psql meta-commands except `\echo`/`\set`/…;
  - server file access;
  - role, privilege and cluster-level statements;
  - dynamic SQL inside `DO`/function bodies.

  It is defence in depth. A determined script could still build SQL by concatenation, which is
  why `script` steps are labelled "detected".
- **Cancel**: if a rehearsal is cancelled before the copy finishes, the runner's TERM handler
  drops the half-made copy. It has no marker yet, so it could never be dropped later.
- **Unattributed writes**: a non-copy database that had other active sessions while it was
  written to (for example a running test Odoo) is reported as
  `foreign_write_unattributed` (warning) rather than blocking.
