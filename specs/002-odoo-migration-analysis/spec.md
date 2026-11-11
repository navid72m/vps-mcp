# Feature Specification: Odoo Migration Analysis & Rehearsal

**Feature Branch**: `002-odoo-migration-analysis`

**Created**: 2026-11-11

**Status**: Draft

**Input**: User description: "Add a Database Migration capability to the existing vps-mcp server: a controlled interface for understanding database schemas, planning Odoo version-upgrade migrations, testing them, and detecting risky changes, supporting the user's existing Odoo 17 -> 19 upgrade pipeline. … The MCP server's role is ANALYZE AND TEST ONLY … It must NEVER apply migrations to production or non-copy databases, never run arbitrary SQL, and must respect the VPS disk constraint."

## Context

The operator upgrades a business-critical Odoo 17 database to Odoo 19. The work happens on the VPS that vps-mcp already manages, through existing shell pipelines.

**The pipelines**:
- The `deploy_v19.sh` family, including the `upgrade_v19.sh` variant, runs in this order:
  1. data cleanup;
  2. remove orphaned view customisations;
  3. upgrade all modules;
  4. re-upgrade modules still at 17.x, up to three rounds;
  5. start a test Odoo 19 instance.
- An OpenUpgrade-based pipeline is the second one.

**Where the data lives**:
- Upgrade dumps and their test databases (`shop_19_initial`, `shop_19_repaired`) live in the VPS's PostgreSQL 18 cluster.
- Production databases live elsewhere and are never reachable from this feature.

**Existing checkers**: the operator already has comparison and smoke-check tools (`compare_migration.py`, `post_upgrade_checks.py`, `deep_smoke_test.py`). Today these are run by hand, and their results are read from scattered log files.

**What this feature adds**: one controlled interface, through the existing vps-mcp assistant tools, for four things:
- **Understanding** a database's schema and Odoo module state.
- **Rehearsing** the pipeline's steps on a disposable copy.
- **Comparing** before and after.
- **Producing a risk report** that tells the operator whether a migration is safe to promote.

The assistant can inspect and rehearse, but it can never change a database that is not a disposable copy it created itself.

## Clarifications

### Session 2026-11-11

- Q: What kind of migration? → A: Odoo version upgrades (17 → 19) of the main business database.
- Q: The server's role in the pipeline? → A: Analyze and test only. The operator's pipeline applies the result to real databases.
- Q: Where does the pipeline run? → A: As a root script on the VPS. The databases under analysis are upgrade dumps and test copies; production lives elsewhere.

## User Scenarios & Testing *(mandatory)*

**Actors**:
- **Migration operator**: the developer who owns the Odoo upgrade and works through an AI assistant.
- **AI assistant**: discovers and calls the capabilities.
- **The existing pipeline**: a separate actor whose outputs (databases, logs) are inspected.

### User Story 1 - Understand a database before migrating (Priority: P1)

The operator asks things like "what's in shop_19_initial?" or "which modules are still at 17.x?". For any allowlisted database, the assistant reports:
- size;
- a table summary, with the largest tables and estimated row counts;
- the installed Odoo modules with their versions and states;
- known problem indicators: modules without code, modules stuck in "to upgrade", inactive or orphaned views.

**Why this priority**: Every migration decision starts with understanding the current state. Today this means ad-hoc database queries by hand. The capability is read-only, so it carries no risk.

**Independent Test**: Point it at a known test database and compare the reported module states, version counts and largest tables with the same facts gathered by hand.

**Acceptance Scenarios**:

1. **Given** an allowlisted database, **When** the operator requests its overview, **Then** size, table count, the 20 largest tables with estimated rows, and module counts by state and major version are returned within 30 seconds.
2. **Given** a database with modules still at 17.x, **When** the operator asks for module state, **Then** each such module is listed, with whether its code exists in the configured addons paths.
3. **Given** a database that is not allowlisted, **When** any analysis is requested, **Then** it is refused with "not permitted". Nothing reveals whether that database exists.
4. **Given** the operator asks for the detailed schema of one table, **When** it is allowlisted, **Then** its columns, types, nullability, defaults, indexes and foreign keys are returned.

