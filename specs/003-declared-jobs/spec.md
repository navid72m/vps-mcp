# Feature Specification: Declared Jobs

**Feature Branch**: `003-declared-jobs`

**Created**: 2026-11-11

**Status**: Draft

**Input**: User description: "Declared jobs for vps-mcp: let the operator register their own long-running rehearsal/maintenance scripts (first case: a full-rehearsal script that drops and rebuilds a test database from a v17 dump via an OpenUpgrade pipeline, restores it, runs the deploy script with the regression suite, and runs a comparison script, logging timestamped lines to a progress file) in the policy file, and have the MCP server plan, start, monitor, cancel and report on them safely. … Builds on feature 002 (migration analysis) infrastructure."

## Context

Feature 002 rehearses pipeline steps on disposable *copies* that the server creates itself. Operators also have their own end-to-end scripts that do more than a copy-based rehearsal can:

- The **full-rehearsal script** (`full_rehearsal.sh` in the examples here) does the following:
  1. stops the test Odoo service and drops the **test database** (`shop_19_test`);
  2. runs the OpenUpgrade pipeline on a v17 dump;
  3. restores the result into the test database and blocks outgoing mail;
  4. runs the deploy script with the regression suite;
  5. runs a comparison script against the v17 and reference databases.

  Each step writes a timestamped line to a progress file.
- These scripts reuse a fixed test-database "slot" instead of creating a new copy, so they fit on a nearly full disk where copies don't.
- Today the operator starts them by hand over SSH, then reads progress files and logs to find out what happened.

A **declared job** turns such a script into something the assistant can run safely. The operator declares, in the policy file:
- what the script is;
- what it may change (databases, services);
- where it reports progress and results.

The assistant can then plan, start (with confirmation), watch, stop and report on it. It never runs anything that wasn't declared, and it flags anything the job touched outside what was declared.

## User Scenarios & Testing *(mandatory)*

**Actors**:
- **Migration operator**: declares jobs and asks the assistant to run them.
- **AI assistant**: calls the job capabilities.
- **The job script**: the operator's own trusted code, running on the VPS.

### User Story 1 - Start a declared job after reviewing its plan (Priority: P1)

The operator says "run the full rehearsal". The assistant shows a **plan** listing:
- which script will run, with a fingerprint of its current content and when it was last changed;
- which databases it is declared to drop or rebuild (e.g. `shop_19_test`), with their current sizes;
- which services it is declared to stop;
- the parameter values that will be passed;
- current free disk against the job's declared minimum;
- whether another job or rehearsal is already running.

Only after the operator confirms does the job start. It runs in the background on the VPS and keeps running if the assistant's session ends.

**Why this priority**: Starting the job safely is the core capability. It replaces "SSH in and run the script by hand", and the plan is where the operator decides whether to destroy and rebuild the test database.

**Independent Test**: Declare a small job that rebuilds a test database. Request it without confirmation, and verify that nothing runs and that the plan names the database to be dropped. Confirm, close the assistant, and verify that the job finished on the VPS.

**Acceptance Scenarios**:

1. **Given** a declared job, **When** the operator requests it without confirmation, **Then** a plan is returned containing:
   - the script fingerprint;
   - the declared databases and services it may change;
   - parameter values;
   - free disk;
   - a plan identifier.

   Nothing runs.
2. **Given** a plan, **When** the operator confirms with that plan's identifier within 30 minutes, **Then** the job starts in the background and its identifier is returned.
3. **Given** the script's content changed after the plan was made, **When** the operator confirms, **Then** the start is refused and a fresh plan is returned.
4. **Given** free disk is below the job's declared minimum, **When** a plan is requested, **Then** it is refused with "insufficient disk".
5. **Given** another declared job or a feature-002 rehearsal is running, **When** a start is confirmed, **Then** it is refused with "conflict".
6. **Given** read-only mode, **When** a start is requested, **Then** it is refused.

---

### User Story 2 - Watch a running job (Priority: P1)

The operator asks "how's the rehearsal going?". The assistant reports:
- the job's state (running, succeeded, failed, stopped, cancelled);
- elapsed time;
- the steps reported so far, read from the job's progress file, with the status the system could derive for each;
- the most recent lines;
- the free disk the job last reported.

**Why this priority**: Full rehearsals run for hours. Seeing progress without SSH is the main reason to run them through the assistant.

**Independent Test**: Run a job whose progress file contains a known sequence of lines, including a failure line, and verify that the reported steps, states and final outcome match.

**Acceptance Scenarios**:

