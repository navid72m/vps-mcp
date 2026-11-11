# Research: VPS Service Management MCP Server

**Feature**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md) | **Date**: 2026-11-11

Every Technical Context unknown is resolved below as a Decision, its Rationale, and the Alternatives considered.

---

## R1. Language and MCP SDK

**Decision**: Python 3.12+ with the official MCP Python SDK (`mcp` package, `FastMCP` high-level API).

**Rationale**:
- The official SDK generates tool input schemas from type hints and Pydantic models, which gives FR-001 (discoverability) and FR-030 (strict validation) almost for free.
- Python has a mature async SSH library (R3) and is the language most operators can read when they audit a tool that has root-equivalent power over their VPS.
- The project is already set up with Python-based speckit tooling.

**Alternatives considered**:
- *TypeScript SDK*: equally capable. However, its SSH libraries are less mature for strict host-key handling, and it brings a Node toolchain onto the operator's workstation.
- *Go* (community SDK): produces a single static binary, but the MCP SDK ecosystem is younger and iteration on schema design is slower.

## R2. MCP transport and client authorization (FR-031)

**Decision**: Use the **stdio** transport. The MCP client (for example Claude Code or Claude Desktop) launches the server as a local child process on the operator's workstation.

**Rationale**:
- With stdio, only the process that spawned the server can talk to it. No network port is opened, so there is no listener to secure, and "authorized session" means "the client the operator configured to launch it".
- This matches Q2 (the server runs off the VPS, typically on the operator's own machine).

**Alternatives considered**:
- *Streamable HTTP with OAuth/bearer tokens*: needed only if the server runs on a shared management host. This is deferred and noted as a future extension. The tool layer is transport-agnostic, so adding it later does not change the contracts.

## R3. Remote connection to the VPS (FR-002–FR-004, FR-013, FR-033)

**Decision**: Use SSH via **asyncssh**:
- **Authentication**: key-based only. Use a key file path from config or the operator's running ssh-agent. Password authentication is disabled.
- **Host verification**: strict pinning against a `known_hosts` file (FR-003). An unknown or changed host key causes a hard failure with error category `connection_failure/identity_mismatch`. The server never uses trust-on-first-use automatically. The operator pins the key with an explicit `vps-mcp trust-host` CLI step.
- **Connection lifetime**: one long-lived connection, reconnected lazily, with a keepalive of 15 s. Failures are classified as `unreachable`, `auth_rejected`, `identity_mismatch`, or `timeout`.
- **Credential hygiene**: the key passphrase and key material never enter log records, audit records, or tool output. A redaction filter on the logger is a second line of defence.

**Rationale**: SSH is already present on every VPS. It is encrypted, it authenticates both ends, and it requires nothing extra installed on the VPS, which satisfies the spec's "nothing installed beyond remote access". asyncssh fits the async MCP server and supports per-command timeouts.

**Alternatives considered**:
- *Paramiko*: synchronous only, which would require thread offloading.
- *Shelling out to the system `ssh` binary*: simpler, but error classification is fragile (text parsing of stderr) and per-command control is weaker.
- *Agent installed on the VPS*: rejected because Q2 (remote) means nothing extra is installed.

## R4. Privilege model on the VPS (FR-018, FR-032)

**Decision**: Connect as a dedicated non-root user (`vpsmcp`) that may run only a fixed list of binaries through `sudo -n`, granted by a generated sudoers drop-in (`/etc/sudoers.d/vps-mcp`). The binaries are `systemctl`, `journalctl`, `apt-get`/`dnf`, `docker`/`podman`, and `install` (for config file writes).

- The server builds every remote command from **fixed templates** inside the codebase. Arguments are validated against strict regexes (R10) and then shell-quoted. No tool accepts a raw command string (FR-032).
- `vps-mcp bootstrap --print-sudoers` prints the recommended sudoers file for the operator to review and install.
- `DEBIAN_FRONTEND=noninteractive` is passed as a validated environment prefix (`DEBIAN_FRONTEND=… sudo -n -- apt-get …`), kept through sudo by `Defaults:vpsmcp env_keep`. The `env` binary is deliberately **not** in sudoers, because allowing it would allow running anything.
- Container bind mounts of `/`, `/etc`, `/root`, `/run`, the docker socket and similar host paths are rejected, because they would hand a container root on the VPS.

**Rationale**: This is least privilege within the limits of what the operations inherently need (installing packages needs root). It also makes the server's attack surface auditable.

**Alternatives considered**:
- *Connect as root*: the simplest option, but it violates least privilege. Still allowed if the operator sets `allow_root = true`, with a warning at startup.
- *polkit rules*: cover systemd only, not package managers or containers.

## R5. OS service backend (FR-005–FR-009, FR-022, FR-024)

**Decision**: Use the **systemd** backend:
- **Status**: `systemctl show <unit> --property=ActiveState,SubState,UnitFileState,ActiveEnterTimestamp,ExecMainStatus,Result,StateChangeTimestamp,MemoryCurrent,CPUUsageNSec`. The output is machine-parseable key=value, so there is no text scraping.
- **Control**: `systemctl start|stop|restart|enable|disable <unit>`, each followed by a verification poll of `show` (FR-010).
- **Logs**: `journalctl -u <unit> -o json --no-pager -n <N> [--since <ts>] [--until <ts>]`. Keyword filtering is done **server-side in Python** as a literal, case-insensitive substring match. This avoids depending on journald being built with PCRE2 and avoids regex injection.
- **Not found**: when `LoadState=not-found`, the unit is reported as `not_found` (spec edge case).

**Rationale**: systemd is the standard service manager on all mainstream distributions (Spec Assumption). `show` output is stable and designed for programs.

**Alternatives considered**: SysV/OpenRC support is out of scope for v1. The backend is behind an interface so it can be added later.

## R6. Container backend (FR-005, Q1)

**Decision**: Use the **Docker Engine CLI over SSH**. Podman is supported through the same commands when `container_runtime = "podman"`.
- **Ownership label**: every container the server manages carries the label `io.vps-mcp.managed=true` and `io.vps-mcp.service=<name>`.
- **Status**: `docker inspect --format '{{json .}}'`.
- **Control**: `docker start|stop|restart`.
- **Boot-start**: mapped to the restart policy. Enabled means `unless-stopped`; disabled means `no`.
- **Logs**: `docker logs --timestamps --tail N [--since] [--until]`.
- **Resources**: `docker stats --no-stream --format '{{json .}}'`.
- **Configuration model**: the declarative fields image reference, env, ports, volumes, networks, restart policy, command, and healthcheck. Applying a configuration recreates the container from that spec.

**Rationale**: Docker and Podman are the dominant runtimes on VPSs. Using the CLI over SSH avoids exposing the Docker socket over the network. Labels give an unambiguous ownership boundary.

**Alternatives considered**:
- *Docker Engine API over an SSH-tunnelled socket*: richer, but it effectively grants root and is harder to restrict with sudoers.
- *docker compose*: multi-service orchestration is out of scope for v1 (Spec Assumption).

## R6a. Container networking (FR-021a)

**Decision**: `ContainerSpec.networks` is a list of user-defined bridge networks. Each one is attached under its own name, so other containers on the same network reach the service by name (for example, Odoo reaches its database at `HOST=odoo-db`).
- **Validation**: network names match `^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$`. `host`, `none`, and `container:*` are rejected because they bypass isolation. `bridge` (the default network) is allowed but has no name resolution, so the server emits a warning.
- **Creation**: if a requested network is missing, deploy creates it with `<runtime> network create --driver bridge --label io.vps-mcp.managed=true <net>`. A network that already exists and is not labelled `io.vps-mcp.managed=true` may be joined but is never deleted.
- **Attachment**: `docker run --network <first>`, then `docker network connect <net> <name>` for each additional network. Podman supports the same commands.
- **Garbage collection**: after a container is removed, or recreated with fewer networks, each network that carries the managed label and has no remaining containers attached is removed (`network rm`). This happens whether or not `delete_data` is set, because networks hold no data.
- **Default**: an empty `networks` list means the runtime's default bridge network, which matches plain `docker run`.

**Rationale**: This is the standard Docker pattern for app-plus-database stacks. It keeps databases off public ports and avoids bringing in compose (orchestration stays out of scope for v1).

**Alternatives considered**:
- *Publishing the database port on the host*: rejected. It exposes the database on the VPS and depends on firewall configuration that this server does not manage.
- *Host networking*: rejected. There is no isolation, and port clashes are likely.
- *A single implicit network for all managed containers*: rejected. Unrelated apps would be able to reach each other.

## R7. Deployment, update, and removal (FR-014–FR-016, FR-021)

**Decision**:

| Kind | Deploy | Update | Remove (keep data) | Remove (delete data) |
|------|--------|--------|--------------------|----------------------|
| OS | `apt-get install -y <pkg>[=<ver>]` or `dnf install -y <pkg>[-<ver>]`, then `systemctl enable --now <unit>` if requested | install the target version | `apt-get remove` / `dnf remove` | `apt-get purge` and removal of the declared `data_paths` |
| Container | `docker pull <image>@<digest or tag>`, then `docker run -d` with labels and spec | pull the new image, then recreate | `docker rm -f` (named volumes kept), then garbage-collect empty managed networks | also `docker volume rm` for the service's declared volumes |

- **Approved sources (FR-014)**:
  - OS packages: the policy lists the allowed APT/DNF repository origins. Before installing, the server checks the candidate's origin with `apt-cache policy` / `dnf info` and refuses packages from unlisted repositories.
  - Containers: the policy lists allowed registry or image prefixes (for example `docker.io/library/`, `ghcr.io/myorg/`). Image references are matched by prefix after normalisation.
- **Disk pre-check (FR-016)**: run `df -P --output=avail` on `/` and on the container data root, and refuse below the threshold (default 1 GB).
- **Cleanup on failure (FR-015)**:
  - Containers: remove the created container and any image pulled in this attempt.
  - OS: `apt-get remove` of a package that this attempt freshly installed. Packages that were present before are never removed.
- **Protected-service removal** is refused outright (FR-028).

**Rationale**: These are the minimal native mechanisms, and they keep the VPS free of extra tooling.

**Alternatives considered**: Building or uploading arbitrary binaries or tarballs was rejected for v1 as too large an attack surface.

## R8. Configuration management and rollback (FR-017–FR-020)

**Decision**:
- **OS services**: the policy declares, per service, the editable config file paths (for example `/etc/nginx/nginx.conf`). Edits replace the whole content of a declared file only. Paths outside the declaration are refused.
  - **Validation**: run an optional per-service `validate_command` template from a **fixed built-in catalogue** (for example `nginx -t`, `sshd -t`). Operators cannot supply arbitrary strings.
  - **Write**: atomic, via `install -m <mode> -o <owner> tmp target`.
  - **Activation**: a reload or restart, as configured.
- **Containers**: the configuration is the declarative container spec (R6). A change produces a new spec revision, and the container is recreated.
- **Preview (FR-018)**: when `confirm` is false, return a unified diff of the old and new content or spec, with secret keys masked, and apply nothing.
- **Revisions (FR-019, FR-020)**: before applying, store the full prior content or spec plus the version in the local revision store (R9). Keep the last 5 revisions per service.
- **Health gate**: after applying, poll until the service is `active` (systemd) or `running`/`healthy` (container). For systemd it must stay that way for a stabilisation window (default 10 s), and the whole check is bounded by the timeout.
  - On failure, automatically restore the prior revision, reactivate the service, and report `outcome = rolled_back`.
  - A failed rollback is reported as `failed` with both errors.
- **Secret masking (FR-017)**: the policy lists `secret_keys` patterns (for example `*PASSWORD*`, `*TOKEN*`). Matching env vars or `key = value` lines are shown as `***` in views and diffs. Revisions store the real values locally (file mode 0600) so that rollback is faithful.

**Rationale**: This gives a declarative, bounded, reversible change model, and it keeps edits to declared files only.

**Alternatives considered**: Storing backups on the VPS (`*.bak` files) was rejected because the audit and revision data live off the VPS (Spec Assumption), and leftover .bak files clutter `/etc`.

## R9. Local storage (audit trail, revisions)

**Decision**: Use **SQLite** (stdlib `sqlite3`, WAL mode) at `~/.local/state/vps-mcp/<vps-name>.db`, with file mode 0600.
- Tables: `audit_records` and `service_revisions`. The managed-set additions from deployments go in `managed_services`.
- Retention: a purge on startup and once a day removes audit rows older than `audit_retention_days` (default 90).

**Rationale**: Zero operations overhead, transactional, and queryable for FR-035 (newest first, limit). Audit records are stored where the server runs (Spec Assumption).

**Alternatives considered**: JSON Lines files are simpler but awkward to purge and query and not crash-safe. A server database is overkill.

## R10. Input validation (FR-030)

**Decision**: Every tool input is a Pydantic model with constrained types:

| Input | Rule |
|-------|------|
| `service` name | `^[a-zA-Z0-9][a-zA-Z0-9_.@-]{0,127}$` |
| `kind` | enum: `os`, `container` |
| package version | `^[A-Za-z0-9.+:~_-]{1,64}$` |
| image reference | the OCI reference grammar subset `registry/path:tag` or `@sha256:<64 hex>`, max 255 characters |
| env / config keys | `^[A-Za-z_][A-Za-z0-9_]{0,127}$` |
| keyword filter | literal text, ≤ 200 characters, no control characters |
| time filters | ISO-8601 or relative forms (`15m`, `2h`, `1d`) |
| `lines` | 1–5000 |
| file paths | must exactly equal a path declared in the policy |

- Values pass these checks and are then still `shlex.quote`d when they are inserted into command templates (defence in depth).

**Rationale**: Validating at the boundary means one layer catches malformed input before any remote call happens.

## R11. Concurrency, timeouts, and connection loss (FR-011–FR-013)

**Decision**:
- **Locking**: one `asyncio.Lock` per (kind, service), so state-changing operations on the same service are serialized. Read-only operations do not take the lock.
- **Timeouts**: `asyncio.wait_for` wraps each operation. Defaults are control 60 s, deploy/update 600 s, query 15 s (SC-008). All are configurable.
- **Connection loss**:
  - If the SSH channel closes mid-action, the result is `outcome = unknown`.
  - A reconciliation task is registered. On the next successful connection it re-reads the service state, updates the audit record with `reconciled_state`, and includes that state in the next response that touches the service.

## R12. Testing strategy

**Decision**:
- **Unit tests** (pytest + pytest-asyncio): backends and policy run against a `FakeRemoteExecutor` that records commands and returns canned outputs. These cover validation, policy refusal, command construction (golden tests for exact argv), rollback logic, and masking.
- **Contract tests**: an in-process MCP client session (SDK memory transport). Each tool is checked for its exposed name, input JSON schema, output shape, and error categories against `contracts/mcp-tools.md`.
- **Negative suite (SC-006)**: at least 50 parametrised malformed or disallowed requests, each asserting that zero remote mutating commands were issued.
- **Integration tests** (opt-in, `-m integration`): run against a disposable Linux VM (for example Multipass or Lima on macOS, or a CI VM) with systemd and Docker, provisioned by `tests/integration/provision.sh`. They exercise every quickstart scenario end to end.

**Rationale**: Most of the logic is testable without a VPS. Real-host tests guard the places where command semantics matter.

## R13. Packaging and configuration format

**Decision**:
- **Packaging**: a `uv`-managed project that exposes a `vps-mcp` console script.
  - `vps-mcp serve --config <path>` runs the MCP server on stdio.
  - `vps-mcp check-config` validates the policy file.
  - `vps-mcp trust-host` pins the VPS host key.
  - `vps-mcp bootstrap --print-sudoers` prints the sudoers snippet.
- **Policy file**: a single TOML file, read with stdlib `tomllib` and validated by Pydantic. See `contracts/policy-config.md`.

**Rationale**: TOML is human-friendly and comments are allowed, which matters for a security policy. `uv` gives reproducible installs.
