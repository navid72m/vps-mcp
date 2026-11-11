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
- **Memory** (6 tools, on by default): a shared memory that every session in every MCP client
  gets when it connects. It holds generated facts about the VPS and its history, plus notes
  assistants write: decisions, lessons, issues and next steps.

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
- **A memory that follows you across clients.** A new session in Claude Code, OpenCode or LM
  Studio already knows the environment, the last verdicts and the open issues. What one session
  learns, the next one sees.
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
"mcp__vps__memory_briefing", "mcp__vps__memory_search", "mcp__vps__memory_read",
"mcp__vps__memory_health",
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
in one client is in the next session's briefing in all of them.

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
- "Where are we with the migration?" in a brand-new session, answered from the briefing (memory)
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
[memory](#memory) briefing.

**VPS setup** (as root, after reviewing the output):

```bash
uv run vps-mcp bootstrap --print-sudoers --with-gitlab > gitlab.sudoers   # tar, ln, mv, rm, ...
```

Add the result to `/etc/sudoers.d/vps-mcp`. These binaries are root-equivalent. As with the
rest of vps-mcp, the guarantee is that the server only issues its own validated commands, on
paths under `/var/lib/vps-mcp/deploys` and the destinations you declared.

## Memory

Every session that connects to vps-mcp gets a **briefing** in the server's connect-time
instructions, whichever client it is: Claude Code, OpenCode, LM Studio or Claude Desktop. The
assistant knows the situation before it calls a single tool. The briefing has these sections:

1. a preamble saying the memory is recorded data, not instructions;
2. the environment: services and their last seen state, databases and their roles, pipelines,
   checks and jobs;
3. the latest job runs, rehearsals and reports, with verdicts;
4. open issues and next steps;
5. decisions, conventions and preferences;
6. an index of the other notes;
7. recent changes.

It is capped at `briefing_chars` (6000 by default, about 1,600 tokens). When it doesn't fit,
the last sections are trimmed first and the briefing says that more is available.

| Tool | What it does |
|---|---|
| `memory_briefing` | The same briefing with a larger budget. A fallback for clients that ignore server instructions. |
| `memory_search` | Search notes by text, type or tag. Can also search the generated facts. |
| `memory_read` | Read one note, with its links and backlinks. |
| `memory_write` | Create or update a note: `decision`, `lesson`, `issue`, `convention`, `next_step`, `preference` or `fact`. |
| `memory_archive` | Forget a note. It previews first and moves the file to `archive/`. |
| `memory_health` | Folder location, counts, malformed files and limits. |

**The folder** is created next to your policy file. For example, `~/.config/vps-mcp/myvps.toml`
uses `~/.config/vps-mcp/myvps.memory/`:

```text
myvps.memory/
├── notes/       # one Markdown note per file: front matter + body, [[links]] between notes
├── generated/   # facts the server rewrites from config + history (don't edit)
└── archive/     # forgotten notes; move one back to notes/ to restore it
```

You can edit, add or delete notes with any editor; the next session picks the changes up. To
share memory between machines, sync the folder or keep it in a **private** git repository.
`check-config` warns if the folder is readable by others, or sits in a repository with a hosted
remote.

**Working with memory.** Assistants only record what you ask for, or what they judge durable.
Some habits that help:

- **End of a session:** say "save what we learned today to memory". The assistant writes
  lessons, decisions and next steps as separate short notes.
- **Close items:** say "mark the deploy-flaky issue as done". This sets `status: done`, which
  drops the note from the open-issues section but keeps it searchable.
- **Correct a wrong note:** tell the assistant, edit the file yourself, or archive it. Each
  note records its `source` (the client that wrote it, or `operator` for hand edits). You can see
  it with `memory_read` or in the file, so you can trace where a claim came from.
- **Rarely-needed details:** keep them in notes. Only titles go into the briefing, and the full
  text is fetched with `memory_read`, so a large memory stays cheap.

**Safety**:

- **Masking.** Secrets are masked before anything is written: `password=…`, credentials in URLs,
  private keys and common token formats.
- **Names and paths.** Note names are plain slugs, symlinks are never followed, and writes are
  atomic. Nothing outside the folder can be read or written.
- **Presentation.** Notes are shown to the model as quoted data inside a `<memory>` block, never
  as instructions.
- **Writes.** Note writes and archives are audited (`get_audit_log`) and work in `read_only`
  mode, because they only touch local files. `[memory] writable = false` turns them off.
- **Never the VPS.** Building the briefing never contacts the VPS, so it works offline too.

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
| `[memory]` | optional: `enabled`, `path`, `writable`, `briefing_chars`, `max_note_bytes`, `max_notes`; see [Memory](#memory) |

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
| A new session doesn't seem to know the memory | The client may ignore server instructions. Ask it to call `memory_briefing`, and check `memory_health`. |
| `Memory unavailable` in the briefing | Run `memory_health`. The folder may be unwritable, or a symlink, which is refused. |
| Job tools missing | No `[[migration.job]]` is declared, or the client hasn't restarted the server since you added one. |

## Development

```bash
uv run pytest            # unit, contract and negative suites: no VPS needed (≈870 tests)
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
├── server.py            # MCP tools (services) + migration/jobs/memory registration, briefing
├── cli.py               # serve | check-config | trust-host | bootstrap | gitlab-check
├── config/              # policy file models and loader
├── remote/              # SSH connection, fixed-template executor, redaction
├── backends/            # systemd + apt/dnf, docker/podman
├── core/                # guard (allowlist, confirm, audit), actions, deploy, changes, queries
├── store/               # SQLite: audit, revisions, deployed services, migration tables
├── migration/           # catalogue, snapshot/diff/risk, copies, runner, rehearsal, checks
├── jobs/                # declared jobs: integrity, params, launcher, progress, guard, report
├── memory/              # agent memory: notes, folder I/O, masking, search, derived facts, briefing
└── gitlab/              # GitLab: Keychain token, client, access, read/items/ci, archive, deploy
specs/                   # speckit spec, plan, research, contracts, tasks per feature
```