1. **Given** a running job, **When** status is requested, **Then**:
   - every progress line written so far is reported in order, each with its timestamp;
   - lines recognised as step outcomes are marked: a non-zero "EXIT n" means failed, "EXIT 0" or "ok" means succeeded, and lines starting with "STOP" are failures;
   - the job state is "running".
2. **Given** the job ended with a final success line ("ALL DONE", or a declared equivalent), **When** status is requested, **Then** the state is "succeeded".
3. **Given** the job process ended without a success line, **When** status is requested, **Then** the state is "failed", along with the last progress line and the last lines of the job's console output.
4. **Given** the assistant session was closed while the job ran, **When** a new session asks for status, **Then** the full state is available.
5. **Given** progress or log output contains secret-looking values, **When** it is shown, **Then** those values are masked.

---

### User Story 3 - Get one report and a verdict when the job ends (Priority: P2)

When a job finishes, the operator asks "is the result safe to promote?". The assistant produces **one report** that combines:
- the job's step outcomes;
- the job's own report file, if one is declared (e.g. the comparison script's summary);
- a database comparison (feature 002) between the job's declared **source** database and its **result** database;
- any guard findings from Story 4.

The report ends with the same verdict scale as feature 002: safe to promote, review warnings, or do not promote.

**Why this priority**: Turning hours of output into one verdict is the payoff. It depends on Stories 1–2 having run a job.

**Independent Test**: Run a job that succeeds but whose result database lost 40% of a table's rows compared with the declared source. The report must be "do not promote", with that row loss as a blocking finding, and must include the job's own summary line.

**Acceptance Scenarios**:

1. **Given** a job finished and declares a source and a result database, **When** its report is produced, **Then** it contains:
   - the step outcomes;
   - the feature-002 comparison findings;
   - an excerpt of the job's own report file;
   - one verdict.
2. **Given** any job step failed, **When** the report is produced, **Then** the verdict is "do not promote", with the failed step as a blocking finding.
3. **Given** the job did not finish (failed early or was cancelled), **When** the report is produced, **Then** the comparison is skipped, the report says why, and the verdict is "do not promote".
4. **Given** a report was produced, **When** the operator asks for it later, **Then** it can be retrieved by identifier.

---

### User Story 4 - Know if a job touched anything it shouldn't have (Priority: P2)

While a job runs, the system watches everything the job was **not** declared to change:
- writes to every other allowlisted database;
- disappearance of any database that isn't declared;
- state changes of every managed service not declared as stoppable.

Anything outside the declaration is reported as a blocking finding, and the operator is alerted in the status.

**Why this priority**: A job is the operator's own trusted script running with root rights. It can't be confined, but undeclared side effects must never go unnoticed.

**Independent Test**: Run a job that writes to a database it doesn't declare, and stops a service it doesn't declare. Both must appear as blocking findings in the status and in the report.

**Acceptance Scenarios**:

1. **Given** a job writes to an allowlisted database outside its declared list, and that database had no other active sessions, **When** status or the report is produced, **Then** it shows a blocking "undeclared database write" finding naming the database.
2. **Given** that database also had other active sessions, **When** the finding is produced, **Then** it is a warning ("write not attributable") rather than blocking.
3. **Given** a managed service outside the declared list changes state during the job, **When** status or the report is produced, **Then** it shows a blocking "undeclared service change" finding.
4. **Given** a database outside the declared list disappears during the job, **When** status or the report is produced, **Then** it shows a blocking "undeclared database dropped" finding.

---

### User Story 5 - Stop a running job (Priority: P3)

The operator says "stop the rehearsal". After confirmation, the system stops the job's processes and records that it was cancelled. It also reports what state the declared databases and services were left in.

**Why this priority**: Stopping is needed, but rare.

**Independent Test**: Start a long job, cancel it with confirmation, and verify that its processes are gone, its state is "cancelled", and the report notes the declared database may be incomplete.

**Acceptance Scenarios**:

1. **Given** a running job, **When** cancellation is requested without confirmation, **Then** a preview shows the current step and warns that the declared databases may be left half-built.
2. **Given** confirmation, **When** the job is stopped, **Then** none of its processes keep running, and its state is "cancelled".
3. **Given** a declared service was stopped by the job before cancellation, **When** cancellation completes, **Then** the result says that the service is still stopped. The system does **not** restart it automatically.

---

### User Story 6 - Pass declared parameters (Priority: P3)

The operator says "run the full rehearsal with run name nightly_1111". Jobs may declare **parameters**: a name, a validation pattern, and a default. The assistant may set only those parameters. Each value is validated before it appears in the plan, and is passed to the script as an environment value, never spliced into a command.

