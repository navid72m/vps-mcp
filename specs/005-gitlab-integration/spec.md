# Feature Specification: GitLab Integration

**Feature Branch**: `005-gitlab-integration`

**Created**: 2026-11-11

**Status**: Draft

**Input**: User description: "GitLab integration for vps-mcp using the operator's own credentials. A GitLab instance URL (gitlab.com or self-hosted) and an allowlist of GitLab projects are declared in the policy file; the operator's access token lives in the macOS Keychain (config names only the Keychain entry; the token is never written to repo, config, memory, audit or tool output; masked everywhere). Capabilities for agents, limited to allowlisted projects:

1. read code and history;
2. issues and merge requests (read, create, comment; no merging);
3. CI/CD (pipelines, jobs, masked logs; retry/cancel/trigger when allowed);
4. deploy to the VPS: declared targets map a project and allowed refs to a VPS directory and services. The flow is plan → confirm → fetch the exact commit → keep the previous version for rollback → verify → audit.

All writes preview and need confirmation, read-only mode refuses writes and deploys, permissions are set per project, results are compact, and outcomes feed the agent memory. Builds on features 001–004."

## Context

The operator keeps their Odoo custom addons and migration scripts in GitLab. Today an assistant can manage the VPS (feature 001), analyse and rehearse the migration (002–003) and remember context (004). It can't see the code those processes run, file an issue for a failed rehearsal, check whether CI passed, or put a reviewed version of the scripts on the VPS. The README even tells operators that the server "never uploads code". This feature closes that gap. It uses the operator's own GitLab credentials, limited to the projects and actions the operator declares.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Read code and history (Priority: P1)

The operator asks:

- "What changed in the migration scripts since last week?"
- "Show me `rehearsal_full.sh` on main"
- "Compare tag v1.4 with main"

The assistant answers from GitLab, for allowlisted projects only.

**Why this priority**: Reading is the foundation for everything else, carries the least risk, and is immediately useful when reasoning about a failed rehearsal.

**Independent Test**: Declare one project with `read` permission. List branches, read a file, list recent commits and compare two refs. All return compact results. A project that isn't on the allowlist is refused without revealing whether it exists.

**Acceptance Scenarios**:

1. **Given** an allowlisted project, **When** the assistant lists branches or tags, browses a directory or reads a file at a ref, **Then** it gets the content. Large files and listings are truncated, and the response says so.
2. **Given** two refs, **When** the assistant compares them, **Then** it gets the commits between them and a per-file change summary. Full diffs are available per file, capped in size.
3. **Given** a project not on the allowlist, **When** any tool targets it, **Then** the request is refused as not permitted, with the same answer whether or not the project exists.
4. **Given** file content contains secret-looking values, **When** it is returned, **Then** those values are masked.

---

### User Story 2 - Deploy a reviewed version to the VPS (Priority: P1)

The operator says "deploy the migration scripts from tag v1.5" or "update the custom addons to main". For a declared deploy target, the assistant shows a plan:

- the version currently deployed and the version to be deployed;
- the files that change;
- the services that will be restarted.

After the operator confirms, exactly that version is put in place on the VPS. The previous version is kept for rollback, the result is verified and the deploy is recorded.

**Why this priority**: This is the only capability that changes the VPS. It turns "I pushed a fix" into "the fix is running" without the operator logging in.

**Independent Test**:

1. Declare a target (project, allowed refs, destination directory, a service).
2. Plan a deploy of an allowed tag and confirm it.
3. Check:
   - the destination holds exactly that commit's files;
   - the previous version is retained;
   - the service was restarted and is healthy;
   - the audit trail and memory show the deployed commit.
4. Roll back and check the previous files and commit are restored.

**Acceptance Scenarios**:

1. **Given** a declared target and an allowed ref, **When** the assistant requests a deploy without confirmation, **Then** it receives a plan. Nothing changes until it confirms with that plan's identifier.
2. **Given** the ref moved to a new commit after the plan was shown, **When** the deploy is confirmed, **Then** the confirmed commit is still the one deployed. If that commit is no longer reachable from an allowed ref, the deploy is refused.
3. **Given** a ref that doesn't match the target's allowed refs, **When** a deploy is requested, **Then** it is refused.
4. **Given** the deploy completes, **When** the declared services are restarted, **Then** their health is checked. If a service is unhealthy, the previous version is restored automatically and the result says so.
5. **Given** a previous version exists, **When** the operator asks to roll back, **Then** after confirmation the previous version is restored and the declared services are restarted.
6. **Given** read-only mode, **When** a deploy or rollback is requested, **Then** it is refused.
7. **Given** a deploy runs, **When** the VPS fetches the code, **Then** the credential is never visible in process listings or shell history on the VPS, and is never left on its disk.