---

### User Story 2 - Compare two databases and get a risk report (Priority: P1)

The operator asks "compare shop_17_dump with shop_19_initial". This could be a pre-upgrade dump against a post-upgrade result, or two pipeline variants. The assistant returns a structured comparison and a risk report: every finding is classified as blocking, warning or info, with a plain-language explanation and the affected objects. The report ends with an overall verdict: "safe to promote", "review warnings", or "do not promote".

**Why this priority**: Detecting risky changes is the core value the operator asked for. The databases to compare already exist on the VPS, so this works without any rehearsal capability.

**Independent Test**: Prepare two small databases with known differences: a dropped column, a narrowed type, a 40% row loss in one table, a module left at 17.x. Every planted difference should appear with the expected severity.

**Acceptance Scenarios**:

1. **Given** two allowlisted databases, **When** a comparison is requested, **Then** the report lists:
   - tables and columns added or removed;
   - column type changes, flagged as narrowing or widening;
   - constraints and indexes added or removed;
   - row-count changes per table;
   - modules installed, removed, upgraded or not upgraded;
   - views deactivated or removed.
2. **Given** a column or table holding data disappears, **When** the risk report is produced, **Then** the finding is **blocking**, and states how many rows of data are affected.
3. **Given** a table's row count drops by more than the configured threshold (default 5%), **When** the report is produced, **Then** the finding is a **warning** at minimum, or **blocking** when the drop exceeds the configured blocking threshold (default 20%).
4. **Given** modules remain at the old major version, or are left in an inconsistent state ("to upgrade", "to install", "to remove"), **When** the report is produced, **Then** each is a **blocking** finding.
5. **Given** the same comparison is requested twice on unchanged databases, **When** both reports are produced, **Then** their findings are identical, so reports are reproducible.
6. **Given** a comparison report was produced, **When** the operator asks for it later, **Then** it can be retrieved by its identifier without recomputing.

---

### User Story 3 - Rehearse the pipeline on a disposable copy (Priority: P2)

The operator asks "rehearse deploy_v19 on a copy of shop_17_dump". The assistant shows a plan first:
- the source;
- the name of the copy;
- the steps that will run;
- estimated disk use against free space.

After the operator confirms, the system creates a disposable copy and runs the operator-declared pipeline steps against **that copy only**, one by one. It records each step's outcome, duration and log excerpt, and stops at the first failure. When it finishes, a before/after comparison and risk report (Story 2) are produced automatically.

**Why this priority**: A rehearsal turns "will this migration work?" into evidence. It changes data on the VPS and needs disk space, so it builds on the read-only stories.

**Independent Test**: Rehearse a declared two-step pipeline on a small source database, where one step is designed to fail. Confirm three things: the copy received the changes, the source is byte-for-byte unchanged, and the failure is reported with its log excerpt.

**Acceptance Scenarios**:

1. **Given** a rehearsal request without confirmation, **When** it is processed, **Then** a plan is returned (source, copy name, ordered steps, estimated size, free disk before and after) and nothing is created.
2. **Given** a confirmed rehearsal and enough free disk, **When** it runs, **Then** a copy is created, each declared step runs against the copy in order, and a per-step result (success or failure, duration, last log lines) is reported.
3. **Given** a step fails, **When** the rehearsal stops, **Then** the remaining steps are marked "not run", the copy is kept for inspection (subject to retention), and the failure appears in the report as blocking.
4. **Given** the estimated copy size would leave less free disk than the configured minimum, **When** the rehearsal is requested, **Then** it is refused before anything is created.
5. **Given** a rehearsal is running, **When** a second rehearsal is requested for the same source, **Then** it is refused with "conflict". Rehearsals of different sources may run one after another, but never at the same time.
6. **Given** a declared step would target any database other than the rehearsal copy, **When** the step is prepared, **Then** the rehearsal refuses to run it.

---

### User Story 4 - Inspect results of the operator's own pipeline runs (Priority: P2)

