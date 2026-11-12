# Feature Specification: Agent Memory Service

**Feature Branch**: `006-agent-memory-service`

**Created**: 2026-11-12

**Status**: Draft

**Input**: User description: "Standalone agent memory ("agent-memory") built on memory best practices, replacing vps-mcp's built-in memory (feature 004). Memory is a managed knowledge system, not a conversation history. It must (1) remember important things, (2) retrieve only relevant things, (3) update or forget outdated information, (4) explain where each memory came from. It is separate from any one agent, model or tool: its own MCP server that any agent can connect to for any project, and vps-mcp uses it as a library to contribute generated facts.

- **Memory types**: episodic, semantic, procedural and decision. Each memory carries type, scope, subject, content, provenance, confidence, importance, timestamps, optional expiry and status.
- **Lifecycle**: observe → evaluate → extract → validate → store → retrieve → update or invalidate.
- **Deterministic write policy**: no secrets; untrusted text never promoted automatically; inferred facts marked as inferred; conflicts kept and flagged; decisions never silently overwritten; supersede with history instead of overwriting.
- **Live state**: memory is never authoritative over live state.
- **Storage**: Markdown files are the source of truth, plus a rebuildable full-text index for hybrid ranked retrieval.
- **What a session receives**: a small pinned core at connect; task-aware search for everything else.
- **Interface**: a small set of operations (search, get, save, update, restricted archive).
- **Migration**: migrate the feature-004 notes.
- **Benchmark**: an evaluation benchmark is included.

Builds on features 001–005."

## Context

Feature 004 gave vps-mcp a memory: Markdown notes plus generated facts, with a ~1,500-word briefing injected at every connect. It works, but it falls short of established memory practice in four ways:

1. **It lives inside vps-mcp.** Work on GitHub, databases or local code in other projects cannot use it.
2. **It injects a broad briefing** instead of retrieving what is relevant to the task at hand. As memory grows, the briefing either gets trimmed arbitrarily or wastes context.
3. **Notes have no provenance or confidence.** There is no source evidence and no verified/inferred distinction. They have no expiry, and no supersede history. An outdated fact looks exactly like a current one, and a decision can be overwritten silently.
4. **Writes are only checked for secrets and size.** They are not checked for duplicates, contradictions or untrusted origin, so memory can be poisoned by text an agent merely read.

This feature turns memory into a **managed knowledge system** that stands on its own. Every agent and every project share it, while each one only sees what is in scope.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Get the right memories for the task at hand (Priority: P1)

An agent receives a task, for example "upgrade the production Odoo database". It asks memory for what is relevant to that task. It gets back a small ranked set grouped by kind:

- **facts** (what is true);
- **decisions** (why things are as they are, including rejected alternatives);
- **experiences** (what happened before);
- **procedures** (how to do it).

Unrelated memories stay out. If the agent needs more, it asks again with a narrower query.

**Why this priority**: Retrieval is what makes memory useful. Getting the right 5–10 items beats getting 200 loosely related ones.

**Independent Test**: Load a memory set of ~300 items across several projects and types. Run 20 benchmark tasks, each with a known list of relevant memories. Measure how many relevant items appear in the top results, how many irrelevant ones appear, and the result size.

**Acceptance Scenarios**:

1. **Given** memories across several projects, **When** an agent searches with a task description and a project scope, **Then** it receives at most a configured number of results (default 8). Results are grouped by type and ranked by relevance, with recency, importance and confidence taken into account. Memories from other projects are excluded.
2. **Given** a superseded, expired or archived memory, **When** searching, **Then** it is not returned unless the agent explicitly asks to include history.
3. **Given** a result describes live state (for example "service X runs on port 8069"), **When** it is returned, **Then** it carries a visible reminder to verify against the live system before acting.
4. **Given** a search returns nothing relevant, **When** the agent receives the response, **Then** it says so plainly instead of padding the result with weak matches.

---

### User Story 2 - Save knowledge safely (Priority: P1)

During or at the end of a task, an agent records something worth keeping. For example: "the production deployment fails when the database migration runs before the application is stopped". It saves this as one concise structured memory with its type, scope, subject, source and confidence. The memory service validates it before storing.

**Why this priority**: What enters memory decides what every future session believes. Validation is the defence against clutter, duplicates and poisoning.

**Independent Test**: Submit 30 candidate memories: valid ones, duplicates, contradictions, secrets, untrusted instructions, and unverified claims. Check that each one is accepted, merged, flagged, refused or downgraded exactly as the write policy says.

**Acceptance Scenarios**:

