# Research: Agent Memory Service

Decisions A1–A15. Each gives the decision and its rationale, and where relevant the alternatives that were rejected.

## A1. Packaging

**Decision**: Build a new top-level package, `src/agent_memory/`, in the same distribution as vps-mcp, with its own console script `agent-memory`.

- `agent_memory` imports nothing from `vps_mcp`. Only vps_mcp depends on agent_memory.
- Generic pieces move from `vps_mcp.memory` into `agent_memory`: the front-matter parser, masking, and folder I/O.
- `vps_mcp.memory` shrinks to an adapter that contributes generated facts and registers the memory tools.

**Rationale**: The spec asks for a standalone service (FR-022) that ships from this repo for now. Keeping the dependency one-way means splitting it into its own repository later is only a packaging step.

**Alternatives**:
- A separate repository right away: two release trains during the migration, so it was rejected for now.
- Keeping it inside `vps_mcp`: violates FR-022.

## A2. Store location and layout

**Decision**: The store defaults to `$AGENT_MEMORY_HOME`, else `$XDG_DATA_HOME/agent-memory`, else `~/.local/share/agent-memory`. It can be overridden with `--store`. Layout:

```text
<store>/
├── memories/
│   ├── user/                       <slug>.<id8>.md
│   ├── project/<name>/             <slug>.<id8>.md
│   ├── environment/<name>/         <slug>.<id8>.md
│   └── task/<id>/                  <slug>.<id8>.md
├── archive/                        archived files, same relative paths
├── events/<YYYY-MM>.jsonl          append-only audit + history events (no content)
├── index.sqlite                    rebuildable (FTS5 + metadata); never the source of truth
└── README.md
```

Directories are created with mode 0700 and files with mode 0600 (FR-027).

- **Superseded, invalidated and expired memories** stay where they are, with their status changed. History therefore comes from the files plus the events (FR-006).
- **Only archive moves files.**

**Rationale**:
- One file per memory, grouped by scope, is easy to browse and git-friendly.
- A slug in the file name helps people reading the folder; the id inside the file is the identity.

## A3. Identifiers

**Decision**: `id = "m_" + YYYYMMDD + "_" + 8 hex characters (random)`. The file name is `<subject-slug>.<last 8 of id>.md`. The parser takes the id from the front matter. If there is none (a hand-written file), it derives a stable id from the path.

## A4. Index and staleness

**Decision**: The index is SQLite in WAL mode.

| Table | Holds |
|---|---|
| `mem` | all front-matter fields, plus `path`, `mtime_ns`, `size` |
| `mem_fts` | FTS5 over subject, content, tags and alternatives, with the `unicode61 remove_diacritics 2` tokenizer |
| `links` | links between memories |

**Keeping it in sync**: on open, and before each search, the service compares the store's file list and `(mtime, size)` pairs with the `mem` table. It re-indexes only the files that changed, and removes entries whose files are gone. Comparing 5,000 file stats takes roughly 20 ms. A full rebuild runs on a schema-version mismatch or when the index is corrupt (FR-005).

**Fallback**: if FTS5 is unavailable, the service uses a `LIKE` token scan and logs a warning. FTS5 is present in both the system Python's SQLite (3.45) and uv's (3.47).

**Rationale**:
- Rebuildable from the files.
- The incremental check makes hand edits visible right away.

## A5. Ranking

**Decision**:

```
score = 0.50·kw + 0.15·recency + 0.15·importance + 0.10·confidence + 0.10·scope
```

| Term | Computed as |
|---|---|
| `kw` | BM25 from FTS5, min-max normalised within the candidate set; 1.0 for a subject-key exact match |
| `recency` | `exp(-age_days / 90)`, where age is measured from `last_confirmed` |
| `importance` | `importance / 5` |
| `confidence` | verified 1.0, inferred 0.6, unverified 0.3 |
| `scope` | task 1.0, environment 0.8, project 0.6, user 0.4 |

