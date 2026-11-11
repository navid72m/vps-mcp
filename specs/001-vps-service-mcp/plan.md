# Implementation Plan: VPS Service Management MCP Server

**Branch**: `001-vps-service-mcp` | **Date**: 2026-11-11 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/001-vps-service-mcp/spec.md`

## Summary

Build an MCP server that runs on the operator's workstation. An MCP client launches it over stdio. It manages **one remote VPS over SSH** and covers the full lifecycle of both **systemd services** and **Docker/Podman containers**: status, start/stop/restart, boot-start, logs, resource usage, deploy, update, configure with preview and automatic rollback, and removal.

Safety comes from these layers:
- **Fixed command templates**: there is no arbitrary execution, and every input is strictly validated.
- **A managed-set allowlist** that grows when services are deployed.
- **Protected services** and **confirmation flags**.
- **A read-only mode.**
- **A lock-out guard** for the SSH service itself.
- **Strict host-key pinning.**
- **A least-privilege sudoers profile** on the VPS.
- **A local SQLite audit trail and revision store.**

Technical decisions are recorded in [research.md](./research.md).

## Technical Context

**Language/Version**: Python 3.12+

**Primary Dependencies**:
- `mcp`: the official MCP Python SDK, using the FastMCP API.
- `asyncssh`: SSH client.
- `pydantic` v2: validation and schemas.
- Standard library: `tomllib` and `sqlite3`.

**Storage**: SQLite on the workstation (`~/.local/state/vps-mcp/<vps>.db`, mode 0600) for the audit trail, service revisions, and deployed-service registry. The TOML policy file is read-only.

**Testing**: pytest and pytest-asyncio. Unit tests run against a fake remote executor. Contract tests use the in-memory MCP client. Opt-in integration tests run against a disposable VM.

**Target Platform**: The server runs on macOS or Linux workstations. The managed VPS runs a Linux distribution with systemd, apt or dnf, and optionally Docker or Podman.

**Project Type**: A single-project Python package. It exposes an MCP server (stdio) and a small operator CLI (`serve`, `check-config`, `trust-host`, `bootstrap`).

**Performance Goals**:
- Status overview in under 10 s (SC-001).
- Restart confirmed in under 30 s (SC-002).
- Every query returns a result or an explicit error within 15 s (SC-008).

**Constraints**:
- No arbitrary command execution.
- No agent installed on the VPS.
- Credentials are never present in output, logs, or audit records.
- All mutating actions are serialized per service and bounded by timeouts.

**Scale/Scope**:
- 1 VPS, about 1–50 managed services, 1 operator or a small team.
- 16 MCP tools (7 read-only, 9 state-changing) (see [contracts/mcp-tools.md](./contracts/mcp-tools.md)).

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

`.specify/memory/constitution.md` is still the **unfilled template**: every principle is a placeholder, so no project-specific gates are defined. Nothing in the constitution blocks this plan.

**Status**: PASS, with no gates to evaluate.

In the absence of ratified principles, the design follows these engineering defaults, which the spec requires anyway:

| Default | How this plan meets it |
|---------|------------------------|
| Security / least privilege | No raw command tool. A sudoers allowlist. Strict host-key pinning. Credential redaction. |
| Testability | Every remote interaction goes through a `RemoteExecutor` interface, so the logic can be unit-tested without a VPS. Contract tests pin the tool surface. |
| Simplicity | A single package, stdio only, SQLite, no on-VPS agent, and no orchestration in v1. |
| Observability | Every mutation produces exactly one audit record. Errors are categorized. |

**Post-design re-check (after Phase 1)**: PASS. The design introduces no extra projects, services, or abstractions beyond the two backend implementations behind one interface, which the spec needs (Q1 = both kinds).

> Recommendation: run `/speckit-constitution` before `/speckit-tasks` so that later features have real gates to check against.

## Project Structure

### Documentation (this feature)

```text
specs/001-vps-service-mcp/
├── plan.md              # This file
├── research.md          # Phase 0: technical decisions R1–R13
├── data-model.md        # Phase 1: config, live-state, persisted entities, action lifecycle
├── quickstart.md        # Phase 1: setup + 18 validation scenarios
├── contracts/
│   ├── mcp-tools.md     # MCP tool surface, schemas, errors
│   └── policy-config.md # TOML policy/connection config
└── tasks.md             # Phase 2 (/speckit-tasks — not created here)
```

### Source Code (repository root)

```text
pyproject.toml                    # uv project, console script `vps-mcp`
examples/
└── vps-mcp.toml                  # annotated example policy

