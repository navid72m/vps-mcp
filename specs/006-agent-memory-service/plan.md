# Implementation Plan: Agent Memory Service

**Branch**: `006-agent-memory-service` | **Date**: 2026-11-12 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/006-agent-memory-service/spec.md`

## Summary

`agent-memory` is a standalone memory service that any MCP client can connect to, and that vps-mcp uses as a library.

**Memories**

- Each memory is one Markdown file with structured front matter: type, scope, subject, provenance, confidence, importance, lifecycle status and links.
- Files are grouped by scope in a store that only the operator can read.
- A SQLite FTS5 index can be rebuilt from the files at any time and is checked for staleness on use.

**Writes**

- Every save passes a deterministic write policy: schema, secrets, untrusted-origin and instruction checks, duplicate confirmation, and contradiction flagging.
- Decisions are only replaced by an explicit supersede.

**Retrieval**

- Hybrid ranking combines keyword, recency, importance, confidence and scope.
- Results come back grouped by type, with staleness and conflict flags.
- Only a ~500-token pinned core is delivered at connect.

**Interface and tooling**

- Five tools, a CLI (serve, save, search, doctor, migrate-004, bench) and a read-only benchmark.
- vps-mcp's feature-004 memory is replaced. Its generated facts become live-state memories that are updated in place.

Decisions A1–A15 are in [research.md](./research.md).

## Technical Context

**Language/Version**: Python 3.12+. It is a new package `agent_memory` in the same distribution, with its own console script.

**Primary Dependencies**: none new. It uses `sqlite3` with FTS5 (present in both the system and uv builds), `fcntl`, and the MCP SDK already in use. Front matter is parsed by the restricted parser moved over from feature 004.

**Storage**: Markdown files under `~/.local/share/agent-memory` (configurable), `events/*.jsonl`, and a rebuildable `index.sqlite`.

**Testing**: pytest throughout:

- pure tests for validation, ranking, the parser and migration mapping;
- store tests on `tmp_path`, with a fake clock for expiry and staleness;
- MCP flow tests through the in-process `Client`;
- two-process concurrency tests;
- the 30-case write-policy suite;
- negative tests for path and scope escapes and poisoning;
- benchmark threshold assertions (SC-001–SC-003);
- a performance test with 5,000 memories (SC-007).

**Target Platform**: the operator's laptop (macOS/Linux).

**Project Type**: a library plus a CLI plus an MCP server, living in the existing repo.

**Performance Goals**:

- search ≤ 300 ms and full index rebuild ≤ 10 s at 5,000 memories;
- staleness check ≤ 50 ms per search.

**Constraints**:

- The service has no LLM inside.
- No vector search.
- Raw secrets are never written.
- The agent cannot raise trust through MCP.
- vps-mcp degrades gracefully when memory is unavailable.

**Scale/Scope**: up to about 10,000 memories, several concurrent client processes, one operator.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

The constitution is still the unfilled template, so there are no ratified gates. **PASS**.

The project defaults apply:

- **Least privilege**: an operator-only store; scope allowlists; operator-only trust elevation.
- **Testability**: deterministic validation and ranking, and a benchmark.
- **Simplicity**: no new dependencies, and files as the source of truth.
- **Observability**: an append-only events log and `doctor`.

**Post-design re-check: PASS.** These risks remain and are documented:

1. Contradiction detection is subject-key based. Two memories about the same thing under different subjects aren't caught. This is mitigated by subject-key guidance in tool descriptions and by `doctor` listing near-duplicate subjects.
2. An agent can still save a wrong fact from a trusted-looking source. This is mitigated by provenance, confidence, conflict flags and operator curation.

## Project Structure

### Documentation (this feature)

```text
specs/006-agent-memory-service/
├── plan.md  research.md  data-model.md  quickstart.md
├── contracts/memory-tools.md
├── checklists/requirements.md
└── tasks.md                  # /speckit-tasks
```

### Source Code (repository root)

```text
src/agent_memory/
├── __init__.py            # Memory facade, public API
├── model.py               # MemoryRecord, enums, field rules, scope parsing, slug/id (data-model §1)
├── frontmatter.py         # moved from vps_mcp.memory (restricted YAML subset)
├── files.py               # store layout, safe paths, O_NOFOLLOW reads, atomic writes, archive, flock (A2, A15)
├── policy.py              # validation: schema, secrets, origin/instruction, confidence cap (A7, A8)
├── similarity.py          # normalisation + Jaccard; duplicate/conflict decisions (A6)
├── index.py               # SQLite FTS5 index, incremental sync, rebuild, LIKE fallback (A4)
├── ranking.py             # hybrid score, grouping, flags (A5)
├── events.py              # events jsonl: append, read history (A14)
├── store.py               # Memory: search/get/save/update/archive/upsert_generated/pinned_core/doctor
├── core_payload.py        # pinned core rendering + escaping (A9)
├── mcp_tools.py           # 5 tools (contracts/memory-tools.md)
├── server.py              # `agent-memory serve`
├── migrate004.py          # migration (A12)
├── bench/                 # benchmark runner + fixtures.json (A13)
└── cli.py                 # agent-memory CLI
src/vps_mcp/
├── memory/                # becomes an adapter: service.py (generated facts via upsert_generated, pinned core),
│                          #   old modules removed/replaced; tools.py delegates to agent_memory.mcp_tools
├── config/policy.py       # [memory]: enabled, store, project, pinned_chars (path kept as legacy → migration hint)
└── server.py              # instructions = base + pinned core; register agent_memory tools; refresh hook unchanged

tests/
├── unit/agent_memory/     # model, policy (30 cases), similarity, index, ranking, store lifecycle, events,
│                          #   core payload, mcp flow, migration, bench thresholds, concurrency, perf
├── unit/memory/           # rewritten for the vps-mcp adapter (generated facts, degradation)
└── negative/test_agent_memory_disallowed.py
```

**Structure Decision**: `agent_memory` stands alone and doesn't depend on `vps_mcp`. vps-mcp keeps the feature-004 behaviours that still fit: the generated-facts refresh hook and graceful degradation. Its own note store and the six feature-004 tools are replaced by the five agent-memory tools.

## Implementation Phasing (for /speckit-tasks)

1. **Foundation**: model and field rules; the front-matter move; files and locking; the events log; index sync and rebuild.
2. **US2 + US3 (P1)**: policy validation, similarity, save with duplicate and conflict handling, update actions, expiry and staleness, archive, plus the 30-case suite.
3. **US1 (P1)**: ranking, search grouping and flags, the "nothing relevant" response, include-history.
4. **US6 (P2)**: `get` with provenance and history.
5. **US5 (P2)**: the pinned core.
6. **US4 (P2)**: the MCP server and tools, scope allowlist, the CLI (serve/save/search/doctor), and the vps-mcp adapter (generated facts, tools and instructions, degradation).
7. **US8 (P2)**: `migrate-004`.
8. **US7 (P3)**: benchmark fixtures and runner, plus threshold tests.
9. **Polish**:
   - negative suite;
   - 5,000-memory performance test;
   - README sections, rewriting the Memory section as agent-memory;
   - updating USAGE_EXAMPLES for the memory tools;
   - the example config;
   - live migration of the operator's notes, as a dry run first.

## Complexity Tracking

There are no constitution violations. The two residual risks above are documented.
