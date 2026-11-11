# vps-mcp

An [MCP](https://modelcontextprotocol.io) server that lets an AI assistant (Claude Code, Claude
Desktop, LM Studio, …) manage **one VPS** safely:

- **Services** (16 tools): systemd services and Docker/Podman containers. Covers status, logs,
  resource usage, start/stop/restart, boot-start, deploy, update, configure (with diff preview
  and automatic rollback) and removal.
- **Migration analysis** (13 optional tools): inspect Odoo/PostgreSQL databases, compare two
  databases into a risk report with a verdict, and rehearse an upgrade pipeline on a disposable
  copy.
- **Declared jobs** (4 optional tools): run operator-owned scripts, such as a full rehearsal,
  through a plan → confirm → watch → report flow.

The server runs **on your laptop**. Your MCP client starts it over stdio, and it reaches the VPS
over SSH. Nothing runs permanently on the VPS: it only needs a dedicated user and a sudoers file
(rehearsals and jobs add transient systemd units and files under `/var/lib/vps-mcp`).

### What it gives your agent

- **Structured answers instead of shell sessions.** "Is Odoo healthy?" is one tool call that
  returns compact JSON, not a series of `ssh`/`grep`/`psql` commands and raw output. That saves
  tokens, and smaller local models can do the job too.
- **Safe changes.** Every state change previews first and needs `confirm=true`, and is
  health-checked, rolled back on failure and audited.
- **Migration judgement.** The agent can compare an upgraded database with its source and give a
  `safe_to_promote` / `review_warnings` / `do_not_promote` verdict backed by findings.
- **Long jobs it can watch.** The agent can start a declared multi-hour script, check its progress
  later (even from another session) and get a final report.
- **Hard limits from your config, not from the prompt.** There's no arbitrary command, SQL or
  script. Only declared services, databases and jobs are reachable, the SSH service is untouchable,
  and `read_only = true` turns off every write tool.

Design documents: [`specs/001-vps-service-mcp/`](specs/001-vps-service-mcp/) (services),
[`specs/002-odoo-migration-analysis/`](specs/002-odoo-migration-analysis/) (migration) and
[`specs/003-declared-jobs/`](specs/003-declared-jobs/) (jobs).

---

## Contents

- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Connect your MCP client](#connect-your-mcp-client)
- [Using it](#using-it)
- [Service tools](#service-tools)
- [Migration analysis](#migration-analysis-optional)
- [Declared jobs](#declared-jobs-optional)
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
"mcp__vps__list_revisions",
"mcp__vps__db_list", "mcp__vps__db_overview", "mcp__vps__db_modules", "mcp__vps__db_table",
"mcp__vps__db_compare", "mcp__vps__get_report", "mcp__vps__list_reports",
"mcp__vps__rehearsal_status", "mcp__vps__pipeline_last_run",
"mcp__vps__list_jobs", "mcp__vps__job_status"
```

The second and third lines only apply if you enabled migration analysis or declared jobs.

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
- "Plan the full-rehearsal job", then "start it", then later "how is the rehearsal going?" (jobs)
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

## Declared jobs (optional)

Some long scripts can't be rehearsed on a copy, for example a full rehearsal that drops and
rebuilds a test database from a dump, runs the deploy script, then compares the result. You can
declare such a script as a **job** under `[[migration.job]]`. Four tools then let the assistant
run it safely: `list_jobs`, `run_job`, `job_status` and `cancel_job`.

```toml
[[migration.job]]
name = "full-rehearsal"
script = "/root/full_rehearsal.sh"            # root-owned, not group/other-writable
progress_file = "/root/rehearsal_logs/STEPS.txt"
report_file = "/root/rehearsal_logs/compare.md"
may_modify = ["shop_19_test"]                 # may be dropped and rebuilt
services = ["odoo-test"]                      # may be stopped
source = "shop_17_dump"                       # compared with result at the end
result = "shop_19_test"
```

How a job runs:

1. **Plan**: `run_job` without `confirm` shows a plan: the script's fingerprint, owner and
   mode; which databases it will rebuild and which services it may stop; parameters; and free
   disk. Nothing runs yet.
2. **Run**: confirming with the plan's `plan_id` re-checks everything, then starts the script
   as-is in a transient systemd unit. The job has a runtime cap, and if it's cancelled its whole
   process tree is killed. It keeps running if your client disconnects. Only one job or
   rehearsal runs at a time.
3. **Status**: `job_status` reads the job's progress file. It only reads lines written since
   this run started, because many scripts append to the same file across runs. It recognises
   outcomes such as `EXIT n`, `rc=n`, `ok`, `FAILED`, `STOP…`, and the success marker
   (`ALL DONE` by default).
4. **Guard**: writes to any database the job doesn't declare, an undeclared database
   disappearing, or a change to any undeclared managed service are all reported as blocking
   findings.
5. **Report**: when the job ends, one report combines its step outcomes, an excerpt of its own
   report file and a `db_compare` of `source` against `result`, and gives a verdict.

**Parameters** are declared with a name and a validation pattern, and are passed only as
environment variables. For example, `RUN_NAME` must match `^[a-z0-9_]{1,40}$` and defaults to
`rehearsal_{date:%Y%m%d_%H%M}`.

> **Limits:**
> - **Not confined.** A job is your own script running as root. The server controls *what*
>   may start and *when*, and **detects** undeclared changes; it doesn't prevent them.
> - **Cancel has limits.** Cancelling stops the job's process tree, but not work it handed to
>   other services. For example, a query already running in PostgreSQL, or a `docker exec`
>   inside a container.

Optional keys:

- `run_as`: defaults to `root`.
- `log_globs`
- `min_free_gb`: defaults to 5.
- `max_runtime_h`: defaults to 24, at most 168.
- `success_marker`
- `outcome_patterns`: regexes with `step`/`status` groups, for progress lines the built-in rules
  don't recognise.

All of them are commented in [`examples/vps-mcp.toml`](examples/vps-mcp.toml). The full schema
is in [`contracts/job-config.md`](specs/003-declared-jobs/contracts/job-config.md), and the
tools are in [`contracts/job-tools.md`](specs/003-declared-jobs/contracts/job-tools.md).

**Setup on the VPS:** regenerate and reinstall the sudoers file with jobs enabled. This adds
`stat`, used for the script owner/mode check.

```bash
uv run vps-mcp bootstrap --print-sudoers --with-migration --with-jobs \
  --config ~/.config/vps-mcp/myvps.toml > vps-mcp.sudoers
```

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
| `[[migration.job]]` | optional, up to 10; see [Declared jobs](#declared-jobs-optional) |

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
| `permission_denied` mentioning `stat` on `run_job` | The jobs sudoers rule is missing. Reinstall with `--with-migration --with-jobs`. |
| `conflict` on `run_job`: "something is already running" | Only one job or rehearsal runs at a time. Check `job_status` / `rehearsal_status`. |
| `conflict` on `run_job`: plan unknown/older than 30 min, or script changed | The error includes a fresh plan. Review it and confirm with its `plan_id`. |
| Job tools missing | No `[[migration.job]]` is declared, or the client hasn't restarted the server since you added one. |

## Development

```bash
uv run pytest            # unit, contract and negative suites: no VPS needed (≈590 tests)
uv run ruff check && uv run ruff format --check && uv run mypy
```

- **Fakes**: tests run against a simulated VPS (`tests/fakes.py`) and a simulated PostgreSQL
  cluster (`tests/fakepg.py`).
- **Planted defects**: one suite plants defects and checks every one is caught with the right
  severity.
- **Negative suites**: over 200 disallowed requests, each asserting that nothing was changed
  remotely.

**Integration tests** run against a disposable VM. Never use them on a production server.

```bash
tests/integration/provision.sh      # creates a Multipass VM with nginx, Docker and PostgreSQL
uv run pytest -m integration --vps-config tests/integration/vps-mcp.test.toml
```

Project layout:

```text
src/vps_mcp/
├── server.py            # MCP tools (services) + migration/jobs registration
├── cli.py               # serve | check-config | trust-host | bootstrap
├── config/              # policy file models and loader
├── remote/              # SSH connection, fixed-template executor, redaction
├── backends/            # systemd + apt/dnf, docker/podman
├── core/                # guard (allowlist, confirm, audit), actions, deploy, changes, queries
├── store/               # SQLite: audit, revisions, deployed services, migration tables
├── migration/           # catalogue, snapshot/diff/risk, copies, runner, rehearsal, checks
└── jobs/                # declared jobs: integrity, params, launcher, progress, guard, report
specs/                   # speckit spec, plan, research, contracts, tasks per feature
```
