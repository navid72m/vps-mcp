# Contract: GitLab Tools

There are 15 tools, registered when `[gitlab]` is configured.

**Inputs and errors**
- Every tool takes `project` as an **alias**.
- The required permission is checked before any network call. Failing it returns `not_permitted`.
- An unknown alias returns `not_permitted` with the message `project '<alias>' is not an allowlisted GitLab project`. The message is the same for every unknown alias.
- A missing token returns `permission_denied` with `subtype: gitlab_token_missing` and the setup command.

**GitLab error mapping**

| GitLab response | Error category |
|---|---|
| 401 | `permission_denied/gitlab_auth` |
| 403 | `permission_denied/gitlab_scope` |
| 404 | `not_found` |
| 429 | `timed_out/gitlab_rate_limited`, with `retry_after_s` |
| network failure | `connection_failure/gitlab_unreachable` |
| timeout | `timed_out` |

**Output**
- Text written by other people is wrapped as `{"kind": "external_content", …}`.

## Read (permission: `read`)

| Tool | Inputs | Output (compact) |
|---|---|---|
| `gitlab_projects` | — | `[{alias, path, default_branch, permissions, deploy_targets[]}]` |
| `gitlab_refs` | `project`, `kind` (`branches` / `tags` / `both`, default `both`), `search?`, `limit` ≤ 200 (default 50) | `[{name, kind, sha12, date, title}]`, `truncated` |
| `gitlab_tree` | `project`, `ref?` (default branch), `path?`, `recursive` (bool, default false), `limit` ≤ 200 | `[{path, type, size?}]`, `truncated` |
| `gitlab_file` | `project`, `path`, `ref?`, `start_line?`, `max_lines?` (≤ 2000) | `{path, ref, sha12, size, lines, content: external_content}`, or `{binary: true, size, sha256}` |
| `gitlab_commits` | `project`, `ref?`, `path?`, `since?`, `until?`, `limit` ≤ 50 (default 20), `sha?` | List: `["sha12 date author: title"]`. With `sha`: `{sha, author, date, message: external_content, stats, files: ["M path", …≤50]}` |
| `gitlab_compare` | `project`, `from`, `to`, `path?` | Without `path`: `{commits: [...≤50], files: ["A/M/D/R path +x -y"], truncated}`. With `path`: `{diff: external_content (≤ 2000 lines)}` |

## Issues and merge requests (permission: `read`)

| Tool | Inputs | Output |
|---|---|---|
| `gitlab_list_items` | `project`, `kind` (`issue` / `mr`), `state` (`opened` / `closed` / `merged` / `all`, default `opened`), `labels?`, `search?`, `author?`, `limit` ≤ 50 | `["#iid [state] title (author, updated, labels)"]` |
| `gitlab_read_item` | `project`, `kind`, `iid`, `notes_limit` ≤ 50 (default 20) | `{iid, title, state, author, labels, web_ref, description: external_content, notes: [external_content…], for MRs: source, target, pipeline_status, merge_status}` |

## Issues and merge requests: writes

| Tool | Permission |
|---|---|
| `gitlab_write` | `write_issues` / `write_mrs` / `close_issues` |

**Inputs**:

- `project`
- `action`, one of:
  - `create_issue`: needs `title` and `description`; optional `labels`
  - `comment`: needs `kind`, `iid` and `body`
  - `create_mr`: needs `source_branch`, `target_branch`, `title` and `description`; optional `labels`
  - `close_issue`: needs `iid`
- `confirm` (bool)

**Behaviour**:

- `confirm=false` returns `{preview: true, request: {action, project, fields (masked, with footer)}}`.
- `confirm=true` sends the request and returns `{web_ref, iid?, note_id?, audit_id}`.
- For `create_mr`, both branches must exist.
- `close_issue` follows research G8.
- There is no merge action.

## CI/CD

**Read tools** (permission: `read`)

