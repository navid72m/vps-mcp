---

description: "Task list for Agent Memory"
---

# Tasks: Agent Memory

**Input**: Design documents from `/specs/004-agent-memory/`

**Prerequisites**:

- [plan.md](./plan.md)
- [spec.md](./spec.md)
- [research.md](./research.md) (M1–M12)
- [data-model.md](./data-model.md)
- [contracts/memory-tools.md](./contracts/memory-tools.md)
- [contracts/memory-config.md](./contracts/memory-config.md)
- [quickstart.md](./quickstart.md)

**Tests**: Included. The spec has testable safety criteria (SC-005, SC-006) and a briefing budget (SC-003). Write each story's tests first.

**Organization**: Tasks are grouped by user story (US1–US5). Paths are relative to the repo root.

**Reuses**:

- from `vps_mcp.remote.redact`: `mask_inline`, `scrub`;
- from `vps_mcp.store`: `audit.AuditStore`, `db.open_db`, `db.utcnow`;
- from `server.py`: `_tool_wrapper`, `INSTRUCTIONS`, `_session_id`;
- the config loader.

**Privacy**: fixtures and docs use generic names (`myvps`, `shop_17`, `shop_19`, `worker`). Every test uses a `tmp_path` memory folder and never `~/.config`.

## Format: `[ID] [P?] [Story] Description`

---

## Phase 1: Setup

- [X] T001 Create the package `src/vps_mcp/memory/__init__.py` and the test package `tests/unit/memory/__init__.py`. Add `tests/unit/memory/conftest.py` with these fixtures:
  - `mem_dir` (`tmp_path / "myvps.memory"`);
  - `mem_settings`;
  - a minimal `AppConfig` with 2 services and, optionally, migration targets `shop_17` / `shop_19` and one job;
  - an in-memory state DB from `open_db(tmp_path / "state.db")`.
- [X] T002 [P] Add a commented `[memory]` block to `examples/vps-mcp.toml`, copied from `specs/004-agent-memory/contracts/memory-config.md`.

---

## Phase 2: Foundational (Blocking Prerequisites)

**⚠️ CRITICAL**: No user story can start until this phase is complete.

- [X] T003 Add `MemorySettings(_Strict)` to `src/vps_mcp/config/policy.py`, with these fields:

  | Field | Type | Default | Rule |
  |---|---|---|---|
  | `enabled` | bool | `True` | |
  | `path` | `Path \| None` | `None` | |
  | `writable` | bool | `True` | |
  | `briefing_chars` | int | 6000 | `ge=1000, le=20000` |
  | `max_note_bytes` | int | 8192 | `ge=512, le=65536` |
  | `max_notes` | int | 500 | `ge=10, le=5000` |

  Add `AppConfig.memory: MemorySettings = Field(default_factory=MemorySettings)`.
- [X] T004 In `src/vps_mcp/config/loader.py`, resolve the memory path:
  - if `path` is unset, use `<config file dir>/<config file stem>.memory`;
  - expand `~` and make the path absolute;
  - reject (`ValueError` → `check-config` error) a path that resolves inside the vps-mcp package dir (`Path(vps_mcp.__file__).parent.parent`).

  Expose the result as `config.memory.path`, always set after loading.
- [X] T005 [P] In `src/vps_mcp/store/db.py`, bump `SCHEMA_VERSION=4` and add `_SCHEMA_V4`:

  ```sql
  CREATE TABLE IF NOT EXISTS service_observations (
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    state TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (name, kind)
  )
  ```

  It must upgrade from v3 and keep all data.
- [X] T006 [P] Add `"memory_write"` and `"memory_archive"` to `ACTIONS` in `src/vps_mcp/store/audit.py`.
- [X] T007 [P] Write tests for front matter in `tests/unit/memory/test_frontmatter.py`. Cover:
  - round-trip of scalars, quoted strings with `:` and `#`, and a `tags: [a, b]` flow list;
  - unknown keys are kept;
  - a missing closing `---` → `FrontMatterError`;
  - a non-`key: value` header line → error;
  - a body with `---` lines inside it is kept intact;
  - CRLF input.
- [X] T008 [P] Implement `src/vps_mcp/memory/frontmatter.py` (research M4):
  - `parse(text) -> tuple[dict[str, str | list[str]], str]`;
  - `render(header: dict, body: str) -> str` with a stable key order: name, type, title, tags, status, created, updated, source, then unknown keys;
  - quote values when they contain `:`, `#`, `[`, leading/trailing spaces or are empty;
  - no YAML dependency.