1. **Given** a valid candidate, **When** it is saved, **Then** it is stored with type, scope, subject, content, source, confidence, importance and timestamps, and the save is audited.
2. **Given** a candidate that duplicates an active memory (same scope, subject and meaning), **When** it is saved, **Then** no duplicate is created. The existing memory is returned, and its confirmation count and timestamp are refreshed.
3. **Given** a candidate that contradicts an active memory on the same subject, **When** it is saved, **Then** both are kept. They are linked as conflicting and marked as needing verification. Neither is silently overwritten.
4. **Given** a candidate containing a password, token, key or similar secret, **When** it is saved, **Then** the secret is never stored. The save is refused, or the value is masked, and the response says which happened.
5. **Given** a candidate whose source is untrusted content (a README, issue, web page or tool output), **When** it is saved, **Then** it is stored as **unverified**, never as verified. Instruction-like text ("ignore previous instructions…", "always run …") from such a source is refused as a procedure or decision unless the operator confirms it.
6. **Given** a candidate the agent inferred rather than observed, **When** it is saved, **Then** it is stored with confidence **inferred**.
7. **Given** a save of type **decision** for a subject that already has an active decision, **When** it is saved, **Then** it does not replace the old one. Replacing requires an explicit supersede, which records the reason and keeps the old decision in the history.

---

### User Story 3 - Update, supersede and forget outdated knowledge (Priority: P1)

The world changes: the database is upgraded from PostgreSQL 16 to 17, a procedure is improved, a decision is revisited. An agent or the operator supersedes the old memory with the new one, expires memories after a set time, or invalidates a memory that turned out to be wrong. Outdated memories stop appearing in normal retrieval, but their history stays inspectable.

**Why this priority**: Stale memory that looks current is worse than no memory. This is the main defect of history-based memory.

**Independent Test**:

- Supersede a fact, then check that searches return only the new version, and that the history shows both versions with the reason.
- Give a memory a 1-day expiry and advance time. Check that it drops out of retrieval and is listed as expired.

**Acceptance Scenarios**:

1. **Given** an active memory, **When** it is superseded by a new one with a reason, **Then** the old memory becomes **superseded** and links to its successor. The new one links back. Normal retrieval returns only the new one.
2. **Given** a memory with an expiry date, **When** that date passes, **Then** the memory is treated as **expired**: excluded from retrieval, still readable on request.
3. **Given** a memory proven wrong, **When** it is invalidated with a reason, **Then** it leaves retrieval and keeps the reason for audit.
4. **Given** memories that describe live state, **When** they have not been re-confirmed within their freshness window (default 7 days), **Then** retrieval marks them as possibly stale.
5. **Given** the operator asks to archive a memory, **When** confirmed, **Then** it leaves all retrieval and is kept in an archive area. Archiving is the only removal path an agent can use, and it always needs confirmation.

---

### User Story 4 - One memory for every agent and project (Priority: P2)

The operator uses Claude Code for the vps-mcp project, OpenCode for an Odoo addons repo, and a local model in LM Studio for scripts. All of them connect to the same memory service. Each sees the memories of its own project, plus the user-wide memories (preferences, conventions), plus nothing else unless it asks. vps-mcp keeps contributing its generated facts (service states, deploys, verdicts, job outcomes) into the right project and environment scope, without any agent writing them.

**Why this priority**: Memory that survives switching agents and models is the long-term value. Without it, every new tool starts from zero.

**Independent Test**: Connect two different clients to the same memory service with different project scopes. A memory saved by one client in project A is visible to the other client only when it works in project A. A user-scope preference is visible to both.

**Acceptance Scenarios**:

1. **Given** the memory service is configured once on the machine, **When** any MCP client connects, **Then** it can search and save memories without the client needing special support.
2. **Given** memories in scopes `user`, `project:<name>`, `environment:<name>` and `task:<id>`, **When** an agent searches with a project, **Then** it gets that project's memories, its environments' memories and user memories. It does not get other projects' memories.
3. **Given** vps-mcp is running, **When** a deploy, job or comparison finishes, or service states are observed, **Then** the matching generated facts are updated in the memory service under the right scope. Each is marked as generated, with the tool output as its source.
4. **Given** the memory service is unavailable, **When** vps-mcp runs, **Then** vps-mcp still works without memory and reports the memory as unavailable.

---

### User Story 5 - A small pinned core at connect (Priority: P2)

When a session connects, the agent gets only a small pinned core of about 500 tokens:

- the memories the operator or agents marked as pinned (key procedures and standing decisions);
- the open issues of the current scope;
- a one-line pointer to the search operation.

Everything else is fetched by search.

