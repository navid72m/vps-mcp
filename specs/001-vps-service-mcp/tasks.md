---

description: "Task list for VPS Service Management MCP Server"
---

# Tasks: VPS Service Management MCP Server

**Input**: Design documents from `/specs/001-vps-service-mcp/`

**Prerequisites**: [plan.md](./plan.md), [spec.md](./spec.md), [research.md](./research.md), [data-model.md](./data-model.md), [contracts/mcp-tools.md](./contracts/mcp-tools.md), [contracts/policy-config.md](./contracts/policy-config.md), [quickstart.md](./quickstart.md)

**Tests**: Included. The spec requires a negative test suite (SC-006), and the plan (research R12) defines unit, contract, and negative tests. Integration tests against a real VM are opt-in (`-m integration`). Write each story's tests first and confirm they fail before implementing.

**Organization**: Tasks are grouped by user story (US1–US8 from spec.md), so each story can be implemented and tested independently.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: The user story the task belongs to (US1–US8)
- Paths are relative to the repository root and use the single-project layout from plan.md (`src/vps_mcp/`, `tests/`).

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Project initialization and tooling

- [X] T001 Create `pyproject.toml` as a uv project named `vps-mcp`.
  - `requires-python = ">=3.12"`.
  - Dependencies: `mcp`, `asyncssh`, `pydantic>=2`.
  - Dev dependencies: `pytest`, `pytest-asyncio`, `ruff`, `mypy`.
  - Console script `vps-mcp = "vps_mcp.cli:main"`.
  - Pytest config: `asyncio_mode = "auto"`, and register a marker `integration` that is deselected by default (`addopts = "-m 'not integration'"`).
- [X] T002 Create the package skeleton from plan.md with empty `__init__.py` files:
  - `src/vps_mcp/`
  - `src/vps_mcp/config/`
  - `src/vps_mcp/models/`
  - `src/vps_mcp/remote/`
  - `src/vps_mcp/backends/`
  - `src/vps_mcp/core/`
  - `src/vps_mcp/store/`
  - `tests/unit/`, `tests/contract/`, `tests/negative/`, `tests/integration/`
  - `src/vps_mcp/__main__.py`, which calls `vps_mcp.cli.main()`.
- [X] T003 [P] Configure ruff (lint + format, line length 100) and mypy (strict for `src/vps_mcp`) in `pyproject.toml`. Add `.gitignore` covering `.venv/`, `__pycache__/`, `*.db`, and `.pytest_cache/`.
- [X] T004 [P] Create `examples/vps-mcp.toml` by copying the annotated example from `specs/001-vps-service-mcp/contracts/policy-config.md`. Add a comment on each key.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Config, validation, the remote connection, the store, the guard pipeline, and the MCP server shell. Every user story depends on these.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

### Tests for Foundation

- [X] T005 [P] Write unit tests for constrained input types in `tests/unit/test_inputs.py`. Cover accept and reject cases for every regex in T009, including:
  - shell metacharacters `;|&$\`<>` and newlines
  - leading `-`
  - length limits
  - `..` in paths
  - `lines` outside 1..5000
- [X] T006 [P] Write unit tests for policy loading in `tests/unit/test_policy.py`. Cover each of the 7 validation rules in `contracts/policy-config.md` §Validation rules. Also check that unknown keys are rejected and that `user = "root"` without `allow_root = true` is rejected.
- [X] T007 [P] Write unit tests for the guard pipeline in `tests/unit/test_guard.py`. Cover every branch of data-model.md §4, in this order:
  - `invalid_input`
  - `not_permitted` (outside the managed set, and remote-access unit)
  - `read_only`
  - `confirmation_required` / preview
  - `protected` on remove
  - `success` path acquiring the lock

  Also assert that exactly one audit record is written per call.
- [X] T008 [P] Write unit tests for connection failure classification in `tests/unit/test_connection.py`. Use asyncssh exception doubles and check the mapping to `unreachable | auth_rejected | identity_mismatch | timeout`. Also assert that no key path or passphrase appears in the exception messages.

### Implementation for Foundation

- [X] T009 [P] Implement constrained input types in `src/vps_mcp/models/inputs.py` as Pydantic `Annotated` types:

  | Type | Rule |
  |------|------|
  | `ServiceName` | `^[a-zA-Z0-9][a-zA-Z0-9_.@-]{0,127}$` |
  | `ServiceKind` | enum `os` \| `container` |
  | `PackageVersion` | `^[A-Za-z0-9.+:~_-]{1,64}$` |
  | `ImageRef` | OCI subset `registry/path:tag` or `@sha256:<64 hex>`, max 255 characters |
  | `ConfigKey` | `^[A-Za-z_][A-Za-z0-9_]{0,127}$` |
  | `Keyword` | literal, ≤ 200 characters, no control characters |
  | `TimeFilter` | ISO-8601 or relative `15m`/`2h`/`1d` |
  | `LineCount` | int 1..5000 |
  | `AbsPath` | absolute path, no `..`, not under `/proc`, `/sys`, `/dev` |
  | `NetworkName` | `^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$`; reject `host`, `none`, and any `container:*` value |

- [X] T010 [P] Implement `ErrorCategory` and `VpsMcpError` in `src/vps_mcp/errors.py`.
  - `ErrorCategory` is an enum with exactly these members: `not_permitted`, `not_found`, `permission_denied`, `connection_failure`, `timed_out`, `invalid_input`, `conflict`, `confirmation_required`, `read_only`, `protected`, `insufficient_disk`, `unapproved_source`, `failed`.
  - `VpsMcpError(category, message, subtype=None, last_known_state=None, extra: dict = {})`.
  - Add `to_envelope()`, which returns `{"ok": false, "error": {...}}` as specified in `contracts/mcp-tools.md` §Conventions.
