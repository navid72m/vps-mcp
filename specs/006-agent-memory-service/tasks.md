---

description: "Task list for Agent Memory Service"
---

# Tasks: Agent Memory Service

**Input**: Design documents from `/specs/006-agent-memory-service/`

**Prerequisites**:

- [plan.md](./plan.md)
- [spec.md](./spec.md)
- [research.md](./research.md) (A1–A15)
- [data-model.md](./data-model.md)
- [contracts/memory-tools.md](./contracts/memory-tools.md)
- [quickstart.md](./quickstart.md)

**Tests**: Included. The spec requires:

- a write-policy suite with 30 cases (SC-004);
- benchmark thresholds (SC-001–SC-003);
- performance at 5,000 memories (SC-007).

Write each story's tests first.

**Organization**: Grouped by user story. The P1 stories run in this order:

1. US2 (save safely) and US3 (lifecycle), together, since they share the write path;
2. US1 (retrieval).

Paths are relative to the repo root.

**Privacy**: fixtures use generic names only: projects `shop`, `blog` and `infra`; environments `prod` and `staging`.

## Format: `[ID] [P?] [Story] Description`

---

## Phase 1: Setup

- [X] T001 Create the package `src/agent_memory/__init__.py` and the console script `agent-memory = "agent_memory.cli:main"` in `pyproject.toml`. Add the mypy and ruff package config if it is needed. Create `tests/unit/agent_memory/__init__.py` and `conftest.py` with:
  - a `store` fixture (`tmp_path / "mem"`);
  - a `clock` fixture (a settable UTC datetime injected into the store);
  - a `mem` fixture (`Memory.open(store, project="shop", environment="prod", client="test", now=clock)`).

---

## Phase 2: Foundational (Blocking Prerequisites)

**⚠️ CRITICAL**: No user story can start until this phase is complete.

- [X] T002 Move `src/vps_mcp/memory/frontmatter.py` to `src/agent_memory/frontmatter.py`, unchanged, together with its tests. In `vps_mcp.memory.frontmatter`, keep a one-line re-export so existing imports still work until the adapter phase.
- [X] T003 [P] Implement `src/agent_memory/model.py` (data-model §1):
  - the enums `TYPES`, `STATUSES`, `CONFIDENCE`, `TRUSTED_SOURCES`, `UNTRUSTED_SOURCES`;
  - `MemoryRecord` (a dataclass with every front-matter field plus `content`, `path` and `extra`);
  - `parse_scope(str)` and `scope_dir(scope)`;
  - `SUBJECT_RE = ^[a-z0-9][a-z0-9._-]{0,99}$`;
  - scope names matching `^[a-z0-9][a-z0-9._-]{0,63}$`;
  - `normalize_subject()` (lowercase; spaces and `/` become `.`);
  - `new_id(now)` (`m_YYYYMMDD_<8hex>`);
  - `file_name(subject, id)` (`<slug>.<id8>.md`);
  - `to_text()` and `from_text(path, text, mtime)`, with lenient defaults for hand-written files and an id derived from a hash of the path;
  - the limits: title 1–120 chars on one line; content 1–2,048 bytes; at most 10 tags matching `^[a-z0-9][a-z0-9_-]{0,31}$`; at most 10 alternatives of ≤ 200 chars each (decision only); importance 1–5; `fresh_days` 1–365.

  Add tests in `tests/unit/agent_memory/test_model.py`.
- [X] T004 [P] Implement `src/agent_memory/files.py` (research A2, A15):
  - the store layout (`memories/<scope path>/`, `archive/`, `events/`), created with directories at 0700 and files at 0600, plus a README;
  - `path_for(record)`, which must resolve under `memories/`;
  - an `O_NOFOLLOW` read;
  - an atomic write (temp file, then rename);
  - `move_to_archive`;
  - `iter_files()`, which skips symlinks and dotfiles and reports malformed files;
  - `lock()`, a context manager using `fcntl.flock` on `<store>/.lock`.

  Add tests in `tests/unit/agent_memory/test_files.py`: layout and modes, path escape refused, symlink refused, atomic write.
- [X] T005 [P] Implement `src/agent_memory/events.py` (research A14):
  - `append(store, event)`, appending to `events/<YYYY-MM>.jsonl` with mode 0600 and `O_APPEND`;
  - `history(store, id, limit=50)`.

  Events never contain content. Add tests in `tests/unit/agent_memory/test_events.py`.