- [X] T009 [P] Write tests for the notes model in `tests/unit/memory/test_notes.py`. Cover:
  - `NAME_RE = ^[a-z0-9][a-z0-9-]{0,63}$`: accept and reject cases, including `..`, `a/b`, `.hidden`, uppercase and a 65-char name;
  - `TAG_RE = ^[a-z0-9][a-z0-9_-]{0,31}$`;
  - at most 10 tags;
  - the 7 note types;
  - title 1–120 chars, single line;
  - `slugify(title)`;
  - `[[link]]` extraction, with invalid targets kept as invalid;
  - `from_file` defaults: name from the file stem, type `fact`, timestamps from mtime;
  - `status` defaults to `open` for `issue` and `next_step`.
- [X] T010 [P] Implement `src/vps_mcp/memory/notes.py`:
  - `NOTE_TYPES`, `NAME_RE`, `TAG_RE`;
  - a `Note` dataclass (`name`, `type`, `title`, `tags`, `created`, `updated`, `source`, `status`, `body`, `extra`);
  - `validate_name`, `slugify`, `links(body)`;
  - `Note.from_text(name_from_file, text, mtime)` and `Note.to_text()`.
- [X] T011 Write tests for the memory folder in `tests/unit/memory/test_folder.py`. Cover:
  - bootstrap creates `notes/`, `generated/`, `archive/` with 0700 and `README.md` with 0600;
  - `note_path("../x")` → `invalid_input`;
  - a symlinked note file or a symlinked `notes/` dir is refused and reported as malformed;
  - an atomic write leaves no temp file behind, and files are 0600;
  - `load_all()` returns active notes plus `malformed` entries (`file`, `reason`) and name mismatches;
  - `archive(name)` moves the file to `archive/<name>.<UTC yyyymmddThhmmss>.md`;
  - a non-writable folder → `writable_fs` is false and writes raise `failed`.
- [X] T012 Implement `src/vps_mcp/memory/folder.py` (research M5, M9, M10) as `MemoryFolder(root)` with:
  - `ensure()` (bootstrap, chmod);
  - `note_path(name)`, which validates the name and checks the resolved parent equals the resolved `notes/`;
  - `read_text(path)` with `os.open(O_RDONLY | O_NOFOLLOW)`;
  - `write_atomic(path, text)` (temp file `.<name>.<pid>.tmp` with 0600, then `os.replace`);
  - `load_all() -> Loaded(notes, malformed, mismatches)`;
  - `archive(name) -> str`;
  - `counts()`;
  - `write_generated(filename, text) -> bool`, which only writes when the content changed;
  - `writable_fs()`.

**Checkpoint**: config, schema, audit actions, the note format and safe folder I/O are in place.

---

## Phase 3: User Story 1 - A new session already knows the situation (Priority: P1) 🎯 MVP

**Goal**: every newly connected session receives a budgeted briefing in `instructions`. `memory_briefing` returns a larger version.

**Independent Test**: with 2 notes plus a stored job run and report in a temp state DB, `build_server(...)` gives instructions that contain:

- the job name;
- the verdict;
- both note titles;
- the preamble;

and the briefing part is ≤ `briefing_chars`.

### Tests for User Story 1

- [X] T013 [P] [US1] Write tests for derived facts in `tests/unit/memory/test_derived.py`. Cover:
  - `environment()` lists services (name, kind, protected), migration targets with roles (`source`/`result` from job and pipeline declarations, otherwise `other`), pipelines, checks and jobs;
  - with no migration configured, those sections are omitted;
  - `outcomes()` reads the last N of `job_runs`, `migration_rehearsals` and `migration_reports` (verdict and counts by severity), newest first;
  - `changes()` reads the last 20 `audit_records`;
  - no IP or `host` value appears in the output (only the config `name`).
- [X] T014 [P] [US1] Write tests for the briefing in `tests/unit/memory/test_briefing.py`. Cover:
  - section order P0–P6 (research M3);
  - P0 is always present, even with a tiny budget;
  - trimming drops lower-priority sections first, cuts at line boundaries and adds the `… N more (use memory_briefing / memory_search)` marker with `omitted=True`;
  - output ≤ budget for budgets of 1000, 6000 and 20000 with 500 notes;
  - note text is inside `<memory>…</memory>`, and a note containing `</memory>` or `<memory` is escaped;
  - only `status=open` issues and next steps appear in P3;
  - a malformed count line appears when there are malformed notes.