- [X] T011 [P] Implement the view models in `src/vps_mcp/models/views.py`, following data-model.md §2:
  - `ServiceState` enum: `running | stopped | failed | transitioning | not_found`.
  - `ServiceStatus` with fields `name`, `kind`, `state`, `boot_enabled`, `uptime_s`, `last_state_change`, `last_exit_reason`, `version`, `protected`, `resources`, `pending_reconciliation`.
  - `LogEntry` and `LogResult` (`entries`, `returned`, `truncated`).
  - `ResourceSnapshot`.
  - `ContainerSpec` with fields `image`, `env`, `ports`, `volumes`, `networks: list[NetworkName] = []`, `command`, `restart_on_boot = true`, `healthcheck`, as in data-model.md §2 ContainerSpec. `ServiceStatus` gets `networks: list[str] = []` for containers.
  - `ActionOutcome` enum: `success | failed | refused | timed_out | unknown | rolled_back | preview`.
- [X] T012 Implement the policy models in `src/vps_mcp/config/policy.py`. Depends on T009.
  - Models: `TargetVPS`, `Timeouts`, `ApprovedSources`, `Policy`, `ConfigFileDecl`, `ServiceDeclaration`, `AppConfig`, with every field and default from data-model.md §1. Examples:
    - `timeouts.control_s = 60`, `deploy_s = 600`, `query_s = 15`
    - `health_stabilize_s = 10`, `log_default_lines = 200`, `log_max_lines = 5000`
    - `min_free_disk_mb = 1024`, `audit_retention_days = 90`, `revisions_kept = 5`
    - `secret_keys = ["*PASSWORD*","*SECRET*","*TOKEN*","*KEY*"]`
    - `remote_access_units = ["ssh","sshd"]`
  - Every model uses `extra="forbid"`.
  - Model validators implement rules 2–6 of `contracts/policy-config.md`.
- [X] T013 Implement `load_config(path) -> AppConfig` in `src/vps_mcp/config/loader.py`. Depends on T012.
  - Read the file with `tomllib` and expand `~` in paths.
  - Enforce rule 1: `known_hosts_path` exists and contains `host`. Otherwise raise with the hint "run `vps-mcp trust-host`".
  - Log a warning when `user == "root"` and `allow_root`.
- [X] T014 [P] Implement the redaction filter in `src/vps_mcp/remote/redact.py`.
  - `install_redaction(logger, secrets: list[str])` registers a `logging.Filter` that replaces any configured secret string (key path, passphrase) with `***`.
  - `mask_mapping(d, secret_globs)` and `mask_text_lines(text, secret_globs)` mask values whose key matches the `secret_keys` globs (case-insensitive), in `KEY=value`, `key = value`, and `key: value` lines (FR-017, FR-033).
- [X] T015 Implement `VpsConnection` in `src/vps_mcp/remote/connection.py`. Depends on T010 and T013.
  - Use asyncssh with key-only authentication: `key_path` or ssh-agent, `password=None`, `preferred_auth="publickey"`.
  - `known_hosts` pinned to the policy file, with no automatic trust (FR-003).
  - Keepalive 15 s, lazy reconnect, and an `async connect()` guarded by a lock.
  - Map exceptions to `VpsMcpError(connection_failure, subtype=...)`:
    - `unreachable`: OSError / ConnectionRefused
    - `auth_rejected`: PermissionDenied
    - `identity_mismatch`: HostKeyNotVerifiable
    - `timeout`: TimeoutError
  - Expose `is_connected` and an `on_reconnect` callback list (used by T039).
- [X] T016 Implement the `RemoteExecutor` protocol and `SshExecutor` in `src/vps_mcp/remote/executor.py`. Depends on T015.
  - `async run(argv: list[str], *, sudo: bool, timeout: float, stdin: str | None = None) -> CommandResult(exit_status, stdout, stderr)`.
  - Every element is `shlex.quote`d. The sudo prefix is `sudo -n --`.
  - Raise `VpsMcpError(timed_out)` on timeout.
  - Raise `VpsMcpError(permission_denied)` when stderr matches sudo's "a password is required" or "not allowed".
  - Raise `ConnectionLost` (a subclass of `VpsMcpError(connection_failure)`) when the channel closes mid-command.
  - No method accepts a raw shell string (FR-032).
- [X] T017 [P] Implement the SQLite store bootstrap in `src/vps_mcp/store/db.py`.
  - `open_db(vps_name)` opens `~/.local/state/vps-mcp/<vps_name>.db`, creates the directory, `chmod 0600` the file, and enables WAL.
  - Schema-version migrations create the tables `managed_services`, `service_revisions`, and `audit_records` with exactly the columns in data-model.md §3, plus an index on `audit_records(ts DESC)` and on `service_revisions(service_name, kind)`.
  - `purge_audit(retention_days)`.
- [X] T018 Implement `AuditStore` in `src/vps_mcp/store/audit.py`. Depends on T017.
  - `record(...) -> int` writes one row with fields `ts` (UTC), `session_id`, `action` (enum `start, stop, restart, enable, disable, deploy, update, configure, rollback, remove`), `service_name`, `kind`, `params_json` (masked via T014), `confirmed`, `outcome`, `error_category`, `error_message` (masked), `resulting_state`, `data_deleted`, and `duration_ms`.
  - `set_reconciled(id, state)`.
  - `query(limit=50, service=None, action=None, outcome=None)`, newest first. `limit` must be 1..500.
- [X] T019 [P] Implement `ServicesStore` in `src/vps_mcp/store/services.py`: `add_deployed(name, kind, spec_json)`, `mark_removed(name, kind)`, and `list_rows()` over `managed_services`.
- [X] T020 Implement `ManagedSet` in `src/vps_mcp/core/managed_set.py`. Depends on T012 and T019.
  - The effective set is declared services ∪ deployed rows with `removed_at IS NULL` − declared services with `removed_at` set (data-model.md §3).
  - `resolve(name, kind|None) -> ServiceDeclaration`:
    - Raises `invalid_input` "ambiguous: specify kind" if the name exists for both kinds.
    - Raises `not_permitted` if the service is absent or its unit is in `remote_access_units`. The message never reveals whether the service exists on the VPS.