The operator ran the pipeline by hand, for example `upgrade_v19.sh`, and asks "how did the last run go?". The assistant reads that pipeline's declared progress and log files and summarises:
- which steps completed;
- the exit status of each step;
- error and warning counts, with the most frequent error messages;
- modules left un-upgraded.

**Why this priority**: The operator's real pipeline runs outside the server. Making their results readable and comparable closes the loop without taking over the pipeline.

**Independent Test**: Feed a known progress file and log of a finished run, including one failed step and repeated errors. The summary should match: steps, statuses, top errors.

**Acceptance Scenarios**:

1. **Given** a declared pipeline with a progress file and log locations, **When** the operator asks for its last run, **Then** step names, statuses and timestamps are returned, together with the top 10 distinct error messages and their counts.
2. **Given** log files larger than the response limit, **When** they are summarised, **Then** the summary says the output was truncated and offers to fetch filtered log excerpts.

---

### User Story 5 - Run the operator's existing checks against a database (Priority: P3)

The operator asks "run the post-upgrade checks on shop_19_initial". The assistant runs the operator-declared, read-only check tools, such as the existing post-upgrade checks or smoke tests, against an allowlisted database, and folds their results into the risk report.

**Why this priority**: It reuses checks the operator already trusts. That adds value, but it isn't required for the core loop.

**Independent Test**: Declare one check that passes and one that fails, and run both. Both results should appear in the report, with the failing check marked as a finding.

**Acceptance Scenarios**:

1. **Given** a declared check and an allowlisted database, **When** it is run, **Then** its exit status, duration and output excerpt are recorded and shown.
2. **Given** a check declared as "read-only" is run against a non-copy database, **When** it finishes, **Then** it has run with access that cannot modify data.
3. **Given** a check is declared as "may modify data", **When** it is requested against anything but a rehearsal copy, **Then** it is refused.

---

### User Story 6 - Clean up disposable copies (Priority: P3)

The operator asks "list my rehearsal copies" or "drop the copies from yesterday". The assistant lists the copies this feature created, with their source, age, size and the report they belong to. After confirmation it drops them. Expired copies are dropped automatically.

**Why this priority**: The VPS disk is about 94% full, so leftover copies are a real operational risk. The other stories only need retention to work.

**Independent Test**: Create two copies, expire one, and request a cleanup. Only copies made by this feature may be dropped; others must be untouchable.

**Acceptance Scenarios**:

1. **Given** copies exist, **When** they are listed, **Then** only databases created by this feature are shown, with source, creation time, size and expiry.
2. **Given** a cleanup request with confirmation, **When** it runs, **Then** the selected copies are dropped and the reclaimed space is reported.
3. **Given** a database not created by this feature, **When** a drop is requested for it, **Then** it is refused, regardless of its name.

---

### Edge Cases

- **Source in use**: the source database is being written to, or has active connections, when a copy is requested. The copy must reflect a consistent point in time, or be refused with an explanation. It must never block or disconnect the source's users without confirmation.
- **Copy name clash**: a copy's name must never collide with an existing database. Names are generated by the system, never by the caller.
- **Long steps**: the "upgrade all modules" step can run for hours. Rehearsals have a long, configurable step timeout; progress is reported while they run; and a timed-out step leaves the copy in place for inspection.
- **Connection drop**: the connection to the VPS drops mid-rehearsal. The rehearsal keeps running on the VPS, and its status and results can be retrieved after reconnecting. It must not be restarted silently.
- **Disk filling up**: disk space drops below the minimum during a rehearsal, because the copy grows while the upgrade runs. The rehearsal aborts at the next step boundary and reports why.
- **Missing objects**: the two databases being compared differ in Odoo major version, or one lacks the Odoo module tables entirely. The comparison still covers schema and row counts and states which checks were skipped.
- **Very large tables**: exact row counts would be too slow. Estimated counts are used and labelled as estimates. Exact counts are computed only for tables flagged as at risk.
- **Declared step not found**: a declared step's script no longer exists, or has changed since it was declared. The rehearsal refuses to run it and reports the mismatch.
- **Secrets in output**: logs or check output may contain secrets such as database passwords or tokens. Output is masked with the existing secret-masking rules.

## Requirements *(mandatory)*

