# Implementation Plan: Lazy Tool Loading

**Branch**: `008-lazy-tool-loading` | **Date**: 2026-11-12 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/008-lazy-tool-loading/spec.md`

## Summary

A new module, `assistant/toolsets.py`, sorts every allowed tool into either:

- the **core set**, sent on every call; or
- a named **toolset**, sent only while loaded.

The groups come from built-in defaults, per-server configuration, and a "one toolset per server" fallback.

The system prompt gets a short, stable **catalog**: one line per toolset.

Before each turn, three things happen:

1. Toolsets idle for N turns are unloaded.
2. The operator's message and the two before it are scored against each toolset's keywords, using agent-memory's existing stemming. The best matches above a threshold are loaded, with no model call.
3. The model can load more at any point through a core `tools__load` tool.

A call to a tool that exists but isn't loaded loads its toolset and asks the model to repeat the call.

The loaded set lives in SQLite per conversation, so it survives restarts and resets on `/new`.

Two things don't change:

- **Policy.** The hub's `never` filtering, approvals and previews are untouched. Lazy loading only decides which of the already-allowed specs are sent.
- **Behaviour when off.** `[tools] lazy = false` gives exactly the old tool list.

Decisions are R1–R8 in [research.md](./research.md).

## Technical Context

**Language/Version**: Python 3.12+, in the same repository and package (`src/assistant`).

**Primary Dependencies**:
- no new dependencies;
- reused: `agent_memory.similarity` (`words`, `stem`, `STOPWORDS`) for keyword matching.

**Storage**: one new SQLite table in `assistant.db`: `toolsets_active`. It is created idempotently, like the other tables.

**Testing**: pytest with the existing harness (FakeTelegram, ScriptedProvider, the in-process `ops` MCP server). A prefetch-quality test covers SC-002 with 20 fixed questions against a fixture of vps-mcp's public tool names and descriptions.

**Target Platform**: unchanged (macOS laptop; Linux later).

**Project Type**: single Python project, a feature inside the existing `assistant` package.

**Performance Goals**:
- tool definitions on an everyday call ≤ 35% of today's 41,994 characters (SC-001);
- prefetch scoring < 5 ms per turn.

**Constraints**:
- the prompt prefix (system prompt plus core tools) stays byte-identical between calls with the same configuration;
- no model calls for tool selection;
- `lazy = false` reproduces the previous tool list exactly.

**Scale/Scope**: about 70 tools across about 12 toolsets now. The design must stay workable at several hundred tools from several servers.

## Constitution Check

`.specify/memory/constitution.md` is still the unfilled template, so there are no formal gates. The project's standing rules from features 001–007 are applied:

| Rule | How this plan meets it |
|---|---|
| Safety model unchanged (allowlist → policy → approval) | Lazy loading filters the hub's already-filtered specs. `never` tools are dropped before grouping (FR-005). Approvals are untouched (FR-016). |
| Everything audited | New audit kind `toolset` for load and unload with the reason. Model-call records gain the tool-section size (FR-017, FR-018). |
| Untrusted text is data | Toolset names a model asks for are validated against the catalog. Unknown names are an error (US3-2). |
| Public repo, nothing private | Defaults and examples use only generic tool names. The operator's groups live in their own config. |
| Off switch, no regressions | `lazy = false` reproduces the old list, tested (SC-004). |

Re-checked after design: no violations.

## Project Structure

### Documentation (this feature)

```text
specs/008-lazy-tool-loading/
├── spec.md
├── plan.md              # this file
├── research.md          # R1–R8
├── data-model.md
├── quickstart.md
├── contracts/
│   └── toolsets.md      # config keys, tools__load, catalog format, /tools, audit
├── checklists/requirements.md
└── tasks.md             # /speckit-tasks
```

### Source Code (repository root)

```text
src/assistant/
├── toolsets.py      # NEW: Toolset, Catalog (grouping, keywords, scoring), ActiveSets
├── config.py        # [tools] section; per-server core/toolsets
├── state.py         # toolsets_active table + accessors; audit kind "toolset"
├── agent.py         # per-turn: unload idle → prefetch → tools = core + loaded; tools__load;
│                    # unloaded-tool call → load + retry message; catalog in system prompt
├── app.py           # builds the catalog from hub + built-ins; built-in default groups
├── commands.py      # /tools
├── cli.py           # `assistant check` reports tool-section sizes and unmatched patterns
└── toolhub.py       # expose the HubTool list per server (for grouping); specs unchanged

examples/assistant.toml                 # [tools] and vps-mcp toolsets
tests/unit/pa/test_toolsets.py          # grouping, scoring, catalog, persistence
tests/unit/pa/test_lazy_flow.py         # turn flows: prefetch, model load, retry, idle unload, /new
tests/unit/pa/test_prefetch_quality.py  # SC-002 on 20 questions
tests/fixtures/vps_tools.json           # public names + descriptions of vps-mcp tools
```

**Structure Decision**: one new module inside `src/assistant`, plus small changes to existing modules. Grouping and scoring are pure functions, so they're unit-tested without the turn loop.

## Complexity Tracking

No constitution violations to justify.
