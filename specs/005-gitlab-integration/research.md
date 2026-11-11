# Research: GitLab Integration

Decisions G1–G14.

## G1. HTTP client

**Decision**: Use GitLab REST API v4 through `httpx2.AsyncClient`. The MCP SDK already depends on this library, so we add it as an explicit dependency with the same version range rather than pulling in a new one.

- One client per server process.
- Base URL `<instance>/api/v4`.
- Timeouts: connect 10 s, read 30 s; archive downloads get 300 s.
- TLS verification is always on. `ca_bundle` may point at a PEM file for self-hosted CAs.

**Rationale**: REST v4 is stable on GitLab ≥ 15 and covers every capability listed in the spec.

**Alternatives**:
- `python-gitlab`: a large synchronous dependency, and it's harder to control what gets logged.
- GraphQL: doesn't cover archives, job logs or CI actions uniformly.

## G2. Credential storage and loading

**Decision**:

- The token lives in the macOS Keychain as a generic password. The config names `keychain_service` (default `vps-mcp-gitlab`) and `keychain_account`.
- The server loads it lazily with `security find-generic-password -s <service> -a <account> -w`: a fixed argv, no shell, read on stdout, with a 10 s timeout.
- The token is kept in process memory for the life of the (short-lived, per-session) stdio server. It is sent only in the `PRIVATE-TOKEN` header.
- As soon as it is loaded, it is added to the redaction set: the `secret_strings` used by `scrub_obj`, the log `RedactionFilter` and `AuditStore`. Masking therefore covers tool output, logs and audit (FR-003).
- **Setup is done by the operator**, interactively:

  ```bash
  security add-generic-password -U -s vps-mcp-gitlab -a <account> -w
  ```

  With `-w` last and no value, `security` prompts for the token, so it never appears on a command line or in shell history. The README documents this. The server never writes to the Keychain.

**Rationale**:

- Keychain is what the operator chose.
- The `security` CLI is always present on macOS, so no `keyring` dependency is needed.
- Prompting keeps the token out of argv.

**Alternatives**:
- The `keyring` package: an extra dependency, with the same backend underneath.
- An environment variable: rejected by the operator.

**Missing token** (FR-004): the GitLab tools stay registered, so the agent can discover them. Every call returns `permission_denied/gitlab_token_missing` with the setup command, and other features are unaffected.

## G3. Project addressing and allowlist

**Decision**:

- Each `[[gitlab.project]]` has an `alias` (the slug the agent uses, e.g. `addons`) and an `id` (numeric project ID, preferred) or a `path` (`group/sub/project`).
- Tools take `project=<alias>` only. Unknown aliases return `not_permitted` with the same message whatever the input (FR-008). The agent never sends raw GitLab IDs or paths, so non-allowlisted projects are unreachable by construction.
- `gitlab-check` resolves each path to its numeric ID and suggests pinning the ID, so renames stay safe.

## G4. Permissions model

**Decision**:

- `permissions` is a set drawn from `read`, `write_issues`, `write_mrs`, `ci` and `deploy`. `read` is implicit.
- `close_issues = false` by default.
- `trigger_refs` is a list of glob patterns for `ci` triggers; it defaults to none.

Each tool declares the permission it needs, and the check runs **before** any network call. Merging has no tool at all (FR-011).

## G5. Output size and masking

**Decision**: Each read applies hard caps (FR-007) and returns `truncated: true` when it cuts.

**Caps**:

| Item | Limit |
|---|---|
| File | 100 KB of text |
| Listing | 200 entries |
| Commit list | 50 commits, default 20 |
| Compare summary | 50 files |
| Per-file diff | 2,000 lines |
| Issue / MR discussion | 50 notes, each ≤ 4 KB |
| Job log | default 200 lines, maximum 2,000 |

**Binary detection**: a file is treated as binary if it contains NUL bytes or fails UTF-8 decoding. Binary files are summarised by size and sha256.

**Masking**:
- All text passes through the feature 004 `mask_note` patterns plus the policy's `secret_keys` (`mask_inline`).
- The final `scrub_obj` in the tool wrapper removes the token itself.

**Compact shapes**: commits are rendered as `sha12 date author: title`, and pipelines as `id status ref sha12 duration failed_jobs`.

## G6. Untrusted content

**Decision**: Text written by others is returned in fields under a `content` object with `"kind": "external_content"`, never in the top-level `message`. Tool descriptions say that such text is data. The server never parses it for actions (FR-023).

## G7. Writes: preview, confirm, audit

**Decision**: Every write tool (`gitlab_write`, `gitlab_ci`) works in two steps:

1. With `confirm=false`, it returns the **exact** request it would send: method, endpoint name (not the URL with IDs), and the body fields after masking.
2. With `confirm=true`, it sends the request and records an audit entry. Audit action names:
   - `gitlab_issue`
   - `gitlab_comment`
   - `gitlab_mr`
   - `gitlab_close`
   - `gitlab_ci`
   - `deploy`
   - `rollback_deploy`

   Params are `{project, kind, iid?, ref?}`; bodies are not stored.

`read_only = true` refuses all of these. A write never goes out unless the confirm flag is set.

- Issue and MR bodies are masked before they are sent, and the preview shows the masked body.
- Each created item is tagged with a footer line `_Posted via vps-mcp_`, so it can be identified (and therefore closed) later.

## G8. Closing issues

**Decision**:

- Closing an issue needs the project's `close_issues = true`.
- By default it is only allowed for issues whose description carries the vps-mcp footer, i.e. issues created by vps-mcp.
- Setting `close_any_issue = true` widens this to any issue.