- [X] T021 [P] Implement per-service locks in `src/vps_mcp/core/locks.py`: `ServiceLocks.acquire(kind, name)` is an async context manager over a dict of `asyncio.Lock` (FR-012).
- [X] T022 Implement the guard pipeline in `src/vps_mcp/core/guard.py`. Depends on T018, T020, and T021.
  - `async run_action(ctx, action, service, kind, *, confirm, needs_confirm: bool, is_remove=False, params, body: Callable)` follows the data-model.md §4 order exactly:
    1. validate
    2. managed set or remote-access check
    3. `read_only`
    4. confirmation, returning a preview or `confirmation_required`
    5. protected-remove
    6. lock
    7. `asyncio.wait_for(body, timeout)`
    8. map `ConnectionLost` to `unknown` and timeout to `timed_out` with `last_known_state`
  - Write exactly one audit record on every exit path, including refusals. Return `{outcome, audit_id, ...}`.
- [X] T023 [P] Define the `ServiceBackend` protocol in `src/vps_mcp/backends/base.py`. Methods:
  - `status(decl) -> ServiceStatus` and `list_status(decls)`
  - `start`, `stop`, `restart`, `set_boot(decl, enabled)`
  - `logs(decl, lines, since, until) -> list[LogEntry]`
  - `get_config` and `apply_config`
  - `deploy`, `update`, `remove`, `cleanup_failed_deploy`
  - `resource_usage(decls)`

  Add `NotImplementedError` defaults for methods that later stories fill in.
- [X] T024 Create the application context and MCP server shell in `src/vps_mcp/server.py`. Depends on T013, T016, T018, and T022.
  - `AppContext` holds the config, connection, executor, stores, managed set, locks, and backends keyed by kind.
  - `build_server(config_path) -> FastMCP`, with the lifespan opening the DB, running `purge_audit`, and installing redaction (T014).
  - A helper `tool_result(payload)` that wraps success as `{"ok": true, ...}`.
  - A decorator `@handles_errors` that converts `VpsMcpError` into an `isError` result with `to_envelope()`.
  - `session_id` comes from the MCP request context.
- [X] T025 Implement `cli.py` with argparse subcommands in `src/vps_mcp/cli.py`. Depends on T024.
  - `serve --config PATH` runs `build_server(...).run(transport="stdio")`.
  - Placeholders for `check-config`, `trust-host`, and `bootstrap`, which exit 2 with "not implemented" (completed in Phase 11).
- [X] T026 [P] Create shared fixtures in `tests/conftest.py`:
  - `FakeRemoteExecutor`: records every argv and its sudo flag, and returns canned `CommandResult`s keyed by argv prefix. Add a helper `mutating_calls()` that filters out read-only commands (`systemctl show`, `journalctl`, `docker inspect/logs/stats`, `df`, `cat /proc/*`, `apt-cache policy`, `dnf info`, `cat` of declared config files).
  - Fixtures `sample_config` (nginx os protected + api container), `tmp_db`, and `app_ctx`, which wires the fakes into `AppContext`.
  - An `mcp_client` fixture that uses the SDK's in-memory client session against `build_server`.

**Checkpoint**: The foundation is ready. `uv run pytest tests/unit` passes for T005–T008, and `vps-mcp serve` starts with zero tools.

---

## Phase 3: User Story 1 - Check the health of services (Priority: P1) 🎯 MVP

**Goal**: The operator can list managed services, both OS services and containers, and see their kind, state, uptime, boot setting, and failure reason.

**Independent Test**: With a known mix of running and stopped services, `list_services` and `get_service_status` match the actual VPS state (quickstart scenarios 1, 2, 17, and 18).

### Tests for User Story 1

- [X] T027 [P] [US1] Write unit tests for the systemd status parser in `tests/unit/test_systemd_status.py`, using canned `systemctl show` output. Cover:
  - `ActiveState=active` → running (with `uptime_s`)
  - `inactive` → stopped
  - `failed` → failed (with `last_exit_reason` built from `ExecMainStatus` + `Result`)
  - `activating`, `deactivating`, `reloading` → transitioning
  - `LoadState=not-found` → not_found (`boot_enabled=None`)
- [X] T028 [P] [US1] Write unit tests for the container status parser in `tests/unit/test_container_status.py`, using canned `docker inspect` JSON. Cover:
  - `running` → running
  - `exited` with code 0 or `created` → stopped
  - `exited` with code ≠ 0 or `dead` → failed
  - `restarting`, `removing` → transitioning
  - inspect "No such object" → not_found
  - restart policy `unless-stopped` → `boot_enabled=True`
- [X] T029 [P] [US1] Write contract tests in `tests/contract/test_status_tools.py`:
  - `list_services` and `get_service_status` exist with `readOnlyHint: true` and the input schemas from `contracts/mcp-tools.md`.
  - A name outside the managed set returns `not_permitted`.
  - An ambiguous name returns `invalid_input`.
  - A `connection_failure` envelope includes the `subtype`.

### Implementation for User Story 1

- [X] T030 [P] [US1] Implement `SystemdBackend.status` and `list_status` in `src/vps_mcp/backends/systemd.py`.
  - Run `systemctl show <unit> --property=LoadState,ActiveState,SubState,UnitFileState,ActiveEnterTimestamp,StateChangeTimestamp,ExecMainStatus,Result,MemoryCurrent,CPUUsageNSec` with sudo=False. `list_status` passes multiple units in one call.
  - Parse the key=value output and apply the state mapping from data-model.md §2.
  - `boot_enabled = UnitFileState == "enabled"`.
  - `version` comes from `dpkg-query -W -f='${Version}' <package>` or `rpm -q --qf '%{VERSION}-%{RELEASE}' <package>` when `package` is set.
- [X] T031 [P] [US1] Implement `ContainerBackend.status` and `list_status` in `src/vps_mcp/backends/container.py`.
  - Run `<runtime> inspect --format '{{json .}}' <name>` with sudo=True.
  - Apply the state mapping from data-model.md §2.
  - `boot_enabled = RestartPolicy.Name in ("always","unless-stopped")`.
  - `version` = the image reference plus `@` digest from `.Image`.
  - `uptime_s` comes from `State.StartedAt`.
  - `networks` = the keys of `NetworkSettings.Networks`.
