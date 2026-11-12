# Feature Specification: Lazy Tool Loading

**Feature Branch**: `008-lazy-tool-loading`

**Created**: 2026-11-12

**Status**: Draft

**Input**: User description: "Come up with a strategy to handle context so that not all of the tools are loaded into the context every time. The assistant should lazy-load tools in an intelligent way so the context doesn't fill up easily."

## Context

The assistant (feature 007) offers the model every allowed tool on every model call. Today that is 64 tools, about 42,000 characters (roughly 10,500 tokens), sent before the operator's message. Most messages need between zero and three tools.

The cost shows up as:

- slower answers;
- hitting free-tier rate and size limits sooner;
- less room for conversation history;
- worse tool choice, because the model must pick from a long list of look-alike tools.

As more MCP servers are connected, the problem grows with each one.

This feature keeps a small set of tools always available, shows the rest as a short catalog grouped into toolsets, and loads a toolset's full tool definitions only when the conversation needs them. Loading happens either automatically from the operator's words or on request by the model.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Everyday questions use a small prompt (Priority: P1)

The operator asks everyday things such as "Is the server OK?", "What's on my schedules?" or "Remind me in 20 minutes". The assistant answers using a small core set of tools. It does not send the descriptions of dozens of unrelated tools (GitLab, database comparison, migration rehearsal, deployment) with every message.

**Why this priority**: This is where most of the size and speed win comes from. Most messages fall here.

**Independent Test**: Send three everyday questions. Each is answered correctly with tools. For each model call, the tool definitions sent are at most 35% of the size sent before this feature, measured from the recorded per-call size.

**Acceptance Scenarios**:

1. **Given** the default configuration, **When** the operator asks "Is the server OK?", **Then** the model can call the service-status tools without any loading step, and the call carries only the core tools plus the catalog.
2. **Given** any model call, **When** it is made, **Then** the model is told which toolsets exist and what each is for, in one short line per toolset, even when that toolset is not loaded.
3. **Given** a tool's policy is `never`, **When** the catalog or any toolset is shown, **Then** that tool does not appear anywhere.

---

### User Story 2 - Specialised questions load what they need automatically (Priority: P1)

The operator asks "Why did the last pipeline fail on the addons repo?" or "Compare the two databases' modules". Before the model is called, the assistant recognises that the message is about GitLab or databases and loads those toolsets. The model can then use the right tools on its first step, without an extra round trip.

**Why this priority**: Without automatic loading, lazy loading would add a step to every specialised question and make the assistant feel slower, not faster.

**Independent Test**: Send five specialised questions, one per toolset. For at least four of them, the needed toolset is already loaded on the first model call. All five are answered correctly.

**Acceptance Scenarios**:

1. **Given** a message mentioning pipelines, merge requests, a repository or GitLab, **When** the turn starts, **Then** the GitLab toolset is loaded before the first model call.
2. **Given** a message that matches no toolset, **When** the turn starts, **Then** no extra toolset is loaded.
3. **Given** a follow-up such as "and the one before that?", **When** it arrives, **Then** toolsets used in the last few turns stay loaded, so the follow-up still works.
4. **Given** a toolset was loaded automatically, **When** the audit trail is read, **Then** it shows which toolset was loaded and why ("matched: pipeline, gitlab").

---

### User Story 3 - The model can load a toolset itself (Priority: P1)

The automatic match misses: "Can you check whether last night's run broke anything?" really needs the migration toolset. The model sees from the catalog that a migration toolset exists, asks for it, and continues in the same turn with those tools available.

**Why this priority**: The safety net that makes lazy loading safe to rely on. Without it a missed match means a wrong or "I can't" answer.

**Independent Test**: With automatic loading turned off, ask a question that needs a non-core toolset. The model loads the toolset, then uses one of its tools and answers correctly, all within one turn.

**Acceptance Scenarios**:

1. **Given** a toolset is not loaded, **When** the model asks to load it, **Then** its tools are available to the model from its next step in the same turn.
2. **Given** the model asks to load an unknown toolset, **When** the request is handled, **Then** it gets an error listing the available toolsets, and nothing changes.
3. **Given** the model calls a tool that exists and is allowed but whose toolset is not loaded, **When** the call arrives, **Then** that toolset is loaded and the call is handled normally. Policy and approvals apply as for any call, and the tool's own argument checks reject a malformed call.
4. **Given** the model calls a tool that does not exist or has policy `never`, **When** the call arrives, **Then** it is refused exactly as today.