---

### User Story 3 - Issues and merge requests (Priority: P2)

The operator says "open an issue for the failed rehearsal with the blocking findings" or "what's the status of the MR for the stock fix?". The assistant can:

- list, search and read issues and merge requests, with their discussion;
- create issues;
- comment on issues and merge requests;
- open merge requests;

where the project's permissions allow it.

**Why this priority**: It closes the loop between what vps-mcp finds (verdicts, failures) and where the team tracks work. It is less critical than reading and deploying.

**Independent Test**: With `write-issues` on a project:

1. Create an issue with a preview, then confirm it.
2. Comment on it.
3. List open issues and see the new one.

A project with only `read` refuses creation.

**Acceptance Scenarios**:

1. **Given** issues and merge requests exist, **When** the assistant lists or searches them by state, label or text, **Then** it gets compact summaries. Reading one returns its description and discussion, capped and masked.
2. **Given** `write-issues` permission, **When** the assistant creates an issue or a comment, **Then** it first gets a preview of exactly what will be posted. It is posted only on confirmation, and is audited.
3. **Given** `write-mrs` permission, **When** the assistant opens a merge request between two existing branches, **Then** this follows the same preview → confirm → audit flow.
4. **Given** any permission level, **When** the assistant tries to merge a merge request or close an issue it didn't create, **Then** it is refused, unless the project explicitly allows closing.

---

### User Story 4 - CI/CD visibility and control (Priority: P2)

The operator asks "did the pipeline for v1.5 pass?" or "why did the test job fail?". Where the project allows it, they also say "retry that job" or "run the pipeline on main".

**Why this priority**: CI status decides whether a version is safe to deploy, and the logs explain failures. Control actions are a convenience.

**Independent Test**: Listing pipelines for a ref shows their status. Reading a failed job returns the tail of its log, masked. With `ci` permission, retry, cancel and trigger follow preview → confirm. Without it, they are refused.

**Acceptance Scenarios**:

1. **Given** pipelines exist, **When** the assistant lists them for a ref, **Then** it gets each pipeline's status, its duration and the failed jobs.
2. **Given** a job log, **When** it is read, **Then** the last N lines are returned (default 200, maximum 2000), with secrets masked.
3. **Given** `ci` permission, **When** the assistant retries or cancels a job or pipeline, or triggers a pipeline on an allowed ref, **Then** it previews first, acts only on confirmation, and audits.
4. **Given** a deploy plan, **When** the target commit's latest pipeline failed or is still running, **Then** the plan shows that prominently. The target can be declared to require a successful pipeline, in which case the deploy is refused.

---

### User Story 5 - Credentials stay private (Priority: P1, cross-cutting)

The operator stores their GitLab token once in the macOS Keychain. Every client that uses vps-mcp (Claude Code, OpenCode, Antigravity, LM Studio) uses it without the token appearing in any configuration, transcript, memory note, audit record or file on the VPS.

**Why this priority**: The operator's personal credentials can do far more than this feature allows. Leaking one is the worst failure this feature can have.

**Independent Test**:

1. Store a known test token.
2. Exercise every tool, including error paths: wrong token, network failure, a GitLab error that echoes the request.
3. Search all outputs, logs, the audit store, memory files and the VPS-side commands and files for the token. It must appear nowhere.

**Acceptance Scenarios**:

1. **Given** the token is in the Keychain, **When** the server starts, **Then** the configuration contains only the Keychain entry name. A missing or unreadable entry disables the GitLab tools with a clear setup message; the rest of the server works.
2. **Given** any tool output, log line, audit record or memory file, **When** it is inspected, **Then** the token never appears in it.
3. **Given** a token that lacks a scope a tool needs, **When** that tool is called, **Then** the operator gets a clear "token lacks scope X" message. The server never tries to widen its own access.
4. **Given** the operator wants to see what the token can do, **When** they run a setup check, **Then** it reports:
   - the token's user;
   - its scopes;
   - its expiry date, with a warning when it expires within 14 days;
   - which declared projects are reachable.

---

### Edge Cases

