---

description: "Task list for GitLab Integration"
---

# Tasks: GitLab Integration

**Input**: Design documents from `/specs/005-gitlab-integration/`

**Prerequisites**:

- [plan.md](./plan.md)
- [spec.md](./spec.md)
- [research.md](./research.md) (G1–G14)
- [data-model.md](./data-model.md)
- [contracts/gitlab-tools.md](./contracts/gitlab-tools.md)
- [contracts/gitlab-config.md](./contracts/gitlab-config.md)
- [quickstart.md](./quickstart.md)

**Tests**: Included. The spec requires:

- a token-leak sweep (SC-002);
- a negative suite of at least 30 cases (SC-003);
- automatic rollback within 2 minutes (SC-004).

Write each story's tests first.

**Organization**: Tasks are grouped by story. US5 (credentials) is cross-cutting P1, so it runs together with US1. Story order: US5 + US1, then US2, then US3, then US4. Paths are relative to the repo root.

**Reuses**:

- `remote.redact` (`scrub_obj`, `RedactionFilter`, `mask_inline`) and `memory.masking.mask_note`;
- `core.guard` (read-only checks, audit) and `AppContext.backend(...).restart`;
- `store.audit.AuditStore` and `store.db.open_db`;
- `jobs.store.JobStore.running()` and `migration_rehearsals` (interlocks);
- `memory.derived` and `memory.service.REFRESH_TOOLS`;
- `tests/fakes.FakeVps` and `tests/conftest.SessionPerCall` / `call`.

**Privacy**: fixtures use generic names: instance `https://gitlab.example.test`, projects `addons` and `scripts`, account `deploy-bot`, and fake token `glpat-TESTTOKEN0123456789abcd`.

## Format: `[ID] [P?] [Story] Description`

---

## Phase 1: Setup

- [X] T001 Create the package `src/vps_mcp/gitlab/__init__.py` and the test package `tests/unit/gitlab/__init__.py`. Add `httpx2` to `[project].dependencies` in `pyproject.toml`, with the same lower bound the installed MCP SDK requires; check with `uv pip show httpx2`.
- [X] T002 [P] Add a commented `[gitlab]` / `[[gitlab.project]]` / `[[gitlab.deploy]]` example to `examples/vps-mcp.toml`, copied from `specs/005-gitlab-integration/contracts/gitlab-config.md`.

---

## Phase 2: Foundational (Blocking Prerequisites)

**⚠️ CRITICAL**: No user story can start until this phase is complete.

- [X] T003 Write config tests in `tests/unit/gitlab/test_config.py`. Cover:
  - **url**: `https://` only, no userinfo, optional sub-path;
  - **keychain fields**: `keychain_service` must match `[A-Za-z0-9._-]{1,128}`, and `keychain_account` must match `[A-Za-z0-9._@-]{1,128}` and is required;
  - **limits**: `timeout_s` 5–120, `max_archive_mb` 1–2000;
  - **project identity**: `alias` must match `^[a-z0-9][a-z0-9-]{0,39}$` and be unique; exactly one of `id` or `path` is set, with `path` matching `^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)+$`;
  - **project rules**:
    - `permissions` must be a subset of `read`, `write_issues`, `write_mrs`, `ci`, `deploy`, and `read` is implied;
    - `close_issues` requires `write_issues`, and `close_any_issue` requires `close_issues`;
    - `trigger_refs` requires `ci`;
    - at most 50 projects;
  - **deploy target identity**: `name` must be a unique slug and `project` must have `deploy`;
  - **deploy target paths**:
    - `refs` needs at least one entry;
    - `destination` must be absolute and outside `/`, `/etc`, `/bin`, `/sbin`, `/usr`, `/lib*`, `/boot`, `/dev`, `/proc`, `/sys` and `/var/lib/vps-mcp`;
    - destinations must be unique and not nested inside each other;
  - **deploy target other fields**:
    - `services` must be declared `[[service]]`s and not remote-access units;
    - `keep_releases` is 1–20;
    - `health_url` must be http(s);
    - at most 20 targets;
  - **unknown keys** are rejected.
