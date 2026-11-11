# Contract: Memory Tools

These 6 tools are registered whenever `[memory] enabled` is true, which is the default.

**Common output rules**:

- Every result uses the existing envelope: `{"ok": true, …}` on success, or `{"ok": false, "error": {category, message}}` on failure.
- Note content is always returned under a key whose object has `"kind": "recorded_memory"`.

**Connect-time `instructions`** = the existing server instructions, then a blank line, then the briefing (budget `briefing_chars`). If loading memory fails for any reason, the briefing is replaced by one line: `Memory unavailable: <reason>. Call memory_health.` The server still starts.

---

## memory_briefing (read-only)

**Description shown to agents**: "Start here in a new session: the recorded memory about this VPS (environment, latest outcomes, open issues, decisions, note index). Larger than the connect-time briefing."

| Input | Type | Default |
|---|---|---|
| `max_chars` | int, 1000–32000 | `briefing_chars × 4`, capped at 32000 |

**Output**: `{briefing: "<markdown>", omitted: bool, counts: {active, malformed, archived}, generated_at}`

---

## memory_search (read-only)

| Input | Type | Rule |
|---|---|---|
| `query` | str | 0–200 chars; may be empty if `type` or `tag` is given |
| `type` | note type, optional | |
| `tag` | str, optional | |
| `include_generated` | bool | default `false`; also searches the `generated/` files |
| `limit` | int | 1–50, default 10 |

**Output**: `{results: [SearchItem], total, kind: "recorded_memory"}` (see data-model §6).

**Errors**: `invalid_input` when all of `query`, `type` and `tag` are empty.

---

## memory_read (read-only)

| Input | Type |
|---|---|
| `name` | str, slug |

**Output**: `{note: {kind: "recorded_memory", name, type, title, tags, created, updated, source, status?, body}, links: [{name, exists: bool}], backlinks: [name]}`

**Errors**:

- `not_found`, with `extra.suggestions` holding up to 3 close names (difflib);
- `invalid_input` for a bad slug, without touching the filesystem.

---

## memory_write (state-changing, local)

| Input | Type | Rule |
|---|---|---|
| `name` | str, optional | slug; when omitted, derived from `title` |
| `type` | note type | required on create; when updating, defaults to the existing type |
| `title` | str | 1–120 chars; required on create |
| `body` | str | ≤ `max_note_bytes` once encoded |
| `tags` | list[str] | ≤ 10 |
| `status` | `open` / `done`, optional | |
| `expected_updated` | str, optional | optimistic concurrency (research M10) |

**Behaviour**:

1. Create the note, or update it in place, keeping `created`.
2. Mask secrets (research M8).
3. Write the file atomically.
4. Audit `memory_write` with outcome `success`.

**Output**: `{name, created: bool, updated_at, masked: int, path_hint: "notes/<name>.md"}`

The response never includes the absolute path, so private usernames don't end up in transcripts.

**Errors**:

| Category | When |
|---|---|
| `invalid_input` | bad slug, type, tags or title |
| `invalid_input` with `extra.limit` | note too large, or `max_notes` reached on create (the limit and current value are stated) |
| `conflict` | `expected_updated` doesn't match; returns the current note |
| `read_only` | `[memory] writable = false` |
| `failed` | the folder isn't writable |

Every refused or failed attempt is audited too.

---

## memory_archive (state-changing, local)

| Input | Type |
|---|---|
| `name` | str, slug |
| `confirm` | bool |

**Behaviour**:

- `confirm=false` previews: returns the title and type, plus the backlinks that will dangle.
- `confirm=true` moves the file to `archive/<name>.<ts>.md` and audits `memory_archive`.

**Output**: `{name, archived_as: "archive/<file>", backlinks: [...]}`

**Errors**: `not_found`, `invalid_input`, `read_only`.

---

## memory_health (read-only)

**Output**:

```json
{
  "folder": "<path with home replaced by ~>",
  "enabled": true,
  "writable": true,
  "writable_fs": true,
  "counts": {"active": 0, "malformed": 0, "archived": 0, "generated": 0},
  "malformed": [{"file": "notes/x.md", "reason": "…"}],
  "name_mismatches": ["…"],
  "limits": {"max_notes": 0, "max_note_bytes": 0, "briefing_chars": 0},
  "last_refresh": "…",
  "connect_briefing_chars": 0
}
```

`malformed` lists at most 20 entries.

---

## Refresh hook (internal)

After a successful call to any tool in the refresh set (research M6), the wrapper calls `memory.refresh()`:

- `list_services` and `get_service_status` first upsert `service_observations`.
- The refresh never raises. Errors are logged and shown by `memory_health`.