- [X] T032 [US1] Implement the status queries in `src/vps_mcp/core/queries.py`. Depends on T030 and T031.
  - `list_services(ctx, kind=None)` and `get_service_status(ctx, service, kind=None)`.
  - Both use `ManagedSet.resolve`, dispatch to the backend for the kind, set `protected` from the declaration, and set `pending_reconciliation` from the audit store.
  - Wrap each call in `asyncio.wait_for(..., timeouts.query_s)` (SC-008).
  - Never fall back to cached state when the connection fails (Story 1, scenario 4).
- [X] T033 [US1] Register the `list_services` and `get_service_status` tools in `src/vps_mcp/server.py`.
  - Annotate them with `readOnlyHint: true`.
  - Each tool description is plain language (FR-001).
  - `list_services` returns `{services, vps: {name, reachable: true}}`.

**Checkpoint**: The MVP read path works. Quickstart scenarios 1, 2, 17, and 18 pass on the test VM.

---

## Phase 4: User Story 2 - Control the lifecycle of a service (Priority: P1)

**Goal**: The operator can start, stop, and restart services, and enable or disable boot-start. The result reports the verified state after the action. The protected, read-only, and lock-out rules are enforced.

**Independent Test**: Stop, start, and restart one OS service and one container, and verify each transition on the VPS (quickstart scenarios 3–6).

### Tests for User Story 2

- [X] T034 [P] [US2] Write golden argv unit tests in `tests/unit/test_control_commands.py`:
  - systemd: `sudo -n -- systemctl start|stop|restart|enable|disable <unit>`
  - container: `sudo -n -- docker start|stop|restart <name>`, and `docker update --restart unless-stopped|no <name>` for boot-start
  - After each mutating call, a status poll follows. If the state has not settled within the control timeout, the result is `timed_out` with `last_known_state`.
  - A failed start includes the last 20 log lines.
- [X] T035 [P] [US2] Write contract tests in `tests/contract/test_control_tools.py`. The tools are `start_service`, `stop_service`, `restart_service`, and `set_boot_start`.
  - Annotations: `destructiveHint` false for start, true for stop and restart.
  - A protected stop without `confirm` returns `confirmation_required`.
  - `stop_service("sshd")` returns `not_permitted`.
  - `read_only=true` returns `read_only`.
  - Every call writes exactly one audit row.

### Implementation for User Story 2

- [X] T036 [P] [US2] Implement `SystemdBackend.start/stop/restart/set_boot` in `src/vps_mcp/backends/systemd.py`.
  - Each method runs the mutating `systemctl` command with sudo=True.
  - Then it polls `status()` every 1 s until the state is no longer `transitioning`, or the control timeout expires (FR-010).
- [X] T037 [P] [US2] Implement `ContainerBackend.start/stop/restart/set_boot` in `src/vps_mcp/backends/container.py`.
  - start/stop/restart use `<runtime> start|stop|restart <name>`.
  - `set_boot` uses `<runtime> update --restart unless-stopped|no <name>`.
  - All of them verify by polling `status()`, the same way as T036.
- [X] T038 [US2] Implement the control use cases in `src/vps_mcp/core/actions.py`. Depends on T022, T036, and T037.
  - `start`, `stop`, `restart`, and `set_boot_start` go through `guard.run_action`.
  - `needs_confirm=True` for stop, restart, and disable when the declaration is `protected` (FR-028).
  - If the post-action state is `failed`, return `VpsMcpError(failed)` with `extra.logs` = the last 20 lines from `backend.logs` (wrapped in try/except, so a log failure does not mask the original error).
- [X] T039 [US2] Implement unknown-outcome reconciliation in `src/vps_mcp/core/reconcile.py`.
  - `Reconciler.schedule(audit_id, decl)` is called by the guard when an outcome is `unknown`.
  - Register it on `VpsConnection.on_reconnect`. On the next successful connection it re-reads the status and calls `AuditStore.set_reconciled` (FR-013).
  - `get_service_status` then shows `pending_reconciliation` until that happens.
- [X] T040 [US2] Register the `start_service`, `stop_service`, `restart_service`, and `set_boot_start` tools in `src/vps_mcp/server.py`.
  - Annotations as in T035.
  - Output `{outcome, service, audit_id}`.

**Checkpoint**: US1 and US2 together make up the MVP. Quickstart scenarios 1–6 pass.

---

## Phase 5: User Story 3 - Read service logs (Priority: P2)

**Goal**: Logs for either kind, filtered by line count, time range, and literal keyword, capped and marked as truncated.

**Independent Test**: Emit known lines and request them with `lines`, `since`, and `keyword` (quickstart scenario 7).

### Tests for User Story 3

- [X] T041 [P] [US3] Write unit tests in `tests/unit/test_logs.py`:
  - journalctl JSON parsing (`__REALTIME_TIMESTAMP`, `MESSAGE`, including byte-array `MESSAGE`)
  - docker `--timestamps` line parsing, with stdout and stderr streams
  - the keyword filter is literal and case-insensitive (a regex metacharacter such as `.*` matches only the literal text)
  - the default of 200 lines applies when `lines` is omitted
  - `truncated=True` when more entries exist than were returned
  - output is ordered oldest first, newest last

### Implementation for User Story 3

- [X] T042 [P] [US3] Implement `SystemdBackend.logs` in `src/vps_mcp/backends/systemd.py`.
  - Run `journalctl -u <unit> -o json --no-pager -n <N+1> [--since <ts>] [--until <ts>]` with sudo=True.
  - Fetch N+1 entries so truncation can be detected.
  - Parse the output into `LogEntry(stream="journal")`.
- [X] T043 [P] [US3] Implement `ContainerBackend.logs` in `src/vps_mcp/backends/container.py`.
  - Run `<runtime> logs --timestamps --tail <N+1> [--since] [--until] <name>` with sudo=True.
  - Parse stdout and stderr separately into `LogEntry(stream=...)`.