- [X] T004 Implement `src/vps_mcp/gitlab/config.py`:
  - define `GitLabSettings`, `ProjectDecl` and `DeployTarget` (pydantic `_Strict`), with the rules above;
  - add `AppConfig.gitlab: GitLabSettings | None = None` in `src/vps_mcp/config/policy.py`;
  - add cross-checks in `AppConfig._cross_checks` for deploy services against declared services and remote-access units.
- [X] T005 [P] Write credential tests in `tests/unit/gitlab/test_credentials.py` (monkeypatch `subprocess.run`). Cover:
  - the argv is exactly `["security","find-generic-password","-s",svc,"-a",acct,"-w"]`, with no shell and a 10 s timeout;
  - stdout is stripped;
  - a non-zero exit → `TokenMissing`;
  - a timeout → `TokenMissing`;
  - on a non-macOS platform → `TokenMissing` with a platform message;
  - the token is cached after the first load;
  - `register_secret` is called with the token.
- [X] T006 [P] Implement `src/vps_mcp/gitlab/credentials.py` (research G2) as `KeychainToken(service, account, on_loaded)`:
  - `get()` loads lazily and caches;
  - `on_loaded(token)` appends the token to `AppContext` secret strings and to the installed `RedactionFilter` secrets.

  Also extend `AppConfig.secret_strings` / `AppContext` with a mutable `extra_secrets: list[str]` that `scrub_obj`, `AuditStore` and the log filter consult. Update `src/vps_mcp/core/context.py`, `src/vps_mcp/store/audit.py` and `src/vps_mcp/remote/redact.py` so they hold references to the list, not copies.
- [X] T007 Write the fake GitLab in `tests/fakegitlab.py`. `FakeGitLab` provides an `httpx2.MockTransport` handler for `/api/v4` covering:
  - projects by id or url-encoded path;
  - `repository/branches` and `tags`, `repository/tree`, `repository/files/:path/raw?ref=`;
  - `repository/commits` (`ref_name`, `path`, `since`, `until`), `commits/:sha`, `commits/:sha/diff`, `commits/:sha/refs`;
  - `repository/compare?from&to`;
  - `repository/archive.tar.gz?sha=`, built with `tarfile` from an in-memory tree with a top-level `proj-<sha>/` dir;
  - issues and MRs (list with `state`, `labels`, `search`, `author_username`), notes, create, update `state_event=close`, MR create;
  - pipelines (list, get, jobs), `jobs/:id/trace`, retry, cancel, `pipeline?ref=` trigger;
  - `personal_access_tokens/self` (scopes, `expires_at`) and `user`.

  It must:
  - record every request, with method, path, headers and body;
  - check the `PRIVATE-TOKEN` header, returning 401 when wrong;
  - support injected failures (403, 404, 429 with `Retry-After`, 500, timeout);
  - support an "echo" mode where error bodies contain the request headers, used by the leak test.
- [X] T008 Write client tests in `tests/unit/gitlab/test_client.py` against FakeGitLab. Cover:
  - the header is set;
  - error mapping: 401 → `permission_denied/gitlab_auth`, 403 → `permission_denied/gitlab_scope`, 404 → `not_found`, 429 → `timed_out/gitlab_rate_limited` with `retry_after_s`, connect error → `connection_failure/gitlab_unreachable`, read timeout → `timed_out`;
  - pagination stops at the cap and reports `truncated`;
  - error messages never include the token, even in echo mode;
  - `ca_bundle` is passed as `verify`;
  - TLS verification is never disabled.
- [X] T009 Implement `src/vps_mcp/gitlab/client.py` (research G1):
  - `GitLabClient(settings, token: KeychainToken, transport=None)`;
  - `get_json`, `get_text`, `get_bytes_stream` (with a size cap), `post_json`, `put_json` and `paged(path, params, cap)`;
  - error mapping as above;
  - project path segments are url-encoded;
  - `aclose()`.
- [X] T010 [P] Implement `src/vps_mcp/gitlab/access.py` (research G3, G4):
  - `resolve(app, alias, need: str) -> ProjectDecl`, which raises `not_permitted` with the fixed message for unknown aliases and `not_permitted` naming the missing permission;
  - `project_ref(decl)`, which returns the id or the url-encoded path;
  - `ref_allowed(target, kind, name)`, glob matching against `branch:`/`tag:` prefixes;
  - `trigger_allowed(decl, ref)`.

  Add tests in `tests/unit/gitlab/test_access.py`.