- **Expiry and revocation**: The token expires or is revoked mid-session. Every GitLab tool fails with an authentication error explaining how to replace it. Nothing is retried in a loop.
- **Self-hosted instance**: The instance uses a private certificate authority. The operator can name a certificate bundle, and verification is never turned off.
- **Project changes**: A project is renamed or moved in GitLab. Declarations should identify projects by a stable identifier where possible. A declared project that is no longer reachable is reported by the setup check.
- **Very large repositories, files or diffs**: Every listing and content response has a size cap and says when it truncated.
- **Destination directory**:
  - If it has local changes that aren't from a vps-mcp deploy, the plan shows "unmanaged changes will be replaced". The deploy proceeds only if the operator confirms with that knowledge.
  - If it doesn't exist yet, the first deploy creates it with the declared owner and mode.
- **Concurrent deploys**: Only one deploy runs per target at a time. A deploy is refused while a declared job or rehearsal that uses that target's services is running.
- **Rate limits**: GitLab rate limits produce a clear "retry after" message. There is no hammering.
- **Huge or binary job logs**: Logs are tailed and decoded safely. Non-text content is summarised, not returned.
- **Untrusted text**: Issue and merge request text written by others may contain instructions aimed at the assistant. It is returned as data and never acted on by the server.

## Requirements *(mandatory)*

### Functional Requirements

**Configuration and credentials**

- **FR-001**: The operator MUST be able to declare in the policy file:
  - one GitLab instance (base address, optional custom certificate bundle);
  - the Keychain entry that holds the access token;
  - an allowlist of projects, each with a local alias, its GitLab identity and a permission set.
- **FR-002**: Permission levels per project MUST be any combination of:
  - `read`: code, history, issues, MRs, pipelines and logs;
  - `write_issues`: create issues and comment on issues;
  - `write_mrs`: open MRs and comment on MRs;
  - `ci`: retry, cancel and trigger pipelines and jobs;
  - `deploy`: use the project's deploy targets.

  `read` is implied by every other level. Closing issues requires an explicit `close_issues` flag. Merging is never available.
- **FR-003**: The access token MUST be read from the Keychain at use time, and never written to configuration, memory, audit, logs, tool output or the VPS disk. All text leaving the server MUST be scrubbed of it.
- **FR-004**: If the token is missing, the GitLab tools MUST be absent or answer with a setup message, without affecting other features.
- **FR-005**: A setup check (CLI) MUST report the token's user, scopes, expiry date and declared-project reachability, and warn when expiry is within 14 days.

**Read (US1)**

- **FR-006**: The assistant MUST be able to:
  - list branches and tags;
  - list a directory at a ref;
  - read a file at a ref;
  - list commits (by ref, path and date range);
  - show one commit;
  - compare two refs.

  All of these apply to allowlisted projects only.
- **FR-007**: Each read MUST be size-capped:
  - a file is capped at 100 KB of text, and binary files are summarised;
  - a listing is capped at 200 entries;
  - a diff is capped at 50 files per summary and 2,000 lines per file.

  Truncation MUST be indicated. Secret-looking values in returned content MUST be masked.
- **FR-008**: Requests for projects that aren't allowlisted MUST be refused identically, whether or not the project exists.

**Issues and merge requests (US3)**

- **FR-009**: The assistant MUST be able to list and search issues and MRs (by state, label, author, text) and read one with its discussion (capped).
- **FR-010**: With the right permission, the assistant MUST be able to create issues, comment on issues and MRs, and open MRs between existing branches. Each is previewed exactly as it will be posted, confirmed, then audited.
- **FR-011**: Merging MRs MUST NOT be possible. Closing issues MUST be possible only with the project's `close_issues` flag.

**CI/CD (US4)**

- **FR-012**: The assistant MUST be able to list pipelines for a project or ref (status, duration, failed jobs), list a pipeline's jobs, and read a job's log tail (default 200 lines, maximum 2,000), masked.
- **FR-013**: With `ci` permission, the assistant MUST be able to retry or cancel a pipeline or job, and trigger a pipeline on a ref matching the project's allowed trigger refs. Each is previewed, confirmed and audited.

**Deploy (US2)**

- **FR-014**: The operator MUST be able to declare deploy targets. Each target has:
  - a name;
  - a project;
  - allowed refs (branch and tag patterns);
  - a destination directory on the VPS, which must be absolute and outside system paths;
  - an owner and mode;
  - optional paths to include or exclude;
  - optional declared services to restart, with a health check;
  - optional `require_pipeline_success`;
  - the number of previous versions to keep (default 3).