- [X] T015 [P] [US1] Write a flow test in `tests/unit/memory/test_memory_flow.py`. Using `build_server` on a temp config with a memory dir, check:
  - `server.instructions` starts with the existing `INSTRUCTIONS`, then the briefing;
  - an in-process `Client(server)` sees the 6 memory tools;
  - `memory_briefing` returns `briefing`, `omitted`, `counts` and `generated_at`;
  - with `[memory] enabled=false`, there are no memory tools and no briefing;
  - an unreadable memory dir makes the instructions end with `Memory unavailable: … Call memory_health.` and the server still builds;
  - building the briefing makes no remote calls: the executor is a fake that fails on any `run`.

### Implementation for User Story 1

- [X] T016 [US1] Implement `src/vps_mcp/memory/derived.py` with:
  - `environment(config, observations)`;
  - `outcomes(conn, limits)`;
  - `changes(conn, n=20)`;
  - `observations(conn)`, which reads `service_observations` and marks states older than 7 days as `unknown (last seen …)`.

  Each returns Markdown text and works with local config and SQLite only.
- [X] T017 [US1] Implement `src/vps_mcp/memory/briefing.py` with `build(env, outcomes, changes, notes, counts, budget) -> Briefing(text, omitted)`:
  - the P0 preamble text from research M11;
  - `escape()` for `<memory`;
  - section-wise trimming (M3);
  - a pure function.
- [X] T018 [US1] Implement the briefing part of `src/vps_mcp/memory/service.py`:
  - `MemoryService(config, conn, audit, secrets)` with `briefing(max_chars)` (loads the folder, derived facts and notes) and `connect_instructions()`;
  - any exception becomes the one-line `Memory unavailable` fallback;
  - it records `last_refresh` and errors for health.
- [X] T019 [US1] Implement `memory_briefing` in `src/vps_mcp/memory/tools.py`, with the description from contracts/memory-tools.md and `max_chars` 1000–32000, defaulting to `min(briefing_chars*4, 32000)`. Add `register(mcp, svc, wrap, session_id)`.
- [X] T020 [US1] Wire memory into `src/vps_mcp/server.py`:
  - in `build_context`, create the `MemoryService` when `config.memory.enabled`;
  - in `build_server`, set `instructions = INSTRUCTIONS + "\n\n" + svc.connect_instructions()` before constructing `MCPServer`;
  - register the memory tools.

**Checkpoint**: a brand-new session receives the environment, outcomes and note index without calling any tool. This is the MVP.

---

## Phase 4: User Story 2 - The assistant records what it learned (Priority: P1)

**Goal**: `memory_write` creates or updates masked, audited notes. It works in read-only mode and is blocked by `writable=false`.

**Independent Test**:

1. Write a note through the in-process client.
2. Check the file content, its 0600 mode and the audit row.
3. Rebuild the server and check the title appears in the instructions.

### Tests for User Story 2

- [X] T021 [P] [US2] Write masking tests in `tests/unit/memory/test_masking.py`. Cover:
  - `password=hunter22` → `password=***`;
  - `token: abc…` masked through the policy's `secret_keys`;
  - `postgres://odoo:s3cret@db/x` → `postgres://odoo:***@db/x`;
  - a PEM private key block → `***`;
  - `ghp_…`, `github_pat_…`, `sk-…`, `xoxb-…`, `AKIA…16`, `glpat-…` and `Authorization: Bearer …` are masked;
  - known secret strings are scrubbed;
  - a sha256 hex string and a UUID are **not** masked;
  - the count equals the number of masked values.
- [X] T022 [P] [US2] Write tests for `memory_write` in `tests/unit/memory/test_write.py`. Cover:
  - create → `created=true`, the file is written and `path_hint="notes/<name>.md"`, with no absolute path in the response;
  - an update keeps `created`, changes `updated` and keeps unknown header keys;
  - the name is derived from the title when omitted;
  - `type` is required on create;
  - a body over `max_note_bytes` → `invalid_input` with `extra.limit`;
  - with `max_notes` reached, a create → `invalid_input` with `extra.limit`, while an update is still allowed;
  - a stale `expected_updated` → `conflict` with the current note;
  - `policy.read_only=true` still allows the write and the response has `read_only_policy: true`;
  - `[memory] writable=false` → `read_only`;
  - every success and refusal is audited as `memory_write`;
  - `source` is taken from the MCP client name (`clientInfo.name`), or `"unknown"`.

### Implementation for User Story 2

- [X] T023 [P] [US2] Implement `src/vps_mcp/memory/masking.py` with `mask_note(text, globs, secrets) -> tuple[str, int]`:
  - run `mask_inline` and `scrub`;
  - then apply `_PATTERNS` (URL credentials, PEM blocks, token prefixes, Bearer) from research M8;
  - count every replacement.