**Why this priority**: The connect payload is paid on every session in every client. Keeping it small and focused saves tokens and keeps attention on what matters most.

**Independent Test**: With 300 memories of which 6 are pinned and 3 are open issues, the connect payload contains exactly those 9 items and the pointer. It stays under the budget, and it has a "more via search" note.

**Acceptance Scenarios**:

1. **Given** pinned memories and open issues in scope, **When** a session connects, **Then** the payload lists them within the budget (default about 500 tokens). Anything that doesn't fit is replaced by a count.
2. **Given** no pinned memories, **When** a session connects, **Then** the payload is a short statement that memory is available through search.
3. **Given** a memory is pinned, **When** it is later superseded, **Then** its successor inherits the pin.

---

### User Story 6 - Know where a memory came from (Priority: P2)

Before acting on a memory, the agent or the operator can see:

- who or what created it (client, task, tool output, file);
- when it was created and last confirmed;
- how confident it is;
- what it replaced;
- whether anything contradicts it.

**Why this priority**: A memory you cannot trace is a memory you cannot trust. Provenance is what lets an agent decide to verify instead of acting blindly.

**Independent Test**: Read a memory that was created, confirmed twice, and then superseded. The response shows all of this, with sources and timestamps.

**Acceptance Scenarios**:

1. **Given** any memory, **When** it is read, **Then** the response includes its source, its created and updated timestamps, its last-confirmed time and confirmation count, its confidence, its status, the memory it supersedes or that superseded it, and any memories it conflicts with.
2. **Given** a generated memory (from vps-mcp), **When** it is read, **Then** its source names the tool and the event that produced it.

---

### User Story 7 - Prove that memory helps (Priority: P3)

The operator runs a benchmark that measures memory quality. It covers:

- **recall** of earlier decisions;
- **relevance** of retrieved items;
- **temporal validity**: outdated facts are avoided;
- **contradictions** are recognised;
- **token cost** of memory per task.

It compares the structured memory against "no memory" and against "retrieve similar past notes".

**Why this priority**: Memory should be judged by whether it improves the work, not by how much it stores. This is the feedback loop for tuning retrieval.

**Independent Test**: Run the benchmark command on its bundled fixture set. It prints a scorecard per metric and per configuration, and the scores are reproducible across runs.

**Acceptance Scenarios**:

1. **Given** the bundled benchmark fixtures (memories plus tasks with expected relevant memories), **When** the benchmark runs, **Then** it reports recall@k, precision@k, the stale-hit rate, the contradiction-flag rate and the average result size in tokens, for each configuration.
2. **Given** the operator's own memory store, **When** the benchmark runs with operator-written tasks, **Then** it reports the same metrics without changing the store.

---

### User Story 8 - Move the existing memory over (Priority: P2)

The operator's existing feature-004 memory is converted into the new memory with no loss: the notes on the er_pipeline and deploy_v19 pipelines, and the generated facts. vps-mcp switches to the memory service.

**Why this priority**: Knowledge already captured must not be lost in the upgrade.

**Independent Test**: Migrate a copy of a feature-004 folder. Every note becomes a memory, with its type mapped, its content unchanged and its provenance noted as "migrated". Generated facts are regenerated. A dry run shows the mapping first.

**Acceptance Scenarios**:

1. **Given** a feature-004 memory folder, **When** migration runs in dry-run mode, **Then** it lists each note with its new type, scope and subject. Nothing is written.
2. **Given** confirmation, **When** migration runs, **Then** every note becomes a memory:
   - its original timestamps and source are preserved;
   - its confidence is **unverified**, unless its origin justifies more;
   - its type is mapped as follows: decision → decision, lesson → episodic, fact → semantic, convention / preference → procedural or user-scope semantic, issue / next_step → episodic with an open status.
3. **Given** migration finished, **When** vps-mcp starts, **Then** it uses the memory service, and the feature-004 folder is left in place, untouched, as a backup.

---

### Edge Cases

- **Two agents save the same fact at the same moment.** One memory results. The second save confirms the first.
- **A contradiction involves a decision.** Neither decision is overwritten. The conflict is flagged and shown in both memories until a supersede resolves it.
- **A memory's subject is ambiguous.** For example "the database" when several exist. Subjects are normalised; an agent saving an unscoped subject in a project with several candidates is asked to qualify it.
- **The index is lost, corrupted, or older than the files after hand edits.** The index is rebuilt from the files automatically. The files are the source of truth.
- **A hand-edited memory file is malformed.** That memory is skipped and reported. Others still load.
- **A memory is very long.** It is refused, with a request to split it into concise memories. The default limit is 2 KB of content.
- **A search matches hundreds of memories.** The result size cap applies. The response reports the total and suggests narrowing the query.
- **Clock skew or missing timestamps in hand-written files.** File times are used. Expiry is evaluated against the service's clock.
- **The benchmark is run on the real store.** It is read-only: it never writes or changes confirmation counts.
- **Memory text contains instructions aimed at the agent.** It is returned as data inside a clearly labelled block, never as instructions. This is the same rule as in feature 004.

