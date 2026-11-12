# vps-mcp: natural-language usage examples

These are things you can say to an assistant (Claude Code, OpenCode, Antigravity, LM Studio, Claude Desktop, …)
that has vps-mcp connected. The assistant picks the tool, so you never have to name one. Each example shows the tool
it maps to, so you can see what is available.

The names are placeholders; use your own:

| Placeholder | Stands for |
|---|---|
| `nginx`, `odoo`, `worker`, `api` | services |
| `shop_17` / `shop_19` | the Odoo 17 source and the upgraded test database |
| `core` | a migration pipeline |
| `full-rehearsal` | a declared job |
| `addons` | an allowlisted GitLab project and its deploy target |

**Conventions**

- **Confirm first.** Tools that change something answer first with a **preview** or **plan**. Nothing happens until
  you say "yes, go ahead" (the assistant then calls the tool again with `confirm=true`).
- **Read-only mode.** In read-only mode every state-changing request is refused. Memory notes are the exception:
  they are local files.
- **Plain language is enough.** A tool name in brackets, such as (`list_services`), is only there for reference.

---

## 1. Looking at the server

| You say | Tool |
|---|---|
| "What's running on my VPS?" / "Is anything down?" / "List only the containers." | `list_services` |
| "Is odoo healthy? Why did worker fail last night?" | `get_service_status` |
| "Show the last 200 lines of odoo's logs." / "Any errors in api's logs since 9am?" / "Grep nginx logs for 502." | `get_service_logs` |
| "Show me nginx's config." / "What image, ports, env and networks does api run with?" | `get_service_config` |
| "How's the server doing: load, memory, disk?" / "Which service uses the most RAM?" | `get_resource_usage` |
| "What changed on the server today?" / "Who restarted odoo, and when?" / "Show refused actions from this week." | `get_audit_log` |
| "What earlier versions of api's config are stored?" / "Which revision can I roll back nginx to?" | `list_revisions` |

## 2. Controlling services

| You say | Tool |
|---|---|
| "Start worker." | `start_service` |
| "Stop api for maintenance." Protected services ask for confirmation. | `stop_service` |
| "Restart odoo." / "Restart nginx, I confirm." | `restart_service` |
| "Make worker start at boot." / "Don't start api automatically after a reboot." | `set_boot_start` |

## 3. Deploying and changing services

| You say | Tool |
|---|---|
| "Deploy redis:7 as a container named cache on the odoo-net network." Shows a preview, then waits for your OK. | `deploy_service` |
| "Install the redis-server package as an OS service." | `deploy_service` |
| "Update api to ghcr.io/myorg/api:1.2." Health-checked, with automatic rollback if unhealthy. | `update_service` |
| "Upgrade nginx to the latest approved package version." | `update_service` |
| "Set LOG_LEVEL=debug on api." / "Publish port 8080 on cache." | `configure_service` |
| "Replace nginx.conf with this version …" The diff is shown first, then validated. | `configure_service` |
| "The last change to api broke it, roll it back." / "Restore nginx's config from two revisions ago." | `rollback_service` |
| "Remove the cache container." / "Uninstall redis-server and delete its data." Always previewed. | `remove_service` |

## 4. Understanding databases (migration analysis)

| You say | Tool |
|---|---|
| "Which databases can you analyse?" / "Are there rehearsal copies lying around?" | `db_list` |
| "Give me an overview of shop_19: size, biggest tables, modules, problems." | `db_overview` |
| "Which modules in shop_19 are still 'to upgrade'?" / "Which installed modules have no code on disk?" / "List the modules still on version 17." | `db_modules` |
| "Show the structure of res_partner in shop_19." / "What indexes and constraints does account_move have?" | `db_table` |

## 5. Comparing and judging a migration

| You say | Tool |
|---|---|
| "Compare shop_17 with shop_19. Is it safe to promote?" / "What did the upgrade lose?" | `db_compare` |
| "Show only the blocking findings of that report." / "Next page of warnings about row counts." | `get_report` |
| "What were the verdicts of the last reports?" / "List this week's comparisons." | `list_reports` |

