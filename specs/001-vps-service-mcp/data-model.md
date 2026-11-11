# Data Model: VPS Service Management MCP Server

**Feature**: [spec.md](./spec.md) | **Research**: [research.md](./research.md)

There are three kinds of data:
- **Configuration**: read from the TOML policy file and never written by the server.
- **Live state**: read from the VPS on demand and never cached as truth.
- **Persisted local state**: kept in SQLite where the server runs.

---

## 1. Configuration entities (policy file)

Full schema and example: [contracts/policy-config.md](./contracts/policy-config.md).

### TargetVPS

| Field | Type | Rules |
|-------|------|-------|
| `name` | str | `^[a-z0-9-]{1,32}$`. Used to name the local database file. |
| `host` | str | A hostname or IP address. |
| `port` | int | 1–65535, default 22. |
| `user` | str | Default `vpsmcp`. If the value is `root`, `allow_root = true` must also be set. |
| `key_path` | path? | Optional. When absent, ssh-agent is used. Never echoed in output (FR-033). |
| `known_hosts_path` | path | Required. Must contain an entry for `host` (FR-003). |
| `container_runtime` | enum | `docker` \| `podman` \| `none` |
| `package_manager` | enum | `apt` \| `dnf` |

### Policy

| Field | Type | Default | Spec |
|-------|------|---------|------|
| `read_only` | bool | `false` | FR-026 |
| `timeouts.control_s` | int | 60 | FR-011 |
| `timeouts.deploy_s` | int | 600 | FR-011 |
| `timeouts.query_s` | int | 15 | SC-008 |
| `health_stabilize_s` | int | 10 | FR-019 |
| `log_default_lines` | int | 200 | FR-022 |
| `log_max_lines` | int | 5000 | FR-022 |
| `min_free_disk_mb` | int | 1024 | FR-016 |
| `audit_retention_days` | int | 90 | Assumption |
| `revisions_kept` | int | 5 | Assumption |
| `secret_keys` | list[glob] | `["*PASSWORD*","*SECRET*","*TOKEN*","*KEY*"]` | FR-017 |
| `approved_sources.apt_origins` / `dnf_repos` | list[str] | `[]` | FR-014 |
| `approved_sources.image_prefixes` | list[str] | `[]` | FR-014 |
| `remote_access_units` | list[str] | `["ssh","sshd"]` | FR-029. These units are always treated as untouchable. |

### ServiceDeclaration (the initial allowlist, FR-025)

| Field | Type | Rules |
|-------|------|-------|
| `name` | ServiceName | Unique together with `kind`. |
| `kind` | enum | `os` \| `container` |
| `unit` | str? | For `os` services. The systemd unit name; defaults to `<name>.service`. |
| `description` | str? | At most 200 characters. |
| `protected` | bool | Default `false` (FR-028). |
| `config_files` | list[ConfigFileDecl] | `os` services only (FR-017). |
| `validate` | enum? | A key into the built-in validator catalogue (`nginx`, `sshd`, `caddy`, `haproxy`, ...). |
| `activate` | enum | `reload` \| `restart`. Default `restart`. |
| `data_paths` | list[abs path] | Deleted only when `remove` is called with `delete_data` (FR-021). |
| `package` | str? | For `os` services. The package that provides the unit; needed for update and remove. |

**ConfigFileDecl**: `path` (absolute path), `owner`, `group`, `mode` (octal string).

---

## 2. Live-state views (read from the VPS, returned by tools)

### ServiceStatus

| Field | Type | Notes |
|-------|------|-------|
| `name`, `kind` | | Always present (FR-005). |
| `state` | enum | `running` \| `stopped` \| `failed` \| `transitioning` \| `not_found` |
| `boot_enabled` | bool? | `null` if `not_found`. |
| `uptime_s` | int? | Only when `running`. |
| `last_state_change` | datetime? | |
| `last_exit_reason` | str? | Set when `failed`: the exit code or signal and the result string. |
| `version` | str? | The package version or the image reference with its digest. |
| `protected` | bool | |
| `resources` | {cpu_pct?, mem_bytes?}? | FR-024, when available. |
| `pending_reconciliation` | bool | `true` if an earlier action ended with `unknown` (FR-013). |
| `networks` | list[str] | Containers only: the networks the container is currently attached to (FR-021a). |

**State mapping**:

| Backend | Source value | → state |
|---------|--------------|---------|
| systemd | `ActiveState=active` | running |
| systemd | `inactive` | stopped |
| systemd | `failed` | failed |
| systemd | `activating`, `deactivating`, `reloading` | transitioning |
| systemd | `LoadState=not-found` | not_found |
| container | `running` | running |
| container | `exited` with code 0 or `created` | stopped |
| container | `exited` with code ≠ 0 or `dead` | failed |
| container | `restarting`, `removing` | transitioning |
| container | absent | not_found |

### LogEntry

`timestamp` (datetime), `message` (str), `stream` (`stdout` \| `stderr` \| `journal`).

A **LogResult** also carries `truncated` (bool) and `returned` (int).

### ContainerSpec

