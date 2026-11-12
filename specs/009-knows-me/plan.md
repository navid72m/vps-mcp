# Implementation Plan: Knows Me

**Branch**: `009-knows-me` | **Date**: 2026-11-12 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/009-knows-me/spec.md`

## Summary

A new module, `assistant/knowme.py`, turns agent-memory into a secretary's knowledge of the operator. Nothing changes in agent-memory itself.

**Facts**
- Facts are ordinary agent-memory records under a fixed subject scheme:
  - `me.<category>.<key>` for the operator;
  - `people.<slug>.<key>` for others.
- They carry tags `profile` and `<category>`, plus `sensitive` when the operator asked for that.
- A correction is agent-memory's `supersede`, which keeps the history. "Forget" is `invalidate`.

**Saving instantly**
- A core tool, `me__remember`, saves facts in the middle of a turn.
- A gate decides whether the call may save immediately (R2). It does when:
  - the operator's current message asked to remember, or corrects a known fact; **and**
  - the fact's words come from that message.

  Anything else becomes a proposal, so a file or web page can never write memory directly.
- Every instant save sends a confirmation with a signed **Undo** button, valid for 24 hours.

**Context in every conversation**
- **Profile card:** about 1,500 characters, rendered from the non-sensitive `me.*` and `people.*` facts, importance first, then most recently confirmed. It replaces the generic pinned core.
- **Recent recaps:** about 1,000 characters.
- Both sit in the system prompt, which stays stable within a conversation.

**Recaps and proposals**
- The existing end-of-conversation extraction call returns a recap plus proposals, filed into the categories. That's one model call (FR-011).
- The recap is stored as an episodic memory (`recap.<date>`), so older ones are searchable.
- Failed recaps are retried by the scheduler tick.

**Commands and interview**
- `/me`, `/me all`, `/me export`, "forget X" (through `me__forget` plus a confirmation button), Save all on proposals.
- The interview is a small state machine in `kv` plus a prompt mode. It's offered at most once a week when the profile is thin.

Decisions R1–R10 are in [research.md](./research.md).

## Technical Context

**Language/Version**: Python 3.12+, in the same repository and package.

**Primary Dependencies**:
- no new dependencies;
- `agent_memory` (save, supersede, invalidate, search by subject prefix, tags, similarity);
- the existing Telegram channel (buttons, sendDocument);
- signer, scheduler and toolsets from features 007–008.

**Storage**:
- facts and recaps in the local agent-memory store (`project:personal`);
- small state in `assistant.db`: undo tokens, interview state, pending recaps and pending edits.

**Testing**: pytest with the existing harness (ScriptedProvider, FakeTelegram). There are injection tests for FR-003 / SC-006 and a scripted 10-fact test for SC-001.

**Target Platform**: macOS laptop (unchanged). Nothing here blocks the later move to a server.

**Project Type**: a feature inside the existing `assistant` package.

**Performance Goals**:
- no extra model call per turn;
- one call per conversation end (recap plus proposals), as before;
- rendering the card < 20 ms.

**Constraints**:
- **Free model.** About 50 requests a day, so every new behaviour rides on existing calls.
- **Card budget.** About 1,500 characters for the card and about 1,000 for recaps.
- **Sensitive facts** never appear in the card.
- **Only the operator's own text** can trigger an instant save.

**Scale/Scope**: hundreds of facts and dozens of people. Recaps accumulate at a few per day.

## Constitution Check

The constitution file is still the unfilled template, so there are no formal gates. The project's standing rules apply:

| Rule | How this plan meets it |
|---|---|
| Untrusted text is data | Instant saves are gated on the operator's own message (R2). Tool, file, web and memory text can only produce proposals. Injection tests are included. |
| Approvals for consequential actions | Instant saves rely on the operator's explicit request, with Undo. Inferred facts still need a tap. Forget needs a confirmation tap. |
| Secrets never stored | agent-memory's write policy still refuses secrets, and `me__remember` surfaces the refusal. |
| Everything audited | `memory_save` is audited for save, update, undo, forget and recap. |
| Free-tier budget | No new per-turn calls. The recap shares the proposal call. |
| Public repo, nothing private | Tests use invented people. The operator's facts live only in the local store. |

Re-checked after design: no violations.

## Project Structure

### Documentation (this feature)

```text
specs/009-knows-me/
├── spec.md
├── plan.md            # this file
├── research.md        # R1–R10
├── data-model.md
├── quickstart.md
├── contracts/
│   └── knows-me.md    # tools, commands, buttons, extraction JSON, card format
├── checklists/requirements.md
└── tasks.md           # /speckit-tasks
```

### Source Code (repository root)

```text
src/assistant/
├── knowme.py      # NEW: subject scheme, categories, remember gate, card + recaps
│                  #      rendering, undo/forget/edit, interview state, export
├── memory.py      # extraction returns {recap, memories}; save with tags/sensitive; card
│                  # replaces pinned core in the system prompt
├── agent.py       # me__* tools (core), gate on the current operator message, prompt
│                  # sections (card, recaps, interview mode)
├── app.py         # buttons: undo, forget-confirm, edit, save-all; recap retry in tick;
│                  # interview offer
├── commands.py    # /me, /me all, /me export, /interview
├── state.py       # undo tokens, pending recaps, pending edit (kv / small table)
└── toolsets.py    # me__remember, me__forget in BUILTIN_CORE; me__interview in core

tests/unit/pa/test_knowme.py        # gate, subjects, card budget, sensitive, undo, forget
tests/unit/pa/test_knowme_flow.py   # SC-001..SC-007 through the harness
```

**Structure Decision**: one new module holds the rules: the subject scheme, the gate, rendering, undo and the interview. The existing modules only wire it in.

## Complexity Tracking

No constitution violations to justify.
