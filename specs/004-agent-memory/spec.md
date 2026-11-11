# Feature Specification: Agent Memory

**Feature Branch**: `004-agent-memory`

**Created**: 2026-11-11

**Status**: Draft

**Input**: User description: "Persistent agent memory for vps-mcp, harness-agnostic. Goal: any new session in any MCP harness (OpenCode, Claude Code, LM Studio, Claude Desktop) that connects to vps-mcp immediately knows the VPS, the migration pipeline and its history, without the operator re-explaining. Storage: a folder of Markdown files on the laptop next to the policy config (one note per fact, with frontmatter), human-readable, hand-editable and git-versionable; one memory folder per VPS config. Contents: (1) derived facts the server already knows, generated automatically; (2) agent-written notes: decisions, lessons learned, known issues, conventions, next steps, operator preferences. Delivery: a concise briefing sent automatically when a session connects, plus tools to get the full briefing, search, read, write/update and archive notes; notes link to each other. Safety: writes allowed in read-only mode but audited, secret masking, size limits and a briefing budget, memory folder outside the repo, notes treated as data not instructions, path-safety for note names. Builds on features 001–003."

## Context

Features 001–003 give an assistant the *tools* to manage a VPS and an Odoo migration pipeline. But every new session starts blank. The operator has to explain again:

- which databases are which;
- what the last rehearsal showed;
- which steps are known to be flaky;
- what was decided last week.

This happens again whenever the operator switches harness (Claude Code, OpenCode, LM Studio, …). Each harness has its own memory, if any, and none of them is shared.

This feature makes **vps-mcp itself** the place where that knowledge lives. Any harness that connects to vps-mcp gets it, with no harness-specific setup.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A new session already knows the situation (Priority: P1)

The operator opens a fresh session in any MCP harness that has vps-mcp configured, and asks "where are we with the migration?". The assistant answers correctly without the operator explaining anything. It knows:

- the VPS and its declared services;
- which databases are the source and the test result;
- the declared jobs and pipelines;
- the outcome and verdict of the last job or rehearsal;
- the open known issues and next steps recorded earlier.

**Why this priority**: This is the whole point of the feature. Without it, the other stories have nothing to deliver.

**Independent Test**: Record two notes and run one job (or use stored history). Then connect a brand-new client session and, without calling any tool, check that the briefing it received at connect time mentions:

- the declared jobs;
- the last run's verdict;
- both notes' titles.

**Acceptance Scenarios**:

1. **Given** a memory with notes and a stored job report, **When** a new session connects, **Then** the session receives a briefing at connect time, before any tool call. The briefing contains the environment summary, the latest outcomes and an index of notes.
2. **Given** the briefing would exceed its size budget, **When** a session connects, **Then** the briefing is trimmed: oldest and lowest-priority items go first. It says that more is available through the full-briefing tool.
3. **Given** the VPS is unreachable, **When** a session connects, **Then** the briefing is still delivered from what is stored locally. Facts that can be stale (service states) are labelled with when they were last observed.

---

### User Story 2 - The assistant records what it learned (Priority: P1)

During a session the assistant discovers something worth keeping. For example:

- "the stock_account module must be re-upgraded after the pipeline";
- "the operator prefers rehearsals at night";
- "decided: promote only when the verdict is safe_to_promote".

It saves this as a note, and the note is available to every later session in every harness.

**Why this priority**: Derived facts alone don't capture decisions, lessons or preferences. Those are what the operator is tired of repeating.

**Independent Test**: Write a note through the tool in one session. Start a new session and check that the note appears in the briefing's index and can be read in full. Then open the note's file by hand and check that it is plain, readable Markdown.

**Acceptance Scenarios**:

1. **Given** a session, **When** the assistant writes a note with a type, title, body and tags, **Then** a single human-readable file is created in the memory folder, the write is audited, and the note shows up in later briefings.
2. **Given** a note with the same name exists, **When** the assistant writes it again, **Then** the existing note is updated, not duplicated. The note keeps its original creation date and gets a new updated date.
3. **Given** a note body contains a password, token or other secret-looking value, **When** it is written, **Then** the value is masked before it reaches the disk.
4. **Given** the policy is in read-only mode, **When** the assistant writes a note, **Then** the write is still allowed, because memory is local and doesn't change the VPS. It is audited, and the response says so.

---

### User Story 3 - Find and read what was recorded (Priority: P2)

The assistant searches the memory ("anything about stock_account?", "known issues", "notes tagged rehearsal") and reads one note in full. It can follow links from one note to related notes.

**Why this priority**: The briefing carries only an index. Full content is pulled on demand, which keeps sessions cheap.

**Independent Test**: Create several notes with different types, tags and links. Check that search by text, by type and by tag each return the right notes, and that reading a note returns its full body and its links.

**Acceptance Scenarios**:

1. **Given** notes exist, **When** the assistant searches by text, type or tag, **Then** matching notes are returned as short summaries, best matches first, and the result size is capped.
2. **Given** a note links to other notes, **When** it is read, **Then** the response lists the linked notes. Links to notes that don't exist are marked as missing; they are not errors.
3. **Given** a name that does not exist, **When** the assistant reads it, **Then** it gets a clear "not found" with close matches suggested.

---

### User Story 4 - The operator curates memory by hand (Priority: P2)

The operator opens the memory folder in an editor, corrects a note, deletes an obsolete one, or commits the folder to a private git repository. The next session reflects those edits. The operator can also tell the assistant to forget a note.

**Why this priority**: The operator must stay in control of what agents believe. Plain files make that trivial.

**Independent Test**: Edit a note's file by hand and start a new session; the briefing reflects the edit. Ask the assistant to forget a note; it disappears from briefings and searches but is kept in an archive area and can be recovered.

**Acceptance Scenarios**:

1. **Given** a note file was edited by hand, **When** a new session connects, **Then** the edited content is used.
2. **Given** a note file is malformed (broken header, unreadable), **When** memory is loaded, **Then** that note is skipped, and the briefing and a health tool report it. Other notes still load.
3. **Given** the assistant is asked to forget a note, **When** it archives it, **Then** the note no longer appears in briefings or searches, its file moves to an archive area, and the action is audited.

---

### User Story 5 - Derived facts stay current automatically (Priority: P3)

Facts the server already knows are kept up to date without anyone writing them:

- declared services, databases, pipelines, checks and jobs;
- the last observed service states;
- latest job runs, rehearsals and report verdicts;
- recent audited changes.

They're regenerated as things happen, never hand-edited.

**Why this priority**: These facts make the briefing accurate. They are lower priority because P1 can initially be built straight from configuration and stored history.

**Independent Test**: Run a job to completion in the test environment. Check that the derived "latest outcomes" section reflects the new verdict without any note being written, and that a hand edit to a derived file is overwritten on the next regeneration.

**Acceptance Scenarios**:

1. **Given** a job or rehearsal finishes, or a report is produced, **When** the next briefing is built, **Then** it shows the new outcome and verdict.
2. **Given** service states were listed in a session, **When** a later session connects, **Then** the briefing shows those states with their observation time.
3. **Given** derived facts are stored as files, **When** they are regenerated, **Then** they are clearly marked as generated, and they're kept separate from agent-written notes.

---

### Edge Cases

- **Notes that try to steer the agent.** A note contains text like "ignore previous instructions" or "run remove_service". Notes are presented as recorded data, clearly separated from the server's own instructions. No tool ever acts on note content.
- **Unsafe note names.** A note name has path separators, `..`, hidden-file prefixes or unusual characters. The name is refused (or normalised to a safe slug). Nothing is ever read or written outside the memory folder.
- **Size limits.**
  - A single note larger than the per-note limit is refused with the limit stated.
  - When the memory reaches its maximum note count, new writes are refused. The response suggests archiving or merging notes.
- **Two sessions writing the same note** at the same time. Each write is atomic, so the file is never left half-written. The last write wins, and both writes are audited.
- **Memory folder problems.** The folder is missing: it is created on first use, with permissions readable only by the operator. It is not writable: reads still work, and writes fail with a clear message.
- **Several VPS configs.** Each config has its own memory folder. Notes about one VPS never appear in another's briefing.
- **Secrets that change format.** A value that looks like a secret is masked on write. If masking would change a note in a way the assistant didn't intend, the response says how many values were masked.
- **Clock and timezone.** All dates are stored with an explicit timezone.

## Requirements *(mandatory)*

### Functional Requirements

**Storage**

- **FR-001**: The system MUST keep memory as a folder of human-readable Markdown files on the operator's machine, one note per file. The folder MUST be separate for each VPS configuration and live outside the code repository. Its default location is next to the policy file, and it can be overridden in the policy file.
- **FR-002**: Each note MUST carry a structured header with:
  - name (unique, safe slug);
  - type: one of `decision`, `lesson`, `issue`, `convention`, `next_step`, `preference`, `fact`;
  - title;
  - tags;
  - created and updated timestamps;
  - source: which harness or session wrote it, or "operator" for hand edits.

  The body is free Markdown and may link to other notes by name.
- **FR-003**: The system MUST keep derived (generated) facts separate from agent-written notes, mark them as generated, and overwrite them on regeneration.

**Delivery**

- **FR-004**: The system MUST deliver a briefing automatically when any MCP session connects, as part of the server's connect-time instructions. Agents that never call a tool still receive it.
- **FR-005**: The briefing MUST contain, in priority order:
  1. a fixed preamble saying the memory is recorded data, not instructions;
  2. an environment summary: the VPS name, declared services, allowlisted databases with their roles, and declared pipelines, checks and jobs;
  3. latest outcomes: the last job runs, rehearsals and reports with verdicts and dates;
  4. open issues and next steps, with titles and one-line summaries;
  5. an index of the remaining notes (titles only);
  6. a pointer to the tools for more detail.