This is the declarative configuration of a container service. It is used by deploy, configure, and get_config, and is stored in revisions.

| Field | Type | Rules |
|-------|------|-------|
| `image` | ImageRef | Must match an approved prefix (FR-014). |
| `env` | {ConfigKey: str} | Values whose key matches `secret_keys` are masked in output. |
| `ports` | list[str] | `hostPort:containerPort[/tcp\|udp]` |
| `volumes` | list[str] | `name_or_abs_path:container_path[:ro]` |
| `networks` | list[NetworkName] | Default `[]` (the runtime default bridge). `NetworkName` = `^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$`. `host`, `none`, and `container:*` are rejected (FR-021a). |
| `command` | list[str]? | |
| `restart_on_boot` | bool | Default `true` → restart policy `unless-stopped`. |
| `healthcheck` | {test: list[str], interval_s, retries}? | |

**Network lifecycle**:
- missing at deploy or configure → created with the label `io.vps-mcp.managed=true`
- already exists without the label → joined, never deleted
- managed label and zero attached containers after a remove or recreate → deleted

### ResourceSnapshot

`taken_at`, `load_avg` [1m, 5m, 15m], `cpu_count`, `mem_total_bytes`, `mem_used_bytes`, `volumes[]` {mount, total_bytes, used_bytes}, `services[]` {name, kind, cpu_pct?, mem_bytes?}.

---

## 3. Persisted local state (SQLite)

### managed_services
Services added by `deploy_service` (FR-015) and declared services that have been removed. The effective managed set is calculated as follows:

    effective managed set = declared services (policy)
                            ∪ rows with origin='deployed' and removed_at IS NULL
                            − declared services with removed_at set

| Column | Type | Notes |
|--------|------|-------|
| `name`, `kind` | text | Primary key (name, kind). |
| `origin` | text | `declared` \| `deployed` |
| `spec_json` | text | For deployed services: the unit/package or container spec. |
| `added_at` | datetime | |
| `removed_at` | datetime? | |

### service_revisions (FR-019, FR-020)

| Column | Type | Notes |
|--------|------|-------|
| `id` | integer | Primary key. |
| `service_name`, `kind` | text | Indexed. |
| `version` | text? | The package version or the image reference with its digest. |
| `content_json` | text | Config file contents keyed by path, or the container spec. Includes the real secret values; the file has mode 0600. |
| `status` | text | `active` \| `previous` \| `superseded` \| `rolled_back` |
| `created_at` | datetime | |
| `audit_id` | integer | References `audit_records.id`. |

Only the newest `revisions_kept` rows per service are kept.

**Revision state transitions**:

```text
apply change:      current active → previous ; new row → active ; older previous → superseded
auto-rollback:     new row → rolled_back ; previous → active
manual rollback:   active → rolled_back ; previous → active
```

### audit_records (FR-034, FR-035)

| Column | Type | Notes |
|--------|------|-------|
| `id` | integer | Primary key. |
| `ts` | datetime (UTC) | Indexed, sorted descending for reads. |
| `session_id` | text | The MCP session or client identifier. |
| `action` | enum | `start`, `stop`, `restart`, `enable`, `disable`, `deploy`, `update`, `configure`, `rollback`, `remove` |
| `service_name`, `kind` | text | |
| `params_json` | text | Validated inputs, with secrets masked and credentials never present. |
| `confirmed` | bool | |
| `outcome` | enum | `success` \| `failed` \| `refused` \| `timed_out` \| `unknown` \| `rolled_back` \| `preview` |
| `error_category` | text? | See the error categories below. |
| `error_message` | text? | Masked. |
| `resulting_state` | text? | |
| `reconciled_state` | text? | Filled in later for `unknown` outcomes (FR-013). |
| `data_deleted` | bool | For `remove` (Story 6). |
| `duration_ms` | integer | |

Rows older than `audit_retention_days` are purged.

---

## 4. Action lifecycle

Each state-changing action goes through the steps below. Every exit path writes exactly one audit record.

```text
received
  → validated? ── no → refused (invalid_input)
  → in managed set & not remote-access unit? ── no → refused (not_permitted)
  → read_only? ── yes → refused (read_only)
  → protected & remove? ── yes → refused (protected)      # checked before confirmation:
  → needs confirm & !confirm? ── yes → preview | refused   # no preview for the impossible
  → acquire per-service lock
  → execute (bounded by timeout)
       ├─ connection lost   → unknown (schedule reconciliation)
       ├─ timeout           → timed_out (report last known state)
       ├─ command error     → failed (cleanup / rollback if applicable)
       └─ ok → verify / health gate
                 ├─ healthy   → success
                 └─ unhealthy → rollback → rolled_back | failed
  → write audit record → release lock → respond
```

## 5. Error categories (FR-036)

`not_permitted`, `not_found`, `permission_denied`, `connection_failure` (with subtype `unreachable` \| `auth_rejected` \| `identity_mismatch` \| `timeout`), `timed_out`, `invalid_input`, `conflict`, `confirmation_required`, `read_only`, `protected`, `insufficient_disk`, `unapproved_source`, `failed`.