- [X] T044 [US3] Implement `get_service_logs` in `src/vps_mcp/core/queries.py`. Depends on T042 and T043.
  - `lines` default `log_default_lines` (200), max `log_max_lines` (5000).
  - When `keyword` is set, fetch up to `log_max_lines` and filter in Python using a literal, case-insensitive substring match. Then keep the last `lines` entries.
  - Return `LogResult(entries, returned, truncated)`.
  - Bounded by `timeouts.query_s`.
- [X] T045 [US3] Register the `get_service_logs` tool in `src/vps_mcp/server.py` with `readOnlyHint: true` and the input schema `{service, kind?, lines?: 1..5000 = 200, since?, until?, keyword?: str(≤200)}`.

**Checkpoint**: Quickstart scenario 7 passes.

---

## Phase 6: User Story 4 - Deploy a new service (Priority: P2)

**Goal**: Deploy an OS package or a container from an approved source. Check free disk space first, refuse name conflicts, clean up on failure, and add the result to the managed set.

**Independent Test**: Deploy `redis:7` as `cache` and see the preview, then success and a listed service. Then try an unapproved image and confirm it is refused with nothing pulled (quickstart scenarios 8 and 9).

### Tests for User Story 4

- [X] T046 [P] [US4] Write unit tests in `tests/unit/test_deploy.py`:
  - An image outside `image_prefixes` returns `unapproved_source`, with zero mutating calls.
  - An apt candidate whose origin is not in `apt_origins` returns `unapproved_source`.
  - Free disk below `min_free_disk_mb` returns `insufficient_disk`.
  - An existing `(name, kind)` in the managed set, or an existing container or unit on the VPS, returns `conflict`.
  - Without `confirm`, the result is `outcome="preview"` with zero mutating calls.
  - If `docker run` fails, the result is `failed` with `cleaned_up=True`, and the container and pulled image are removed.
  - A package that was installed before the attempt is never removed during cleanup.
  - On success, `ServicesStore.add_deployed` is called.
  - Networks:
    - A missing network produces `network create --driver bridge --label io.vps-mcp.managed=true <net>` before `run`.
    - An existing network is not recreated.
    - The first network goes to `run --network`, and the rest to `network connect`.
    - The preview lists the networks to create.
    - On failure, cleanup removes the networks this attempt created.
- [X] T047 [P] [US4] Write a contract test in `tests/contract/test_deploy_tool.py` for the `deploy_service` input schema from `contracts/mcp-tools.md`:
  - `kind="os"` requires `os`, and `kind="container"` requires `container`.
  - `ContainerSpec` port, volume, and network formats are validated. `networks: ["host"]` returns `invalid_input`.
  - Error categories as listed in the contract.

### Implementation for User Story 4

- [X] T048 [P] [US4] Implement image-prefix normalisation and matching in `src/vps_mcp/core/sources.py`.
  - A bare `redis:7` becomes `docker.io/library/redis:7`.
  - `user/x` becomes `docker.io/user/x`.
  - Match by prefix against `approved_sources.image_prefixes`.
  - Also add an `apt`/`dnf` origin check that parses `apt-cache policy <pkg>` (candidate `o=` origin) or `dnf info <pkg>` (`From repo` / `Repository`) against `apt_origins` / `dnf_repos`.
- [X] T049 [P] [US4] Implement `ContainerBackend.deploy` and `cleanup_failed_deploy` in `src/vps_mcp/backends/container.py`.
  - Steps:
    1. Pull the image with `<runtime> pull <image>`.
    2. Run `<runtime> run -d --name <name> --label io.vps-mcp.managed=true --label io.vps-mcp.service=<name> --restart unless-stopped|no`, plus `-e`, `-p`, `-v` flags, an optional `--health-cmd`, and the image and command.
    3. Verify with `status()`.
  - Before `run`, inspect each network in `spec.networks` and create the missing ones with the managed label. Pass the first to `run --network <net>`, then `network connect <net> <name>` for each remaining one. Record which networks this attempt created.
  - Env values are passed through `--env-file /dev/stdin` using the executor's stdin, so that secrets never appear in argv.
  - Cleanup runs `rm -f <name>`, then `rmi <image>` only if this attempt pulled it (decided by an image-presence check before the pull), then `network rm` for the networks this attempt created.
  - Add a shared helper `gc_managed_networks(names)`: for each name, remove the network if it carries `io.vps-mcp.managed=true` and `network inspect` shows zero attached containers. Return the removed names. T058 and T062 use it.
- [X] T050 [P] [US4] Implement `SystemdBackend.deploy` and `cleanup_failed_deploy` in `src/vps_mcp/backends/systemd.py`.
  - Record whether the package is already installed. Install it with `apt-get install -y <pkg>[=<ver>]` or `dnf install -y <pkg>[-<ver>]` (with `DEBIAN_FRONTEND=noninteractive` via `env`). If `enable_on_boot` is set, run `systemctl enable --now <unit>`, or `start` only.
  - Cleanup removes the package only if it was not installed before.
- [X] T051 [US4] Implement the deploy use case in `src/vps_mcp/core/deploy.py`. Depends on T048–T050.
  - Run checks in this order: approved source → conflict (managed set plus a backend `status() != not_found`) → disk (`df -P --output=avail -B1M / <docker-root>`, where `<docker-root>` comes from `docker info --format '{{.DockerRootDir}}'`) → preview if not `confirm`.
  - Then run the backend deploy under `timeouts.deploy_s`. On any exception call `cleanup_failed_deploy` and report `cleaned_up`.
  - On success call `ServicesStore.add_deployed` with `spec_json` and store an initial `active` revision (only if T056 exists, otherwise skip).
  - Use `guard.run_action(action="deploy", needs_confirm=True)`.
- [X] T052 [US4] Register the `deploy_service` tool in `src/vps_mcp/server.py` with `destructiveHint: false` and output `{outcome, service, audit_id}` or a preview description.

**Checkpoint**: Quickstart scenarios 8 and 9 pass. A deployed service is then usable through US1–US3.

---

## Phase 7: User Story 5 - Update or reconfigure a service (Priority: P2)

**Goal**: View the configuration with secrets masked, preview changes as a diff, then apply them with a health gate. Roll back automatically when the service is unhealthy, and support a manual one-step rollback.