- [X] T006 Implement `src/agent_memory/index.py` (research A4, data-model §2):
  - `Index(store)` opens `index.sqlite` in WAL mode with `busy_timeout` 5000;
  - schema version stored in `meta`;
  - `sync(files)` re-indexes only changed paths (by `mtime_ns` and size) and deletes vanished ones;
  - `rebuild()`;
  - an FTS5 virtual table, falling back to a `LIKE` scan when FTS5 is unavailable;
  - the query helpers `candidates(query, scopes, types, subject_prefix, statuses)` returning BM25, and `by_subject(scope, subject, statuses)` and `by_generated_key(key)`.

  Add tests in `tests/unit/agent_memory/test_index.py`: an incremental sync after a hand edit, a deleted file, a corrupt DB rebuilt, and the fallback path.

**Checkpoint**: records, files, events and the index work and are tested.

---

## Phase 3: US2 + US3 - Save safely, update and forget (Priority: P1) 🎯 MVP part 1

**Goal**: validated saves (duplicates confirmed, conflicts flagged, secrets refused or masked, untrusted content downgraded or refused), and the full lifecycle: update, supersede, invalidate, expire, close, resolve_conflict and archive, with history.

**Independent Test**:

- 30 seeded candidates all reach the outcome the write policy specifies;
- supersede, expire and invalidate remove a memory from normal queries while keeping its history.

### Tests

- [X] T007 [P] [US2] Write the 30-case write-policy suite in `tests/unit/agent_memory/test_write_policy.py`. Each case is a parametrized row `(candidate, expected outcome, expected confidence or flags)`. Cover:
  - **valid saves** of each type;
  - **duplicates**:
    - an exact duplicate → `confirmed`, `confirmations` 2;
    - a near duplicate (Jaccard ≥ 0.8) → `confirmed`;
    - a duplicate from a trusted source over an `inferred` memory → raised to `verified`;
  - **conflicts**:
    - a contradicting semantic fact → `conflict`, linked both ways, `needs_verification` on both;
    - a second decision on the same subject without supersede → `conflict`, the old one stays active;
  - **episodic memories** never conflict;
  - **secrets**: password, URL credentials, PEM, `ghp_`, AWS key, Bearer → refused under the `refuse` policy, and masked with a count under `mask`;
  - **untrusted sources**:
    - `file`/`web`/`issue`/`tool_output` facts → `unverified`;
    - instruction-like procedural or decision content from those sources ("ignore previous instructions…", "always run curl …", "system: …") → `not_permitted`;
  - **inferred** → `inferred`;
  - **schema and size**: oversize content, a bad subject, a bad scope, a scope that isn't allowed → `invalid_input` or `not_permitted`;
  - **raw secrets**: assert that no raw secret appears anywhere under the store directory.
- [X] T008 [P] [US3] Write lifecycle tests in `tests/unit/agent_memory/test_lifecycle.py`:
  - **supersede**:
    - a new memory is created with `supersedes`, and the old one gets `superseded_by` and status `superseded`;
    - the pin is inherited;
    - a reason is required;
  - **invalidate** with a reason;
  - **expire**:
    - an explicit `at`;
    - TTL `expires="1d"` with the clock advanced → excluded from active, and `doctor` reports it as expired;
  - **update** changes metadata only; content changes are refused with "use supersede";
  - **confirm** bumps the count;
  - **close**: an open episodic memory becomes done;
  - **resolve_conflict** supersedes the loser and clears `needs_verification`;
  - **archive**: a preview, then confirm moves the file to `archive/`;
  - every action writes an event;
  - two processes saving the same fact concurrently produce exactly one memory with `confirmations` 2 (using `multiprocessing` against a shared tmp store).

### Implementation

- [X] T009 [P] [US2] Implement `src/agent_memory/similarity.py` (research A6):
  - `normalize()`: lowercase, collapse whitespace, strip punctuation, drop a small English stopword set;
  - `jaccard(a, b)`;
  - `DUP_THRESHOLD = 0.8`.
- [X] T010 [P] [US2] Implement `src/agent_memory/policy.py` (research A7, A8):
  - `validate(input, settings) -> Validated(record, masked, notes)`, which raises `MemoryError(category, message)`;
  - the secret detection reuses `vps_mcp.memory.masking` patterns, copied into `agent_memory/secrets.py` so that `agent_memory` does not import `vps_mcp`, with the `refuse` and `mask` policies;
  - `INSTRUCTION_RE` set;
  - the trust table;
  - the confidence cap: untrusted → `unverified`.