- The weights are set in `config.toml` under `[ranking]` (FR-017).
- **Text queries**: only memories with `kw > 0` are candidates. If none qualify, the response says "nothing relevant" (US1 scenario 4).
- **Empty queries**: listing by filters (type, subject) ranks without `kw`.
- **Rationale**: this is the hybrid formula from the brief, minus the semantic term. Weights are tuned with the benchmark (A13).

## A6. Duplicates and contradictions

**Decision**: Text is normalised by lowercasing, collapsing whitespace, stripping punctuation and dropping stopwords. Similarity is the Jaccard index of word sets:

| Situation | Treatment |
|---|---|
| Same scope, same type, same subject, similarity ≥ 0.8 | **Duplicate**: confirm the existing memory (count +1, `last_confirmed` = now, confidence raised per FR-008) and return it with `outcome: confirmed`. |
| Same scope, same subject, type semantic, procedural or decision, active, similarity < 0.8, and no `supersedes` given | **Contradiction**: store the new memory, add `conflicts_with` links in both directions, set `needs_verification: true` on both, and return `outcome: conflict` with the other memory's summary (FR-009). |
| Episodic memories | They never conflict, since events accumulate. They can still be duplicates. |
| Decision with an existing active decision on the subject | Always treated as a conflict unless the save is an explicit supersede through `memory_update(action="supersede")` (US2 scenario 7). |

**Rationale**:
- Deterministic and cheap to compute.
- Subject keys carry most of the precision, so tool descriptions tell agents to use dotted keys such as `postgres.version` or `deploy.order`.

## A7. Secrets

**Decision**: Detection reuses feature 004's patterns (`mask_note`, `mask_inline`) plus a policy `secrets = "refuse"` (the default) or `"mask"`:

- **refuse**: the save fails with `invalid_input` and names the kind of secret found.
- **mask**: the value is stored masked and the response reports the count.

Raw secrets are never written to disk in either mode (SC-004).

## A8. Untrusted origins and instruction-like content

**Decision**: Every save carries `source.kind`.

| Trust | Source kinds |
|---|---|
| Trusted | `client`, `task`, `command`, `operator`, `migration`, `tool_generated` |
| Untrusted | `file`, `web`, `issue`, `tool_output`, `email`, `unknown` |

Rules:

1. Saves from untrusted sources have their confidence capped at `unverified`.
2. When the content is instruction-like and the type is procedural or decision, a save from an untrusted source is refused with `not_permitted` and a hint that the operator can save it with the CLI (FR-010). Content counts as instruction-like when it matches either:
   - the regex set `ignore (all|any|previous)`, `disregard`, `you (must|should) (now )?`, `system:`, `always (run|execute|use)`, `never (ask|confirm)`, `<\s*/?\s*(system|instructions?)`;
   - imperative command lines such as `curl …`, `rm -rf …` or `sudo …`.
3. `agent-memory save --operator` marks `source.kind = operator`. Only the CLI can do that; agents cannot.

**Rationale**:
- The agent cannot raise its own trust level through the MCP interface.
- The operator path lives outside the agent's reach.

## A9. Pinned core

**Decision**: The connect payload contains, within the session's scope (task, environment, project, user):

- active memories with `pinned: true`;
- episodic memories with `state: open`.

Ordering is pinned first, then by importance, then by recency. Each item is one line: `[type] subject: first sentence`. The budget is 2,000 characters by default (≈500 tokens), configurable. Anything over budget becomes "+N more pinned/open (memory_search)". Live-state items get a "(verify live)" suffix. The block is wrapped in the feature 004 "recorded data" preamble and the `<memory>` tags, with escaping.

## A10. MCP server and tools

**Decision**: `agent-memory serve [--store P] [--project NAME] [--environment NAME] [--client NAME]` runs a stdio MCP server. Its `instructions` field carries the pinned core. It exposes five tools:

- `memory_search`
- `memory_get`
- `memory_save`
- `memory_update` (actions: `update`, `supersede`, `invalidate`, `expire`, `confirm`, `close`)
- `memory_archive` (needs `confirm`)