src/vps_mcp/
├── __main__.py
├── cli.py                        # serve | check-config | trust-host | bootstrap
├── server.py                     # FastMCP app, tool registration, annotations
├── errors.py                     # ErrorCategory enum, VpsMcpError → MCP error envelope
├── config/
│   ├── policy.py                 # pydantic models for the TOML policy (contracts/policy-config.md)
│   └── loader.py
├── models/
│   ├── inputs.py                 # constrained input types (ServiceName, ImageRef, ...)
│   └── views.py                  # ServiceStatus, LogEntry, ResourceSnapshot, ContainerSpec
├── remote/
│   ├── connection.py             # asyncssh connection, host-key pinning, failure classification
│   ├── executor.py               # RemoteExecutor protocol + SSH impl (argv templates, sudo -n, timeouts)
│   └── redact.py                 # credential/secret redaction filter
├── backends/
│   ├── base.py                   # ServiceBackend protocol
│   ├── systemd.py                # status/control/logs/config files/packages (apt|dnf)
│   ├── container.py              # docker|podman: inspect/control/logs/stats/run/recreate
│   └── validators.py             # built-in config validator catalogue (nginx -t, sshd -t, ...)
├── core/
│   ├── managed_set.py            # effective allowlist = declared ∪ deployed − removed
│   ├── guard.py                  # policy enforcement pipeline (data-model §4)
│   ├── locks.py                  # per-service asyncio locks
│   ├── actions.py                # control / boot-start
│   ├── deploy.py                 # deploy + cleanup, approved-source checks, disk pre-check
│   ├── changes.py                # configure / update / rollback + health gate
│   ├── removal.py
│   ├── queries.py                # status, logs, resources
│   └── reconcile.py              # unknown-outcome reconciliation
└── store/
    ├── db.py                     # sqlite init, migrations, retention purge
    ├── audit.py
    ├── revisions.py
    └── services.py               # managed_services table

tests/
├── conftest.py                   # FakeRemoteExecutor, temp DB, sample policy
├── unit/                         # validation, guard, backends (golden argv), changes/rollback, masking
├── contract/                     # in-memory MCP client vs contracts/mcp-tools.md
├── negative/                     # ≥50 disallowed/malformed requests → zero mutating commands (SC-006)
└── integration/                  # -m integration, real VM; provision.sh
```

**Structure Decision**: A single Python package (`src/vps_mcp`) using the src layout. `remote/` isolates every interaction with the VPS behind `RemoteExecutor`. `backends/` hides the OS-versus-container differences behind one `ServiceBackend` protocol. `core/` holds the policy-enforced use cases, which are independent of MCP. `server.py` is a thin adapter from MCP tools to `core/`. This layout lets user stories be built incrementally: US1 and US2 need only `remote`, `backends` (status and control), `core/guard`, `queries`, `actions`, and `store/audit`.

## Implementation Phasing (guidance for /speckit-tasks)

1. **Foundation**:
   - Config and policy loading.
   - Errors and input models.
   - The SSH connection with host-key pinning.
   - The executor.
   - The store, including audit.
   - The guard pipeline.
2. **MVP (US1 + US2, P1)**: list and status, control, and boot-start for both backends, plus audit writes.
3. **US3 Logs (P2)**: logs and keyword filtering.
4. **US4 Deploy (P2)**: approved sources, disk pre-check, cleanup, and managed-set growth.
5. **US5 Update/Configure (P2)**: revisions, diff preview, the health gate, and automatic or manual rollback.
6. **US6 Remove**, **US7 Resources**, and **US8 Audit query (P3)**.
7. **Hardening**:
   - The negative suite.
   - Reconciliation after connection loss.
   - The `bootstrap` sudoers generator.
   - Integration tests and the quickstart run-through.

## Complexity Tracking

No constitution violations to justify. The constitution has not been ratified yet.