---

### User Story 4 - Loaded tools come and go with the conversation (Priority: P2)

During a long conversation that moves from GitLab to schedules to the weather, toolsets that have not been used for a while are unloaded, so the prompt shrinks again. Starting a new conversation starts from the core set.

**Why this priority**: Keeps long conversations from drifting back to "everything loaded".

**Independent Test**: Load the GitLab toolset, then send several messages that don't use it. After the configured number of turns without use, it is no longer sent. `/new` resets to the core set.

**Acceptance Scenarios**:

1. **Given** a toolset was last used or loaded N turns ago (default 6), **When** the next turn starts, **Then** it is unloaded unless the new message matches it again.
2. **Given** the operator sends `/new` or the conversation ends, **When** the next conversation starts, **Then** only the core set is loaded.
3. **Given** the operator sends `/tools`, **When** it is handled, **Then** the reply lists toolsets, their tool counts, which are loaded now, and the approximate size of the tools section of the prompt.

---

### User Story 5 - The operator controls the grouping (Priority: P2)

The operator can choose which tools are always available and how a server's tools are grouped into toolsets, in the configuration file. Sensible groups for the bundled servers and built-in tools work without any configuration.

**Why this priority**: Different operators use different tools daily. New MCP servers must fit in without code changes.

**Independent Test**: Add a configuration that moves `db_overview` into the core set and defines a custom toolset. After a restart, `db_overview` is available on every call and the custom toolset appears in the catalog.

**Acceptance Scenarios**:

1. **Given** a server with no grouping configured, **When** the assistant starts, **Then** that server's tools form one toolset named after the server, described by its tools' names.
2. **Given** a grouping that names a tool which does not exist, **When** the configuration is checked, **Then** a warning names it. Startup still succeeds.
3. **Given** a tool matches no configured group, **When** the assistant starts, **Then** it goes into a "more <server> tools" toolset rather than being lost.
4. **Given** lazy loading is turned off in the configuration, **When** the assistant runs, **Then** every allowed tool is sent on every call, exactly as before this feature.

### Edge Cases

- **Many toolsets matched by one message.** Automatic loading adds at most a configured number of toolsets per turn (default 2), the best matches first. The model can still load more itself.
- **Tool loading interacts with approvals.** Loading a toolset never changes a tool's policy. `ask` tools still ask, `never` tools are never shown, and approvals are bound to the exact call as before.
- **Loop of loading.** The model asks to load toolsets repeatedly. Loads count against the turn's tool-call budget like any other tool call.
- **Scheduled runs.** A schedule's prompt is matched like a message, so a scheduled "server briefing" loads what it needs.
- **Model switching.** Loaded toolsets belong to the conversation, not the model, so switching models mid-conversation keeps them.
- **A server is down.** Its toolsets are listed in the catalog as unavailable. Asking to load one returns that error.
- **Byte-identical prompt prefix.** The system prompt and the always-loaded part of the tool list stay identical between calls, so providers that cache prompt prefixes still benefit. Loaded toolsets come after the core tools, in a stable order.

## Requirements *(mandatory)*

### Functional Requirements

**Catalog and core**

- **FR-001**: Every allowed tool MUST belong to exactly one toolset, or to the core set.
- **FR-002**: Core tools MUST be sent on every model call.
- **FR-003**: A non-core toolset's tool definitions MUST be sent only while that toolset is loaded for the conversation.
- **FR-004**: Every model call MUST include a catalog: one line per toolset with its name, tool count, availability and a short description of what it is for. Each catalog line MUST be at most 160 characters.
- **FR-005**: Tools with policy `never` MUST NOT appear in the core set, any toolset or the catalog.

**Loading**

- **FR-006**: The model MUST have a tool to load one or more toolsets by name. Loaded tools MUST be available from the model's next step in the same turn.
- **FR-007**: Before the first model call of a turn, the assistant MUST score each toolset against the operator's current message, using keyword matching with stemming. Earlier messages are not matched: a follow-up finds its toolset because it is still loaded, and earlier words must not keep a finished topic's tools around (found in the live test). It MUST load the best-scoring toolsets above a threshold, at most the configured number per turn (default 2). This step MUST NOT call a model.
- **FR-008**: Each toolset MUST have matching keywords:
  - derived automatically from its tools' names and descriptions;
  - optionally extended with configured keywords.