- [X] T011 [P] Implement `src/vps_mcp/gitlab/render.py` (research G5, G6):
  - `external(text, author=None, created_at=None)` builds the envelope, masked with `mask_note` and the policy's `secret_keys`, and capped;
  - `commit_line()`, `pipeline_line()`, `item_line()`;
  - `is_binary(bytes)`;
  - the caps constants.

  Add tests in `tests/unit/gitlab/test_render.py`.
- [X] T012 [P] In `src/vps_mcp/store/db.py`, bump `SCHEMA_VERSION=5` and add `_SCHEMA_V5` with the `gitlab_deploys` table and index from data-model §2. It must upgrade from v4 and keep all data.
- [X] T013 [P] Add `gitlab_issue`, `gitlab_comment`, `gitlab_mr`, `gitlab_close`, `gitlab_ci` and `rollback_deploy` to `ACTIONS` in `src/vps_mcp/store/audit.py`. `deploy` already exists.
- [X] T014 Add `stdin_bytes: bytes | None = None` to `RemoteExecutor.run` and `SshExecutor.run` in `src/vps_mcp/remote/executor.py`:
  - when it is set, call `conn.run(..., input=stdin_bytes, encoding=None)` and decode stdout/stderr as UTF-8 with replacement;
  - `stdin` and `stdin_bytes` are mutually exclusive.

  Update `tests/fakes.FakeVps.run` to accept it and record `stdin_bytes` in its call log. Add a test in `tests/unit/test_executor.py`, or the existing executor test file.
- [X] T015 Wire GitLab into `src/vps_mcp/server.py` and `src/vps_mcp/core/context.py`:
  - add `AppContext.gitlab: Any = None`, holding the `GitLabRuntime(settings, client, token, store)`, created in `build_context` when `config.gitlab` is set;
  - accept a `gitlab_transport` kwarg so tests can inject FakeGitLab;
  - register `vps_mcp.gitlab.tools` in `create_server` when `app.gitlab` is set.

**Checkpoint**: config, credentials, client, access, render, schema, audit, binary stdin and wiring are in place.

---

## Phase 3: US5 + US1 - Credentials and read code (Priority: P1) 🎯 MVP

**Goal**:

- With the token in the Keychain, assistants read code, history and diffs of allowlisted projects;
- without it, they get a setup message;
- the token never leaks.

**Independent Test**: With FakeGitLab and a fake Keychain:

- the 6 read tools return compact, capped, masked results;
- a non-allowlisted alias gets an identical refusal;
- a missing token gives the setup message;
- the leak sweep finds nothing;
- `gitlab-check` prints the user, scopes and expiry, without the token.

### Tests

- [X] T016 [P] [US1] Write read tests in `tests/unit/gitlab/test_read.py`. Cover:
  - `gitlab_projects` lists aliases, permissions and targets;
  - `gitlab_refs` (`both`, `search`, `limit`, `truncated`);
  - `gitlab_tree` (`path`, `recursive`, cap of 200);
  - `gitlab_file`:
    - text: ≤ 100 KB, `start_line` / `max_lines` ≤ 2000;
    - binary: size + sha256;
    - masked `password=x`;
  - `gitlab_commits` list lines are `sha12 date author: title`, and `sha` gives detail (files ≤ 50);
  - `gitlab_compare` summary (`A/M/D/R path +x -y`, ≤ 50 files) and `path` diff (≤ 2000 lines, `truncated`);
  - a default ref is used when `ref` is omitted;
  - an unknown alias → `not_permitted`, with an identical message for `"other"` and `"../x"`, and **no HTTP request was made** (FakeGitLab request log is empty);
  - external content has `kind: external_content`.
- [X] T017 [P] [US5] Write credential flow tests in `tests/unit/gitlab/test_token_flow.py`. Cover:
  - a missing Keychain entry → every GitLab tool returns `permission_denied` with `subtype gitlab_token_missing` and the `security add-generic-password … -w` hint;
  - non-GitLab tools still work;
  - a 401 mid-session → `gitlab_auth` with replacement guidance and no retry loop (one request recorded);
  - a 403 → `gitlab_scope` naming the missing scope when GitLab returns it.