## Requirements *(mandatory)*

### Functional Requirements

**Model**

- **FR-001**: Every memory MUST have:
  - a unique identifier;
  - a type: `episodic`, `semantic`, `procedural` or `decision`;
  - a scope: `user`, `project:<name>`, `environment:<name>` or `task:<id>`;
  - a subject, normalised to a short key;
  - content: concise, at most 2 KB;
  - a source / provenance: kind (`client`, `task`, `file`, `command`, `tool`, `migration`, `operator`), a reference, and the client;
  - a confidence: `verified`, `inferred` or `unverified`;
  - an importance from 1 to 5;
  - created, updated and last-confirmed timestamps, and a confirmation count;
  - an optional expiry;
  - a status: `active`, `superseded`, `expired`, `invalidated` or `archived`;
  - optional links: supersedes, superseded-by, conflicts-with, related;
  - a pinned flag;
  - a live-state flag.
- **FR-002**: Decisions MUST also support recording the rejected alternatives and the rationale.
- **FR-003**: The issue / next-step concept from feature 004 MUST be expressible as an episodic memory with an `open` / `done` state. Open items are part of the pinned core.

**Storage**

- **FR-004**: Memories MUST be stored as human-readable files: one per memory, with structured front matter, hand-editable and git-friendly. These files are the source of truth.
- **FR-005**: A search index MUST be derivable from the files at any time. It is rebuilt automatically when missing or out of date, including after hand edits, and never treated as the source of truth.
- **FR-006**: Every change MUST keep history. Superseded and invalidated memories remain readable, with the reason and the time of the change.

**Write policy (validation)**

- **FR-007**: Every save MUST pass deterministic validation before storage:
  - required fields;
  - size;
  - secret detection (refuse or mask; never store raw secrets);
  - duplicate detection;
  - contradiction detection on the same subject and scope;
  - origin check.
- **FR-008**: Duplicates MUST NOT create new memories. They confirm the existing one: confirmation count +1, last-confirmed refreshed, and confidence raised from inferred to verified only when the new source is verified.
- **FR-009**: Contradictions MUST be kept on both sides, linked as conflicting, and flagged as needing verification. A decision MUST NEVER be replaced except by an explicit supersede with a reason.
- **FR-010**: Saves whose source is untrusted content MUST be stored as `unverified`. Instruction-like content from such sources MUST be refused as `procedural` or `decision` unless the operator confirms it.
- **FR-011**: Inferred knowledge MUST be stored with confidence `inferred`, and MUST be shown as such in every retrieval.
- **FR-012**: Every save, update, supersede, invalidation and archive MUST be audited with actor, time, memory identifier and outcome. The audit records no content.

**Lifecycle**

- **FR-013**: The service MUST support:
  - **update**: changes to importance, pin and expiry, without changing meaning;
  - **supersede**: new content that replaces old, with a reason;
  - **invalidate**: wrong, with a reason;
  - **expire**: by time-to-live or an explicit date;
  - **archive**: restricted, needs confirmation.
- **FR-014**: Expired, superseded, invalidated and archived memories MUST be excluded from normal retrieval, and included only when history is explicitly requested.
- **FR-015**: Live-state memories MUST carry a freshness window (default 7 days). Once past it, they MUST be marked possibly stale in retrieval. All live-state memories MUST be shown with a reminder to verify against the live system.

**Retrieval**

- **FR-016**: Search MUST accept a task or query text, a scope (project, environment, task), optional types, optional subjects, a limit (default 8, maximum 30) and an include-history flag.
- **FR-017**: Ranking MUST combine keyword relevance, recency, importance, confidence and scope closeness, with configurable weights. A task scope ranks above environment, which ranks above project, which ranks above user. Semantic or vector similarity MAY be added later as an optional signal. It MUST NOT be required.
- **FR-018**: Results MUST be grouped by type (facts, decisions, experiences, procedures). Each result shows its confidence, age, source kind and any conflict or staleness flags. The response states the total matches and whether results were cut.
- **FR-019**: Retrieved content MUST be presented to agents as recorded data, not instructions, the same as in feature 004.
- **FR-020**: The connect payload MUST contain only pinned memories and open items in scope, plus a pointer to search. Its default budget is about 500 tokens (configurable). The rest is replaced by counts.