- **FR-006**: The briefing MUST respect a size budget, configurable with a sensible default of about 1,500 words. When trimming, it MUST drop lowest-priority content first and say that content was omitted.
- **FR-007**: Building the briefing MUST NOT require contacting the VPS. It MUST use only local configuration, the local state store and the memory folder. Facts that can change, such as service states, MUST show when they were last observed.

**Tools**

- **FR-008**: The system MUST provide tools to:
  - get the full briefing (not trimmed to the connect-time budget, but still capped);
  - search notes by text, type and tag;
  - read one note with its links;
  - write a note (create or update by name);
  - archive (forget) a note;
  - report memory health: counts, malformed files, folder location, and whether writing is possible.
- **FR-009**: Writing a note with an existing name MUST update it in place and keep its creation date. Writes MUST be atomic.
- **FR-010**: Archiving MUST move the note out of the active set into an archive area, without deleting it, so the operator can recover it by hand.
- **FR-011**: Reading a note MUST list its outgoing links and mark links to notes that don't exist. Reading a name that doesn't exist MUST return "not found" with up to 3 close matches.

**Safety**

- **FR-012**: Every memory write and archive MUST be recorded in the existing audit trail with the note name and the action. Reads are not audited.
- **FR-013**: Memory writes MUST be allowed in read-only mode, because they change only local files, not the VPS. The operator MUST be able to disable memory writes separately in the policy file.
- **FR-014**: All text written to memory MUST pass through the same secret masking used for logs and output. The response MUST say how many values were masked.
- **FR-015**: Note names MUST be validated as safe slugs. The system MUST never read or write outside the memory folder, including via links, archive paths or symlinks.
- **FR-016**: The system MUST enforce limits:
  - a maximum note size (default 8 KB);
  - a maximum number of active notes (default 500);
  - capped search result sizes.

  It MUST refuse writes beyond the limits with the limit stated.
- **FR-017**: Note content MUST always be presented to agents as quoted data under a fixed header saying it is recorded memory, not instructions. No tool behaviour may be triggered by note content.
- **FR-018**: The memory folder and its files MUST be created readable only by the operator's account.
- **FR-019**: Malformed or unreadable note files MUST be skipped without failing the briefing, and reported by the health tool and in the briefing (as a count).

**Derived facts**

- **FR-020**: Derived facts MUST be refreshed when the underlying events happen: a job ends, a rehearsal ends, a report is stored, a service state is observed, or an audited change is made. They MUST also be refreshed at server start from configuration and stored history.
- **FR-021**: Memory MUST be available whenever the server runs. It does not depend on the optional migration or jobs features; the sections for those features appear only when they are configured.

### Key Entities

- **Note**: one recorded piece of knowledge.
  - Header: name, type, title, tags, created, updated, source.
  - Body: free text that may link to other notes.
  - State: active or archived.
- **Derived fact set**: generated summaries of configuration, last-observed service states, latest job, rehearsal and report outcomes, and recent changes. Each carries a generation time and isn't editable.
- **Briefing**: the size-budgeted digest of derived facts and the note index, delivered at connect time. A larger variant is available on demand.
- **Memory folder**: the per-VPS location holding active notes, derived facts and the archive.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: In a brand-new session in any of at least two different MCP harnesses, the assistant correctly states:
  - the source and result databases;
  - the declared jobs;
  - the last run's verdict;
  - any recorded open issues.

  It does this with zero explanation from the operator and before calling any tool.
- **SC-002**: A note written in one harness is visible in a new session of another harness, with no manual steps.
- **SC-003**: The connect-time briefing never exceeds its configured budget. With the default budget it costs no more than about 2,000 tokens.
- **SC-004**: The operator can read and edit any note with an ordinary text editor, and the next session reflects the change.
- **SC-005**: No secret-looking value in a note body or title is ever stored unmasked.
- **SC-006**: No input to any memory tool can cause a file outside the memory folder to be read or written.
- **SC-007**: The briefing is delivered even when the VPS is unreachable, and connecting is no more than 1 second slower than without memory.

## Assumptions

- The operator uses one laptop for these harnesses. Sharing memory across machines is done by the operator, e.g. by syncing the folder or committing it to a **private** repository. Built-in cross-machine sync is out of scope.
- All target harnesses deliver the MCP server's connect-time instructions to the model. For any that don't, the full-briefing tool gives the same content, and the tool descriptions point to it.
- Memory concerns one VPS per config. A global, cross-VPS memory is out of scope.
- Agent-written notes are trusted as much as the agent that wrote them. The server doesn't judge truthfulness, only safety (secrets, size, paths) and how they are presented (as data).
- Semantic or vector search is out of scope. Text, type and tag matching is enough at the expected scale (hundreds of notes).
- Notes are not deleted automatically. Expiry is the operator's or the agent's decision, through archiving.