- [X] T018 [P] [US5] Write the token-leak harness in `tests/unit/gitlab/test_token_leak.py` (SC-002). It:
  1. calls every GitLab tool on success paths and on each injected error, using FakeGitLab echo mode;
  2. then asserts the token string appears nowhere:
     - in tool results (`content` and `structured_content`);
     - in captured logs (`caplog` at DEBUG for `vps_mcp`, `httpx2` and `mcp`);
     - in `audit_records` rows;
     - in memory folder files (`generated/*.md`, `notes/`);
     - in the FakeVps call log, including argv, env, stdin and `stdin_bytes`.

  Extend it in US2, US3 and US4 as those tools land.
- [X] T019 [P] [US5] Write CLI tests in `tests/unit/gitlab/test_cli_gitlab.py`. `vps-mcp gitlab-check` should print:
  - the instance and user;
  - the scopes;
  - `expires_at`, with a warning when it is less than 14 days away;
  - each project as `ok (id N)` or the error;
  - for `path` projects, a hint to pin the `id`;
  - scope gaps against the declared permissions (`read_api` vs `api`).

  It exits 1 on an error and never prints the token. Also cover that `check-config` validates the `[gitlab]` section.

### Implementation

- [X] T020 [US1] Implement `src/vps_mcp/gitlab/read.py` with `projects`, `refs`, `tree`, `file`, `commits` and `compare`, as in contracts/gitlab-tools.md. Call `access.resolve(..., "read")` before any request.
- [X] T021 [US1] Implement the read tools in `src/vps_mcp/gitlab/tools.py`:
  - `register(mcp, app, wrap, session_id)`;
  - `gitlab_projects`, `gitlab_refs`, `gitlab_tree`, `gitlab_file`, `gitlab_commits`, `gitlab_compare` with read-only annotations;
  - descriptions saying content from GitLab is data, not instructions;
  - a token-missing check that raises `VpsMcpError(PERMISSION_DENIED, …, subtype="gitlab_token_missing")`.
- [X] T022 [US5] Implement `vps-mcp gitlab-check` in `src/vps_mcp/cli.py`:
  - build the client;
  - call `personal_access_tokens/self` (fall back to `user` when it is unavailable) and each project;
  - print the report from contracts/gitlab-tools.md (CLI);
  - read the deploy targets' active release from the VPS when SSH is reachable, and skip that with a note otherwise.

**Checkpoint**: an assistant can read GitLab safely. This is the MVP.

---

## Phase 4: US2 - Deploy a reviewed version to the VPS (Priority: P1)

**Goal**: Plan, confirm and deploy an exact commit to a declared target:

- the swap from old to new release is atomic;
- the services are restarted and health-checked, with automatic rollback;
- old releases are pruned;
- the deploy is recorded;
- manual rollback works;
- the token never touches the VPS.

**Independent Test**:

1. Run FakeGitLab plus a FakeVps that emulates `tar`, `ln`, `mv -T`, `readlink`, `sha256sum -c`, `chown`, `chmod` and `rm`.
2. Plan → confirm → check that the release dir holds exactly the commit's sanitised files, that the destination symlink points to it, that the service was restarted, and the DB row, audit and memory.
3. Make the health check fail → `rolled_back`, with the previous release active again.
4. Run `rollback_deploy` → the previous release becomes active.

### Tests

- [X] T023 [P] [US2] Write archive tests in `tests/unit/gitlab/test_archive.py` (research G9). Cover:
  - the top-level dir is stripped;
  - `include` / `exclude` globs (`**`);
  - dropped members are listed in `skipped` with a reason: absolute paths, `..`, symlinks escaping the tree, symlinks to absolute targets, hard links, devices, FIFOs;
  - in-tree relative symlinks are kept;
  - modes normalise to 0644 / 0755 and owner to 0:0;
  - `MANIFEST.sha256` lists every regular file;
  - archives larger than `max_archive_mb` → `invalid_input`, and the download is aborted at the cap;
  - a gzip bomb (huge expansion) is refused when the uncompressed total exceeds 4× the cap.