### Functional Requirements

**Scope & safety**

- **FR-001**: The system MUST only analyse databases on an operator-defined allowlist, plus copies it created itself. All other databases MUST be refused, without revealing whether they exist.
- **FR-002**: The system MUST NOT execute caller-supplied SQL or commands. Every database interaction MUST come from a fixed catalogue of read-only queries, or from operator-declared pipeline steps and checks.
- **FR-003**: Operator-declared pipeline steps and checks that may modify data MUST only ever run against a disposable copy created by the system in the same rehearsal. Each step MUST be bound to the copy by the system, never by the caller.
- **FR-004**: Analysis of allowlisted (non-copy) databases MUST use access that cannot modify data.
- **FR-005**: Every state-changing action (creating a copy, running a rehearsal, running a check that may modify data, dropping a copy) MUST require explicit confirmation, MUST be refused in read-only mode, and MUST be recorded in the existing audit trail.
- **FR-006**: The system MUST identify copies it created by a marker that callers cannot forge. It MUST NOT drop or modify any database without that marker.

**Understanding (US1)**

- **FR-007**: The system MUST report a database overview: size, table count, the largest tables with estimated row counts, and Odoo module counts by state and major version.
- **FR-008**: The system MUST list Odoo modules with name, state and installed version, and flag modules whose code is missing from the declared addons paths.
- **FR-009**: The system MUST return the detailed structure of a requested table: columns, types, nullability, defaults, indexes, constraints and foreign keys.
- **FR-010**: The system MUST report known Odoo problem indicators: modules in transitional states, inactive views and orphaned view customisations.

**Comparison & risk (US2)**

- **FR-011**: The system MUST compare two allowlisted or copy databases and report differences in:
  - tables;
  - columns;
  - column types (classified as narrowing or widening);
  - constraints and indexes;
  - per-table row counts;
  - module state and version;
  - views.
- **FR-012**: The system MUST classify each finding as blocking, warning or info, using documented rules with operator-configurable thresholds. Defaults:
  - **Blocking**: loss of a populated column or table; a row-count drop above 20%; modules left at the old major version or in a transitional state; a failed rehearsal step.
  - **Warning**: a row-count drop above 5%; type narrowing; newly inactive views.
- **FR-013**: Every risk report MUST end with an overall verdict ("safe to promote", "review warnings", "do not promote") derived from the most severe finding.
- **FR-014**: Reports MUST be stored with an identifier and be retrievable later, at least for the audit retention period.
- **FR-015**: Comparisons MUST be deterministic: the same inputs give the same findings, in the same order.

**Rehearsal (US3)**

- **FR-016**: The operator MUST be able to declare named pipelines in the server's configuration. Each pipeline is an ordered list of steps, where each step is one of:
  - a SQL file from an approved directory;
  - a script from an approved directory;
  - an Odoo module upgrade (all modules, or the modules still at the old version, with a round limit).
- **FR-017**: Before a rehearsal, the system MUST present a plan:
  - the source;
  - the copy name;
  - the ordered steps, with a fingerprint of each step's file content;
  - the estimated copy size;
  - free disk before and after.

  Nothing may be created until the operator confirms.
- **FR-018**: The system MUST refuse to create a copy when the estimated free disk afterwards would fall below a configurable minimum (default: 10% of the disk, or 10 GB, whichever is larger). It MUST abort a running rehearsal at the next step boundary if free disk falls below the minimum.
- **FR-019**: The system MUST run steps in order, against the copy only, stopping at the first failure. It MUST record each step's status, duration, exit code and a masked log excerpt.
- **FR-020**: The system MUST refuse to run a step whose file content fingerprint differs from the one shown in the confirmed plan.
- **FR-021**: Rehearsals MUST keep running if the assistant's connection drops. Their status and results MUST remain retrievable afterwards.
- **FR-022**: Only one rehearsal at a time may run on the VPS. A second request is refused with "conflict".
- **FR-023**: When a rehearsal completes or stops, the system MUST automatically produce a comparison of the source against the copy, plus the risk report, and include the step results.

**Pipeline runs & checks (US4, US5)**