**Rationale**: This follows the spec's "no closing others' work unless allowed".

## G9. Code delivery to the VPS

**Decision**: The **laptop** downloads `GET /projects/:id/repository/archive.tar.gz?sha=<full sha>`. The download is streamed to a temp file (0600) and capped at `max_archive_mb` (default 200).

The laptop then **rebuilds a clean tar** with Python `tarfile`:

- strip the archive's top-level directory;
- apply `include` and `exclude` globs;
- drop symlinks whose resolved target leaves the tree, and absolute paths or `..` members;
- drop device, FIFO and hard-link members;
- normalise ownership to 0:0 and modes to 0644 / 0755 (keeping the exec bit);
- write a `MANIFEST.sha256` of every file.

The clean tar is streamed over the existing SSH connection as **binary stdin** to:

```
sudo -n -- tar -x -f - -C <release_dir> --no-same-owner
```

**Rationale**:

- The VPS needs no GitLab access, and the token never leaves the laptop, which satisfies FR-018.
- Sanitising on the laptop defeats tar-slip and symlink tricks, whatever the repository contains.

**Alternatives**:
- `git clone` on the VPS: needs the credential on the VPS (rejected by FR-018) and git installed.
- `rsync`: an extra dependency, and no exact-commit guarantee.

**Executor change**: `RemoteExecutor.run` gains `stdin_bytes: bytes | None`. Over SSH this means `encoding=None` for that call and decoding stdout/stderr as UTF-8 with replacement.

## G10. Release layout and atomic switch

**Decision**: Each target gets this layout:

```
/var/lib/vps-mcp/deploys/<target>/
├── releases/<UTC yyyymmddThhmmss>-<sha12>/   # extracted + MANIFEST.sha256 + .vps-mcp-release (json: target, sha, ref, ts)
└── (nothing else)
<destination>  ->  symlink to the active release
```

**Switching to the new release**:

1. `ln -s <release> <destination>.vps-mcp-new`
2. `mv -T <destination>.vps-mcp-new <destination>`, an atomic rename on the same filesystem.

**First deploy where `<destination>` is a real directory**: the directory is moved (not deleted) to `releases/pre-vps-mcp-<ts>/`, then the symlink is created. The plan states this explicitly as `unmanaged_existing_dir` (spec edge case).

After extraction, run:

```
chown -R <owner>:<group> <release>
chmod -R u+rwX,go+rX,go-w <release>
```

**Retention**: keep `keep_releases` (default 3) previous releases besides the active one. Prune the oldest with `rm -rf -- <path under /var/lib/vps-mcp/deploys/<target>/releases/>`. The path is validated server-side, and `pre-vps-mcp-*` releases are never pruned automatically.

**Rationale**:

- The symlink swap gives FR-017's "old or new, never half".
- Odoo addons paths and scripts follow symlinks.
- Rollback is a swap back.

**Destination rules**:
- Absolute path.
- Not `/`.
- Not under `/etc`, `/bin`, `/sbin`, `/usr`, `/lib*`, `/boot`, `/dev`, `/proc`, `/sys`, `/var/lib/vps-mcp`.
- Not a remote-access unit path.
- Unique across targets.

## G11. Plan identity and drift

**Decision**: `deploy` with `confirm=false` builds a plan. The `plan_id` is the sha256 of:

- the target declaration;
- the resolved full commit SHA;
- the current active release (and its manifest status);
- the file-change list;
- the services.

Plans expire after 30 minutes and are single-use, the same as feature 003's jobs.

**On confirm**, the plan is rebuilt and compared with the stored one:

- **Moving ref**: if the ref now points elsewhere, the confirmed SHA is still deployed, as long as it is reachable from an allowed ref. GitLab's `repository/commits/:sha/refs` checks this (FR-016).
- **Other drift**: any other difference returns `conflict` together with a fresh plan.

## G12. Health and automatic rollback

**Decision**: After the switch, restart each declared service with feature 001's backend `restart()`. That includes `health_stabilize_s`, the state check and an optional `health_url` probe run on the VPS (`curl -fsS --max-time 10`, fixed argv).

If any service fails, the server:

1. switches the symlink back to the previous release;
2. restarts the services again;
3. reports `rolled_back` with the failing service's last log lines (masked);
4. audits the outcome as `rolled_back` (SC-004).

## G13. Concurrency and interlocks

**Decision**:

- Each target has a lock, in-process plus a DB row with `status='running'` (stale after 1 h).
- `deploy` and `rollback_deploy` are refused if:
  - a `job_runs` or `migration_rehearsals` row is running whose declared services intersect the target's services;
  - **or** the target's destination is under any running job's or pipeline's paths.

  In practice only the service intersection is checked, since the job declares its services (FR-020).

## G14. State and memory

**Decision**:

- **Schema v5** adds `gitlab_deploys`: one row per plan or deploy, holding the deployed SHA, ref, release dir, previous release, result and audit_id.
- **Feature 004's `derived.py`** gains a "Deployed versions" section and "Latest GitLab activity" (created issues and MRs, CI actions). The latter comes from `audit_records` with `gitlab_*` actions; their params include `web_ref` (e.g. `addons#42`), not URLs with hostnames.
- **Pipeline state** of deployed SHAs is recorded at plan and deploy time, with no polling (FR-025).
- **Refresh set** gains `deploy_from_gitlab`, `rollback_deploy`, `gitlab_write` and `gitlab_ci`.