## 6. Rehearsing a pipeline on a disposable copy

| You say | Tool |
|---|---|
| "Rehearse the core pipeline on a copy of shop_17. Show me the plan first." Then: "Go." | `rehearse` |
| "How is the rehearsal going?" / "Which step is it on?" / "What was the verdict?" | `rehearsal_status` |
| "Stop the rehearsal, it's taking too long." | `cancel_rehearsal` |
| "Summarise the last core run I started myself: which steps failed, and what do the logs say?" | `pipeline_last_run` |
| "Run the post-checks against shop_19." / "Run the fixer check on shop_19." Write checks need confirmation. | `run_check` |
| "Drop the expired rehearsal copies." / "Delete copy cpy_123." | `drop_copies` |

## 7. Declared jobs (your own long scripts)

| You say | Tool |
|---|---|
| "Which jobs can you run?" / "When did full-rehearsal last run and how did it end?" | `list_jobs` |
| "Plan a full rehearsal." Shows the script fingerprint, what it rebuilds and stops, and free disk. Then: "Start it." / "Run full-rehearsal with RUN_NAME=nightly_3." | `run_job` |
| "How's the rehearsal job doing?" / "Which step is it on and how long has it been running?" / "Show the final report and verdict." | `job_status` |
| "Cancel the running job." | `cancel_job` |

## 8. Memory (shared across sessions, clients and projects)

| You say | Tool |
|---|---|
| "What do we know about upgrading the database?" / "Search memory for stock valuation." / "Any open issues recorded?" | `memory_search` |
| "Show where the postgres.version fact came from." / "What did this decision replace?" | `memory_get` |
| "Remember as a decision: promote only when the verdict is safe_to_promote; we rejected promoting on warnings." / "Save what we learned today as a lesson." / "Record that production runs PostgreSQL 17." | `memory_save` |
| "PostgreSQL was upgraded to 17, update that fact." / "That fact was wrong, invalidate it." / "Mark the flaky-deploy issue as done." / "Pin the backup procedure." | `memory_update` |
| "Forget the note about the old staging server." Previewed, then archived. | `memory_archive` |

## 9. GitLab: reading code and history

| You say | Tool |
|---|---|
| "Which GitLab projects can you see, and what may you do on each?" | `gitlab_projects` |
| "List the branches and tags of addons." / "Which tags start with v1?" | `gitlab_refs` |
| "What's in the stock_fix folder on main?" / "List every file in addons at tag v1.5." | `gitlab_tree` |
| "Show models/stock.py on main." / "Show lines 200–400 of the deploy script at v1.5." | `gitlab_file` |
| "What was committed to addons this week?" / "Who changed models/stock.py, and when?" / "Show commit abc123 in full." | `gitlab_commits` |
| "What changed between v1.4 and v1.5?" / "Show the diff of models/stock.py between those tags." | `gitlab_compare` |

## 10. GitLab: issues and merge requests

| You say | Tool |
|---|---|
| "List the open issues in addons." / "Any open MRs labelled migration?" / "Find issues mentioning valuation." | `gitlab_list_items` |
| "Read issue #42 with its comments." / "What's the status of MR !17 and did its pipeline pass?" | `gitlab_read_item` |
| "Open an issue in addons with the blocking findings of the last comparison." The exact text is previewed. | `gitlab_write` |
| "Comment 'fixed in v1.5' on issue #42." / "Open an MR from fix/stock to main titled 'Stock valuation fix'." | `gitlab_write` |
| "Close the issue you opened yesterday." Only issues vps-mcp created, unless the project allows more. Merging is never possible. | `gitlab_write` |

## 11. GitLab: CI/CD

| You say | Tool |
|---|---|
| "Did the pipeline for v1.5 pass?" / "Show the last pipelines on main and which jobs failed." | `gitlab_pipelines` |
| "Why did the test job fail? Show the end of its log." | `gitlab_job_log` |
| "Retry that failed job." / "Cancel pipeline 900." / "Run the pipeline on main." Previewed, and only on allowed branches. | `gitlab_ci` |