**Independent Test**: Change `worker_connections` in nginx.conf to see the preview and then success. Then apply an invalid config and see it rolled back with nginx still running. Then roll back the `cache` env (quickstart scenarios 10–12).

### Tests for User Story 5

- [X] T053 [P] [US5] Write unit tests for revision state transitions in `tests/unit/test_revisions.py`, following data-model.md §3:
  - Applying a change: active → previous, new → active, older previous → superseded.
  - Auto-rollback: new → rolled_back, previous → active.
  - Manual rollback: active → rolled_back, previous → active.
  - Pruning keeps the newest `revisions_kept` (5) rows per service.
  - Rolling back with no `previous` raises `not_found`.
- [X] T054 [P] [US5] Write unit tests for the change flow in `tests/unit/test_changes.py`:
  - A `files[].path` that is not declared returns `not_permitted`.
  - The preview diff masks secret keys and applies nothing.
  - A validator (`nginx -t`) failure returns `failed` with no write.
  - A health gate that fails after the write produces `rolled_back`: the prior content is rewritten and the service reactivated.
  - If the rollback itself fails, the result is `failed` with both errors.
  - For containers, `spec_patch` is merged with `null` meaning delete, and the container is recreated.
  - An update of the version or image follows the same gate and rollback path.

### Implementation for User Story 5

- [X] T055 [P] [US5] Implement the built-in validator catalogue in `src/vps_mcp/backends/validators.py`. It maps keys to fixed argv: `nginx` → `nginx -t -c <path>`, `sshd` → `sshd -t -f <path>`, `caddy` → `caddy validate --config <path>`, `haproxy` → `haproxy -c -f <path>`. The validator runs against a temporary staged file, and operators cannot supply arbitrary strings.
- [X] T056 [P] [US5] Implement `RevisionStore` in `src/vps_mcp/store/revisions.py`:
  - `snapshot(service, kind, version, content_json, audit_id) -> id`
  - `activate_new`, `mark_rolled_back`, `previous(service, kind)`, `list(service, kind)`, and `prune(revisions_kept)`
  - The status transitions follow data-model.md §3.
- [X] T057 [US5] Implement `SystemdBackend.get_config`, `apply_config`, and `update` in `src/vps_mcp/backends/systemd.py`.
  - `get_config` runs `cat <path>` with sudo for each declared `config_files[].path`.
  - `apply_config`:
    1. Write the new content to `/tmp/vps-mcp.<rand>` through stdin (`tee`).
    2. Run the optional validator.
    3. Run `install -m <mode> -o <owner> -g <group> <tmp> <path>`.
    4. Run `systemctl reload|restart <unit>`, according to `activate`.
    5. Remove the temporary file.
  - `update`: install the target package version (as in T050).
- [X] T058 [US5] Implement `ContainerBackend.get_config`, `apply_config`, and `update` in `src/vps_mcp/backends/container.py`.
  - `get_config` reconstructs the `ContainerSpec` from `inspect` (env, ports, mounts, `NetworkSettings.Networks` keys, restart policy, command, healthcheck, image).
  - `apply_config(spec)` runs `pull` if the image changed, then `rename <name> <name>.vps-mcp-old`, then runs the new container (T049 logic, including network creation and attachment). On success it removes the old container and calls `gc_managed_networks` on the networks the old spec had but the new one lacks; on failure it removes the new one and renames the old one back.
  - `update(image)` = `apply_config` with the new image.
- [X] T059 [US5] Implement the change use cases in `src/vps_mcp/core/changes.py`. Depends on T055–T058.
  - Use cases: `get_service_config`, `configure_service`, `update_service`, `rollback_service`, and `list_revisions`.
  - For a change:
    1. Snapshot the current state as a revision.
    2. Build the proposed state and a unified diff (`difflib`), masked via T014.
    3. If `confirm` is not set, return a preview (and validation output for OS services).
    4. Apply the change under `timeouts.deploy_s`.
    5. Run the health gate: poll until running/active, or container `healthy` when a healthcheck exists. The service must stay healthy for `health_stabilize_s` (10).
    6. If the gate fails, re-apply the previous revision automatically and return `outcome=rolled_back`.
  - `rollback_service` restores `previous` and requires `confirm`.
- [X] T060 [US5] Register five tools in `src/vps_mcp/server.py`:
  - `get_service_config` and `list_revisions` (`readOnlyHint: true`).
  - `configure_service`, `update_service`, and `rollback_service` (`destructiveHint: true`), with input schemas as in `contracts/mcp-tools.md`.

**Checkpoint**: Quickstart scenarios 10–12 pass.

---

## Phase 8: User Story 6 - Remove a service (Priority: P3)

**Goal**: Stop, uninstall, and remove a service from the managed set. Data is kept unless deletion is requested, and protected services can never be removed.

**Independent Test**: Remove `cache` with its data kept and check that the volume still exists. Then try to remove `nginx` and confirm `protected` (quickstart scenarios 13 and 14).

### Tests for User Story 6

- [X] T061 [P] [US6] Write unit tests in `tests/unit/test_removal.py`:
  - A protected service returns `protected` even with `confirm=True`.
  - A remote-access unit returns `not_permitted`.
  - Without `confirm`, the preview lists what will be removed and which data paths or volumes are kept or deleted.
  - Container keep: `rm -f` only.
  - Container delete: also `volume rm` for named volumes from the spec.
  - In both container cases, `gc_managed_networks` removes managed networks left empty, and never removes a network that is unlabelled or still has containers attached. The result includes `networks_removed`.
  - OS keep: `apt-get remove`.
  - OS delete: `apt-get purge` plus `rm -rf --` for each declared `data_paths` entry, which are validated `AbsPath`s.
  - Success calls `ServicesStore.mark_removed`, and the audit record has `data_deleted` set.

### Implementation for User Story 6

