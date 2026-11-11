# Contract: MCP Tools

This is the interface that the server exposes to MCP clients over stdio. Contract tests assert the exact tool names, the input schemas, the output shapes, and the error categories listed here.

The types `ServiceStatus`, `LogEntry`, `ResourceSnapshot`, and the audit fields are defined in [data-model.md](../data-model.md).

## Conventions

- **Tool annotations**: every tool declares MCP tool annotations. Read-only tools set `readOnlyHint: true`. Mutating tools set `destructiveHint` as listed below, so that clients can prompt for consent.
- **Service reference**: every service-targeting tool takes `service: ServiceName` and an optional `kind: "os" | "container"`. If `kind` is omitted and the name exists for both kinds, the tool returns `invalid_input` with the message "ambiguous: specify kind".
- **Success envelope**: tools return structured content `{ "ok": true, ...result }`.
- **Error envelope**: errors return `isError: true` with structured content `{ "ok": false, "error": { "category": <ErrorCategory>, "subtype"?: str, "message": str, "last_known_state"?: ServiceStatus } }`.
- **Action envelope**: state-changing tools also return `audit_id` and `outcome`. The `outcome` values are `success`, `failed`, `timed_out`, `unknown`, `rolled_back`, and `preview`.
- **Credentials**: no tool output ever contains credentials (FR-033).

---

## Read-only tools

### `list_services`
- **In**: `{ kind?: "os"|"container" }`
- **Out**: `{ services: ServiceStatus[], vps: { name, reachable: true } }`
- **Errors**: `connection_failure`, `timed_out`
- **Spec**: FR-006, Story 1

### `get_service_status`
- **In**: `{ service, kind? }`
- **Out**: `{ service: ServiceStatus }`
- **Errors**: `not_permitted`, `invalid_input`, `connection_failure`, `timed_out`
- **Spec**: FR-007

A service that is in the managed set but missing on the VPS returns `ok` with `state: "not_found"`. A service outside the managed set returns `not_permitted`, and the response never reveals whether it exists.

### `get_service_logs`
- **In**: `{ service, kind?, lines?: 1..5000 = 200, since?: str, until?: str, keyword?: str(≤200) }`
- **Out**: `{ entries: LogEntry[], returned: int, truncated: bool }`. Entries are ordered oldest first, newest last.
- **Errors**: as for `get_service_status`.
- **Spec**: FR-022, Story 3

### `get_service_config`
- **In**: `{ service, kind? }`
- **Out**: `{ kind, files?: [{ path, content }], spec?: ContainerSpec, version, revision_id }`. Values that match secret keys are shown as `***`.
- **Spec**: FR-017

### `get_resource_usage`
- **In**: `{ include_services?: bool = true }`
- **Out**: `ResourceSnapshot`
- **Spec**: FR-023, FR-024, Story 7

### `get_audit_log`
- **In**: `{ limit?: 1..500 = 50, service?, action?, outcome? }`
- **Out**: `{ records: AuditRecord[] }`, newest first.
- **Spec**: FR-035, Story 8

### `list_revisions`
- **In**: `{ service, kind? }`
- **Out**: `{ revisions: [{ id, version, status, created_at }] }`
- **Spec**: FR-020

---

## State-changing tools

All of these are refused with `read_only` when the policy sets `read_only = true` (FR-026). Each call writes exactly one audit record.

### `start_service` / `stop_service` / `restart_service`
- **In**: `{ service, kind?, confirm?: bool = false }`
- **Out**: `{ outcome, service: ServiceStatus (verified post-action), audit_id }`
- **Confirmation**: for a protected service, `stop` and `restart` require `confirm: true`, otherwise the call fails with `confirmation_required`.
- **Remote access**: stopping a remote-access unit always fails with `not_permitted` (FR-029).
- **On failure to start**: the error includes the last 20 log lines (Story 2, scenario 2).
- **Annotations**: `destructiveHint` is `false` for `start` and `true` for `stop` and `restart`.
- **Spec**: FR-008, FR-010, FR-028

### `set_boot_start`
- **In**: `{ service, kind?, enabled: bool, confirm?: bool = false }`
- **Confirmation**: disabling a protected service requires `confirm`.
- **Spec**: FR-009