- [X] T024 [US2] Add `write(...)` to `src/vps_mcp/memory/service.py`:
  - check the name, type, tags and title;
  - mask the title, body and tags;
  - check the size against the encoded text;
  - check the count on create;
  - check `expected_updated`;
  - write atomically;
  - audit (`action="memory_write"`, `service_name=None`, `params={name,type}`, `confirmed=True`, plus the outcome);
  - trigger a refresh of the generated files.
- [X] T025 [US2] Add the `memory_write` tool to `src/vps_mcp/memory/tools.py`, with non-destructive, non-read-only annotations and the parameters from the contract. Pass the client name from `ctx.session.client_params.clientInfo.name` when available.

**Checkpoint**: notes written in one session appear in the next session's briefing.

---

## Phase 5: User Story 3 - Find and read what was recorded (Priority: P2)

**Goal**: ranked search by text, type and tag; reading one note with links, backlinks and close-match suggestions.

**Independent Test**: search and read over a 12-note fixture with links, as below.

### Tests for User Story 3

- [X] T026 [P] [US3] Write search tests in `tests/unit/memory/test_search.py`. Cover:
  - scoring: exact name 10, title term 4, tag term 3, body term 1 (up to 5 per term);
  - ties broken by newest `updated`;
  - filtering by `type` and `tag`;
  - `include_generated` also searches `generated/` files;
  - the snippet is ≤ 160 chars around the first match;
  - `limit` from 1 to 50;
  - all of `query`, `type` and `tag` empty → `invalid_input`;
  - 500 notes searched in under 200 ms.
- [X] T027 [P] [US3] Write read tests in `tests/unit/memory/test_read.py`. Cover:
  - the full note is returned with `kind: "recorded_memory"`;
  - `links` have `exists` flags;
  - `backlinks` are listed;
  - an unknown name → `not_found` with ≤ 3 `suggestions`;
  - a bad slug → `invalid_input` without touching the filesystem (check with a monkeypatched `os.open`).

### Implementation for User Story 3

- [X] T028 [P] [US3] Implement `src/vps_mcp/memory/search.py` with `search(notes, query, type, tag, limit)` and `snippet()`. These are pure functions.
- [X] T029 [US3] Add `search()` and `read()` to `src/vps_mcp/memory/service.py`. Compute backlinks by scanning the loaded notes, and suggestions with `difflib.get_close_matches(n=3)`.
- [X] T030 [US3] Add the `memory_search` and `memory_read` tools (read-only annotations) to `src/vps_mcp/memory/tools.py`.

**Checkpoint**: an agent can pull full detail on demand.

---

## Phase 6: User Story 4 - The operator curates memory by hand (Priority: P2)

**Goal**: hand edits are picked up, malformed files are reported, and archiving is previewed, then confirmed and audited.

**Independent Test**:

1. Edit a note file and rebuild the server; the briefing shows the edit.
2. Break a note's header; health reports it.
3. Archive a note with a preview and then a confirm; it's gone from the briefing and present in `archive/`.

### Tests for User Story 4

- [X] T031 [P] [US4] Write archive and health tests in `tests/unit/memory/test_curate.py`. Cover:
  - a hand-edited body is reflected in the briefing and `memory_read`;
  - a hand-written file without `type` or timestamps loads with defaults and `source` `operator`;
  - a broken header → listed in `memory_health.malformed` (≤ 20) and the briefing shows the malformed count;
  - a file-name/`name` mismatch → listed in `name_mismatches`;
  - `memory_archive confirm=false` previews the title, type and backlinks, and changes nothing;
  - `confirm=true` moves the file and audits `memory_archive`;
  - with `writable=false`, archiving → `read_only`;
  - the health `folder` replaces home with `~`.

### Implementation for User Story 4

- [X] T032 [US4] Add `archive(name, confirm, session_id)` and `health()` to `src/vps_mcp/memory/service.py`. Health returns the fields from contracts/memory-tools.md: `connect_briefing_chars` is the length of the last connect briefing, and `last_refresh` and errors come from the service.
- [X] T033 [US4] Add the `memory_archive` tool (destructive=false, `confirm`) and the `memory_health` tool (read-only) to `src/vps_mcp/memory/tools.py`.

**Checkpoint**: the operator stays in control of what agents believe.

---

## Phase 7: User Story 5 - Derived facts stay current automatically (Priority: P3)

**Goal**: service observations are recorded and `generated/*.md` is refreshed after relevant tool calls.

**Independent Test**:

1. Call `list_services` on FakeVps; `service_observations` gets rows and `generated/environment.md` shows "seen … ago".
2. Finish a job in the fakes and call `job_status`; `generated/outcomes.md` shows the verdict.
3. Hand-edit a generated file; the next refresh overwrites it.