- [X] T062 [P] [US6] Implement `ContainerBackend.remove(decl, delete_data)` in `src/vps_mcp/backends/container.py`. Read the container's networks before `rm -f`, run `volume rm` for the spec's named volumes when `delete_data` is set, then call `gc_managed_networks` on the networks it read and return `networks_removed`.
- [X] T063 [P] [US6] Implement `SystemdBackend.remove(decl, delete_data)` in `src/vps_mcp/backends/systemd.py`.
  - Disable and stop the unit, run `apt-get remove|purge -y` or `dnf remove -y`, and delete `data_paths` when requested.
  - Requires `package` to be declared. Otherwise return `failed` with "package not declared".
- [X] T064 [US6] Implement the remove use case in `src/vps_mcp/core/removal.py`. Depends on T062 and T063.
  - Use `guard.run_action(action="remove", needs_confirm=True, is_remove=True)`.
  - Then call `ServicesStore.mark_removed`.
  - The audit record gets `data_deleted`.
- [X] T065 [US6] Register the `remove_service` tool in `src/vps_mcp/server.py` with `destructiveHint: true` and output `{outcome, data_deleted, networks_removed, audit_id}`.

**Checkpoint**: Quickstart scenarios 13 and 14 pass.

---

## Phase 9: User Story 7 - View server resource usage (Priority: P3)

**Goal**: Overall load, memory, and disk usage, plus CPU and memory for each managed service where available.

**Independent Test**: Compare the summary with `uptime`, `free`, and `df` on the VM; it should agree within 5% (quickstart scenario 15).

### Tests for User Story 7

- [X] T066 [P] [US7] Write unit tests in `tests/unit/test_resources.py` that parse canned `/proc/loadavg`, `/proc/meminfo` (used = MemTotal − MemAvailable), `nproc`, `df -P -B1 -x tmpfs -x devtmpfs -x overlay` output, `systemctl show MemoryCurrent/CPUUsageNSec` (`[not set]` maps to None), and `docker stats --no-stream --format '{{json .}}'`.

### Implementation for User Story 7

- [X] T067 [P] [US7] Implement host resource collection in `src/vps_mcp/core/resources.py`. It runs the T066 commands with sudo=False under `timeouts.query_s` and builds a `ResourceSnapshot`.
- [X] T068 [US7] Implement `SystemdBackend.resource_usage` in `src/vps_mcp/backends/systemd.py`.
  - CPU % comes from two `CPUUsageNSec` samples taken 1 s apart.
  - Memory comes from `MemoryCurrent`.
- [X] T069 [US7] Implement `ContainerBackend.resource_usage` in `src/vps_mcp/backends/container.py` using `docker stats --no-stream`. Then add `get_resource_usage(ctx, include_services=True)` to `src/vps_mcp/core/queries.py`.
- [X] T070 [US7] Register the `get_resource_usage` tool in `src/vps_mcp/server.py` with `readOnlyHint: true`.

**Checkpoint**: Quickstart scenario 15 passes.

---

## Phase 10: User Story 8 - Review an audit trail of actions (Priority: P3)

**Goal**: The operator can retrieve the audit trail, newest first, with filters.

**Independent Test**: After quickstart scenarios 3–14, `get_audit_log` lists them all newest first, including refused and rolled-back ones (quickstart scenario 16).

### Tests for User Story 8

- [X] T071 [P] [US8] Write a contract test in `tests/contract/test_audit_tool.py`:
  - `get_audit_log` input `{limit?: 1..500 = 50, service?, action?, outcome?}`.
  - Records are newest first.
  - Refused and rolled-back records are present.
  - No record contains the configured key path or the test passphrase (SC-007).

### Implementation for User Story 8

- [X] T072 [US8] Register the `get_audit_log` tool in `src/vps_mcp/server.py`.
  - It calls `AuditStore.query` and returns `{records}`, including `reconciled_state`.
  - Annotate it with `readOnlyHint: true`.

**Checkpoint**: All 16 tools are registered. Quickstart scenario 16 passes.

---

## Phase 11: Polish & Cross-Cutting Concerns

**Purpose**: Operator CLI commands, the negative suite, integration tests, and documentation.

- [X] T073 [P] Implement `check-config` in `src/vps_mcp/cli.py`. It calls `load_config`, prints `OK` with exit 0, or prints each validation error with exit 1.
- [X] T074 [P] Implement `trust-host --host H [--port 22] --known-hosts PATH` in `src/vps_mcp/cli.py`.
  - Fetch the host key with `asyncssh.get_server_host_key` and print its SHA256 fingerprint.
  - Require an interactive `yes` before appending the key to `known_hosts`.
  - Refuse to overwrite an existing entry with a different key unless `--replace` is given.
- [X] T075 [P] Implement `bootstrap --print-sudoers [--user vpsmcp] [--runtime docker|podman] [--pm apt|dnf]` in `src/vps_mcp/cli.py`.
  - Emit a sudoers drop-in that allows only `systemctl`, `journalctl`, `apt-get`/`dnf`, `docker`/`podman`, `install`, `tee /tmp/vps-mcp.*`, `rm`, `cat`, and the validator binaries from T055, all with absolute paths and `NOPASSWD`.
  - Include a header comment telling the operator to run `visudo -c`.
- [X] T076 [P] Build the negative suite in `tests/negative/test_disallowed_requests.py`: at least 50 parametrised cases across all 9 state-changing tools.
  - Cases include:
    - injection strings in every string field
    - names outside the managed set
    - remote-access units
    - protected services without confirm
    - remove of a protected service
    - unapproved images and packages
    - undeclared config paths
    - `read_only=true`
    - `lines=0` or `5001`
    - an oversized keyword
  - Each case asserts that `FakeRemoteExecutor.mutating_calls() == []` and the expected error category (SC-006).
- [X] T077 [P] Write a credential leak test in `tests/unit/test_redaction.py`. Run every tool with a configured key path and passphrase, then assert that neither appears in tool outputs, in captured logs, or in any column of the SQLite DB (SC-007, FR-033).
- [X] T078 [P] Write `tests/integration/provision.sh`, which provisions an Ubuntu 24.04 VM with Multipass:
  - Create the `vpsmcp` user and authorize a key.
  - Install nginx and docker.
  - Install the sudoers file from T075.
  - Write a test TOML to `tests/integration/vps-mcp.test.toml`.