- **FR-024**: For each declared pipeline, the operator MUST be able to declare where its own runs write progress and logs. The system MUST summarise the latest run: steps, statuses, timestamps, top error messages with counts, and truncation notice.
- **FR-025**: The operator MUST be able to declare check tools as either "read-only" or "may modify data". The system MUST run them against allowlisted databases or copies according to FR-003 and FR-004, and fold their results into reports.

**Copies & retention (US6)**

- **FR-026**: The system MUST list the copies it created, with source, creation time, size, expiry and linked report.
- **FR-027**: Copies MUST expire after a configurable retention period (default 24 hours) and be dropped automatically. Dropping a copy early requires confirmation.

### Key Entities

- **Analysis Target**: an allowlisted database on the VPS. Attributes: name, connection profile (the cluster), purpose label (dump / test result), allowed access (read-only).
- **Disposable Copy**: a database the system created from a target for one rehearsal. Attributes: generated name, source, creation time, size, expiry, ownership marker, linked rehearsal.
- **Pipeline Definition**: an operator-declared ordered list of steps. Each step has a kind (SQL file, script, module upgrade), file path, content fingerprint and timeout. The definition also records where the operator's own runs write progress and logs.
- **Check Definition**: an operator-declared tool, with a name, path, access level (read-only / may modify) and timeout.
- **Rehearsal**: one execution of a pipeline against a copy. Attributes: plan, confirmation, status (planned / running / succeeded / failed / aborted / timed out), per-step results, linked report.
- **Snapshot**: a captured description of a database at one moment. It holds the schema, row counts, module states and view states.
- **Comparison Report**: the differences between two snapshots, plus classified findings and the overall verdict. It has an identifier, creation time and inputs.
- **Finding**: one detected difference or problem. Attributes: severity, category, affected objects, explanation, measured values.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: An operator can get a full overview of a database (size, largest tables, module states, problem indicators) in under 30 seconds, without running a single query by hand.
- **SC-002**: A comparison of two production-sized databases (tens of GB) completes in under 5 minutes.
- **SC-003**: In a planted-defect test set (dropped populated column, narrowed type, 40% row loss, module left at 17.x, failed step), 100% of defects are reported with the expected severity, and the verdict is "do not promote".
- **SC-004**: Zero modifications to any database other than a system-created copy, across a negative test suite of at least 30 attempts. The suite includes declared steps aimed at other databases, forged copy names, and confirmation-less requests.
- **SC-005**: Zero rehearsals start when the post-copy free disk would fall below the minimum. Zero expired copies remain more than one hour past their expiry.
- **SC-006**: An operator can go from "is this upgrade safe?" to a verdict on a fresh rehearsal with three requests or fewer: plan, confirm, read report.
- **SC-007**: Rehearsal step results match what the same steps produce when the operator runs the pipeline by hand on the same source (same steps succeed or fail), in at least 95% of rehearsals.

## Assumptions

- **Cluster**: all databases in scope live in one PostgreSQL cluster on the VPS (currently the PostgreSQL 18 cluster that serves `shop_19_initial` and `shop_19_repaired`). Other database servers are out of scope for v1.
- **Production**: production databases are never on the allowlist, and are out of reach by design. Promoting a migration to production stays in the operator's own pipeline.
- **Trusted pipeline files**: the operator's pipeline files (cleanup SQL, scripts, upgrade tooling) are trusted, but only within a rehearsal copy. Their content is pinned by fingerprint at plan time.
- **No new privileges on the VPS**: copies are made with the database server's own copy facilities. The feature reuses the existing vps-mcp connection and audit trail. Any extra database privileges the feature needs are granted by the operator during setup, and are documented.
- **Starting a test Odoo instance is out of scope for v1**. Today's pipeline does this as its last step. Operators can inspect copies with the existing service tools.
- **Reporting**: reports are returned in the assistant conversation and stored for later retrieval. Dashboards, notifications and exports are out of scope.
- **Compatibility with the existing feature**: the existing vps-mcp safety model (allowlist, confirmations, read-only mode, audit, secret masking, lock-out guards) applies unchanged. This feature only adds capabilities.
