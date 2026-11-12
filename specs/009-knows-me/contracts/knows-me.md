# Contract: Knows Me

## Tools (core, built-in)

### `me__remember`

```json
{"name": "me__remember",
 "description": "Save a fact the operator told you about themselves or someone in their life. Use when the operator asks you to remember something or corrects a fact. The runtime decides whether it is saved now or kept as a suggestion; report its answer as is.",
 "input_schema": {"type": "object", "properties": {
   "category": {"type": "string", "enum": ["identity", "work", "people", "interests", "routines", "other"]},
   "key": {"type": "string", "description": "short slug, e.g. 'name', 'employer', 'city'"},
   "person": {"type": "string", "description": "for people facts: first name or slug"},
   "fact": {"type": "string", "maxLength": 300},
   "sensitive": {"type": "boolean", "description": "health, money, relationships, religion, politics"}},
  "required": ["category", "key", "fact"]}}
```

**Results** (not errors, except `refused`):

| Result | When |
|---|---|
| `{"outcome": "saved", "subject": "me.identity.city"}` | new fact, saved now |
| `{"outcome": "updated", "subject": "…", "was": "Munich"}` | correction, saved now |
| `{"outcome": "suggested", "why": "not asked by the operator in this message"}` | gate failed; becomes a proposal |
| `{"outcome": "refused", "why": "looks like a secret"}` | `is_error` = true |

For `saved` and `updated`, the runtime sends a one-line confirmation with **↩️ Undo**: "📝 Saved: lives in Berlin (was Munich)".

### `me__forget`

The input is `{"query": "address"}`. The runtime lists the matching facts with **🗑 Forget** buttons, at most 5. The tool returns `{"outcome": "asked", "matches": 2}`.

### `me__interview`

The input is `{"action": "next|skip|stop"}`. It returns `{"topic": "work", "question_hint": "…"}` or `{"done": true}`.

## Commands

| Command | Effect |
|---|---|
| `/me` | The card by category, with **✏️ Edit** and **🗑 Remove** buttons per fact (paged, 10 at a time) |
| `/me all` | Everything, including sensitive facts and people |
| `/me export` | A Markdown document of everything, with sources and dates, plus the last 30 recaps |
| `/interview` | Start or continue the interview. `/interview stop` stops it. `/interview sensitive on` opts in. |

## Buttons (signed callback data)

| Data | Effect |
|---|---|
| `un:<token>` | Undo a save, update or forget (24 h, single use) |
| `fg1:<memory id>` | Forget this fact (after `me__forget` or `/me`) |
| `ed:<memory id>` | The next message becomes the fact's new text |
| `pa:<conversation>` | Save all pending proposals of that conversation |

## Extraction output (end of conversation)

```json
{"recap": {"topics": ["pipeline failure"], "outcomes": ["cause: migration check"],
           "open_loops": ["call the bank"]},
 "memories": [{"type": "semantic", "category": "work", "subject": "me.work.client",
               "title": "Current client", "content": "…", "confidence": "inferred",
               "why": "…"}]}
```

## System prompt sections (in order)

1. The rules (existing).
2. The tool catalog (008).
3. `## About the operator`: the card, about 1,500 characters.
4. `## Recent conversations`: the recaps, about 1,000 characters, with dates.
5. `## Interview mode`, only while active.
6. Other pinned memory, with agent-memory's header replaced as in feature 007.