### `deploy_service`
- **In**:

  ```text
  {
    name: ServiceName,
    kind: "os" | "container",
    os?: { package: str, version?: str, unit?: str, enable_on_boot?: bool = true },
    container?: ContainerSpec,
    start?: bool = true,
    confirm: bool
  }
  ```

  - `ContainerSpec` = `{ image: ImageRef, env?: {KEY: str}, ports?: ["hostPort:containerPort[/proto]"], volumes?: ["name_or_abs_path:container_path[:ro]"], networks?: [NetworkName], command?: str[], restart_on_boot?: bool = true, healthcheck?: { test: str[], interval_s, retries } }`
  - `networks`: names of private networks to join (see [data-model.md §2 ContainerSpec](../data-model.md#containerspec)). A missing network is created and labelled as managed. `host`, `none`, and `container:*` fail with `invalid_input`.
- **Confirmation**: without `confirm: true` the call returns `outcome: "preview"` with a description of what would be installed, including the networks that will be created, and nothing is applied.
- **Errors**: `unapproved_source`, `conflict`, `insufficient_disk`, `timed_out`, `failed` (with a `cleaned_up: bool` field)
- **Out**: `{ outcome, service: ServiceStatus, audit_id }`
- **Spec**: FR-014–FR-016, Story 4

### `update_service`
- **In**: `{ service, kind?, version?: str, image?: ImageRef, confirm: bool }`
- **Preview**: without `confirm`, returns `{ outcome: "preview", from_version, to_version }`.
- **Applied**: returns `{ outcome: "success" | "rolled_back" | "failed", service, revision_id, audit_id }`.
- **Spec**: FR-018, FR-019

### `configure_service`
- **In**: `{ service, kind?, files?: [{ path, content }], spec_patch?: Partial<ContainerSpec>, confirm: bool }`
  - `files[].path` must be declared for that service.
  - `spec_patch` is merged into the current container spec. A key set to `null` removes it. `networks` is replaced as a whole list.
- **Preview**: without `confirm`, returns `{ outcome: "preview", diff: str (unified diff, secrets masked), validation?: { ok, output } }`.
- **Applied**: returns the same fields as `update_service`.
- **Spec**: FR-017–FR-019, Story 5

### `rollback_service`
- **In**: `{ service, kind?, confirm: bool }`
- **Behaviour**: restores the `previous` revision.
- **Errors**: `not_found` when there is no previous revision.
- **Spec**: FR-020

### `remove_service`
- **In**: `{ service, kind?, delete_data?: bool = false, confirm: bool }`
- **Refusals**:
  - Protected services always fail with `protected`.
  - Remote-access units always fail with `not_permitted`.
  - Without `confirm`, returns a preview that lists what will be removed, which data paths or volumes are kept or deleted, and which managed networks will be deleted because they become empty.
- **Out**: `{ outcome, data_deleted: bool, networks_removed: [str], audit_id }`
- **Annotations**: `destructiveHint: true`
- **Spec**: FR-021, FR-028, Story 6

---

## Error categories

| Category | Typical cause |
|----------|---------------|
| `invalid_input` | Fails schema or format validation, or an ambiguous kind. |
| `not_permitted` | Outside the managed set, or a remote-access unit. |
| `not_found` | No previous revision; unknown audit filter value. |
| `permission_denied` | sudo or the VPS refused the operation. |
| `connection_failure` | Subtypes `unreachable`, `auth_rejected`, `identity_mismatch`, `timeout`. |
| `timed_out` | The operation exceeded its timeout. Includes `last_known_state`. |
| `conflict` | A deploy name already exists. |
| `confirmation_required` | A protected or destructive action was requested without `confirm`. |
| `read_only` | The policy has `read_only = true`. |
| `protected` | Removal of a protected service. |
| `insufficient_disk` | Free disk is below `min_free_disk_mb`. |
| `unapproved_source` | The package origin or image prefix is not approved. |
| `failed` | The command failed. The message gives the reason and includes `cleaned_up` and `rolled_back` where relevant. |
