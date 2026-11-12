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
- **GitLab** (15 optional tools): read code, history, issues, merge requests and CI of allowlisted
  projects with your own token, from the macOS Keychain. Open issues and MRs, and deploy a reviewed
  commit to the VPS with automatic rollback.
- **Personal assistant** (`assistant`): an always-on agent of its own that you talk to on
  Telegram. It uses these tools and the shared memory, and asks for approval on your phone
  before any change. See [Personal assistant](#personal-assistant).
- **Memory** (5 tools, on by default): **agent-memory**, a managed knowledge store shared by every
  agent and project. It holds facts, decisions, procedures and experiences, with where each one
  came from, how confident it is, and when it expires. It is also a standalone MCP server that
  agents can use for work that has nothing to do with the VPS.

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
- **From GitLab to the VPS.** "Deploy tag v1.5 of the addons" shows the commit, changed files,
  pipeline status and services affected. Once you confirm, that exact commit goes live; if a
  service fails its health check, the previous version comes back automatically.
- **A memory that follows you across clients and projects.** A new session in Claude Code,
  OpenCode or LM Studio starts with the key procedures, decisions and open issues, and finds the
  rest with a task-aware search. Every memory records where it came from and how sure it is.
  Outdated facts are superseded rather than left lying around.
- **Hard limits from your config, not from the prompt.** There's no arbitrary command, SQL or
  script. Only declared services, databases and jobs are reachable, the SSH service is untouchable,
  and `read_only = true` turns off every write tool.

Design documents: [`specs/001-vps-service-mcp/`](specs/001-vps-service-mcp/) (services),
[`specs/002-odoo-migration-analysis/`](specs/002-odoo-migration-analysis/) (migration),
[`specs/003-declared-jobs/`](specs/003-declared-jobs/) (jobs),
[`specs/004-agent-memory/`](specs/004-agent-memory/) (memory) and
[`specs/005-gitlab-integration/`](specs/005-gitlab-integration/) (GitLab).

---

## Contents

- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Connect your MCP client](#connect-your-mcp-client)
- [Using it](#using-it)
- [Service tools](#service-tools)
- [Migration analysis](#migration-analysis-optional)
- [Declared jobs](#declared-jobs-optional)
- [GitLab](#gitlab-optional)
- [Memory](#memory)
- [Personal assistant](#personal-assistant)
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

`check-config` also prints where the memory folder will be. It's created the first time a
client starts the server.

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
"mcp__vps__list_jobs", "mcp__vps__job_status",
"mcp__vps__memory_search", "mcp__vps__memory_get",
"mcp__vps__gitlab_projects", "mcp__vps__gitlab_refs", "mcp__vps__gitlab_tree",
"mcp__vps__gitlab_file", "mcp__vps__gitlab_commits", "mcp__vps__gitlab_compare",
"mcp__vps__gitlab_list_items", "mcp__vps__gitlab_read_item", "mcp__vps__gitlab_pipelines",
"mcp__vps__gitlab_job_log", "mcp__vps__deploy_targets"
```

The migration and job lines only apply if you enabled those features.

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

**OpenCode** (`~/.config/opencode/opencode.json`, or `opencode.json` in a project):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "vps": {
      "type": "local",
      "command": ["/absolute/path/to/uv", "run", "--directory", "/absolute/path/to/vps-mcp",
                  "vps-mcp", "serve", "--config", "/absolute/path/to/.config/vps-mcp/myvps.toml"],
      "enabled": true
    }
  }
}
```

Every client that uses the same `--config` shares the same [memory](#memory), so a note written
in one client can be found by the next session in all of them.

## Using it

Ask in plain language. The assistant picks the tools.

The full set of examples, covering every tool, is in [`docs/USAGE_EXAMPLES.md`](docs/USAGE_EXAMPLES.md).

- "What's running on my VPS?"
- "Show the last 100 lines of odoo19's logs containing 'error'"
- "Which service uses the most memory?"
- "Restart worker", or for a protected service, "restart nginx, I confirm"
- "Deploy redis:7 as a container named cache", which shows a preview first and asks you to confirm
- "Update api to ghcr.io/myorg/api:1.2", which rolls back automatically if it comes up unhealthy
- "Compare db_before with db_after and tell me if it's safe to promote" (migration tools)
- "Plan the full-rehearsal job", then "start it", then later "how is the rehearsal going?" (jobs)
- "Where are we with the migration?" in a brand-new session, answered from memory
- "Remember as a decision: promote only when the verdict is safe_to_promote" (memory)
- "What changed in the addons since v1.4? Did the pipeline pass? Then deploy v1.5" (GitLab)
- "Open an issue in addons with the blocking findings of the last rehearsal" (GitLab)
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

## GitLab (optional)

Add a `[gitlab]` section and the assistant can work with your GitLab, using **your own token**.
It only reaches the projects you allowlist, and only with the permissions you give each one.

**1. Store the token in the macOS Keychain.** This prompts for the token, so it never appears on
a command line or in shell history:

```bash
security add-generic-password -U -s vps-mcp-gitlab -a <account> -w
```

| What you declare | Minimum token scope |
|---|---|
| `read` (and `deploy`, which only downloads archives) | `read_api` |
| `write_issues`, `write_mrs`, `ci` | `api` |

Prefer a project or group access token with the lowest role that works. The token can do
whatever GitLab allows it to; vps-mcp only limits what assistants can do *through vps-mcp*.

**2. Declare the projects and deploy targets** in the policy file. See the commented block in
[`examples/vps-mcp.toml`](examples/vps-mcp.toml) and
[`contracts/gitlab-config.md`](specs/005-gitlab-integration/contracts/gitlab-config.md). Then
check that everything resolves:

```bash
uv run vps-mcp gitlab-check --config ~/.config/vps-mcp/myvps.toml
```

`gitlab-check` prints:

- the token's user and scopes;
- its expiry date, with a warning when that's under 14 days away;
- whether each project is reachable;
- what each deploy target currently has on the VPS.

It never prints the token.

| Purpose | Tools | Permission |
|---|---|---|
| Read | `gitlab_projects`, `gitlab_refs`, `gitlab_tree`, `gitlab_file`, `gitlab_commits`, `gitlab_compare` | `read` |
| Issues and MRs | `gitlab_list_items`, `gitlab_read_item`; `gitlab_write` (create issue, comment, open MR, close an issue vps-mcp created) | `read`; `write_issues` / `write_mrs` |
| CI | `gitlab_pipelines`, `gitlab_job_log`; `gitlab_ci` (retry, cancel, trigger on `trigger_refs`) | `read`; `ci` |
| Deploy | `deploy_targets`, `deploy_from_gitlab`, `rollback_deploy` | `deploy` |

Assistants address projects by the **alias** you give them, never by GitLab path or ID, so
projects you didn't declare are unreachable. There is no merge tool at all. Closing issues needs
`close_issues = true`, and by default only covers issues vps-mcp created; they carry a
`_Posted via vps-mcp_` footer. Every write previews first, needs `confirm=true`, is audited, and
is refused in read-only mode.

**Deploying.** `deploy_from_gitlab` without `confirm` returns a plan with:

- the exact commit;
- the version currently deployed and any files edited on the VPS since;
- the files that change;
- the pipeline status;
- the services that will restart;
- free disk space.

Confirming with the plan's `plan_id` deploys **exactly that commit**:

1. Your laptop downloads the commit's archive from GitLab. The VPS never contacts GitLab, and
   the token never leaves your laptop.
2. The laptop rebuilds it as a clean tar. Unsafe paths, links leaving the tree, special files and
   excluded paths are dropped, and ownership and modes are normalised.
3. The tar is streamed over SSH into a new release directory on the VPS:

   ```text
   /var/lib/vps-mcp/deploys/<target>/releases/<time>-<sha12>/   files + MANIFEST.sha256 + marker
   <destination>  ->  symlink to the active release
   ```

4. The symlink is switched in one step, so readers see either the old version or the new one.
5. The declared services are restarted and health-checked, including `health_url` if you set it.
   If anything is unhealthy, the previous release is switched back automatically.

Releases you didn't deploy through vps-mcp are moved aside to `releases/pre-vps-mcp-<time>/`,
never deleted. The last `keep_releases` versions are kept, and `rollback_deploy` switches back to
one of them. Deploys are refused:

- while another deploy of the same target is running;
- while a declared job that uses the same services is running;
- while a rehearsal is running.

The deployed version of each target, and recent GitLab activity, appear in the
[memory](#memory), as facts that are kept up to date.

**VPS setup** (as root, after reviewing the output):

```bash
uv run vps-mcp bootstrap --print-sudoers --with-gitlab > gitlab.sudoers   # tar, ln, mv, rm, ...
```

Add the result to `/etc/sudoers.d/vps-mcp`. These binaries are root-equivalent. As with the
rest of vps-mcp, the guarantee is that the server only issues its own validated commands, on
paths under `/var/lib/vps-mcp/deploys` and the destinations you declared.

## Memory

Memory is **agent-memory**, a managed knowledge store rather than a conversation log. It ships in
this repo as its own package and command (`agent-memory`). Every agent and every project can
share it. vps-mcp uses it as a library.

**What a memory is.** One concise piece of knowledge, stored as one Markdown file with
structured front matter:

| Field | Meaning |
|---|---|
| `type` | `semantic` (what is true), `decision` (why; with rejected alternatives), `procedural` (how to do something), `episodic` (what happened; open/done for issues and next steps) |
| `scope` | `user`, `project:<name>`, `environment:<name>` or `task:<id>`; a session sees its own scopes plus `user` |
| `subject` | a dotted key such as `postgres.version`, used for duplicates and conflicts |
| provenance | the source (`client`, `task`, `file`, `web`, `issue`, `tool_output`, `tool_generated`, `operator`, …), the client, created/updated/last-confirmed times and a confirmation count |
| `confidence` | `verified`, `inferred` or `unverified` |
| lifecycle | `active`, `superseded` (linked to its successor), `expired`, `invalidated` or `archived`; an optional expiry; `live_state` facts carry a freshness window |

**The write policy is enforced by code on every save, not left to the agent:**

- **Secrets** are refused and never written. Store a reference to where a secret lives instead.
- **Duplicates** (same subject, nearly the same text) become a confirmation of the existing
  memory, not a second copy.
- **Contradictions** on the same subject are kept on both sides, linked, and flagged
  `needs verification`. Decisions are only replaced by an explicit `supersede` with a reason, so
  nothing is overwritten silently.
- **Text from untrusted sources** (READMEs, issues, web pages, tool output) is stored as
  `unverified`. If it reads like an instruction ("ignore previous…", "always run curl …") it is
  refused as a procedure or decision. Only the operator can save such text, with
  `agent-memory save --operator`. Agents cannot raise trust through the tools.
- **Facts about live state** (for example "web is running") always come back with a
  "verify live" flag, and a "possibly stale" flag once they are older than their freshness window.

**Retrieval is task-aware.** When a session connects it gets only a small **pinned core**
(~500 tokens): pinned procedures and decisions, plus open issues. Everything else is searched
for. `memory_search` ranks matches by keyword relevance, recency, importance, confidence and
scope (task > environment > project > user). It returns at most 8 by default, grouped as facts,
decisions, experiences and procedures, and says "nothing relevant" instead of padding. Superseded,
expired and invalidated memories stay out of results unless you ask for history.

| Tool | What it does |
|---|---|
| `memory_search` | Ranked, scope-aware search, grouped by type, with flags (verify live, stale, conflict, inferred, open) |
| `memory_get` | One memory with full provenance: history, what it superseded, conflicts, related memories |
| `memory_save` | A validated save: created, confirmed (duplicate) or conflict (flagged) |
| `memory_update` | `update` metadata, `supersede`, `invalidate`, `expire`, `confirm`, `close`, `resolve_conflict` |
| `memory_archive` | Forget a memory. It previews first and moves the file to `archive/` |

**vps-mcp's own facts.** vps-mcp writes generated, live-state facts into
`environment:<vps name>`: service states, the deployed version per GitLab target, the last
outcome of each job and rehearsal, and the latest report verdict. They are updated in place,
never duplicated. Agents can search them like any other memory. If the store can't be opened,
vps-mcp keeps working and says memory is unavailable.

**The store.** By default it lives in `~/.local/share/agent-memory/` (or `$AGENT_MEMORY_HOME`):

```text
agent-memory/
├── memories/<scope>/   one .md file per memory: read, edit or delete by hand; noticed on next search
├── archive/            archived memories
├── events/             append-only history and audit (no memory content)
└── index.sqlite        search index (SQLite FTS5), rebuilt from the files automatically
```

**Use it from any agent**, not only through vps-mcp:

```bash
claude mcp add --scope user memory -- "$(which uv)" run --directory "$PWD" \
  agent-memory serve --project my-project --client claude-code
```

The same works in OpenCode, Antigravity or LM Studio (`agent-memory serve --project …`). Each
session sees its project's memories plus `user` scope, and never other projects' memories.

**Operator commands:**

```bash
uv run agent-memory search "database backup" --project my-project
uv run agent-memory save --operator --type procedural --scope project:my-project \
  --subject deploy.order --title "Stop the app first" --content "…" --pinned
uv run agent-memory doctor            # counts, malformed files, conflicts, stale facts, warnings
uv run agent-memory bench             # recall / precision / stale hits / conflicts / token cost
uv run agent-memory migrate-004 ~/.config/vps-mcp/myvps.memory --project vps-myvps \
  --environment myvps                 # dry run; add --apply to import feature-004 notes
```

**Benchmark** (built-in fixtures: 289 memories, 22 tasks). Compared with plain keyword search,
ranked retrieval keeps recall and never returns an outdated memory:

| Configuration | Recall@8 | Precision@8 | Stale hits | Conflicts flagged | Tokens per result |
|---|---|---|---|---|---|
| naive keyword search | 0.95 | 0.36 | 100% | 0% | 25 |
| **agent-memory** | **0.97** | **0.80** | **0%** | **100%** | 208 |

The pinned core costs about 150 tokens.

## Personal assistant

`assistant` is a background process that is **its own agent**. It runs on your laptop and talks
to you through a Telegram bot. It thinks with a language model (Claude, or a local model via LM Studio or Ollama,
or anything else with an OpenAI-compatible API), uses MCP servers such as vps-mcp as its tools, and shares agent-memory with your
other clients.

**Where it runs.** On your laptop, as a launchd user agent: it starts at login and is restarted
after a crash. It only makes outgoing connections (Telegram long polling, the model API, the MCP
servers), so there's no open port, domain or webhook. While the laptop sleeps it is offline. When
it wakes it sends one "missed" note instead of replaying schedules. Moving it to an always-on box
later only needs a different service definition (`--systemd-user`) and secrets provider (`file`).

**Setup.**

1. In Telegram, ask **@BotFather** for a bot token (`/newbot`), and **@userinfobot** for your user
   id.
2. Store the secrets in the Keychain. Run these in your own Terminal; each one prompts, so nothing
   is echoed:

   ```bash
   security add-generic-password -U -s vps-mcp-assistant -a telegram_bot_token -w
   security add-generic-password -U -s vps-mcp-assistant -a anthropic_api_key -w
   ```

   The second one is only needed for Claude. A local model needs no key:
   set `[model] provider = "openai"` and `base_url = "http://localhost:1234/v1"` (LM Studio's
   server) as shown in the example config. Tool use needs a model that is good at it: small
   models (4B to 8B) work for simple lookups but get multi-step tasks wrong more often.

   **Several models.** `[model]` is the default; add `[models.<name>]` for others (see the
   example config). On the phone, `/model` lists them with a button each, `/model gemini`
   switches at once, and "switch to gemini" makes the assistant send a confirm button (it
   never switches on its own, so text in a tool result can't move your chats to a cloud
   model). The choice survives restarts. Only the default model's key is needed at startup;
   a missing key for another model is reported when you switch.

3. Copy [`examples/assistant.toml`](examples/assistant.toml) to
   `~/.config/assistant/assistant.toml`. Set `allowed_user_ids`, `timezone` and the vps server's
   `command`.
4. Check and install:

   ```bash
   uv run assistant check
   uv run assistant service install
   ```

   Status is `assistant service status`; logs are in `~/.local/state/assistant/logs/`.

**Using it.** Write to the bot. Only your account gets answers: messages from anyone else are
dropped before any model or tool call, and audited.

| Command | What it does |
|---|---|
| any text | Ask or instruct. It uses read-only tools directly and asks before anything else |
| `/new` · `/done` | Start fresh / end the conversation, and get memory proposals to Save or Discard |
| `/status` · `/usage` | Servers, schedules, pending approvals, heartbeat · today's tokens, calls and cost |
| `/schedules` · `/schedule add <name> <daily HH:MM\|weekdays HH:MM\|every Nh> <prompt>` | Recurring prompts, e.g. a morning briefing; also `pause` / `resume` / `remove` |
| `/stop` · `/resume` | Kill switch: cancels running work and voids approvals; nothing runs until `/resume` |
| `/forget` | Delete stored conversation history (memory is not affected) |
| `/model [name]` | List the models (with switch buttons) or switch to one |
| `/tools` | Core tools, toolsets, which are loaded now, and the size of the tool list |

**Approvals.** Each tool has a policy: `auto`, `ask` or `never`. By default read-only tools are
`auto` and everything else is `ask`; tools that aren't allowlisted are never offered to the
model. For an `ask` tool you get a message showing the tool, its arguments and the tool's own
preview (vps-mcp's `confirm=false` plan), with **Approve / Reject** buttons.

- An approval is bound to that exact call, works once, and expires (10 min by default).
- Buttons are signed with a key that changes on every restart, so old or forged buttons do nothing.
- Scheduled runs **always** ask before a state-changing action, whatever the policy.

**Memory.** Every turn includes agent-memory's pinned core, and the model can search and read
memories. It **cannot** write them. At the end of a conversation (`/done`, or 30 minutes idle) it
proposes up to 5 memories, and only the ones you tap **Save** on are stored, through
agent-memory's normal write policy (secrets refused, duplicates merged, conflicts flagged).

**Tools and context.** Not every tool is sent to the model on every message: tool definitions
alone were about 10,000 tokens per call. A few **core** tools are always there (memory search,
schedules, service status). The rest are grouped into **toolsets** (gitlab, db, deploy,
migration, jobs, services, files, …), and the model sees a one-line catalog of them. A toolset is
loaded when:

- your message matches its keywords ("why did the pipeline fail?" loads `gitlab` before the
  model even starts);
- the model asks for it with `tools__load`;
- the model calls one of its tools directly.

It stays loaded while the conversation uses it, is unloaded after 6 messages without use, and
`/new` starts again from the core. Loading never changes a tool's policy: `ask` tools still ask
and `never` tools are never shown. Groups, core tools and keywords are set per server in the
config (`core = [...]`, `[server.toolsets.<name>]`, see the example); `[tools] lazy = false` sends
everything as before. `assistant check` prints the size with and without lazy loading.

**Limits and audit.**

- Per-turn and per-day caps on model calls, tool calls, tokens and estimated cost stop runaway
  loops, and the message says which limit was hit.
- Every model call, tool call, approval, refusal, schedule run and command is audited (no message
  text or tool output): `assistant audit --since 1d`.

**Privacy.**

- The model provider sees your conversation and the tool results it is given. With a local
  model, nothing leaves the laptop except Telegram messages.
- Secrets never leave the Keychain except to authenticate. Anything outgoing to Telegram is
  scrubbed of known secrets, including text the model writes.
- Tool output and memory are given to the model as labelled, untrusted data. An instruction
  hidden in them can't bypass the policy or an approval, and the approval message always shows
  the real call.

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
| `[gitlab]` | optional: `url`, `keychain_service`, `keychain_account`, `ca_bundle`, `timeout_s`, `max_archive_mb`, plus `[[gitlab.project]]` and `[[gitlab.deploy]]`; see [GitLab](#gitlab-optional) |
| `[memory]` | optional: `enabled`, `store` (default agent-memory's store), `project` (default `vps-<name>`), `writable`, `pinned_chars`; see [Memory](#memory) |

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
| `gitlab_token_missing` | Store the token: `security add-generic-password -U -s vps-mcp-gitlab -a <account> -w`. |
| `gitlab_auth` | The token expired or was revoked. Store a new one with the same command. |
| `gitlab_scope` | The token lacks a scope or role for that action. `gitlab-check` lists scope gaps. |
| `gitlab_rate_limited` | GitLab's rate limit was hit. Retry after the given number of seconds. |
| A deploy reports `rolled_back` | A service failed its health check after the switch. The previous release is active again; the response includes the service's last log lines. |
| A new session doesn't seem to know the memory | Memory is searched, not injected: ask it to "search memory for …". Only pinned items and open issues arrive at connect. |
| `Memory unavailable` in the instructions | Run `agent-memory doctor`. The store may be unwritable or a symlink, which is refused. |
| `check-config` mentions `migrate-004` | You have feature-004 notes; import them with the command it prints. |
| Job tools missing | No `[[migration.job]]` is declared, or the client hasn't restarted the server since you added one. |

## Development

```bash
uv run pytest            # unit, contract and negative suites: no VPS needed (≈820 tests)
uv run ruff check && uv run ruff format --check && uv run mypy
```

- **Fakes**: tests run against a simulated VPS (`tests/fakes.py`) and a simulated PostgreSQL
  cluster (`tests/fakepg.py`).
- **Planted defects**: one suite plants defects and checks every one is caught with the right
  severity.
- **Negative suites**: over 300 disallowed requests, each asserting that nothing was changed
  remotely.

**Integration tests** run against a disposable VM. Never use them on a production server.

```bash
tests/integration/provision.sh      # creates a Multipass VM with nginx, Docker and PostgreSQL
uv run pytest -m integration --vps-config tests/integration/vps-mcp.test.toml
```

Project layout:

```text
src/vps_mcp/
├── server.py            # MCP tools (services) + migration/jobs/memory registration, pinned core
├── cli.py               # serve | check-config | trust-host | bootstrap | gitlab-check
├── config/              # policy file models and loader
├── remote/              # SSH connection, fixed-template executor, redaction
├── backends/            # systemd + apt/dnf, docker/podman
├── core/                # guard (allowlist, confirm, audit), actions, deploy, changes, queries
├── store/               # SQLite: audit, revisions, deployed services, migration tables
├── migration/           # catalogue, snapshot/diff/risk, copies, runner, rehearsal, checks
├── jobs/                # declared jobs: integrity, params, launcher, progress, guard, report
├── memory/              # adapter onto agent-memory: generated facts, tool registration
└── gitlab/              # GitLab: Keychain token, client, access, read/items/ci, archive, deploy
src/assistant/           # personal assistant: config, secrets, state, telegram, providers,
                         #   toolhub (policy + previews), approvals, memory, budget, agent loop,
                         #   scheduler, commands, app, service, cli
src/agent_memory/        # standalone memory: model, files, index (FTS5), policy, ranking, store,
                         #   pinned core, MCP tools/server, CLI, migrate-004, bench
specs/                   # speckit spec, plan, research, contracts, tasks per feature
```