**Interface**

- **FR-021**: The service MUST expose a small agent interface:
  - **search**;
  - **get** (with provenance and history);
  - **save** (validated);
  - **update** (update fields, supersede, invalidate, expire);
  - **archive** (restricted).
- **FR-022**: The service MUST be usable as a standalone MCP server by any MCP client, and as a library by other programs. vps-mcp is the first such program.
- **FR-023**: vps-mcp MUST contribute its generated facts through the library: service states, deploy outcomes, job and rehearsal outcomes, report verdicts. They go into environment scope, as generated, source-tagged, live-state memories, updated in place rather than duplicated. vps-mcp MUST keep working if the memory service is unavailable.

**Migration**

- **FR-024**: A migration command MUST convert a feature-004 memory folder into the new store. It MUST have a dry run, a type mapping, preserved timestamps, `migrated` provenance, and leave the source folder untouched.
- **FR-025**: After migration, vps-mcp's built-in feature-004 memory tools MUST be replaced by the memory service. The old memory configuration MUST keep working, pointing at the migrated store or reporting clearly what to change.

**Evaluation**

- **FR-026**: A benchmark command MUST measure, on bundled fixtures and optionally on the operator's own store with operator-written tasks:
  - recall@k;
  - precision@k;
  - stale-hit rate;
  - contradiction-flag rate;
  - average result size in tokens.

  It MUST cover three configurations: no memory, naive keyword retrieval, and the full ranked retrieval. It MUST be read-only.

**Safety**

- **FR-027**: The memory store MUST be readable only by the operator's account, and MUST live outside any code repository by default. The service MUST warn when the store is in a repository with a hosted remote.
- **FR-028**: No input to any memory operation may read or write outside the store.

### Key Entities

- **Memory**: one unit of knowledge, with the attributes in FR-001. It is the file on disk plus its index entry.
- **Memory event**: one change to a memory (saved, confirmed, updated, superseded, invalidated, expired, archived), with actor, time and reason. Together the events form the memory's history and the audit trail.
- **Scope**: who sees a memory: user, project, environment or task. Scopes nest from narrow to broad: task, environment, project, user.
- **Pinned core**: the small set delivered at connect, made of pinned memories and open items in scope.
- **Benchmark case**: a task text, a scope, the memories expected to be relevant, and optionally memories that must not appear (stale or out of scope).

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: On the bundled benchmark, at least 85% of expected relevant memories appear in the top 8 results (recall@8 ≥ 0.85). At least 60% of returned items are relevant (precision@8 ≥ 0.6).
- **SC-002**: On the bundled benchmark, superseded, expired or invalidated memories appear in normal results in 0% of cases. Every seeded contradiction is flagged (100%).
- **SC-003**: The connect payload costs no more than about 500 tokens by default. A typical search result costs no more than about 1,000 tokens. Both are measured on the benchmark.
- **SC-004**: Out of 30 seeded candidate memories, every one is handled as the write policy specifies (100%). This covers duplicates merged, contradictions flagged, secrets refused or masked, untrusted instructions refused, and inferred items marked. No raw secret is ever written to the store.
- **SC-005**: A memory saved from one client is retrievable from a different client within the same scope, with no manual steps. It is never retrievable from another project's scope.
- **SC-006**: Migrating the operator's existing memory loses no note content. Every note maps to exactly one memory.
- **SC-007**: Search over 5,000 memories returns within 300 ms on the operator's laptop. Rebuilding the index from 5,000 files takes under 10 seconds.
- **SC-008**: vps-mcp keeps working with full functionality, except memory, when the memory service is unavailable.

## Assumptions

- **Deployment**: one operator, one laptop. The memory service runs locally, started by each MCP client as a stdio server, the same model as vps-mcp. Sharing across machines is done by syncing or a private git repository of the store; built-in sync is out of scope.
- **Who writes memories**: memory extraction (turning a task's observations into concise memories) is done by the agent itself, guided by the tool descriptions and the write policy. The service validates, but does not call an LLM. LLM-based extraction or consolidation is out of scope for this feature.
- **Vector search**: not part of this feature. The ranking design leaves room for an optional semantic signal later.
- **Scope selection**: the project scope for a session comes from the client's configuration (an argument when the server is started) or from the agent's request. A default scope can be configured per client.
- **Migration**: the feature-004 note types map as listed in US8. Generated facts are regenerated by vps-mcp rather than migrated.
- **Repository**: the memory service ships from this repository for now, as a separately runnable component with its own entry point. Splitting it into its own repository later is a packaging step, not a design change.