- [X] T079 Write integration tests in `tests/integration/test_quickstart.py`. Depends on T078.
  - Mark them `@pytest.mark.integration`.
  - Add a `--vps-config` option in `tests/conftest.py`.
  - Automate quickstart scenarios 1–16 against the VM with real SSH, and assert SC-001 (<10 s) and SC-002 (<30 s) timings.
- [X] T080 [P] Write `README.md` covering:
  - the purpose
  - a security model summary (the 8 safety layers from plan.md §Summary)
  - install with `uv`
  - setup (bootstrap → trust-host → config → check-config)
  - `claude mcp add` registration
  - a tool table linking to `specs/001-vps-service-mcp/contracts/mcp-tools.md`
- [X] T081 Run `uv run ruff check`, `uv run mypy src`, and `uv run pytest`, and fix every failure.
- [ ] T082 Run the full manual run-through of `specs/001-vps-service-mcp/quickstart.md` (scenarios 1–18) against the provisioned VM, and record the results in `specs/001-vps-service-mcp/checklists/quickstart-results.md`.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies.
- **Foundational (Phase 2)**: Depends on Setup. **Blocks all user stories.**
- **US1 (Phase 3)**: Depends on Foundational.
- **US2 (Phase 4)**: Depends on Foundational. It reuses the backend `status()` from US1 for verification (T030 and T031), so build it after US1, or do T030 and T031 first.
- **US3 (Phase 5)**: Depends on Foundational. It is independent of US2. T038's failed-start log excerpt uses US3's `logs()` when present and degrades gracefully when it is absent.
- **US4 (Phase 6)**: Depends on Foundational and on US1 (`status()`, for conflict detection and verification).
- **US5 (Phase 7)**: Depends on Foundational and US1. The container `apply_config` reuses the T049 run logic, so build US4 first or extract the shared run helper early.
- **US6 (Phase 8)**: Depends on Foundational and US1. It is independent of US4 and US5.
- **US7 (Phase 9)**: Depends on Foundational only.
- **US8 (Phase 10)**: Depends on Foundational only. The audit store is foundational. To be meaningful it needs at least one mutating story (US2).
- **Polish (Phase 11)**: T073–T075 and T077–T078 can start after Foundational. T076, T079, and T082 need all stories.

### Story completion order (recommended)

```text
Setup → Foundational → US1 → US2  ══ MVP ══
                         ├→ US3
                         ├→ US7 ─┐
                         ├→ US8 ─┤
                         └→ US4 → US5 → US6 → Polish
```

### Within Each User Story

- Write the tests first and confirm they fail.
- Backends (one file per kind, in parallel) → core use case → MCP tool registration in `server.py`.
- `server.py`, `backends/systemd.py`, and `backends/container.py` are shared files. Tasks touching the same file across stories are sequential, never [P] with each other.

### Parallel Opportunities

- **Phase 1**: T003 and T004.
- **Phase 2**:
  - Tests T005–T008 run together.
  - T009, T010, T011, T014, T017, T021, T023, and T026 run in parallel.
  - Then T012 → T013 → T015 → T016, and T018/T019 → T020 → T022 → T024 → T025.
- **Per story**: the two backend tasks (systemd vs container) are always [P], and so are the story's test tasks.
- **Across stories** (with more than one developer): after US1, US3, US7, and US8 can run in parallel with US2.

---

## Parallel Example: User Story 1

```bash
# Tests together:
Task: "T027 Unit tests for systemd status parser in tests/unit/test_systemd_status.py"
Task: "T028 Unit tests for container status parser in tests/unit/test_container_status.py"
Task: "T029 Contract tests for list_services/get_service_status in tests/contract/test_status_tools.py"

# Backends together:
Task: "T030 SystemdBackend.status/list_status in src/vps_mcp/backends/systemd.py"
Task: "T031 ContainerBackend.status/list_status in src/vps_mcp/backends/container.py"
```

## Parallel Example: User Story 2

```bash
Task: "T034 Golden-argv control tests in tests/unit/test_control_commands.py"
Task: "T035 Contract tests for control tools in tests/contract/test_control_tools.py"
Task: "T036 SystemdBackend start/stop/restart/set_boot in src/vps_mcp/backends/systemd.py"
Task: "T037 ContainerBackend start/stop/restart/set_boot in src/vps_mcp/backends/container.py"
```

## Parallel Example: User Story 5

```bash
Task: "T053 Revision transition tests in tests/unit/test_revisions.py"
Task: "T054 Change-flow tests in tests/unit/test_changes.py"
Task: "T055 Validator catalogue in src/vps_mcp/backends/validators.py"
Task: "T056 RevisionStore in src/vps_mcp/store/revisions.py"
```

---

## Implementation Strategy

### MVP First (US1 + US2)

1. Phase 1 Setup → Phase 2 Foundational.
2. Phase 3 (US1): read-only status. **Stop and validate** quickstart scenarios 1, 2, 17, and 18. This is already safe to use against a real VPS.
3. Phase 4 (US2): control. **Stop and validate** quickstart scenarios 3–6.
4. Optionally ship the MVP with `read_only = true` as the recommended default for first use.

### Incremental Delivery

1. MVP (US1 + US2).
2. Add US3 Logs.
3. Add US7 Resources and US8 Audit query. These are cheap read-only additions.
4. Add US4 Deploy.
5. Add US5 Update/Configure, the highest-risk story, which comes with rollback.
6. Add US6 Remove.
7. Polish: the negative suite, CLI commands, integration tests, and the quickstart run.

Each increment leaves every earlier story working. Rerun `uv run pytest` at each checkpoint.

---

## Notes

- [P] tasks touch different files and have no dependencies on incomplete tasks.
- No task may add a tool or method that accepts a raw shell command string (FR-032). Reviewers should reject any such change.
- Every mutating path must go through `core/guard.run_action`, so that each attempt gets exactly one audit record.
- Commit after each task or logical group, and stop at any checkpoint to validate the story on its own.