- [X] T011 [US2] Implement `src/agent_memory/store.py` `Memory` with these methods:
  - `open()`;
  - `save()` under the store lock: validate → duplicate check via `index.by_subject` and Jaccard → conflict check → write the file → `index.sync` → event;
  - `update()`, with `update`, `supersede`, `invalidate`, `expire`, `confirm`, `close` and `resolve_conflict`;
  - `archive()`;
  - expiry is evaluated against the injected clock;
  - `doctor()`.

  Also add `src/agent_memory/errors.py` (`MemoryError` with the categories `invalid_input`, `not_permitted`, `not_found`, `conflict` and `failed`).

**Checkpoint**: knowledge can be written and kept current safely.

---

## Phase 4: US1 - Get the right memories for the task (Priority: P1) 🎯 MVP part 2

**Goal**: task-aware ranked search, grouped by type, with flags, scope isolation, and a plain "nothing relevant" answer.

**Independent Test**: ranking and grouping tests on a 40-memory fixture across 3 projects.

### Tests

- [X] T012 [P] [US1] Write search tests in `tests/unit/agent_memory/test_search.py`:
  - **scope isolation**: project `shop` sees `shop`, `environment:prod` and `user`, but never `blog`;
  - **ranking**:
    - the scope weight puts task above environment above project above user;
    - higher importance and confidence rank first among equal keyword matches;
    - recency decay;
  - **output shape**:
    - groups are `facts`, `decisions`, `experiences` and `procedures`;
    - `limit` defaults to 8, maximum 30;
    - `total` and `truncated` are reported;
  - **status filtering**: superseded, expired, invalidated and archived memories are excluded unless `include_history=True`;
  - **flags**:
    - live-state items carry `live_state: verify`;
    - after `fresh_days` they carry `possibly stale (Nd)`;
    - also `conflict`, `inferred`, `unverified` and `open`;
  - **no match**: a text query with no keyword match returns `total 0` and the "nothing relevant" message;
  - **empty query**: with `types`/`subject` filters it lists by score without keyword;
  - **subject prefix**: `subject="postgres."` works.

### Implementation

- [X] T013 [P] [US1] Implement `src/agent_memory/ranking.py` (research A5):
  - `score(row, bm25_norm, now, weights)`;
  - the defaults keyword 0.50, recency 0.15, importance 0.15, confidence 0.10, scope 0.10, with a half-life of 90 days;
  - weights loaded from `<store>/config.toml` `[ranking]` via `tomllib`;
  - `flags(row, now)`;
  - `group(results)`.
- [X] T014 [US1] Implement `Memory.search()` in `src/agent_memory/store.py`:
  - sync the index before querying;
  - build the scope list from the session;
  - get candidates from the index and score them;
  - cap the results;
  - shape the output per data-model §4, including the "recorded data" note.

**Checkpoint**: the MVP is complete (save + lifecycle + search) and usable as a library.

---

## Phase 5: US6 - Know where a memory came from (Priority: P2)

- [X] T015 [P] [US6] Write tests in `tests/unit/agent_memory/test_provenance.py`. `get` by id and by `subject`+`scope` must return:
  - every field;
  - the history (create, confirm ×2, supersede);
  - the `supersedes` / `superseded_by` summaries;
  - the conflicts and related memories.

  A generated memory's source must show `tool_generated` with its reference.
- [X] T016 [US6] Implement `Memory.get()` in `src/agent_memory/store.py`.

---

## Phase 6: US5 - Small pinned core at connect (Priority: P2)

- [X] T017 [P] [US5] Write tests in `tests/unit/agent_memory/test_core_payload.py`. With 300 memories, of which 6 are pinned and 3 are open, the core must:
  - contain exactly those 9 items, ordered pinned first, then by importance and recency;
  - stay within 2,000 characters by default, with the overflow becoming `+N more`;
  - say "memory available via memory_search" when nothing is pinned;
  - carry the preamble and `<memory>` escaping;
  - append "(verify live)" to live-state items;
  - let the successor of a pinned memory inherit the pin.
- [X] T018 [US5] Implement `src/agent_memory/core_payload.py` and `Memory.pinned_core(budget_chars)`, reusing the escape and preamble from `vps_mcp.memory.briefing`. Copy them into `agent_memory`; the vps-mcp module is removed later.

---

## Phase 7: US4 - One memory for every agent and project (Priority: P2)

**Goal**: a standalone MCP server and CLI, scope allowlists, and vps-mcp using the library with its generated facts.

**Independent Test**: two in-process MCP servers with different `--project` values on the same store; scope isolation; and a vps-mcp `list_services` call upserting `service.*.state` facts in place.

### Tests