## 12. GitLab: deploying to the VPS

| You say | Tool |
|---|---|
| "What's deployed on the server from GitLab right now?" / "Which releases of addons are kept?" | `deploy_targets` |
| "Deploy tag v1.5 of addons." The plan shows the commit, changed files, pipeline status, local edits and services. Then: "Go." | `deploy_from_gitlab` |
| "Update addons to main." / "Deploy commit 3f2a… to addons." The commit must be on an allowed branch or tag. | `deploy_from_gitlab` |
| "The new addons release is broken, roll back." / "Switch addons back to the 2026-11-01 release." | `rollback_deploy` |

---

## Longer conversations that chain tools

**Morning check**
> "Anything wrong on the server? Check the services, the disk, and whether last night's rehearsal finished."

The assistant uses `list_services`, then `get_resource_usage`, then `job_status` (or `rehearsal_status`).

**Upgrade review**
> "Compare shop_17 and shop_19. If it's not safe to promote, open an issue in addons listing the blocking findings
> and remember the main blocker as an open issue."

The assistant runs `db_compare` and `get_report`. With your OK it calls `gitlab_write` (`create_issue`), then
`memory_save` (type `episodic`, state `open`).

**Ship a fix**
> "What changed in addons since the version that's deployed? Did CI pass? Then deploy it and tell me if odoo comes
> back healthy."

The assistant calls `deploy_targets` and `gitlab_compare`, then `gitlab_pipelines`. Next comes `deploy_from_gitlab`:
first the plan, then the deploy after you confirm. It finishes with `get_service_status`.

**Incident**
> "odoo is slow and throwing errors. Find out why, restart it if needed, and write down what you found."

The assistant checks `get_service_status`, `get_service_logs` and `get_resource_usage`. If needed it calls
`restart_service` (after you confirm), then records what it found with `memory_save` (type `episodic`).

**Full rehearsal overnight**
> "Plan a full rehearsal, start it after I confirm, and tomorrow give me the verdict."

The assistant uses `run_job` twice (plan, then start). The next day, in any client, it uses `job_status`. The
job's latest outcome is also a memory fact.

**New session, different client**
> "Where are we with the migration?"

The assistant gets the pinned core at connect (key procedures, decisions and open issues), then uses
`memory_search` and `memory_get` for the rest: latest verdicts, deploys, lessons. You don't re-explain anything.

---

## Tool index

53 tools in total. Some are present only when the matching section is configured: migration, jobs and GitLab are
optional, and memory is on by default.

| Area | Tools |
|---|---|
| Services: look | `list_services`, `get_service_status`, `get_service_logs`, `get_service_config`, `get_resource_usage`, `get_audit_log`, `list_revisions` |
| Services: control | `start_service`, `stop_service`, `restart_service`, `set_boot_start` |
| Services: lifecycle | `deploy_service`, `update_service`, `configure_service`, `rollback_service`, `remove_service` |
| Migration: understand | `db_list`, `db_overview`, `db_modules`, `db_table` |
| Migration: compare | `db_compare`, `get_report`, `list_reports` |
| Migration: rehearse | `rehearse`, `rehearsal_status`, `cancel_rehearsal` |
| Migration: your runs, checks, cleanup | `pipeline_last_run`, `run_check`, `drop_copies` |
| Declared jobs | `list_jobs`, `run_job`, `job_status`, `cancel_job` |
| Memory | `memory_search`, `memory_get`, `memory_save`, `memory_update`, `memory_archive` |
| GitLab: read | `gitlab_projects`, `gitlab_refs`, `gitlab_tree`, `gitlab_file`, `gitlab_commits`, `gitlab_compare` |
| GitLab: issues and MRs | `gitlab_list_items`, `gitlab_read_item`, `gitlab_write` |
| GitLab: CI | `gitlab_pipelines`, `gitlab_job_log`, `gitlab_ci` |
| GitLab: deploy | `deploy_targets`, `deploy_from_gitlab`, `rollback_deploy` |