- [X] T024 [P] [US2] Extend `tests/fakes.py` FakeVps with deploy emulation:
  - `tar -x -f - -C <dir> --no-same-owner` reads `stdin_bytes` into `files`;
  - `mkdir -p`, `ln -s`, `mv -T`, `readlink -f`/`readlink`;
  - `rm -rf --` (it refuses paths outside `/var/lib/vps-mcp/deploys/` and asserts if called on one);
  - `chown -R`, `chmod -R`;
  - `sha256sum -c --quiet`, which reports modified files;
  - `cat` of the marker file, `df`, `curl -fsS --max-time` (configurable status).

  Model a real directory versus a symlink for the destination.
- [X] T025 [P] [US2] Write deploy tests in `tests/unit/gitlab/test_deploy.py`. Cover:
  - **the plan** (no remote writes: the FakeVps write-verb log is empty) shows:
    - the commit (sha, author, date, title);
    - `current` (none, then the managed release with `local_changes` from `sha256sum -c`);
    - the changes: compare from the deployed sha to the target, filtered by include/exclude, ≤ 50 files;
    - the pipeline status;
    - `unmanaged_existing_dir` when the destination is a real dir;
    - the services, disk and warnings;
  - **`plan_id` rules**:
    - stable for identical facts;
    - single use;
    - expires after 30 minutes;
  - **refs**:
    - a disallowed ref → `invalid_input`;
    - a full sha unreachable from allowed refs → `invalid_input`;
    - a ref moved after the plan → the planned sha is still deployed;
    - other drift → `conflict` with a fresh plan;
  - **pipeline gate**: `require_pipeline_success` with a failed or running pipeline → `failed`;
  - **interlocks**:
    - `read_only` → refused and audited;
    - a running job whose services intersect the target's → `conflict`;
    - the target is busy → `conflict`;
  - **disk**: insufficient disk → `insufficient_disk`;
  - **confirm**:
    - the release dir is `/var/lib/vps-mcp/deploys/<t>/releases/<ts>-<sha12>/` and contains the marker and manifest;
    - `chown` and `chmod` were applied;
    - the symlink was switched via `ln -s` to `<dest>.vps-mcp-new`, then `mv -T`;
    - the services were restarted and are healthy → `succeeded`;
    - a `gitlab_deploys` row and an audit row (`deploy`, `kind=gitlab`, `service_name=target`) were written;
  - **first deploy over a real dir**: the dir is moved to `releases/pre-vps-mcp-<ts>/` and never deleted;
  - **retention**: keeps `keep_releases`; never prunes `pre-vps-mcp-*`;
  - **health failure** (unhealthy service, or `health_url` non-2xx) → the symlink is switched back, the services restarted, the status is `rolled_back` with masked log lines, and the audit outcome is `rolled_back`;
  - **`rollback_deploy`**: preview, then confirm, switches to the previous release or the named one; refused with no previous release;
  - **`deploy_targets`** shows `current` and the previous releases from `readlink` and the marker;
  - **the leak sweep** (T018 harness) covers deploy, including `stdin_bytes`.

### Implementation

- [X] T026 [P] [US2] Implement `src/vps_mcp/gitlab/archive.py`:
  - `async download(client, project, sha, cap) -> Path`, a temp file with mode 0600 in a private temp dir that is cleaned up in `finally`;
  - `sanitize(src: Path, include, exclude, cap) -> Clean(tar_bytes, files, manifest, skipped, total_bytes)`, as in research G9.
- [X] T027 [P] [US2] Implement `src/vps_mcp/gitlab/store.py` (`DeployStore`, with `add_plan`, `update`, `by_plan`, `running(target)`, `latest(target)`, `recent()`) over `gitlab_deploys`.
- [X] T028 [US2] Implement `src/vps_mcp/gitlab/deploy.py` (research G10–G13) with these functions:
  - `targets(app)`;
  - `plan(app, target, ref)`;
  - `deploy(app, target, ref, plan_id, confirm, session_id)`;
  - `rollback(app, target, release, confirm, session_id)`.

  Implementation requirements:
  - every VPS command is a fixed argv built from validated paths;
  - a path guard `_under(base, path)` runs before every `rm` and `mv`;
  - deploy and rollback go through `core.guard` (or `run_db_action`-style) wrappers, so read-only refusal and audit apply consistently;
  - services restart via `app.backend(kind).restart(decl)`;
  - `health_url` is probed with `curl -fsS -o /dev/null --max-time 10 <url>`;
  - a per-target `asyncio.Lock` plus a DB `running` row (stale after 1 h).