- [X] T019 [P] [US4] Write MCP flow tests in `tests/unit/agent_memory/test_mcp.py`. Through `SessionPerCall`, check:
  - the 5 tools are listed with read-only and write annotations;
  - `instructions` equal the pinned core;
  - `memory_save` → `memory_search` → `memory_get` → `memory_update(supersede)` → `memory_archive` preview and confirm;
  - the `--allow-scope` restriction: saving to `project:blog` from a `shop` session → `not_permitted`;
  - a second server on the same store with `project=shop` sees the memory, and one with `project=blog` doesn't;
  - envelope and error categories.
- [X] T020 [P] [US4] Write CLI tests in `tests/unit/agent_memory/test_cli.py`:
  - `save --operator` stores `source_kind operator` and `verified`, and is allowed to save instruction-like procedures;
  - `search` prints grouped results;
  - `doctor` reports counts, malformed files, expired-but-not-persisted memories, near-duplicate subjects, and a warning when the store is inside a git repo with a hosted remote;
  - `doctor --rebuild-index` rebuilds the index.
- [X] T021 [P] [US4] Rewrite the adapter tests in `tests/unit/memory/` for vps-mcp:
  - `[memory] store` and `project` config;
  - the pinned core in `instructions`;
  - the 5 agent-memory tools registered, and the old `memory_briefing`, `memory_health` and `memory_read` gone;
  - `list_services` and `get_service_status` upsert `service.<name>.state` (`environment:<vps>`, `live_state`, `tool_generated`, `verified`), and a repeat call updates in place with no duplicate;
  - deploy, job and report outcomes become generated facts;
  - an unavailable store → vps-mcp still serves all other tools and its instructions say memory is unavailable.

### Implementation

- [X] T022 [US4] Implement `src/agent_memory/mcp_tools.py`, using the descriptions and schemas from contracts/memory-tools.md: `register(mcp, memory, wrap, session_id)` and the 5 tools.
- [X] T023 [US4] Implement `src/agent_memory/server.py`, a standalone `MCPServer` with its own envelope wrapper and the pinned core as `instructions`. Then implement `src/agent_memory/cli.py`, with `serve`, `save --operator`, `search`, `doctor`, `migrate-004` (wired in US8) and `bench` (wired in US7).
- [X] T024 [US4] Implement `Memory.upsert_generated(key, ...)` in `src/agent_memory/store.py`. It looks up by `generated_key` and updates in place, refreshing `last_confirmed`, without creating supersede entries.
- [X] T025 [US4] Replace the vps-mcp feature-004 memory with an adapter:
  - `src/vps_mcp/memory/service.py` becomes `MemoryAdapter`. It wraps `agent_memory.Memory`, keeps `REFRESH_TOOLS` and `after_tool()`, and maps tool results to `upsert_generated` keys (`service.<n>.state`, `deploy.<target>.version`, `job.<name>.last_outcome`, `rehearsal.last_outcome`, `report.latest_verdict`). Its `connect_instructions()` is the pinned core, or "Memory unavailable…".
  - `src/vps_mcp/memory/tools.py` delegates to `agent_memory.mcp_tools`.
  - Delete the obsolete `notes.py`, `folder.py`, `search.py`, `briefing.py` and `derived.py`. Keep only `masking.py` if GitLab still imports it; otherwise point GitLab at `agent_memory.secrets`.
  - In `src/vps_mcp/config/policy.py`, `MemorySettings` gains `store: str | None` (default as in research A2) and `project: str | None` (default `vps-<name>`). `path` is kept as a legacy field: when set and it holds a feature-004 folder, `check-config` prints the `agent-memory migrate-004 …` hint.
  - In `src/vps_mcp/server.py`, wire the adapter.

**Checkpoint**: every client can share memory, and vps-mcp contributes its facts.

---

## Phase 8: US8 - Move the existing memory over (Priority: P2)

- [X] T026 [P] [US8] Write migration tests in `tests/unit/agent_memory/test_migrate004.py`, against a fixture feature-004 folder holding one note of each of the 7 old types, a `[[link]]`, a done issue and a hand-written note:
  - the dry run prints the mapping and writes nothing;
  - `--apply` creates exactly one memory per note, mapped per research A12, keeping timestamps and content, with source `migration` / `004:<name>` and the links mapped to `related`;
  - `generated/` is skipped;
  - the source folder is byte-identical afterwards;
  - re-running `--apply` creates no duplicates (they are confirmed instead).
- [X] T027 [US8] Implement `src/agent_memory/migrate004.py` and wire `agent-memory migrate-004` in `src/agent_memory/cli.py`.

---

## Phase 9: US7 - Prove that memory helps (Priority: P3)

- [X] T028 [P] [US7] Create `src/agent_memory/bench/fixtures.json` with generic content, about 300 memories, containing:
  - 3 projects, 2 environments and user scope;
  - 20 superseded, 10 expired and 5 invalidated memories;
  - 6 seeded contradictions;
  - 25 tasks, each with `expected` and `forbidden` keys.
