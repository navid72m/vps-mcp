# Contract: Memory Tools and Library

The same five tools are served by `agent-memory serve` and by vps-mcp. They are registered from
`agent_memory.mcp_tools.register(mcp, memory, wrap, session_id)`.

**Shared rules**

- Every result uses an envelope: `{"ok": true, …}` on success, or `{"ok": false, "error": {category, message}, …}` on failure.
- The error categories are `invalid_input`, `not_permitted`, `not_found`, `conflict` and `failed`.
- Memory content is always returned inside `kind: "recorded_memory"`.

**Tool descriptions** (written for the agent) include this guidance:

- Search at the start of a task and before risky actions.
- Save only durable, concise knowledge: decisions with their reasons, verified facts, procedures, and lessons from failures.
- Never save secrets, raw logs or conversation transcripts.
- Use dotted subject keys.
- Mark guesses as `inferred`.
- Verify live state on the real system.

---

## memory_search (read-only)

| Input | Type | Default |
|---|---|---|
| `query` | str, 0–300 chars | `""`; empty means filter-only listing |
| `scope` | `{project?, environment?, task?}` | from the session flags |
| `types` | list of `episodic` / `semantic` / `procedural` / `decision` | all |
| `subject` | str; a prefix match on dotted keys, e.g. `postgres.` | none |
| `limit` | 1–30 | 8 |
| `include_history` | bool | false |

**Output**: data-model §4. Groups map to types as follows: semantic → `facts`, decision → `decisions`, episodic → `experiences`, procedural → `procedures`.

**Rules**:

- If a text query matches nothing, the result is `{"total": 0, "message": "nothing relevant in memory for this query"}`.
- Each item carries `flags`, any of:
  - `live_state: verify`
  - `possibly stale (Nd)`
  - `conflict`
  - `needs verification`
  - `inferred`
  - `unverified`
  - `open`

## memory_get (read-only)

**Input**: `id`, or `subject` together with `scope`. With a subject, the active memory is returned, or the latest one if `include_history` is set.

**Output**:

```json
{
  "memory": {"…all fields…", "content": "…"},
  "history": [
    {"ts": "…", "action": "save", "actor": "…", "reason": "…"}
  ],
  "supersedes": {"id": "…", "title": "…", "status": "…"},
  "superseded_by": {"…": "…"},
  "conflicts": [{"id": "…", "title": "…"}],
  "related": [{"id": "…", "title": "…"}]
}
```

`history` lists at most 50 events.

## memory_save (validated write)

**Inputs**:

| Input | Required | Notes |
|---|---|---|
| `type` | yes | |
| `subject` | yes | |
| `title` | yes | |
| `content` | yes | |
| `scope` | no | default: the project scope; `user` scope for type semantic with tag `preference` |
| `confidence` | no | `verified` or `inferred`; default `verified` when `source_kind` is trusted, `unverified` otherwise |
| `importance` | no | |
| `source_kind` | no | default `client` |
| `source_ref` | no | |
| `tags` | no | |
| `pinned` | no | |
| `live_state` | no | |
| `fresh_days` | no | |
| `expires` | no | ISO date, or a duration such as `30d` |
| `alternatives` | no | decision only |
| `state` | no | episodic only |
| `related` | no | ids |

**Validation order**:

1. schema and size;
2. secrets (A7);
3. origin and instruction check (A8);
4. confidence cap;
5. duplicate check, which returns `confirmed`;
6. conflict check, which returns `conflict`;
7. write and record the event.

**Output**:

```json
{
  "outcome": "created | confirmed | conflict",
  "id": "…",
  "path_hint": "memories/<scope>/<file>",
  "confidence": "…",
  "masked": 0,
  "conflicts": [{"id": "…", "title": "…", "content": "…"}],
  "message": "…"
}
```

**Refusals**:

- `invalid_input`: schema, size, or a secret under the `refuse` policy;
- `not_permitted`: an untrusted instruction-like procedure or decision, or a scope that isn't allowed.

## memory_update

**Inputs**:

| Input | Notes |
|---|---|
| `id` | required |
| `action` | required; one of the actions below |
| `reason` | required for `supersede`, `invalidate` and `expire` |

**Actions**:

| Action | Takes | What it does |
|---|---|---|
| `update` | `importance?`, `pinned?`, `tags?`, `expires?`, `fresh_days?` | Changes metadata only; never the content or meaning. |
| `supersede` | `content`, `title?`, `confidence?`, `alternatives?`, `reason` | Creates the successor; the old memory becomes superseded; links both ways; the pin is inherited. This is the only way to replace a decision. |
| `invalidate` | `reason` | |
| `expire` | `reason`, `at?` (default now) | |
| `confirm` | `source_kind?`, `source_ref?` | Re-observed: count +1 and `last_confirmed` = now. Confidence goes from inferred to verified only if the source is trusted. |
| `close` | `reason?` | Episodic only: state `open` → `done`. |
| `resolve_conflict` | `keep` (the id that stays active), `reason` | The other memory becomes superseded by `keep`. |

**Output**: `{"outcome": "<action>", "id": "…", "new_id"?: "…", "status": "…"}`

## memory_archive (restricted)

**Input**: `id`, `confirm`.

- `confirm=false` returns a preview of the title, status and inbound links.
- `confirm=true` moves the file to `archive/` and records an event.

**Output**: `{"outcome": "archived", "archived_path": "archive/…"}`

---

## Library API (`agent_memory`)

```python
mem = Memory.open(store: Path | None = None, *, project: str | None, environment: str | None = None,
                  client: str = "unknown", allow_scopes: list[str] | None = None)
mem.search(query, scope=None, types=None, subject=None, limit=8, include_history=False) -> SearchResult
mem.get(id=None, *, subject=None, scope=None, include_history=False) -> dict
mem.save(MemoryInput) -> SaveResult
mem.update(id, action, **kw) -> dict
mem.archive(id, confirm) -> dict
mem.upsert_generated(key, *, subject, title, content, scope, importance=3, fresh_days=7) -> dict
mem.pinned_core(budget_chars=2000) -> str
mem.doctor() -> dict   # counts, malformed files, expired-not-persisted, index status, warnings
```

## CLI (`agent-memory`)

```text
agent-memory serve      [--store P] [--project N] [--environment N] [--client N] [--allow-scope PATTERN]...
agent-memory save       --operator --type T --scope S --subject K --title T (--content TEXT | --file F)
agent-memory search     QUERY [--project N] [--json]
agent-memory doctor     [--store P] [--rebuild-index]
agent-memory migrate-004 FOLDER --project N [--environment N] [--apply]
agent-memory bench      [--fixtures builtin|PATH] [--store P --tasks PATH] [--k 8] [--json]
```