### Tests for User Story 5

- [X] T034 [P] [US5] Write refresh tests in `tests/unit/memory/test_refresh.py`. Cover:
  - observations are upserted from `list_services` and `get_service_status` results;
  - a refresh runs only for tools in `REFRESH_TOOLS`: it is not triggered by `get_service_logs`, and it is triggered by `restart_service`;
  - generated files are rewritten only when the content changed (mtime stable otherwise);
  - generated files start with the `<!-- generated by vps-mcp … do not edit … -->` header;
  - a refresh error is swallowed, logged and shown in `memory_health`;
  - a refresh at server start creates the generated files.

### Implementation for User Story 5

- [X] T035 [US5] Add `observe(tool_name, payload)` and `refresh()` to `src/vps_mcp/memory/service.py`, plus a `REFRESH_TOOLS` frozenset (research M6):
  - `observe` upserts `service_observations` from `services[]` (list) or a single status payload;
  - `refresh` writes `generated/environment.md`, `outcomes.md` and `changes.md` through `MemoryFolder.write_generated`.
- [X] T036 [US5] Add the refresh hook to `_tool_wrapper` in `src/vps_mcp/server.py`. After a successful payload, if `app.memory` is set and `fn.__name__` is in `REFRESH_TOOLS`, call `app.memory.observe(...)` and then `app.memory.refresh()` inside `try/except Exception` (log only). Also call `refresh()` once in `build_server`.

**Checkpoint**: the briefing reflects the latest runs, verdicts and observed states.

---

## Phase 8: Polish & Cross-Cutting Concerns

- [X] T037 [P] Write the negative suite in `tests/negative/test_memory_disallowed.py`, with at least 20 cases. Each asserts that no file outside `mem_dir` was created or changed (by snapshotting `tmp_path` before and after). Cover:
  - traversal names (`..`, `../x`, `a/b`, `%2e%2e`, absolute paths, NUL, unicode look-alikes);
  - a symlink to an outside file;
  - a link `[[../../x]]`;
  - an oversized body or title;
  - 11 tags;
  - a bad type;
  - a write when `writable=false`;
  - an archive of an unknown name;
  - `</memory>` injection escaped in the briefing;
  - a secret in a tag;
  - a `max_notes` overflow.
- [X] T038 [P] Extend `check-config` in `src/vps_mcp/cli.py` to print `memory: <~path> (<n> notes)` and two warnings:
  - the folder is group- or world-readable;
  - the path is inside a git work tree whose `git remote -v` contains `github.com`, `gitlab.com` or `bitbucket.org`.

  Add tests in `tests/unit/memory/test_cli_memory.py`.
- [X] T039 [P] Add a `## Memory` section to `README.md` covering:
  - what the memory is;
  - the folder layout;
  - the briefing and its budget;
  - the 6 tools;
  - the safety rules;
  - keeping the folder in a **private** repo;
  - adding the read-only memory tools to the Claude Code allow-list;
  - the `[memory]` row in the configuration reference;
  - the intro tool count.
- [X] T040 Run `uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest -q`, fix any issues, and run a privacy grep over `src tests specs/004-agent-memory README.md examples` for the IP, hostnames, database names and personal names.
- [X] T041 Live validation on the operator's laptop (quickstart §2–§4, read-only, no VPS changes):
  1. Run `check-config` on the real config.
  2. Build the server and print the briefing length.
  3. Start a fresh client session and confirm the instructions name the declared job and databases.

  Writing test notes is done only with the operator's go-ahead.

---

## Dependencies & Execution Order

- **Phase 1 → Phase 2**, which blocks everything.
- **US1** needs T003–T012.
- **US2** needs US1, because write triggers a refresh and the flow test reuses the server wiring.
- **US3** and **US4** each need only US2, so they can run in parallel after it.
- **US5** needs US1. It can run alongside US3 and US4, but it edits `server.py` and `service.py`, so it is done sequentially in practice.
- **Polish** comes after all stories.

## Parallel Opportunities

- Phase 2: T005, T006, T007/T008 and T009/T010 touch different files.
- US1: the tests T013, T014 and T015 run together.
- US2: T021, T022 and T023 run together.
- US3: T026, T027 and T028 run together.
- Polish: T037, T038 and T039 run together.

## Implementation Strategy

1. **MVP**: Phases 1–3 (US1). New sessions are briefed from config and history alone.
2. Add US2 (notes) next. This is the operator's core need: "it will know about everything".
3. Then US3 and US4 for retrieval and curation, then US5 for live freshness.
4. Each checkpoint can be shipped on its own. Run the full suite at each checkpoint.
