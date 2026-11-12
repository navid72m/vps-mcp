# Tasks: Lazy Tool Loading

**Input**: Design documents from `/specs/008-lazy-tool-loading/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/toolsets.md, quickstart.md

**Tests**: included. The spec's success criteria SC-002 to SC-005 are test-defined.

## Phase 1: Setup

- [X] T001 Capture the public vps-mcp tool names, descriptions and read-only flags into `tests/fixtures/vps_tools.json`. No arguments and no operator data.

## Phase 2: Foundational (blocks all stories)

- [X] T002 Add configuration in `src/assistant/config.py`:
  - a `ToolsSection`: `lazy=true`, `core=[]`, `max_auto=2` (1–5), `idle_turns=6` (≥1), `threshold=2.0` (>0);
  - on `ServerSection`: `core: list[str]` and `toolsets: dict[str, ToolsetSection(tools, description ≤120 chars, keywords)]`;
  - toolset names must match `^[a-z][a-z0-9-]{0,23}$`;
  - `Config.tools`.
- [X] T003 Add the `toolsets_active` table and accessors (`toolsets_active`, `toolset_load`, `toolset_used`, `toolset_unload`) and a monotonic `operator_turn(conversation)` counter (kept in `kv`, because old turns are folded away) to `src/assistant/state.py`. Add the audit kind `toolset`.
- [X] T004 Get the allowed, non-`never` tools per server plus availability. Done without a ToolHub change: `hub.specs()` already drops `never` tools and tools of servers that are down, and `hub.status()` gives availability.
- [X] T005 Create `src/assistant/toolsets.py`:
  - `Toolset` dataclass;
  - `build_catalog(servers, builtins, cfg) -> Catalog`, following the R3 precedence: configured toolsets, then core patterns, then built-in defaults, then `<server>`/`<server>-more` fallback; first match wins;
  - keyword weights from R4, with common stems dropped;
  - `Catalog.render()` (≤160 characters per line, sorted);
  - `Catalog.score(texts) -> list[(name, score, words)]` (idf, weights);
  - `Catalog.toolset_of(tool)`;
  - `Catalog.specs(core + loaded)` in stable order.
- [X] T006 [P] Unit tests in `tests/unit/pa/test_toolsets.py`:
  - precedence and fallback grouping;
  - `never` tools absent;
  - catalog line limits and stable order;
  - scoring with idf;
  - the threshold and `max_auto`;
  - unmatched-pattern warnings.

## Phase 3: User Story 1 - Small prompt for everyday questions (P1) 🎯 MVP

**Goal**: send only the core tools plus the catalog.

**Independent Test**: an everyday question uses ≤ 35% of the old tool-definition size.

- [X] T007 [US1] In `src/assistant/agent.py`:
  - the catalog goes in the system prompt, after the rules and before the memory core;
  - `tools()` returns core + loaded specs when `lazy` is on, and all specs when it is off;
  - the model-call audit outcome gains ` tools=<chars>`.
- [X] T008 [US1] Build the catalog on demand in `src/assistant/agent.py` (`Agent.catalog()`, cheap) from `hub.specs()`, `hub.status()` and the built-in specs, so it follows servers going up or down. Built-in defaults:
  - core: `memory__search`, `memory__get`, `schedule__add`, `schedule__list`, `assistant__switch_model`, `tools__load`;
  - `files`;
  - `schedules`.
- [X] T009 [P] [US1] Tests in `tests/unit/pa/test_lazy_flow.py`:
  - an everyday turn sends only the core tools plus the catalog;
  - `lazy=false` gives exactly the old list (SC-004);
  - the system prompt is byte-identical across turns.

## Phase 4: User Story 2 - Automatic prefetch (P1)

**Goal**: load the toolsets matched by the operator's words before the first model call.

**Independent Test**: specialised questions have the right toolset on the first call.

- [X] T010 [US2] In `src/assistant/agent.py` `run_turn`, before the first model call:
  - score the message plus the last two operator messages (weight 0.5);
  - load the top `max_auto` toolsets above the threshold;
  - audit `loaded: auto (<words>)`;
  - re-matching a loaded toolset refreshes its `loaded_turn`.
- [X] T011 [US2] Scheduled runs use the same prefetch on their prompt, in `src/assistant/app.py` `run_schedule` (through `chat_turn`, so no change is needed beyond verifying).
- [X] T012 [P] [US2] Prefetch-quality test in `tests/unit/pa/test_prefetch_quality.py`: 20 questions, 4 per toolset (gitlab, db, deploy, migration, files), against the fixture with the example config's groups. At least 80% must be hits, and plain chit-chat must load nothing.
- [X] T013 [P] [US2] Flow tests:
  - a GitLab question has `gitlab` tools on the first call;
  - a follow-up keeps them;
  - the audit reason lists the matched words.

## Phase 5: User Story 3 - Model-requested loading (P1)

**Goal**: the model can load what prefetch missed, within the same turn.

**Independent Test**: with prefetch off, the model loads a toolset and uses it in one turn.

- [X] T014 [US3] Add the `tools__load` core tool in `src/assistant/agent.py`:
  - validate names against the catalog: unknown names return an error with the list, unavailable ones return an error;
  - load the toolsets, audit `loaded: model`, and count it as a tool call;
  - recompute tools before every model call within the turn.
- [X] T015 [US3] Handle a call to an unloaded tool in `src/assistant/agent.py` `_run_tool`: if `Catalog.toolset_of(name)` is not loaded, load it, audit `loaded: called <tool>`, then handle the call normally (R5, revised). Unknown or `never` tools are refused as before.
- [X] T016 [US3] Calls to a loaded toolset's tools update `last_used_turn`.
- [X] T017 [P] [US3] Flow tests:
  - a model load followed by use in the same turn;
  - an unknown toolset gives an error;
  - an unloaded-tool call loads the toolset and runs the call, with approval for an `ask` tool;
  - a `never` tool is never offered or run (SC-005).

## Phase 6: User Story 4 - Unload and reset (P2)

- [X] T018 [US4] At turn start, unload toolsets idle for at least `idle_turns` (audit `unloaded: idle`) unless they are re-matched. A new conversation has an empty set, since rows are keyed by conversation id.
- [X] T019 [US4] Add a `/tools` command in `src/assistant/commands.py`, using the format in contracts/toolsets.md, and add it to HELP.
- [X] T020 [P] [US4] Tests: idle unload after N turns, `/new` resets to the core, and `/tools` output.

## Phase 7: User Story 5 - Operator-controlled grouping (P2)

- [X] T021 [US5] In `src/assistant/cli.py`, `assistant check` prints the `tools:` line (lazy state, core count, toolset count, lazy and full sizes) and warnings for patterns that match nothing.
- [X] T022 [US5] In `examples/assistant.toml`, add the `[tools]` section and the vps-mcp `core` plus toolsets from R3, with descriptions and keywords.
- [X] T023 [P] [US5] Tests: configured core and toolsets are honoured, leftovers go to `<server>-more`, and an unmatched pattern produces a warning.

## Phase 8: Polish

- [X] T024 [P] README: a "Tools and context" subsection in the Personal assistant section, and `/tools` in the command table.
- [X] T025 Run ruff, ruff format, mypy and the full pytest suite. Do the privacy grep.
- [X] T026 Apply the vps-mcp groups to the operator's local `~/.config/assistant/assistant.toml`, run `assistant check`, and record the SC-001 measurement.
- [ ] T027 Live check, following quickstart §3, with the operator.

## Dependencies & Execution Order

- **Setup and foundation**: T001 → T002–T005 → T006.
- **US1**: T007–T009, needs the foundation.
- **US2 and US3**: need US1. They can be built in either order.
- **US4 and US5**: need US1. US4 needs the state accessors from T003.
- **Polish**: last.

## Parallel Opportunities

- T006, T009, T012, T013, T017, T020, T023 and T024 touch separate test or doc files.

## Implementation Strategy

- **MVP**: US1, which cuts the size of every call.
- **Then**: US2 and US3 together, which recover the specialised questions with no or one extra step.
- **Then**: US4 and US5, which keep long conversations lean and let the operator tune the groups.