- **FR-015**: A deploy plan MUST show:
  - the resolved target commit (full identifier, author, date, message);
  - the currently deployed commit, if any;
  - files added, changed and removed;
  - the latest pipeline status of the target commit;
  - unmanaged local changes in the destination;
  - the services that will restart;
  - free disk space.

  The plan MUST carry an identifier that binds exactly these facts.
- **FR-016**: Confirming MUST deploy exactly the planned commit. If any planned fact changed (except a moving ref that still contains the commit), the deploy MUST be refused and a fresh plan returned.
- **FR-017**: The deploy MUST place the new version without leaving the destination half-updated. A reader sees either the old version or the new one. The deploy MUST keep the declared number of previous versions, restart the declared services, verify their health, and on failure restore the previous version automatically.
- **FR-018**: The credential MUST NOT appear on the VPS:
  - not in any process's command line;
  - not in shell history;
  - not in a persistent file.

  Code MUST reach the VPS as a fetched archive of the exact commit, or through an equivalent mechanism that meets this rule.
- **FR-019**: Rollback to the previous retained version MUST be available with preview → confirm, restart the services, and check their health.
- **FR-020**: Deploy and rollback MUST be refused in read-only mode, while another deploy of the same target is running, and while a declared job or rehearsal touching the target's services is running.
- **FR-021**: Each deployed version MUST be recorded: target, commit, ref, time, operator session and result. This record MUST be visible in the audit log and the agent memory's derived facts.

**Cross-cutting**

- **FR-022**: Every GitLab write and deploy attempt (including refused and failed ones) MUST be audited. GitLab reads are not audited.
- **FR-023**: Content written by others (issues, comments, MR descriptions, commit messages, file contents, logs) MUST be returned as data and never interpreted as instructions by the server.
- **FR-024**: Results MUST be compact, with summaries by default and detail on request.
- **FR-025**: Deploys, created issues and MRs, and the latest pipeline state of deployed refs MUST appear in the agent memory's derived facts.
- **FR-026**: Network, authentication, permission, not-found, rate-limit and timeout failures MUST each map to a distinct, clear error category.

### Key Entities

- **GitLab instance**: base address, certificate settings and the Keychain credential reference.
- **Allowlisted project**: alias, GitLab identity, permissions, allowed trigger refs and the `close_issues` flag.
- **Deploy target**:
  - name, project, allowed refs;
  - destination, owner and mode;
  - include and exclude paths;
  - services and their health check;
  - `require_pipeline_success`;
  - retention.
- **Deploy plan**: target commit, current commit, file changes, pipeline status, unmanaged changes, services, disk, plan identifier and expiry.
- **Deployed version**: target, commit, ref, deployed time, retained location, result and audit link.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: The operator can go from "what changed since tag X?" to a deployed, healthy new version on the VPS in one conversation, without opening a terminal, in under 5 minutes excluding service start time.
- **SC-002**: Searching all tool outputs, logs, audit records, memory files and VPS-side traces of a full test run (every tool, including error paths) for the token finds zero occurrences.
- **SC-003**: 100% of requests for projects that aren't allowlisted, actions without the needed permission, or refs outside the allowed patterns are refused, with no change on GitLab or the VPS. A negative suite of at least 30 cases covers this.
- **SC-004**: A deploy whose service fails its health check leaves the previous version running within 2 minutes, without operator action.
- **SC-005**: Typical read answers are small. A file listing, a commit list of 20 or a pipeline summary each fit in under 2,000 tokens.
- **SC-006**: A new session in any client knows the currently deployed version of each target from the briefing alone.

## Assumptions

- **GitLab version and token**: the operator uses GitLab 15 or later (gitlab.com or self-hosted) and a personal, group or project access token with scopes matching the permissions they declare. `read_api` covers read-only use, and `api` is needed for writes, CI control and archive fetches of private projects.
- **Laptop and VPS connectivity**: the laptop can reach GitLab. The VPS may or may not be able to reach GitLab directly. Delivering code to the VPS works in either case, because the laptop fetches the commit and streams it over the existing SSH channel.
- **Credential storage**: the Keychain is macOS-specific. On other platforms, an equivalent OS credential store or an environment variable may be added later. That is out of scope here.
- **Deploy contents**: a deploy installs files only. Build steps (compiling, installing dependencies) are out of scope. Restarting declared services is the only follow-up action.
- **Out of scope**:
  - repository administration: creating projects, managing members, protected branches, CI variables;
  - wiki and package registry;
  - webhooks and any persistent agent on the VPS.
- **Odoo addons**: deploying addons doesn't run module upgrades. The operator can follow up with existing tools, such as a declared check or job.