- [X] T029 [US2] Add the `deploy_targets` (read-only), `deploy` and `rollback_deploy` tools to `src/vps_mcp/gitlab/tools.py`. The descriptions warn that the destination will be replaced and the services restarted.
- [X] T030 [US2] Add the `--with-gitlab` argument to `bootstrap --print-sudoers` in `src/vps_mcp/cli.py`, using `Cmnd_Alias VPSMCP_DEPLOY` from contracts/gitlab-tools.md. Add a test in `tests/unit/gitlab/test_cli_gitlab.py`.

**Checkpoint**: the reviewed GitLab version runs on the VPS in one conversation.

---

## Phase 5: US3 - Issues and merge requests (Priority: P2)

**Goal**: List and read issues and MRs. Create issues, comment and open MRs, with a preview and confirmation first, then an audit entry. Closing follows research G8. Merging is impossible.

**Independent Test**: With `write_issues`, previewing an issue sends no POST; confirming sends exactly one POST with the masked body and footer, plus an audit row. A project with only `read` refuses.

### Tests

- [X] T031 [P] [US3] Write tests in `tests/unit/gitlab/test_items.py`. Cover:
  - **reading**:
    - `gitlab_list_items` for `kind` issue/mr and `state`/`labels`/`search`/`author`, ≤ 50, with lines `#iid [state] title (author, updated, labels)`;
    - `gitlab_read_item` returns `external_content` description and notes (≤ 50, each ≤ 4 KB, masked), MR `source` / `target` / `pipeline_status`, and `web_ref` as `alias#iid` or `alias!iid`;
  - **writing**:
    - `gitlab_write` `create_issue` preview shows the masked fields and footer, and no POST is recorded;
    - confirm → one POST, returns `web_ref`, audit `gitlab_issue`;
    - `comment` on an issue and on an MR;
    - `create_mr` checks both branches exist;
  - **closing**:
    - `close_issue` without `close_issues` → `not_permitted`;
    - with `close_issues` on an issue without the vps-mcp footer → `not_permitted`;
    - with `close_any_issue` → allowed;
  - **refusals**:
    - in read-only mode, every write → `read_only` and is audited;
    - a `read`-only project → `not_permitted`;
    - there is no tool whose name or action contains `merge`.

### Implementation

- [X] T032 [US3] Implement `src/vps_mcp/gitlab/items.py` with `list_items`, `read_item` and `write(action, …, confirm, session_id)`, following research G7 and G8. The footer `\n\n_Posted via vps-mcp_` is appended to created issues, MRs and comments.
- [X] T033 [US3] Add the `gitlab_list_items`, `gitlab_read_item` and `gitlab_write` tools to `src/vps_mcp/gitlab/tools.py`.

**Checkpoint**: failures found by vps-mcp can become tracked issues.

---

## Phase 6: US4 - CI/CD visibility and control (Priority: P2)

**Goal**: Pipeline and job status, masked log tails, and CI actions behind `ci` with a preview first. The pipeline gate in deploy plans was covered in US2.

**Independent Test**:

- The pipeline list shows failed job names.
- A job log returns the last 200 lines, masked.
- `retry_job` with `ci` previews and then posts.
- Without `ci`, it is refused.
- `trigger_pipeline` on a ref outside `trigger_refs` is refused.

### Tests

- [X] T034 [P] [US4] Write tests in `tests/unit/gitlab/test_ci.py`. Cover:
  - `gitlab_pipelines` list lines are `id status ref sha12 dur failed:[jobs]`, and `pipeline_id` gives the jobs;
  - `gitlab_job_log`:
    - default 200 lines, maximum 2000, `truncated`;
    - masks `TOKEN=…` and the real token;
    - a binary or huge trace is summarised;
  - `gitlab_ci`:
    - each of the 5 actions previews first, then confirms → one request plus audit `gitlab_ci`;
    - without `ci` → `not_permitted`;
    - `trigger_pipeline` with a ref not matching `trigger_refs` → `invalid_input`;
    - read-only → refused.

### Implementation

- [X] T035 [US4] Implement `src/vps_mcp/gitlab/ci.py` with `pipelines`, `job_log` and `action(...)`.
- [X] T036 [US4] Add the `gitlab_pipelines`, `gitlab_job_log` and `gitlab_ci` tools to `src/vps_mcp/gitlab/tools.py`, bringing the total to 15. Extend the T018 leak harness to cover them.

