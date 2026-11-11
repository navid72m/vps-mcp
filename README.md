# vps-mcp

An [MCP](https://modelcontextprotocol.io) server that lets an AI assistant (Claude Code, Claude
Desktop, LM Studio, …) manage **one VPS** safely:

- **Services** (16 tools): systemd services and Docker/Podman containers. Covers status, logs,
  resource usage, start/stop/restart, boot-start, deploy, update, configure (with diff preview
  and automatic rollback) and removal.
- **Migration analysis** (13 optional tools): inspect Odoo/PostgreSQL databases, compare two
  databases into a risk report with a verdict, and rehearse an upgrade pipeline on a disposable
  copy.

The server runs **on your laptop**. Your MCP client starts it over stdio, and it reaches the VPS
over SSH. Nothing runs permanently on the VPS: it only needs a dedicated user and a sudoers file
(rehearsals add transient systemd units and files under `/var/lib/vps-mcp`).

Design documents: [`specs/001-vps-service-mcp/`](specs/001-vps-service-mcp/) (services) and
[`specs/002-odoo-migration-analysis/`](specs/002-odoo-migration-analysis/) (migration).

---

## Contents

- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Connect your MCP client](#connect-your-mcp-client)
- [Using it](#using-it)
- [Service tools](#service-tools)
- [Migration analysis](#migration-analysis-optional)
- [Security model](#security-model)
- [Configuration reference](#configuration-reference)
- [Troubleshooting](#troubleshooting)
- [Development](#development)

## Requirements

| Where | What |
|---|---|
| Laptop | Python ≥ 3.12, [uv](https://docs.astral.sh/uv/), an MCP client |
| VPS | Linux with systemd (≥ 248 recommended: Ubuntu 22.04+, Debian 12+, RHEL 9+), apt or dnf, SSH; optionally Docker or Podman |
| Migration tools | PostgreSQL ≥ 14 on the VPS (for the `pg_read_all_data` role) |

## Quick start

```bash
git clone https://github.com/navid72m/vps-mcp && cd vps-mcp
uv sync
```

**1. Create a dedicated SSH key and VPS user.** Run the `ssh root@…` part with any account that
already has root on the VPS.

```bash
ssh-keygen -t ed25519 -N "" -f ~/.ssh/vps_mcp_ed25519
ssh root@<vps> "useradd -m -s /bin/bash vpsmcp && passwd -l vpsmcp && \
  install -d -m 700 -o vpsmcp -g vpsmcp /home/vpsmcp/.ssh && \
  echo 'restrict,pty $(cat ~/.ssh/vps_mcp_ed25519.pub)' > /home/vpsmcp/.ssh/authorized_keys && \
  chown vpsmcp: /home/vpsmcp/.ssh/authorized_keys && chmod 600 /home/vpsmcp/.ssh/authorized_keys"
```

**2. Install the least-privilege sudoers profile.** Review the generated file first.

```bash
uv run vps-mcp bootstrap --print-sudoers --runtime docker --pm apt > vps-mcp.sudoers
scp vps-mcp.sudoers root@<vps>:/tmp/
ssh root@<vps> "visudo -cf /tmp/vps-mcp.sudoers && install -m 0440 /tmp/vps-mcp.sudoers /etc/sudoers.d/vps-mcp"
```

**3. Pin the VPS host key.** Compare the printed fingerprint with one you already trust, for
example from `ssh-keygen -lF <vps>`.

```bash
mkdir -p ~/.config/vps-mcp
uv run vps-mcp trust-host --host <vps> --known-hosts ~/.config/vps-mcp/known_hosts
```

**4. Write the policy file.** Start in read-only mode.

```bash
cp examples/vps-mcp.toml ~/.config/vps-mcp/myvps.toml   # set host, key, services; read_only = true
uv run vps-mcp check-config --config ~/.config/vps-mcp/myvps.toml
```

**5. Connect your MCP client** (next section). Once you trust it, set `read_only = false`.

## Connect your MCP client

The server is a stdio program. Clients start it on demand and stop it when they're done, so
seeing no `vps-mcp` process between sessions is normal. Use absolute paths, because GUI apps
don't inherit your shell `PATH`.

**Claude Code** (available in every new session):

```bash
claude mcp add --scope user vps -- "$(which uv)" run --directory "$PWD" \
  vps-mcp serve --config ~/.config/vps-mcp/myvps.toml
```

Optionally, let the read-only tools run without approval prompts. Add this to
`~/.claude/settings.json` under `permissions.allow`:

```json
"mcp__vps__list_services", "mcp__vps__get_service_status", "mcp__vps__get_service_logs",
"mcp__vps__get_resource_usage", "mcp__vps__get_service_config", "mcp__vps__get_audit_log",
"mcp__vps__list_revisions"
```

Keep the state-changing tools on "ask".

**LM Studio** (`~/.lmstudio/mcp.json`) or **Claude Desktop**
(`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "vps": {
      "command": "/absolute/path/to/uv",
      "args": ["run", "--directory", "/absolute/path/to/vps-mcp",
               "vps-mcp", "serve", "--config", "/absolute/path/to/.config/vps-mcp/myvps.toml"]
    }
  }
}
```

In LM Studio, turn on `mcp/vps` in the chat's Program panel, and pick a model with
tool-calling support.

## Using it

Ask in plain language. The assistant picks the tools.

- "What's running on my VPS?"
- "Show the last 100 lines of odoo19's logs containing 'error'"
- "Which service uses the most memory?"
- "Restart worker", or for a protected service, "restart nginx, I confirm"
- "Deploy redis:7 as a container named cache", which shows a preview first and asks you to confirm
- "Update api to ghcr.io/myorg/api:1.2", which rolls back automatically if it comes up unhealthy
- "Compare db_before with db_after and tell me if it's safe to promote" (migration tools)
- "Show the audit log"

## Service tools

| Purpose | Tools |
|---|---|
| Look | `list_services`, `get_service_status`, `get_service_logs`, `get_service_config`, `list_revisions`, `get_resource_usage`, `get_audit_log` |
| Control | `start_service`, `stop_service`, `restart_service`, `set_boot_start` |
| Lifecycle | `deploy_service`, `update_service`, `configure_service`, `rollback_service`, `remove_service` |

- **Previews**: deploy, update, configure, rollback and remove return a preview unless
  `confirm=true`.
- **Rollback**: changes are health-gated and roll back automatically on failure.
- **Compact output**: results are compact JSON, and logs are returned as plain
  `YYYY-MM-DD HH:MM:SS message` lines to keep token usage low.

The full contract is in
[`specs/001-vps-service-mcp/contracts/mcp-tools.md`](specs/001-vps-service-mcp/contracts/mcp-tools.md).

### Deploying your own code

The server deploys **images and packages from approved sources**. It never uploads or builds
code on the VPS. To ship your code:

1. Build an image, ideally in CI (e.g. GitHub Actions).
2. Push it to a registry.
3. Add the registry prefix to `approved_sources.image_prefixes`.
4. Ask the assistant to deploy or update that image.

### Example: Odoo with PostgreSQL

Containers that share a `networks` entry reach each other by service name, so the database never
needs a published port:

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

This requires `docker.io/library/` in `approved_sources.image_prefixes`. Networks the server
creates are removed automatically once no managed container uses them.

> **Docker Compose stacks:** restarting them and reading their logs through the server is fine,
> but change their configuration or version with `docker compose`. `configure_service` and
> `update_service` recreate the container from their own spec, which would detach it from
> Compose.

## Migration analysis (optional)

Add a `[migration]` section to the policy file to enable 13 more tools. They're designed for
an Odoo version upgrade (e.g. 17 → 19) where upgrade dumps and test databases live on the VPS
and production lives elsewhere.

| Purpose | Tools |
|---|---|
| Understand | `db_list`, `db_overview`, `db_modules`, `db_table` |
| Compare + risk | `db_compare`, `get_report`, `list_reports` |
| Rehearse on a disposable copy | `rehearse`, `rehearsal_status`, `cancel_rehearsal` |
| Your own runs and checks | `pipeline_last_run`, `run_check` |
| Cleanup | `drop_copies` |

**Comparison.** `db_compare` snapshots both databases. Each difference becomes a finding,
classified **blocking / warning / info**:

- **Schema**: tables and columns removed or added, type narrowing and widening, and
  constraints and indexes.
- **Rows**: row losses. The largest tables are always counted exactly, because statistics can
  be stale after an upgrade.
- **Odoo state**: modules not upgraded or stuck in "to upgrade", and deactivated or orphaned
  views.

The report ends with a verdict: `safe_to_promote`, `review_warnings` or `do_not_promote`.

**Rehearsal.** `rehearse` without `confirm` returns a plan: the copy name, each step with a
fingerprint of its file, and free disk before and after. Confirming with the plan's `plan_hash`:

1. creates an HMAC-marked copy of the source;
2. runs your declared steps (SQL files, scripts, `odoo-bin -u all`, re-upgrade of stale
   modules) against **the copy only**, in a background systemd unit that keeps running if your
   client disconnects;
3. produces a source-vs-copy report.

Copies expire after `copy_retention_h` (default 24 h).

**Setup on the VPS** (as root, after reviewing each output):

```bash
uv run vps-mcp bootstrap --print-sudoers --with-migration --config ~/.config/vps-mcp/myvps.toml
uv run vps-mcp bootstrap --print-migration-sql | ssh root@<vps> "sudo -u postgres psql -p <port>"
ssh root@<vps> "setfacl -m u:vpsmcp:rx /opt/odoo"   # lets the module-code check list addons
```

The `[migration]` schema (targets, Odoo profile, pipelines, checks, thresholds) is documented in
[`contracts/migration-config.md`](specs/002-odoo-migration-analysis/contracts/migration-config.md)
and in the commented section of [`examples/vps-mcp.toml`](examples/vps-mcp.toml).

**How it stays safe:**

- **Analysis** runs as a database role with `pg_read_all_data` only, in read-only sessions.
  Queries come from a fixed catalogue, and no tool accepts SQL.
- **SQL step files** are scanned before a rehearsal. Anything that could reach another
  database, the cluster, the filesystem or a shell is refused (`\c`, `\!`, `dblink`,
  `DROP DATABASE`, `GRANT`, `pg_read_file`, dynamic SQL in `DO` blocks, …).
- **Script steps** are *detected, not confined*. Write counters on every other allowlisted
  database are checked around each step, and a write aborts the rehearsal with a blocking
  finding.
- **Copies** can only be dropped if both the local record and the database's HMAC marker match.
- **Disk**: a rehearsal needs about 1.3× the source size free plus
  `max(min_free_gb, min_free_pct%)`. It is refused up front otherwise.

## Security model

1. **No arbitrary commands.** Every remote command is a fixed template, and every input is
   validated and shell-quoted.
2. **Allowlist.** Only declared services, services deployed through the server, and allowlisted
   databases can be touched. Anything else returns `not_permitted`, without revealing whether
   it exists.
3. **Confirmations and protected services.** Destructive actions preview first. Protected
   services need confirmation to stop or restart, and can never be removed.
4. **Read-only mode** refuses every state change.
5. **Lock-out guard.** The SSH service can never be stopped, disabled, changed or removed.
6. **Strict host-key pinning.** There is no trust-on-first-use, and a changed host key stops
   everything.
7. **Least privilege.** A dedicated VPS user with a narrow sudoers profile.
8. **Audit trail.** Every state-changing attempt (including refused, failed and timed-out ones)
   is recorded locally in SQLite (mode 0600). Credentials and secret-looking values
   (`*PASSWORD*`, `*TOKEN*`, …) are masked in output, logs and the database.

> Access to the container runtime, the package manager, `psql` as postgres and `systemd-run`
> is root- or superuser-equivalent by nature. The guarantees above come from the server only
> ever issuing its own generated, validated commands, so protect the SSH key accordingly.

## Configuration reference

See [`examples/vps-mcp.toml`](examples/vps-mcp.toml). Every key is commented, and unknown keys
are errors. The main sections:

| Section | Purpose |
|---|---|
| `[vps]` | `host`, `port`, `user`, `key_path`, `known_hosts_path`, `container_runtime`, `package_manager` |
| `[policy]` | `read_only`, `min_free_disk_mb`, `secret_keys`, `remote_access_units`, `health_stabilize_s` |
| `[policy.timeouts]` | `control_s`, `deploy_s`, `query_s` |
| `[policy.approved_sources]` | `apt_origins`, `dnf_repos`, `image_prefixes` |
| `[[service]]` | `name`, `kind` (`os`/`container`), `protected`, `package`, `config_files`, `validate`, `data_paths` |
| `[migration]` | optional; see [Migration analysis](#migration-analysis-optional) |

Validate with `vps-mcp check-config --config <file>`. The local audit/state database lives in
`~/.local/state/vps-mcp/<vps-name>.db`.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `connection_failure/identity_mismatch` | The VPS host key changed. Verify it out-of-band, then `trust-host --replace`. |
| `connection_failure/auth_rejected` | The key isn't in `/home/vpsmcp/.ssh/authorized_keys`, or `key_path` is wrong. |
| `connection_failure/timeout` | Port 22 is blocked or the host is down. |
| `permission_denied` | `/etc/sudoers.d/vps-mcp` is missing or out of date. Regenerate it with `bootstrap --print-sudoers`. |
| `read_only` on every action | `read_only = true` in `[policy]`. The change applies the next time the client starts the server. |
| Logs return 0 lines for nginx | nginx logs to `/var/log/nginx/*.log`, not the journal. Only journal and container logs are read. |
| Tools missing in Claude Code | Start a new session after `claude mcp add`, then check with `/mcp`. |
| `insufficient_disk` on `rehearse` | Free space on the VPS, or lower `[migration] min_free_gb` / `min_free_pct` knowingly. |

## Development

```bash
uv run pytest            # unit, contract and negative suites: no VPS needed (≈500 tests)
uv run ruff check && uv run ruff format --check && uv run mypy
```

- **Fakes**: tests run against a simulated VPS (`tests/fakes.py`) and a simulated PostgreSQL
  cluster (`tests/fakepg.py`).
- **Planted defects**: one suite plants defects and checks every one is caught with the right
  severity.
- **Negative suites**: over 170 disallowed requests, each asserting that nothing was changed
  remotely.

**Integration tests** run against a disposable VM. Never use them on a production server.

```bash
tests/integration/provision.sh      # creates a Multipass VM with nginx, Docker and PostgreSQL
uv run pytest -m integration --vps-config tests/integration/vps-mcp.test.toml
```

Project layout:

```text
src/vps_mcp/
├── server.py            # MCP tools (services) + migration registration
├── cli.py               # serve | check-config | trust-host | bootstrap
├── config/              # policy file models and loader
├── remote/              # SSH connection, fixed-template executor, redaction
├── backends/            # systemd + apt/dnf, docker/podman
├── core/                # guard (allowlist, confirm, audit), actions, deploy, changes, queries
├── store/               # SQLite: audit, revisions, deployed services, migration tables
└── migration/           # catalogue, snapshot/diff/risk, copies, runner, rehearsal, checks
specs/                   # speckit spec, plan, research, contracts, tasks per feature
```