| Tool | Inputs | Output |
|---|---|---|
| `gitlab_pipelines` | `project`, `ref?`, `status?`, `limit` ≤ 50 (default 10), `pipeline_id?` | List: `["id status ref sha12 dur failed:[jobs]"]`. With `pipeline_id`: `{pipeline, jobs: ["id name stage status dur"]}` |
| `gitlab_job_log` | `project`, `job_id`, `lines` (default 200, maximum 2000) | `{job, status, lines, log: external_content, truncated}`, masked |

**Write tool**: `gitlab_ci` (permission: `ci`)

| Input | Rule |
|---|---|
| `project` | alias |
| `action` | `retry_job`, `cancel_job`, `retry_pipeline`, `cancel_pipeline` or `trigger_pipeline` |
| `job_id` / `pipeline_id` | identifies the job or pipeline for the retry and cancel actions |
| `ref` | needed for `trigger_pipeline`; must match the project's `trigger_refs` |
| `confirm` | false previews, true acts |

## Deploy (permission: `deploy`)

### `deploy_targets` (read-only)

**Input**: none.

**Output**: a list, one entry per target:

```text
{name, project, refs, destination,
 services, current: {sha12, ref, deployed_at, release} | null,
 previous_releases: [...]}
```

The active release is read from the VPS with `readlink` and the release marker.

### `deploy_from_gitlab` (state-changing)

Named `deploy_from_gitlab` so it can't be confused with feature 001's `deploy_service`.

| Input | Rule |
|---|---|
| `target` | target name |
| `ref` | e.g. `v1.5`, `main`, `tag:v1.5`, `branch:main`, or a full commit SHA reachable from an allowed ref |
| `confirm` | `false` returns the plan; `true` deploys |
| `plan_id` | required when `confirm=true` |

**Behaviour**:

- `confirm=false` returns the plan from data-model §4.
- `confirm=true` with `plan_id` runs, in order:
  1. lock the target;
  2. check interlocks;
  3. re-plan and compare against the stored plan;
  4. download and sanitise the archive;
  5. check free disk;
  6. stream the archive and extract it into a new release;
  7. run `chown`/`chmod` and write the marker;
  8. switch the symlink;
  9. restart the services and check health;
  10. roll back automatically on failure;
  11. prune old releases;
  12. record the result and audit it.

**Output**: `{deploy_id, status, commit, release, services: [...], rolled_back?: {reason, logs}}`

**Refusals**:

| Error | When |
|---|---|
| `read_only` | the server is in read-only mode |
| `invalid_input` | the ref isn't allowed |
| `conflict` | the plan drifted (returned with a fresh plan), or the target is busy |
| `failed` | the pipeline isn't successful and the target sets `require_pipeline_success` |
| `insufficient_disk` | not enough free space on the VPS |

### `rollback_deploy` (state-changing)

| Input | Rule |
|---|---|
| `target` | target name |
| `release` | optional; defaults to the previous release |
| `confirm` | `false` previews: current release → release to restore, plus the services |

**Behaviour**: switches the symlink, restarts the services and checks health. It never deletes the release it rolls back from.

## CLI

### `vps-mcp gitlab-check --config <file>`

Prints:

- the instance;
- the token user;
- the scopes;
- the expiry date, with a warning when it expires within 14 days;
- for each project: whether it resolves, its numeric ID, and whether the token's scopes cover the declared permissions;
- for each deploy target: the destination and the active release, read from the VPS.

It exits non-zero on any error and never prints the token.

### Sudoers

`vps-mcp bootstrap --print-sudoers --with-gitlab` adds this rule:

```text
Cmnd_Alias VPSMCP_DEPLOY = /usr/bin/tar, /usr/bin/mkdir, /usr/bin/ln, /usr/bin/mv,
    /usr/bin/chown, /usr/bin/chmod, /usr/bin/rm, /usr/bin/readlink, /usr/bin/sha256sum,
    /usr/bin/cat, /usr/bin/df, /usr/bin/find, /usr/bin/curl
```

As with features 001–003, the guarantee comes from the server issuing only its own validated argv. The README states that these binaries are root-equivalent.