**Checkpoint**: CI decides deploy readiness, and failures are explained.

---

## Phase 7: Memory integration (FR-025, SC-006)

- [X] T037 Extend `src/vps_mcp/memory/derived.py`:
  - add a `deployed(conn, config)` section listing each target with its last succeeded or rolled-back deploy (sha12, ref, time, status);
  - add GitLab activity from `audit_records` with `gitlab_*` actions (with `web_ref` params; no hostnames).

  Include it in `environment()` / `outcomes()`, and in the briefing P2 ("Latest outcomes"). Add `deploy_from_gitlab`, `rollback_deploy`, `gitlab_write` and `gitlab_ci` to `REFRESH_TOOLS` in `src/vps_mcp/memory/service.py`. Add tests in `tests/unit/gitlab/test_memory_gitlab.py`, including that a new server's instructions name the deployed version of each target.

---

## Phase 8: Polish & Cross-Cutting Concerns

- [X] T038 [P] Write the negative suite in `tests/negative/test_gitlab_disallowed.py`, with at least 30 cases. Each asserts that FakeGitLab recorded no write requests (POST/PUT/DELETE) and that FakeVps recorded no write verbs. Cover:
  - **unknown aliases**, including look-alikes, `../`, numeric IDs and paths;
  - **missing permissions** for each write tool;
  - **disallowed refs and shas**;
  - **trigger refs** outside `trigger_refs`;
  - **closing**: `close_issue` on a foreign issue;
  - **merge attempts**:
    - no tool exists;
    - `gitlab_write action="merge"` → `invalid_input`;
  - **read-only mode** for each write and deploy tool;
  - **plans**: expired, reused and forged `plan_id`;
  - **rollback** with a release name containing `/` or `..`;
  - **busy targets**;
  - **archive attacks**: tar-slip and symlink-escape archives, where the deploy still succeeds with those members skipped and nothing is written outside the release;
  - **config attacks**: a destination in a system path (refused by config); a `health_url` with `file://` (refused by config).
- [X] T039 [P] Add a `## GitLab` section to `README.md` covering:
  - what it does;
  - the token setup with the `security … -w` prompt;
  - the scopes table;
  - the permissions model;
  - the deploy flow and release layout;
  - rollback;
  - the sudoers `--with-gitlab` rule with its root-equivalence note;
  - the residual risks (token breadth; deploy sudo rights);
  - troubleshooting rows (`gitlab_token_missing`, `gitlab_auth`, `gitlab_scope`, `gitlab_rate_limited`, `rolled_back`);
  - the intro tool count;
  - the read-only tools to add to the Claude Code allow-list.
- [X] T040 Run `uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest -q` and fix any issues. Then run a privacy grep over `src tests specs/005-gitlab-integration README.md examples` for the operator's IP, hostnames, database names, personal names and any real token prefix.
- [ ] T041 Live validation (quickstart §2–§3, read-only) once the operator has:
  - stored their token in the Keychain;
  - added `[gitlab]` and their projects to their local config (not the repo).

  Then run `gitlab-check` and one read per tool. Writes and deploys need the operator's explicit go-ahead and a throwaway project or destination.

---

## Dependencies & Execution Order

- **Phase 1 → Phase 2**, which blocks everything.
- **US5 + US1** need T003–T015.
- **US2** needs US1: it reuses the client, access and read pieces (`compare`, `commits/:sha/refs`).
- **US3** and **US4** each need only US1, so they can run in parallel with US2.
- **Memory** needs US2 and US3.
- **Polish** comes last.

## Parallel Opportunities

- Phase 2: T005/T006, T010, T011, T012, T013 (different files).
- US1: tests T016–T019 run together.
- US2: T023, T024 and T025 run together; T026 and T027 run together.
- US3 and US4 run in parallel with each other.
- Polish: T038 and T039 run together.

## Implementation Strategy

1. **MVP**: Phases 1–3. Assistants can read GitLab and the token is proven not to leak.
2. **US2 deploy**: the main value for the operator.
3. **US3 and US4** in either order.
4. **Memory integration**, then the polish phase. Each checkpoint can be shipped on its own.
