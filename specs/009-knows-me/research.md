# Research: Knows Me

## R1: Where facts live

**Decision**: in agent-memory, as semantic records under a fixed subject scheme, tagged for the profile. Recaps are episodic records.

| Kind | Subject | Tags |
|---|---|---|
| operator fact | `me.<category>.<key>`, e.g. `me.identity.name`, `me.work.employer` | `profile`, `<category>` (+ `sensitive`) |
| person fact | `people.<slug>.<key>`, e.g. `people.sara.relation`, `people.sara.city` | `profile`, `people` (+ `sensitive`) |
| recap | `recap.<yyyy-mm-dd>.<conversation>` | `recap` |

The categories are `identity`, `work`, `people`, `interests`, `routines` and `other`.

**Rationale**:
- agent-memory already has everything needed:
  - subject-prefix search (`subject="me"`);
  - `supersede` with history (FR-007);
  - `invalidate` (forget);
  - the secret refusal (FR-004);
  - similarity for duplicates;
  - a Markdown file per fact, which the operator can read.
- Other clients (Claude Code, Antigravity) see the same knowledge.

**Legacy**: the five memories saved on 2026-11-12 use subjects `operator.*`. The card reads `operator.*` too (as category `other`, or mapped by keyword), so nothing is lost. A one-off `assistant knowme migrate` command can re-file them under `me.*`.

**Alternatives considered**:
- A separate profile table in `assistant.db`: duplicated storage, and invisible to other clients.
- One big "profile" memory: no per-fact history or undo.

## R2: Gate for instant saves (FR-001, FR-003)

**Decision**: `me__remember` saves immediately only if all of the following hold:

1. **The turn came from the operator** (origin `chat`), not from a schedule or the system.
2. **The current operator message is the source**: at least 60% of the fact's content words (stemmed, stopwords removed) appear in it. Tool results, files and web text don't count, because they aren't in that message.
3. **The operator asked**:
   - the message contains a remember trigger:
     - English: remember, note that, keep in mind, don't forget, save that;
     - German: merk dir, merke;
     - Persian: یادت باشه, به خاطر بسپار;
   - **or** the call updates an existing fact (same subject) that the operator is correcting;
   - **or** the interview is active (an answer to the interview's question).

When the gate fails, the fact becomes an end-of-conversation proposal, and the tool tells the model that ("saved as a suggestion for /done"). Sensitive facts additionally need an explicit trigger: a correction or interview answer alone doesn't qualify.

**Rationale**:
- It's deterministic, cheap (no model call) and testable.
- An injection inside a tool result can't satisfy rule 2, since its text isn't in the operator's message.

**Alternatives considered**:
- Asking the model whether the operator asked: costs a call, and can be fooled by the same injection.
- Approve every save: that's exactly the friction the operator rejected.

## R3: Profile card (FR-006)

**Decision**:

- **Selection**: render from active, non-sensitive facts under `me.*`, `people.*` and legacy `operator.*`.
- **Order**: identity first, then by importance (1–5), then by last confirmed.
- **Format**: one line per fact, grouped by category, people grouped per person.
- **Budget**: about 1,500 characters. When facts are left out, the card ends with `(+N more facts — memory__search finds them)`.
- **Placement**: in the system prompt, replacing agent-memory's generic pinned core. Other pinned memories (from other clients' projects) still follow, within their own budget.
- **Rebuilt**: at the start of each turn. It changes only when facts change, so the prompt prefix stays stable in between.

**Rationale**:
- The facts are always known, with no search (SC-001).
- The fixed size protects free-tier prompt caps.

## R4: Undo, corrections, forget

| Action | How | Signed button | Undo means |
|---|---|---|---|
| New fact | `save` | `un:<token>` | `invalidate` the new record |
| Correction | `supersede` (old → new) | `un:<token>` | `supersede` back to the old content |
| Forget | ask, then `invalidate` | `fg1:<id>` | the token restores it within 24 h (`supersede` from history) |

- **Undo tokens**: stored in state with `{action, ids, old_content, expires}`. They're single use and expire after 24 h.
- **Editing in `/me`**: the button `ed:<id>` sets a pending edit for that chat, and the next operator message becomes the new text (a supersede). `/cancel`, or any command, clears it.

## R5: Recaps (FR-009 – FR-011)

**Decision**: the existing extraction call (feature 007 R9, fixed today) returns:

```json
{"recap": {"topics": ["…"], "outcomes": ["…"], "open_loops": ["…"]},
 "memories": [ … proposals with subject, category, sensitive=false … ]}
```

- **Storage**: the recap is saved immediately, with no approval. It's the assistant's own notes about the conversation, not facts about the operator, and the open loops are phrased as the operator stated them.
- **In context**: the last 3 recaps or 7 days, whichever is fewer, at most about 1,000 characters, newest first, with dates.
- **Retry**: if the call fails, the conversation is marked `recap_pending` and the scheduler tick retries it, at most once an hour, giving up after 24 hours.

**Rationale**: one call yields both outputs (FR-011, SC-005).

## R6: Proposals filed into categories, Save all, duplicates

- **Subjects**: the extraction prompt asks for subjects in the R1 scheme and forbids sensitive proposals.
- **Duplicates**: before a proposal is shown, it's compared with existing profile facts. Jaccard ≥ 0.8 means it's dropped (FR-018).
- **Save all**: one button, `pa:<conversation>`, saves every pending proposal of that conversation.

## R7: Interview (FR-012 – FR-014)

**Decision**: a small state machine in `kv:interview`:

```json
{"done": [], "skipped": [], "sensitive_opt_in": false, "last_offered": "…", "active": false}
```

- **Topics, in order**: name and address form, location and languages, work, people, interests, routines and answer style.
- **While active**, the system prompt gets an "Interview mode" section with the next topic. The model asks one question and saves answers with `me__remember`, which the gate allows during the interview. It calls `me__interview` with `next`, `skip` or `stop`.
- **Off-topic messages** pause it: the operator's next non-answer message ends the interview mode for that turn.
- **The offer**: when a conversation starts with fewer than 3 profile facts and the last offer was more than 7 days ago, the runtime appends one line to the first reply: "I don't know much about you yet — /interview?". This needs no model call.

## R8: Sensitive facts (FR-017)

- **Saving**: only on an explicit trigger. They're tagged `sensitive` and never put in the card.
- **Proposals**: never proposed; the extraction prompt forbids it and the parser drops proposals tagged sensitive.
- **Retrieval**: through `memory__search`, which matches only when the query is about the topic.
- **Export**: included in `/me export` under a clear "Sensitive" heading.

## R9: Export (FR-015)

`/me export` sends a Markdown document through Telegram `sendDocument`:

- the profile by category, then people, then sensitive facts, then the last 30 recaps;
- each fact with its source and date.

It's generated in memory and not kept on disk.

## R10: Honesty (FR-019)

- The system-prompt rule from today stays: never claim a search or save without the tool.
- `me__remember` returns exactly what happened, which can be one of:
  - `saved`;
  - `updated (was …)`;
  - `kept as a suggestion for /done`;
  - `refused: secret`.

  The model is told to repeat it as is.
- The runtime, not the model, sends the confirmation with the Undo button, so a false "saved" is impossible.