- [X] T029 [US7] Implement `src/agent_memory/bench/runner.py`:
  - load the fixtures into a temporary store, with the clock fixed;
  - run the `none`, `naive` and `ranked` configurations;
  - compute recall@k, precision@k, the stale-hit rate, the contradiction-flag rate, and average tokens (characters / 4) for results and for the pinned core;
  - produce a table or JSON;
  - `--store` together with `--tasks` runs against a real store read-only, by copying the index to a temporary location.

  Wire it as `agent-memory bench`.
- [X] T030 [US7] Write threshold tests in `tests/unit/agent_memory/test_bench.py`. For `ranked`:
  - recall@8 ≥ 0.85;
  - precision@8 ≥ 0.60;
  - stale-hit rate == 0;
  - contradiction-flag rate == 1.0;
  - average result tokens ≤ 1,000;
  - pinned core ≤ 500 tokens.

  Also check that `ranked` beats `naive` on recall and the stale-hit rate, and that a read-only run leaves the store's files and events unchanged.

---

## Phase 10: Polish & Cross-Cutting Concerns

- [X] T031 [P] Write the negative suite in `tests/negative/test_agent_memory_disallowed.py`, with at least 25 cases. Each asserts that nothing outside the store changed (by snapshotting `tmp_path`). Cover:
  - **path and scope escapes**:
    - path traversal in subject, scope and id;
    - a symlinked memory or scope directory;
    - saving to a scope that isn't allowed;
  - **trust attacks**:
    - forging `source_kind: operator` through MCP, which must be downgraded to `client`;
    - prompt-injection procedures;
    - secrets in the title, tags and alternatives;
  - **lifecycle misuse**:
    - replacing a decision through `save`;
    - changing content through `update`;
    - archiving without `confirm`;
    - superseding an id that doesn't exist;
  - **malformed input**:
    - oversize content;
    - a malformed hand-edited file, which must be skipped and listed by `doctor`.
- [X] T032 [P] Write the performance test in `tests/unit/agent_memory/test_perf.py`, with 5,000 generated memories:
  - search must take ≤ 300 ms;
  - a full rebuild must take ≤ 10 s;
  - the sync check with no changes must take ≤ 50 ms.

  Mark it `@pytest.mark.slow` and include it in the default run if it takes under about 15 s.
- [X] T033 [P] Docs:
  - rewrite the README Memory section as **agent-memory**: what it is, the store, types, the write policy, the pinned core, the 5 tools, the standalone server setup for each client, migration, the benchmark;
  - update `docs/USAGE_EXAMPLES.md` §8 for the 5 tools;
  - update `examples/vps-mcp.toml` `[memory]`;
  - update the tool counts and project layout.
- [X] T034 Run `uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest -q` and fix any issues. Run a privacy grep over `src tests specs/006-agent-memory-service README.md docs examples`.
- [ ] T035 Live migration on the operator's laptop:
  - run `agent-memory migrate-004 ~/.config/vps-mcp/<vps>.memory --project vps-<vps> --environment <vps>` as a dry run, and show the mapping;
  - with the operator's go-ahead, run `--apply`;
  - point the local vps-mcp config `[memory]` at the store;
  - run `agent-memory bench`, and `search` for one of the migrated notes.

---

## Dependencies & Execution Order

- **Phase 1 → Phase 2**, which blocks everything.
- **US2/US3** need Phase 2.
- **US1** needs US2/US3: search needs stored records and the status logic.
- **US6** and **US5** need US1. They are independent of each other and can run in parallel.
- **US4** needs US1, US5 and US6, since the tools expose all of them.
- **US8** needs US2. It can run in parallel with US4.
- **US7** needs US1 and US5.
- **Polish** comes last.

## Parallel Opportunities

- Phase 2: T003, T004 and T005 in parallel, then T006.
- US2/US3: T007, T008, T009 and T010 in parallel, then T011.
- US1: T012 and T013 in parallel.
- After US1: US5 and US6 in parallel; US8 alongside US4.
- Polish: T031, T032 and T033 in parallel.

## Implementation Strategy

1. **MVP**: Phases 1–4. A library with validated saves, lifecycle and ranked search, already usable in tests.
2. **Expose it**: provenance (US6), then the pinned core (US5), then the MCP server, CLI and vps-mcp adapter (US4).
3. **Migration** (US8) before switching the operator over.
4. **Benchmark** (US7) to verify the thresholds and tune the ranking weights.
5. **Polish** and live migration.
