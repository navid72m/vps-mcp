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

## Development

```bash
uv run pytest                 # unit, contract and negative suites (no VPS needed)
uv run ruff check && uv run mypy
tests/integration/provision.sh && uv run pytest -m integration \
    --vps-config tests/integration/vps-mcp.test.toml   # disposable VM only
```
