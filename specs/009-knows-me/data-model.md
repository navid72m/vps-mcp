# Data Model: Knows Me

## Fact (agent-memory record, type `semantic`, scope `project:personal`)

| Field | Value |
|---|---|
| subject | `me.<category>.<key>` or `people.<slug>.<key>`. The key is a slug, at most 40 characters. |
| title | short label ("Sister", "Employer") |
| content | the statement, at most 300 characters ("Sara lives in Hamburg") |
| tags | `profile`, `<category>`, optionally `sensitive` |
| importance | 1–5. Identity is 5; the default is 3. |
| confidence | `verified` (told by the operator), `inferred` (approved proposal) |
| source_kind / source_ref | `task` / `conv:<id>`, plus `remember`, `interview` or `proposal` in the tags |
| superseded_by / history | agent-memory supersede chain (corrections) |

**Categories**: `identity`, `work`, `people`, `interests`, `routines`, `other`.

**Validation**:
- The category must be one of these.
- `people.*` needs a slug, `^[a-z][a-z0-9-]{0,30}$`.
- Content at most 300 characters.
- Secrets are refused by agent-memory.

## Recap (agent-memory record, type `episodic`, scope `project:personal`)

| Field | Value |
|---|---|
| subject | `recap.<yyyy-mm-dd>.<conversation id>` |
| title | the topics, joined, at most 120 characters |
| content | topics, outcomes and open loops as short lines, at most 600 characters |
| tags | `recap` |

## UndoToken (`assistant.db`, table `undo`)

| Column | Type | Notes |
|---|---|---|
| token | TEXT PK | random |
| action | TEXT | `saved`, `updated` or `forgotten` |
| memory_id | TEXT | the new or forgotten record |
| previous_id | TEXT | the superseded record, for `updated` |
| previous_content | TEXT | to restore |
| expires | TEXT | 24 h after creation |
| used | INTEGER | single use |

## State in `kv`

| Key | Value |
|---|---|
| `interview` | JSON: `done[]`, `skipped[]`, `sensitive_opt_in`, `last_offered`, `active`, `current_topic` |
| `pending_edit:<chat>` | memory id whose new text is the next message |
| `recap_pending:<conversation>` | first failure time (retried hourly until 24 h) |

## Proposal (existing table; payload extended)

Adds `category` and `subject` in the R1 scheme, and `sensitive` (always false; dropped if true).

## Profile card (rendered, not stored)

```
## About the operator
Identity: Sam Rivera · lives in Berlin · speaks English, German, Spanish
Work: backend engineer · current: a database migration for a client
People: Sara (sister, Hamburg) · Ali (colleague, vegetarian)
Interests: …
Preferences: short answers, no emojis
(+4 more facts — memory__search finds them)
```

## State transitions

- **Fact**:
  - `active` → `superseded` on a correction;
  - `active` → `invalidated` on forget or undo of a save;
  - `superseded` / `invalidated` → (new) `active` on undo, within 24 h.
- **Interview**: idle → offered → active → (next/skip)* → done or stopped; it's resumable.
