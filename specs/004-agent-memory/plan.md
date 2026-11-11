# Implementation Plan: Agent Memory

**Branch**: `004-agent-memory` | **Date**: 2026-11-11 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/004-agent-memory/spec.md`

## Summary

vps-mcp becomes the place where knowledge about the VPS and the migration pipeline lives, so every harness connecting to it gets that knowledge.

- **Storage**:
  - a per-config folder of Markdown notes with front matter, on the laptop and outside the repo;
  - generated files for derived facts.
- **Delivery**:
  - a size-budgeted **briefing** is put into the MCP `instructions` at server start. Stdio servers start once per session, so every new session in any harness receives it before any tool call;
  - six `memory_*` tools search, read, write, archive and inspect notes.
- **Derived facts** are rebuilt from config and the local state DB after relevant tool calls.
- **Safety**:
  - masking: the existing masking plus token and URL patterns;
  - paths: slug-only names, no symlinks, atomic writes;
  - presentation: notes are shown as quoted data;
  - audit: writes are audited, with a separate `writable` switch.

Decisions M1–M12 are in [research.md](./research.md).

## Technical Context

**Language/Version**: Python 3.12+, in the same package.

**Primary Dependencies**: none new. Front matter is parsed with a restricted, hand-written parser (M4).

**Storage**:

- Markdown files under `<config dir>/<stem>.memory/` (`notes/`, `generated/`, `archive/`).
- SQLite schema v4 adds `service_observations`.

**Testing**: pytest with `tmp_path` memory folders. The suites are:

- pure tests for the parser, masking, slugs, scoring and the trimmer;
- a flow test through the in-process `Client(server)` that checks `instructions` content;
- a negative suite (traversal, symlinks, sizes, injection, writes switch).

**Target Platform**: the operator's laptop (macOS/Linux). The VPS isn't involved.

**Project Type**: an extension of the single package, as a new `vps_mcp.memory` subpackage.

**Performance Goals**:

- briefing build ≤ 150 ms with 500 notes and no network (SC-007);
- search ≤ 200 ms over 500 notes.

**Constraints**:

- connect-time briefing ≤ `briefing_chars` (default 6000);
- note ≤ 8 KB;
- ≤ 500 active notes;
- the VPS is never contacted for memory.

**Scale/Scope**: tens to a few hundred notes per VPS; 1–3 harnesses on one laptop.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

The constitution is still the unfilled template, so there are no ratified gates. **PASS**.

The engineering defaults from features 001–003 apply:

- **Least privilege**: no new VPS rights, and files are 0600.
- **Testability**: pure functions for parsing, masking, ranking and trimming.
- **Simplicity**: plain files, no dependency, no daemon.
- **Observability**: writes are audited, and `memory_health` reports problems.

**Post-design re-check**: PASS. The residual risk that remains is documented: an agent can write wrong facts into memory. This is mitigated by:

- showing memory as data;
- recording the `source` of each note;
- operator curation;
- audit.

## Project Structure

### Documentation (this feature)

```text
specs/004-agent-memory/
├── plan.md  research.md  data-model.md  quickstart.md
├── contracts/ (memory-tools.md, memory-config.md)
├── checklists/requirements.md
└── tasks.md                  # /speckit-tasks
```

### Source Code (repository root)

```text
src/vps_mcp/
├── config/policy.py          # + MemorySettings ([memory]); AppConfig.memory with defaults
├── config/loader.py          # resolve default memory path from config file location
├── memory/
│   ├── __init__.py
│   ├── frontmatter.py        # restricted front-matter parse/render (M4), pure
│   ├── notes.py              # Note model, slug rules, validation, link extraction, pure
│   ├── masking.py            # mask_note(): existing masking + token/URL/PEM patterns (M8), pure
│   ├── folder.py             # MemoryFolder: bootstrap, safe paths, O_NOFOLLOW reads, atomic writes,
│   │                         #   load/scan (malformed), archive (M5, M9, M10)
│   ├── search.py             # scoring + snippets (data-model §6), pure
│   ├── derived.py            # build environment/outcomes/changes from config + state DB (M6, M7)
│   ├── briefing.py           # sections P0–P6, escaping, budgeted trim (M3, M11), pure
│   ├── service.py            # MemoryService: briefing(), search/read/write/archive/health, refresh()
│   └── tools.py              # 6 MCP tools
├── store/db.py               # schema v4: service_observations
├── store/audit.py            # + memory_write, memory_archive
├── server.py                 # instructions = base + briefing; register memory tools; refresh hook in wrapper
└── cli.py                    # check-config prints memory folder + note count + warnings

tests/
├── unit/memory/              # frontmatter, notes, masking, search, briefing, folder, derived, flow
└── negative/test_memory_disallowed.py
```

**Structure Decision**:

- Memory is its own subpackage and is independent of migration and jobs (FR-021). `derived.py` reads the migration and job tables only when those features are configured.
- `server.py` gets three small changes:
  - build the instructions;
  - register the tools;
  - call `refresh()` in `_tool_wrapper` for tools in the refresh set.

## Implementation Phasing (for /speckit-tasks)

1. **Foundation**:
   - `MemorySettings` and the default path;
   - schema v4;
   - the audit actions;
   - `MemoryFolder` (bootstrap, safe I/O);
   - the front-matter parser and notes model.
2. **US1 (P1)**:
   - derived environment and outcomes from config and the DB (without observations yet);
   - the briefing builder and trimmer;
   - instructions at startup;
   - `memory_briefing`;
   - the flow test that checks `instructions`.
3. **US2 (P1)**:
   - masking;
   - `memory_write` (create/update, `expected_updated`, limits, audit, writes switch, read-only behaviour).
4. **US3 (P2)**: `memory_search`, then `memory_read` with links, backlinks and suggestions.
5. **US4 (P2)**: `memory_archive` (preview/confirm); malformed handling; `memory_health`.
6. **US5 (P3)**:
   - `service_observations` upsert;
   - the refresh hook in the wrapper;
   - writing `generated/*.md` only when the content changed.
7. **Polish**:
   - negative suite;
   - `check-config` output and warnings;
   - README "Memory" section;
   - `examples/vps-mcp.toml` `[memory]` block;
   - live validation in two harnesses (quickstart §3–4).

## Complexity Tracking

There are no constitution violations.