**Why this priority**: It makes scripts reusable (fresh run names) without opening a path for arbitrary input.

**Independent Test**: Declare a parameter `RUN_NAME` with pattern `^[a-z0-9_]{1,40}$`. Verify that a valid value reaches the script's environment, an invalid one is refused, and an undeclared parameter is refused.

**Acceptance Scenarios**:

1. **Given** a declared parameter, **When** a valid value is supplied, **Then** it appears in the plan and reaches the job's environment unchanged.
2. **Given** a value not matching the declared pattern, or a parameter that isn't declared, **When** a plan is requested, **Then** it is refused with "invalid input".
3. **Given** no value is supplied, **When** a plan is requested, **Then** the declared default is used. A default may be generated from the date (e.g. `run_<YYYYMMDD_HHMM>`).

---

### Edge Cases

- **Script made writable by others**: if the script file is writable by anyone other than its root owner, or its directory is, planning is refused, because someone else could change what runs between plan and start.
- **Script changes after start**: the fingerprint is checked again right before the job starts. Changes made after the start are outside the system's control, which the plan says explicitly.
- **Job started by hand**: if the operator also starts the same script by hand over SSH, the system reports a conflict if it can detect a running instance; otherwise it can only warn about it in status.
- **Missing or truncated progress file** (e.g. the job recreates it): status reports what is there and does not invent steps.
- **Job outlives the connection**: the status read after reconnecting must be complete. It must not depend on the assistant having been connected.
- **Very long job** (e.g. 12 h): there is a declared maximum runtime, default 24 h. When it is exceeded, the job is stopped and reported as "timed out".
- **Cancelled job left things stopped**: no automatic repair. The report lists the declared services that are not running.
- **Huge job output**: the report includes only an excerpt of the job's own report file (default up to 200 lines), marked as truncated when shortened.
- **Many progress lines**: status shows the last 50 lines plus all lines recognised as step outcomes.

## Requirements *(mandatory)*

### Functional Requirements

**Declaration**

- **FR-001**: The operator MUST be able to declare jobs in the policy file, each with:
  - a unique name;
  - the script path;
  - the user it runs as (default: root);
  - the progress file and optional log locations;
  - an optional report file;
  - the databases it may change;
  - the services it may stop or start;
  - an optional source and result database for comparison;
  - optional parameters (name, validation pattern, default);
  - a minimum free disk;
  - a maximum runtime;
  - an optional success marker (default "ALL DONE").
- **FR-002**: Declared databases MUST be allowlisted analysis targets (feature 002), and declared services MUST be managed services (feature 001). Declarations that reference anything else MUST be rejected when the configuration is validated.

**Plan and start**

- **FR-003**: Requesting a job without confirmation MUST return a plan with:
  - the script fingerprint and last modification time;
  - the declared databases (with current sizes) and services (with current states);
  - the resolved parameter values;
  - free disk against the declared minimum;
  - any running job or rehearsal;
  - a plan identifier.

  It MUST change nothing.
- **FR-004**: A job MUST start only on confirmation with a matching plan identifier issued within the last 30 minutes. The script fingerprint, ownership and permissions, free disk, and concurrency MUST be re-checked at that moment.
- **FR-005**: The system MUST refuse to plan or start a job whose script, or the directory containing it, is writable by anyone other than its owner, or whose owner is not the declared run-as user or root.
- **FR-006**: Only one declared job MAY run at a time. A job MUST NOT start while a feature-002 rehearsal is running, and vice versa.
- **FR-007**: Jobs MUST run detached on the VPS, so they keep running regardless of the assistant's connection, and MUST be stopped automatically after the declared maximum runtime.
- **FR-008**: Parameters MUST be accepted only if declared, MUST match their declared pattern, and MUST be passed to the script only as environment values.

**Status**

- **FR-009**: The system MUST report the job state:
  - planned;
  - running;
  - succeeded (the success marker was seen and the process exited 0);
  - failed (the process ended otherwise);
  - timed out;
  - cancelled.

  It MUST also report the elapsed time.
- **FR-010**: The system MUST parse the progress file into timestamped lines and recognise step outcomes using built-in rules (`EXIT <n>`, `exit <n>`, `rc=<n>`, `ok`, `FAILED`, lines starting with `STOP`, the success marker). The operator MAY add patterns per job. Unrecognised lines MUST be shown as information, never dropped.
- **FR-011**: Status output MUST be masked with the existing secret-masking rules, and MUST be limited to the last 50 progress lines plus all recognised outcomes.

**Guard**

