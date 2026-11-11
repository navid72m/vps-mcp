# vps-mcp

An MCP server that lets an AI assistant manage the services on **one VPS**: systemd services
and Docker/Podman containers. It covers status, start/stop/restart, boot-start, logs, resource
usage, deploy, update, configure (with preview and automatic rollback) and removal.

It runs on **your workstation**, launched by your MCP client over stdio, and reaches the VPS
over SSH. Nothing is installed on the VPS.

Specification and design: [`specs/001-vps-service-mcp/`](specs/001-vps-service-mcp/).

## Security model

1. **No arbitrary commands.** Every remote command is a fixed template in the code, and all
   inputs are strictly validated and shell-quoted.
2. **Allowlist.** Only services declared in the policy file, or deployed through the server,
   can be touched. Asking about any other service returns `not_permitted` and does not reveal
   whether it exists.
3. **Protected services and confirmations.** Stop, restart and disable need `confirm=true` on
   protected services. Deploy, update, configure, rollback and remove always return a preview
   first. Protected services can never be removed.
4. **Read-only mode** refuses every state change.
5. **Lock-out guard.** The SSH service (`remote_access_units`) can never be stopped, disabled,
   changed or removed.
6. **Strict host-key pinning.** There is no trust-on-first-use. A changed host key stops
   everything.
7. **Least privilege on the VPS.** The server connects as a dedicated user with a narrow
   sudoers profile (`vps-mcp bootstrap --print-sudoers`).
8. **Audit trail and revisions.** Every state-changing attempt (including refused, failed,
   timed-out and unknown ones) is recorded in a local SQLite database (mode 0600).
   Credentials never appear in output, logs or the database.

> Access to the container runtime and the package manager is root-equivalent by nature. The
> guarantees above come from the server only ever issuing its fixed, validated commands, so
> protect the SSH key accordingly.

## Install

```bash
uv sync
```

## Set up

```bash
# 1. On the VPS: create a dedicated user with your public key, then install the sudoers profile
uv run vps-mcp bootstrap --print-sudoers --runtime docker --pm apt > vps-mcp.sudoers
#    review it, copy it to the VPS, then:
#    sudo install -m 0440 vps-mcp.sudoers /etc/sudoers.d/vps-mcp && sudo visudo -c

# 2. Pin the VPS host key (compare the fingerprint out-of-band)
uv run vps-mcp trust-host --host <vps-ip> --known-hosts ~/.config/vps-mcp/known_hosts

# 3. Write the policy file
cp examples/vps-mcp.toml ~/.config/vps-mcp/prod.toml   # edit host, key, services
uv run vps-mcp check-config --config ~/.config/vps-mcp/prod.toml

# 4. Register with your MCP client (Claude Code shown)
claude mcp add vps -- uv run --directory "$PWD" vps-mcp serve --config ~/.config/vps-mcp/prod.toml
```

## Tools

| Purpose | Tools |
|---|---|
| Look | `list_services`, `get_service_status`, `get_service_logs`, `get_service_config`, `list_revisions`, `get_resource_usage`, `get_audit_log` |
| Control | `start_service`, `stop_service`, `restart_service`, `set_boot_start` |
| Lifecycle | `deploy_service`, `update_service`, `configure_service`, `rollback_service`, `remove_service` |

The full contract is in [`contracts/mcp-tools.md`](specs/001-vps-service-mcp/contracts/mcp-tools.md).

### Example: Odoo with PostgreSQL

Containers that share a `networks` entry reach each other by service name, so the database
never needs a published port:

```text
deploy_service name="odoo-db" kind="container" confirm=true
  container={ image:"postgres:16", networks:["odoo-net"],
              env:{POSTGRES_USER:"odoo", POSTGRES_PASSWORD:"…", POSTGRES_DB:"postgres"},
              volumes:["odoo-db-data:/var/lib/postgresql/data"] }

deploy_service name="odoo" kind="container" confirm=true
  container={ image:"odoo:18", networks:["odoo-net"], ports:["8069:8069"],
              env:{HOST:"odoo-db", USER:"odoo", PASSWORD:"…"},
              volumes:["odoo-data:/var/lib/odoo"] }
```

This requires `docker.io/library/` in `approved_sources.image_prefixes`.

## Migration analysis (optional `[migration]` section)

13 more tools analyse Odoo/PostgreSQL databases and rehearse upgrade pipelines. They're
designed for an Odoo 17 → 19 style upgrade, where dumps and test copies live on the VPS and
production lives elsewhere.

| Purpose | Tools |
|---|---|
| Understand | `db_list`, `db_overview`, `db_modules`, `db_table` |
| Compare + risk | `db_compare`, `get_report`, `list_reports` (findings are blocking/warning/info, with a verdict) |
| Rehearse on a disposable copy | `rehearse` (plan → confirm with `plan_hash`), `rehearsal_status`, `cancel_rehearsal` |
| Your own runs and checks | `pipeline_last_run`, `run_check` |
| Cleanup | `drop_copies` (copies also expire after `copy_retention_h`) |

Setup on the VPS, as root, after reviewing each step:

```bash
uv run vps-mcp bootstrap --print-sudoers --with-migration --config <policy.toml>   # → /etc/sudoers.d/vps-mcp
uv run vps-mcp bootstrap --print-migration-sql | sudo -u postgres psql -p 5435      # read-only role
sudo setfacl -m u:vpsmcp:rx /opt/odoo                                                # module code check
```

Declare targets, pipelines and checks as shown in
[`contracts/migration-config.md`](specs/002-odoo-migration-analysis/contracts/migration-config.md).

How each part is kept safe:
- **Analysis** runs as a database role that can read everything and write nothing. Sessions are
  also forced read-only.
- **Queries** come from a fixed catalogue. No tool accepts SQL.
- **Rehearsals** run your declared steps on a system-created copy, as a background job on the
  VPS that keeps running if your client disconnects. Each copy carries an HMAC marker, and only
  marked copies can ever be dropped.
- **SQL step files** are scanned before running. Anything that could reach another database,
  the cluster, the filesystem or a shell is refused (`\c`, `\!`, `dblink`, `DROP DATABASE`,
  `GRANT`, `pg_read_file`, …).
- **Script steps** are *detected, not confined*. Write counters on every other allowlisted
  database are checked around each step, and any write aborts the rehearsal with a blocking
  finding.
- **Disk**: a rehearsal needs roughly 1.3× the source database size free, plus `min_free_gb` /
  `min_free_pct` to spare. It is refused up front otherwise.

> The extra sudo rights (`psql`/`createdb`/`dropdb` as postgres, `systemd-run` as root) are
> database-superuser and root equivalent. The guarantees come from the server only issuing
> generated, fingerprinted commands.

## Development

```bash
uv run pytest                 # unit, contract and negative suites (no VPS needed)
uv run ruff check && uv run mypy
tests/integration/provision.sh && uv run pytest -m integration \
    --vps-config tests/integration/vps-mcp.test.toml   # disposable VM only
```
