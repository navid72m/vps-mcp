# Data Model: GitLab Integration

## 1. Configuration

### `[gitlab]`

The section is optional. If it is missing, there are no GitLab tools.

| Key | Type | Default | Rule |
|---|---|---|---|
| `url` | https URL | — | Required. `https://` only. No path beyond an optional sub-path; no credentials in the URL. |
| `keychain_service` | str | `vps-mcp-gitlab` | 1–128 chars, `[A-Za-z0-9._-]` |
| `keychain_account` | str | — | Required. 1–128 chars, `[A-Za-z0-9._@-]` |
| `ca_bundle` | absolute path | none | PEM file on the laptop |
| `timeout_s` | int | 30 | 5–120 |
| `max_archive_mb` | int | 200 | 1–2000 |

### `[[gitlab.project]]`

At most 50 entries.

| Key | Type | Default | Rule |
|---|---|---|---|
| `alias` | slug | — | `^[a-z0-9][a-z0-9-]{0,39}$`, unique |
| `id` | int | — | Set exactly one of `id` or `path`. |
| `path` | str | — | `group/…/project`, `^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)+$`. Set exactly one of `id` or `path`. |
| `permissions` | list | `["read"]` | Subset of `read`, `write_issues`, `write_mrs`, `ci`, `deploy` |
| `close_issues` | bool | false | Requires `write_issues` |
| `close_any_issue` | bool | false | Requires `close_issues` |
| `trigger_refs` | list[glob] | `[]` | Requires `ci`. Globs over branch and tag names, e.g. `main`, `release/*`, `v*` |

### `[[gitlab.deploy]]`

At most 20 entries.

| Key | Type | Default | Rule |
|---|---|---|---|
| `name` | slug | — | `^[a-z0-9][a-z0-9-]{0,39}$`, unique |
| `project` | alias | — | Must have the `deploy` permission |
| `refs` | list[glob] | — | Required, at least 1. Matched against `branch:<name>` / `tag:<name>`. Plain globs match both. |
| `destination` | absolute path | — | Must pass the rules in research G10. Unique; no target may sit inside another. |
| `owner` / `group` | unix user | `root` / `root` | Same as feature 001's `_unix_user` rule |
| `include` / `exclude` | list[glob] | `[]` | Repo-relative globs; `**` allowed |
| `services` | list[service name] | `[]` | Must be declared `[[service]]`s and not remote-access units |
| `health_url` | http(s) URL | none | Probed on the VPS after the restart |
| `require_pipeline_success` | bool | false | |
| `keep_releases` | int | 3 | 1–20 |

`check-config` reports every violation.

## 2. State DB: schema v5

```sql
CREATE TABLE IF NOT EXISTS gitlab_deploys (
  deploy_id      TEXT PRIMARY KEY,          -- dep_<hex>
  target         TEXT NOT NULL,
  kind           TEXT NOT NULL,             -- deploy | rollback
  plan_hash      TEXT NOT NULL,
  plan_json      TEXT NOT NULL,
  status         TEXT NOT NULL,             -- planned | running | succeeded | failed | rolled_back
  commit_sha     TEXT,
  ref            TEXT,
  release_dir    TEXT,
  previous_dir   TEXT,
  pipeline_status TEXT,
  error          TEXT,
  audit_id       INTEGER,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gitlab_deploys_target ON gitlab_deploys (target, created_at DESC);
```

**Active release**: the authority is the VPS. It is the `readlink` of the destination plus that release's `.vps-mcp-release` JSON. The DB row is the history.

**Status transitions**:

```text
planned → running → succeeded
                  → failed        (before the switch: nothing changed on the destination)
                  → rolled_back   (after the switch: the health check failed and the previous release is active again)
```

## 3. Audit actions (new)

- `gitlab_issue`
- `gitlab_comment`
- `gitlab_mr`
- `gitlab_close`
- `gitlab_ci`
- `deploy`
- `rollback_deploy`

`deploy` already exists for service deploys, so GitLab deploys use `service_name=<target>` and `kind="gitlab"`.

## 4. Deploy plan (`plan_json`)

```json
{
  "target": "addons", "project": "addons", "ref": "tag:v1.5",
  "commit": {"sha": "…40…", "short": "…12…", "author": "…", "date": "…", "title": "…"},
  "current": {"sha": "…", "release": "20261101T…-abc", "managed": true,
              "local_changes": ["models/x.py"]},
  "changes": {"added": 3, "modified": 5, "removed": 1, "files": ["M models/x.py", "…≤50"],
              "truncated": false},
  "pipeline": {"status": "success", "id": 123},
  "unmanaged_existing_dir": false,
  "services": ["odoo-v19-repaired"],
  "disk": {"free_bytes": 0, "archive_bytes_est": 0},
  "warnings": ["…"],
  "plan_id": "…", "expires_at": "…"
}
```

## 5. Release marker (`<release>/.vps-mcp-release`)

```json
{"target": "addons", "sha": "…", "ref": "tag:v1.5", "deployed_at": "…", "deploy_id": "dep_…"}
```

## 6. Untrusted content envelope

```json
{"kind": "external_content", "author": "…", "created_at": "…", "text": "…masked, capped…"}
```