- **FR-009**: A call to an existing, allowed tool whose toolset is not loaded MUST load that toolset and then be handled exactly like a call to a loaded tool, with the same policy, approvals and argument checks.

**Unloading**

- **FR-010**: Loaded toolsets MUST be remembered per conversation across turns and restarts.
- **FR-011**: A loaded toolset that was never used MUST be unloaded after M operator turns (default 2). One that was used MUST be unloaded after N turns without use (default 6).
- **FR-012**: A new conversation MUST start with no loaded toolsets.

**Configuration**

- **FR-013**: The configuration MUST allow, per server:
  - a list of core tool patterns;
  - named toolsets as tool patterns, with an optional description and keywords.

  The built-in tools (files, schedules, model switching) MUST have default groups.
- **FR-014**: Unconfigured tools MUST be grouped as one toolset per server. For vps-mcp, defaults MUST be provided in the example configuration.
- **FR-015**: Lazy loading MUST be switchable off, globally and per model. When off, behaviour MUST be identical to before this feature. Switching models with `/model` switches the strategy with it (a large, fast model can take every tool; a small or local one answers faster with fewer).

**Policy, audit and visibility**

- **FR-016**: Loading MUST NOT change any tool's policy, approval or audit behaviour.
- **FR-017**: Every load and unload MUST be audited with the toolset name and the reason:
  - automatic, with the matched words;
  - requested by the model;
  - triggered by a call to an unloaded tool;
  - unloaded after going idle.
- **FR-018**: Each model call's audit record MUST include the approximate size of the tool definitions sent.
- **FR-019**: A `/tools` chat command MUST show:
  - the toolsets, and which are loaded;
  - the core tools;
  - the current size of the tools section.

**Prompt stability**

- **FR-020**: The tool list order MUST be stable: core tools first, then loaded toolsets in catalog order, each toolset's tools in a fixed order.

### Key Entities

- **Toolset**: a named group of tools from one server or the built-ins. It has:
  - a description (one line) and matching keywords;
  - availability (its server is up or down).
- **Core set**: the tools sent on every call.
- **Loaded toolsets (per conversation)**: which toolsets are loaded, when each was last used or loaded, and why it was loaded.
- **Catalog**: the rendered list of toolsets shown to the model.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: With the default configuration and the operator's current servers, the tool definitions sent on an everyday question are at most 35% of their size before this feature. That is a cut of 65% or more, from about 10,500 tokens to at most 3,700.
- **SC-002**: On a test set of 20 specialised questions (4 per toolset for GitLab, databases, deploy, migration and files), the needed toolset is already loaded on the first model call for at least 80% of them.
- **SC-003**: On the same 20 questions, at least 95% are answered with the right tool, with automatic loading and model-requested loading combined. This is no worse than with all tools loaded.
- **SC-004**: Turning lazy loading off restores the exact previous tool list. All existing tests pass unchanged with it on.
- **SC-005**: No `never` tool is ever offered or run, and no `ask` tool runs without an approval, with or without lazy loading. This is covered by tests.

## Assumptions

- Keyword matching with stemming, the same approach agent-memory already uses for search, is good enough for prefetch. Semantic embeddings are out of scope. The model-requested load is the fallback for misses.
- **Default core set:**
  - memory search and get;
  - schedule add and list;
  - the model-switch tool;
  - the load-toolset tool;
  - from vps-mcp: list_services, get_service_status, get_resource_usage.

  This covers the most common phone questions.
- **Default toolsets for vps-mcp:**
  - gitlab: the gitlab_* tools;
  - db: db_*;
  - deploy: deploy, rollback, update, configure and deploy targets;
  - migration: rehearsals, checks, reports, pipeline runs and copies;
  - jobs;
  - services: start, stop, restart, boot start, logs, config, remove, audit log, revisions;
  - memory admin: vps-mcp's own memory tools.
- **Built-in toolsets:** files (all file tools), schedules (remove, pause, resume).
- The model-requested load is itself a core tool and counts against the per-turn tool budget.
- The catalog sits in the system prompt and is stable for a given configuration and set of available servers.

## Out of Scope

- Semantic or embedding-based tool retrieval, or a second model call to pick tools.
- Shortening or rewriting the MCP servers' own tool descriptions.
- Lazy-loading MCP resources or prompts. Only tools are covered.
- Image support and reminder Taken/Snooze buttons, which are separate features.