The default scope comes from the flags. An agent may pass `scope` explicitly, but it can only read and write scopes allowed by `--allow-scope` patterns. The defaults are the session's project, its environments, and user.

**Rationale**:
- This is the small interface from the brief.
- The feature 004 extras are dropped: `briefing` is replaced by the pinned core, and `health` moves to the CLI as `agent-memory doctor`.

## A11. vps-mcp integration

**Decision**:

- vps-mcp imports `agent_memory.Memory(store, project=..., environment=<vps name>)`.
- It registers the same five tools from `agent_memory.mcp_tools`, so existing clients keep one server.
- It puts the pinned core into its `instructions` (replacing the feature 004 briefing).
- It contributes generated facts through `Memory.upsert_generated(key, ...)`:
  - type semantic, scope `environment:<vps>`, `live_state: true`, `source.kind = tool_generated`, `confidence = verified`;
  - examples: `service.<name>.state`, `deploy.<target>.version`, `job.<name>.last_outcome`, `report.latest_verdict`;
  - updated in place under a stable `generated_key`, refreshing `last_confirmed`, so there is no supersede churn (FR-023).
- Config: `[memory] enabled`, `store` (default as in A2), `project` (default `vps-<vps name>`), `pinned_chars`.
- If the store can't be opened, vps-mcp logs it, reports "memory unavailable", and keeps working (SC-008).

**Alternatives**: running vps-mcp as a client of a separate memory server process. Rejected: it adds IPC and lifetime coupling, and the file-based store already supports several processes safely (A15).

## A12. Migration from feature 004

**Decision**: `agent-memory migrate-004 <folder> --project P [--environment E] [--apply]`. Without `--apply` it is a dry run. Type mapping:

| Feature 004 type | Feature 006 type, state and scope |
|---|---|
| decision | decision |
| lesson | episodic |
| fact | semantic |
| convention | procedural |
| preference | semantic, scope `user` |
| issue / next_step | episodic, `state: open` (or `done` when the note's status says so) |

Other rules:

- Timestamps, tags and body are kept.
- `source = {kind: migration, ref: "004:<name>", client: <old source>}`.
- Confidence is `unverified`, unless the old source was `operator`, which gives `verified`.
- The subject is the old name with `-` replaced by `.`.
- `[[links]]` are mapped to `related` by name.
- `generated/` is skipped: vps-mcp regenerates those facts.
- The source folder is never modified (FR-024).

## A13. Benchmark

**Decision**: `agent-memory bench [--fixtures builtin|PATH] [--store PATH --tasks PATH] [--k 8] [--json]`.

- **Built-in fixtures** (`agent_memory/bench/fixtures.json`, generic names) contain:
  - about 300 memories across 3 projects, 2 environments and user scope;
  - superseded, expired and invalidated items;
  - seeded contradictions;
  - 25 tasks, each with `expected` ids and `forbidden` ids (stale or out of scope).
- **Configurations**:
  - `none`: returns nothing; this gives the baseline token cost of 0 and recall 0;
  - `naive`: a substring match ordered by recency, with no status or scope filters;
  - `ranked`: the real search.
- **Metrics**:
  - recall@k;
  - precision@k;
  - stale-hit rate (forbidden ids returned);
  - contradiction-flag rate (seeded conflicts returned with their flag);
  - average result tokens (characters / 4).
- **Read-only**: it loads into a temporary store, or opens the user's store read-only (FR-026).

## A14. Audit and events

**Decision**: `events/<YYYY-MM>.jsonl` lines are `{ts, actor (client/session), action, id, outcome, reason?, related?}`. They never contain content or secrets (FR-012). They are written with `O_APPEND` and mode 0600, and they are also the history source for `memory_get`.

## A15. Concurrency

**Decision**:

- **Writes** take an exclusive `fcntl.flock` on `<store>/.lock` for the duration of validate-and-write. This matters for duplicate detection, which needs a consistent view.
- **Files** are written atomically: temp file, then rename (as in feature 004).
- **SQLite** runs in WAL mode with `busy_timeout` set to 5 s.
- **Several clients' server processes** can share the store safely.