- **FR-012**: While a job runs, the system MUST record:
  - write counters for every allowlisted database not declared by the job;
  - the existence of every allowlisted database;
  - the state of every managed service not declared by the job.

  It MUST record these at start, at every status check, and at the end.
- **FR-013**: The system MUST report the following:
  - a write to an undeclared database with no other active sessions, as **blocking**;
  - such a write while other sessions were active, as a **warning**;
  - an undeclared database disappearing, as **blocking**;
  - an undeclared service changing state, as **blocking**.

**Report**

- **FR-014**: When a job reaches a final state, the system MUST produce one stored report containing:
  - the step outcomes;
  - the guard findings;
  - an excerpt of the declared report file (up to 200 lines, masked);
  - if the job succeeded and declares a source and a result database, the feature-002 comparison between them;
  - one verdict.
- **FR-015**: The verdict MUST be "do not promote" whenever the job did not succeed, any step failed, or any guard finding is blocking. Otherwise it MUST follow the feature-002 rules.

**Cancel**

- **FR-016**: Cancelling MUST require confirmation, MUST stop all of the job's processes, and MUST report the state of the declared databases and services afterwards. It MUST NOT restart services or repair databases.

**Safety model**

- **FR-017**: Every plan, start and cancel MUST be recorded in the audit trail, and MUST be refused in read-only mode.
- **FR-018**: The assistant MUST NOT be able to run anything except declared jobs with declared parameters. No capability accepts a script path, command or free-form argument.

### Key Entities

- **Job Declaration**: name, script, run-as user, progress file, log locations, report file, declared databases, declared services, source and result databases, parameters, minimum free disk, maximum runtime, success marker, extra outcome patterns.
- **Parameter Declaration**: name, validation pattern, default (literal or date-based template).
- **Job Plan**: plan identifier, declaration snapshot, script fingerprint, resolved parameters, disk figures, creation time.
- **Job Run**: run identifier, plan identifier, state, start and end times, exit code, guard baseline, latest guard readings, report identifier.
- **Progress Line**: timestamp, text, recognised outcome (step, status), or information.
- **Guard Finding**: kind (undeclared write, undeclared drop, undeclared service change), object, severity, measured values.
- **Job Report**: a feature-002 report extended with step outcomes, a job report excerpt and guard findings.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: An operator can go from "run the full rehearsal" to a running job in **2 requests** (plan, confirm), without opening a terminal.
- **SC-002**: The status of a running job is available within **10 seconds**, including after the assistant was closed and reopened during the run.
- **SC-003**: In a test where a job, in addition to its declared changes, writes to an undeclared database, drops an undeclared database and stops an undeclared service, **100%** of these are reported as blocking, and the verdict is "do not promote".
- **SC-004**: **Zero** jobs start when:
  - the script changed after the plan;
  - the script is writable by others;
  - free disk is below the declared minimum;
  - another job or rehearsal is running;
  - the server is in read-only mode.

  This is verified by a negative test suite of at least 20 cases.
- **SC-005**: For a real full-rehearsal run, the report's step outcomes match the operator's own reading of the progress file in **100%** of steps.
- **SC-006**: **Zero** job processes remain running after a confirmed cancel or after the maximum runtime is exceeded.

## Assumptions

- **Trust**: job scripts are the operator's own trusted code, running as root by default. The system cannot confine them; it controls *what* may be started and *when*, and detects undeclared side effects (FR-012/013). This is stated in every plan.
- **Ownership**: the operator keeps scripts root-owned and not writable by others. Otherwise planning is refused (FR-005).
- **Progress format**: progress files are line-based, one event per line, optionally prefixed with a timestamp. The full-rehearsal script's format (`YYYY-MM-DD HH:MM:SS <event> | free <n>`) is the reference.
- **Parameters**: scripts that should accept parameters read them from environment variables (e.g. `RUN_NAME=${RUN_NAME:-…}`). Scripts with hard-coded values still work, just without parameters.
- **Scope of the guard**: only allowlisted databases (feature 002) and managed services (feature 001) are watched. Effects outside those (files, other services, containers outside the allowlist) are not monitored in v1.
- **Report comparison**: the feature-002 comparison is reused unchanged. A job's source database (e.g. the v17 dump database) must be an allowlisted target.
- **Feature 002 setup**: the VPS-side setup (read-only database role, sudoers additions) is a prerequisite. This feature adds a small number of further sudo rights, documented in the plan.
- **Out of scope for v1**:
  - scheduling jobs (cron-like);
  - running more than one job at a time;
  - automatic repair after a cancel;
  - streaming live output into the conversation.
