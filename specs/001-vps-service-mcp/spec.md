# Feature Specification: VPS Service Management MCP Server

**Feature Branch**: `001-vps-service-mcp`

**Created**: 2026-11-11

**Status**: Draft

**Input**: User description: "Develope an mcp server that manage different services on a vps"

## Clarifications

### Session 2026-11-11

- Q: Which service types are in scope? → A: Both operating-system services and containerized services.
- Q: Where does the server run? → A: Remotely, off the VPS, managing a single VPS over a secure remote connection.
- Q: Beyond control, what lifecycle operations are needed? → A: Full lifecycle: install/deploy, update, configure, and remove services.
- Q: How do interdependent containers (for example, Odoo and PostgreSQL) reach each other? → A: Through shared, named private networks declared on each container (FR-021a).

## User Scenarios & Testing *(mandatory)*

The primary actor is a **VPS operator** (a developer or administrator) who works through an AI assistant. The assistant connects to this server, and the server runs **off the VPS** (for example, on the operator's workstation or a management host). The server reaches **one target VPS** over a secure remote connection and acts on the operator's behalf. Two kinds of services are managed: **operating-system services**, which are supervised by the VPS's service manager, and **containerized services**. A secondary actor is the **AI assistant** itself, which discovers available capabilities and invokes them.

### User Story 1 - Check the health of services (Priority: P1)

The operator asks the assistant "what's running on my server?" or "is the web server up?". The assistant lists the services it is allowed to manage, both OS and container services, and reports each one's kind and state: running, stopped, failed, or restarting. It also reports how long each has been up and whether it starts automatically at boot.

**Why this priority**: Read-only visibility is the foundation for every other action and carries no risk of breaking anything. On its own it already lets an operator diagnose problems without opening a terminal.

**Independent Test**: Connect the server to a test VPS that has a known mix of OS and container services, some running and some stopped. Ask for a status overview and confirm that every reported state matches the actual state on the VPS.

**Acceptance Scenarios**:

1. **Given** the server is connected and three managed services exist (two OS services and one container; two running and one stopped), **When** the operator asks for a status overview, **Then** all three are listed with their kind, correct state, uptime for the running ones, and boot-start setting.
2. **Given** a managed service has crashed, **When** the operator asks for its status, **Then** it is reported as failed, along with the time of failure and its most recent exit reason.
3. **Given** the operator asks about a service that is not managed, **When** the request is processed, **Then** the operator is told the service is unknown or not permitted. Nothing is revealed about services outside the managed set.
4. **Given** the VPS is unreachable, **When** the operator asks for status, **Then** the operator is told the VPS cannot be reached and why (for example: timeout, authentication rejected, or host unknown). No stale state is presented as current.

---

### User Story 2 - Control the lifecycle of a service (Priority: P1)

The operator tells the assistant to start, stop, or restart a service, for example "restart nginx" or "stop the worker container". The action runs on the VPS and the resulting state is reported back.

**Why this priority**: Controlling services is the core of the "manage services" request. Together with Story 1, it makes up the minimum useful product.

**Independent Test**: On a test VPS, ask the assistant to stop, start, and restart one OS service and one container. Verify each state transition on the VPS and in the reported result.

**Acceptance Scenarios**:

1. **Given** a managed service is running, **When** the operator requests a restart, **Then** the service is restarted and the reported result shows it running with a new start time.
2. **Given** a managed service is stopped, **When** the operator requests a start and the service fails to come up, **Then** the result reports failure with the reason and recent log lines. It never claims success.
3. **Given** a service is marked as protected, **When** a stop is requested without explicit confirmation, **Then** the action is refused and the operator is told that confirmation is required.
4. **Given** the server is running in read-only mode, **When** any state-changing action is requested, **Then** the action is refused with a clear explanation.
5. **Given** the remote connection drops during an action, **When** the result is reported, **Then** the outcome is reported as "unknown — connection lost". The server re-checks the service state once the connection returns and reports what it finds.

---

### User Story 3 - Read service logs (Priority: P2)

The operator asks "why did the API crash?" and the assistant retrieves recent log output for that service, whether it is an OS service or a container. The operator can filter by time range, line count, or a search term.

**Why this priority**: Logs are the most common next step after spotting a failed status, but status and control already deliver value without them.

**Independent Test**: Generate known log lines from one managed OS service and one container. Request logs with a line limit, a time window, and a keyword, and confirm the results match.

**Acceptance Scenarios**:

1. **Given** a managed service has produced output, **When** the operator requests the last 100 lines, **Then** at most 100 lines are returned, newest last, with timestamps.
2. **Given** the logs contain thousands of entries, **When** the operator requests logs without a limit, **Then** a default cap is applied and the response says it was truncated.
3. **Given** the operator searches for a keyword, **When** results are returned, **Then** only matching lines from that service are included.

---

### User Story 4 - Deploy a new service (Priority: P2)

The operator says "deploy Redis as a container" or "install and run the monitoring agent". The assistant installs or deploys the service on the VPS from an operator-approved source. It registers the service in the managed set, starts it if requested, and confirms that it is running.

**Why this priority**: Deploying new services moves the tool from "operating what exists" to "managing the VPS end to end", which the operator asked for. It is riskier than control, so it comes after the P1 foundation.

**Independent Test**: On a clean test VPS, deploy one container service and one OS-level service from approved sources. Confirm that each one is installed, running, appears in the status overview, and is recorded in the audit trail.

**Acceptance Scenarios**:

1. **Given** an approved source contains the requested service, **When** the operator requests deployment with confirmation, **Then** the service is installed, added to the managed set, started, and reported as running.
2. **Given** the requested source is not on the approved list, **When** deployment is requested, **Then** it is refused and nothing is installed.
3. **Given** a deployment fails part-way (for example, a download fails or the disk is full), **When** the result is reported, **Then** the failure reason is given, any partial artifacts are cleaned up, and the VPS is left as it was before the attempt.
4. **Given** a service with the same name already exists, **When** deployment is requested, **Then** it is refused with a conflict message, not overwritten.
5. **Given** a database container and an application container are deployed on the same private network (for example, Odoo and PostgreSQL on `odoo-net`), **When** the application starts, **Then** it reaches the database by its service name, and the database port is not published on the VPS.

---

### User Story 5 - Update or reconfigure a service (Priority: P2)

The operator asks to "update the API to the latest version" or "change the worker's concurrency to 8". The assistant shows what will change. Once the operator confirms, it applies the change. It keeps the previous version and configuration so the change can be rolled back.

**Why this priority**: Updates and configuration changes are frequent day-2 tasks. Because they can break running services, they need confirmation and rollback.

**Independent Test**: Change one configuration setting on a managed service and update another service to a newer version. Verify that each change takes effect. Then trigger a rollback and verify that the previous state is restored.

**Acceptance Scenarios**:

1. **Given** a managed service, **When** the operator requests to view its configuration, **Then** the current configuration is returned. Values the operator has marked as secret are masked.
2. **Given** a proposed configuration change, **When** the operator submits it without confirmation, **Then** a preview of the differences is returned and nothing is applied.
3. **Given** a confirmed configuration change or version update, **When** it is applied and the service fails to become healthy within the timeout, **Then** the previous configuration or version is automatically restored and the operator is told that a rollback occurred.
4. **Given** a previous change succeeded, **When** the operator requests a rollback, **Then** the immediately prior version or configuration is restored and the service is restarted.

---

### User Story 6 - Remove a service (Priority: P3)

The operator says "remove the old staging container". The assistant stops the service, uninstalls it, and removes it from the managed set. The operator chooses whether its data is kept or deleted.

**Why this priority**: Removal completes the lifecycle but happens rarely. It is also the most destructive operation, so it needs the strongest safeguards.

**Independent Test**: Deploy a throwaway service and remove it once with data kept and once with data deleted. Verify the outcome on the VPS each time.

**Acceptance Scenarios**:

1. **Given** a managed, non-protected service, **When** removal is requested with confirmation, **Then** the service is stopped, uninstalled, and removed from the managed set, and its data is kept by default.
2. **Given** removal is requested with the "delete data" option, **When** it is confirmed, **Then** the service's data is also deleted, and the audit record notes that data was deleted.
3. **Given** a service is marked as protected, **When** removal is requested, **Then** it is refused, even with confirmation, until the operator removes the protected flag in the server's configuration.

---

### User Story 7 - View server resource usage (Priority: P3)

The operator asks "is the server overloaded?" and gets a summary of overall CPU, memory, and disk usage, plus usage for each managed service where it is available.

**Why this priority**: This helps explain service problems and check capacity before a deployment, but it supports the core workflow rather than being part of it.

**Independent Test**: Request a resource summary and compare it with figures taken directly on the VPS at the same moment. They should agree within a small tolerance.

**Acceptance Scenarios**:

1. **Given** the server is connected, **When** the operator requests a resource summary, **Then** overall CPU load, memory used/total, and disk used/total for each mounted volume are returned.
2. **Given** a managed service is running, **When** its details are requested, **Then** its own memory and CPU consumption are included where the platform exposes them.

---

### User Story 8 - Review an audit trail of actions (Priority: P3)

The operator reviews which state-changing actions the assistant has performed, when it performed them, and with what outcome.

**Why this priority**: An audit trail is important for trust and troubleshooting when an AI acts on production infrastructure. However, the trail must already be recorded by Stories 2 and 4–6. This story is only about making it reviewable.

**Independent Test**: Perform several actions of different kinds, including a refused one and a rolled-back one. Then request the audit trail and confirm that every attempt is listed in order with its outcome.

**Acceptance Scenarios**:

1. **Given** actions have been performed, **When** the audit trail is requested, **Then** each entry shows the time, the action, the target service, the outcome, and any error.
2. **Given** an action was refused by policy or rolled back, **When** the audit trail is reviewed, **Then** that outcome is also recorded.

---

### Edge Cases

- A service name is ambiguous or contains unexpected characters. The request is rejected; the name is never passed through unvalidated.
- An OS service and a container share the same name. Both are listed with their kind, and requests must specify the kind to disambiguate.
- Two state-changing requests target the same service at the same moment. They run one after the other, and each reports its own true outcome.
- A start, restart, deployment, or update hangs. It is abandoned after a bounded timeout, and the result reports "timed out" along with the last known state.
- The remote connection fails authentication, the VPS's identity no longer matches the one on record, or the host is unreachable. The server refuses to proceed and reports the specific reason. If the VPS's identity has changed, the server never silently accepts the new one.
- The connection drops in the middle of an action. The outcome is reported as unknown and the state is re-checked when the connection returns.
- The account the server uses on the VPS lacks permission for an action. The operator gets a clear permission error, not a generic failure.
- The VPS is under heavy load or the disk is full. Queries still respond, or fail with an explicit reason, within the timeout. Deployments are refused before they start if free disk space is below the configured minimum.
- The operator tries to stop, disable, or remove the VPS remote-access service that the server depends on. The request is refused to prevent lock-out.
- An update succeeds but a later rollback is requested after a second change has been made. Only one-step rollback to the immediately prior revision is guaranteed; see Assumptions.
- Log or configuration output contains secrets. Configuration values the operator has marked secret are masked. Log lines are returned as-is; see Assumptions.
- A service is defined in configuration but no longer exists on the VPS because it was removed out-of-band. It is reported as "not found", not as stopped.

## Requirements *(mandatory)*

### Functional Requirements

**Connection & discovery**

- **FR-001**: The system MUST expose its capabilities in a form that a connected AI assistant can discover, with a plain-language description of each capability and its inputs.
- **FR-002**: The system MUST run off the VPS and manage exactly one target VPS over an authenticated, encrypted remote connection, using credentials supplied by the operator.
- **FR-003**: The system MUST verify the target VPS's identity on every connection and refuse to proceed if it does not match the identity on record.
- **FR-004**: The system MUST report connectivity failures with a specific cause: unreachable, authentication rejected, identity mismatch, or timed out.

**Status**

- **FR-005**: The system MUST manage both operating-system services and containerized services, and MUST show each service's kind in every response.
- **FR-006**: The system MUST list all services in the managed set. For each one it MUST give the name, kind, current state (running / stopped / failed / transitioning / not found), whether it starts at boot, and the uptime when it is running.
- **FR-007**: The system MUST return detailed status for a single named service, including the last state change time and, for a failed service, the most recent exit reason.

**Lifecycle control**

- **FR-008**: The system MUST allow starting, stopping, and restarting a managed service of either kind.
- **FR-009**: The system MUST allow enabling or disabling whether a managed service starts at boot.
- **FR-010**: After every state-changing action, the system MUST report the resulting state as verified after the action, not just whether the command was accepted.
- **FR-011**: State-changing actions MUST be bounded by a timeout that the operator can configure. The default is 60 seconds for control actions and 10 minutes for deployments and updates. When the timeout is reached, the result MUST say so explicitly.
- **FR-012**: Concurrent state-changing actions on the same service MUST be run one at a time.
- **FR-013**: If the connection is lost during an action, the system MUST report the outcome as unknown and re-check the service state once the connection returns.

**Deployment, update, configuration, removal**

- **FR-014**: The system MUST allow deploying a new service, either an OS-level package or a container, from a source on an operator-approved list. Deployments from any other source MUST be refused.
- **FR-015**: A successfully deployed service MUST be added to the managed set automatically. A failed deployment MUST clean up its partial artifacts.
- **FR-016**: The system MUST refuse a deployment whose name conflicts with an existing service, and MUST refuse to start a deployment when free disk space is below a configurable minimum (default 1 GB).
- **FR-017**: The system MUST allow viewing a managed service's configuration. Values the operator has designated as secret MUST be masked.
- **FR-018**: The system MUST allow changing a managed service's configuration and updating it to a different version. When a change is submitted without confirmation, the system MUST return a preview of the differences and apply nothing.
- **FR-019**: Before applying any configuration change or update, the system MUST keep the prior revision. If the service does not become healthy within the timeout after the change, the system MUST automatically restore the prior revision and report that it rolled back.
- **FR-020**: The operator MUST be able to request a manual rollback to the immediately prior revision of a service.
- **FR-021a**: The system MUST let containerized services be attached to one or more named private networks so they can reach each other by service name (for example, an application reaching its database) without publishing the database's ports on the VPS. Networks the system creates MUST be removed when no managed service uses them any more.
- **FR-021**: The system MUST allow removing a managed service: stopping it, uninstalling it, and removing it from the managed set. Its data MUST be kept unless deletion is explicitly requested.

**Logs & resources**

- **FR-022**: The system MUST return recent log output for a managed service of either kind. The request can be filtered by line count, time range, and keyword. A default cap of 200 lines applies, and the response MUST say when output was truncated.
- **FR-023**: The system MUST report overall server CPU load, memory usage, and disk usage per volume.
- **FR-024**: The system SHOULD report CPU and memory usage for each managed service where the platform makes it available.

**Safety & access control**

- **FR-025**: Control, configuration, and removal actions MUST apply only to services in the managed set, which starts as an operator-defined allowlist and grows through FR-015. Requests for any other service MUST be refused without revealing whether that service exists.
- **FR-026**: The system MUST support a read-only mode in which every state-changing action is refused.
- **FR-027**: Deploying, updating, changing configuration, deleting data, and removing a service MUST each require an explicit confirmation in the request.
- **FR-028**: The operator MUST be able to mark individual services as protected. Stopping, restarting, or disabling a protected service MUST require confirmation. Removing a protected service MUST be refused outright.
- **FR-029**: The system MUST refuse to stop, disable, reconfigure, or remove the VPS remote-access service that it depends on.
- **FR-030**: All service identifiers, versions, source references, configuration keys, and filter inputs MUST be validated against a strict format before use. Malformed input MUST be rejected.
- **FR-031**: The system MUST only accept requests from the assistant session(s) the operator has authorized.
- **FR-032**: The system MUST NOT offer arbitrary command execution on the VPS. Every operation MUST be one of the defined capabilities above.
- **FR-033**: VPS credentials MUST never appear in responses, logs, or audit records.

**Auditing & errors**

- **FR-034**: Every state-changing attempt MUST be recorded in an audit trail. This includes successful, failed, refused, timed-out, unknown, and rolled-back attempts. Each record holds the timestamp, the requesting session, the action, the target, any confirmation given, the outcome, and any error message.
- **FR-035**: The audit trail MUST be retrievable through the server, with the most recent entries first and an optional limit.
- **FR-036**: Errors MUST be reported to the assistant as clear, categorized messages: not permitted, not found, permission denied, connection failure, timed out, invalid input, conflict, confirmation required, or failed with reason. They must be specific enough for the assistant to explain the problem to the operator.

### Key Entities

- **Target VPS**: The single remote machine being managed. Attributes: address, recorded identity, connection credentials reference, connection status.
- **Managed Service**: A long-running process on the VPS in the managed set. Attributes: name, kind (OS service / container), description, current state, boot-start setting, protected flag, uptime, last exit reason, current version, current configuration revision.
- **Service Network**: A named private network on the VPS that containerized services can share. Attributes: name, whether the system created it, attached services.
- **Approved Source**: An operator-approved place that services may be deployed from, such as a package repository or container registry. Attributes: name, kind, location.
- **Service Revision**: A snapshot of a service's version and configuration, taken before each change. Used for previews and rollback. Attributes: service, version, configuration, timestamp, status (active / previous / rolled back).
- **Service Policy / Configuration**: Operator-defined settings that govern the server. Attributes: initial allowlist, protected services, read-only flag, approved sources, secret configuration keys, timeouts, log line cap, minimum free disk, authorized sessions.
- **Action Request**: An attempt by the assistant to change state. Attributes: action type, target service, parameters, confirmation flag, requesting session, timestamp.
- **Audit Record**: The persisted outcome of an Action Request. Attributes: request details, outcome category, resulting state, rollback indicator, error message, duration.
- **Resource Snapshot**: A point-in-time reading of server-wide resource usage, with usage for each service where available.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: An operator can find out the state of any managed service through the assistant in under 10 seconds, without opening a terminal on the VPS.
- **SC-002**: An operator can restart a misbehaving service and see a confirmed result in under 30 seconds for services that normally start within 10 seconds.
- **SC-003**: 100% of the states reported in a status overview match the actual state on the VPS at the time of the query, verified across at least 20 test scenarios that cover both service kinds.
- **SC-004**: An operator can deploy a common service (for example, a cache or a database) from an approved source and see it running in under 5 minutes, excluding download time over slow links.
- **SC-005**: In 100% of test cases where a change makes a service unhealthy, the service is restored to its prior working revision without operator intervention.
- **SC-006**: 0 actions are executed against services outside the managed set, from unapproved sources, or as destructive operations without confirmation, across a negative test suite of at least 50 malformed or disallowed requests.
- **SC-007**: 100% of state-changing attempts appear in the audit trail with the correct outcome, and 0 audit records or responses contain VPS credentials.
- **SC-008**: Status and log queries return a result or an explicit error within 15 seconds, even when the VPS is under high load or unreachable.
- **SC-009**: In operator testing, at least 90% of common service-management tasks (check status, restart, read logs, deploy, change a setting) are completed successfully through the assistant on the first attempt.

## Assumptions

- The operator has administrative access to the VPS and provides the server with credentials for an account that has enough privileges to manage the services in scope.
- The VPS runs a mainstream Linux distribution with a standard service manager and a standard container runtime already installed. Installing the container runtime itself is out of scope.
- One operator, or a small trusted team, uses the server. Multi-tenant role hierarchies are out of scope for v1. Every authorized session has the same permissions, limited by policy, protected flags, and confirmations.
- Managing multiple VPSs from one server is out of scope for v1. The design should not prevent it later.
- Rollback is guaranteed only to the immediately prior revision. Up to 5 revisions per service are kept for reference.
- Data deletion on removal is permanent. Backups of service data are out of scope.
- Log lines are returned as stored. Automatic secret redaction in logs is out of scope for v1. Only configuration values the operator marks as secret are masked.
- Audit records are stored where the server runs, not on the VPS, and are kept for at least 90 days by default. The operator can configure this.
- Interdependent services (for example, an application and its database) are deployed as separate managed services that share a private network (FR-021a). Orchestrating them as one unit (a "stack") is out of scope for v1. Multi-service orchestration, alerting, scheduled jobs, metrics history, and a graphical dashboard are out of scope for v1.
- Firewall, user account, and OS update management are out of scope for v1.
