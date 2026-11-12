---

description: "Task list for Personal Assistant Runtime"
---

# Tasks: Personal Assistant Runtime

**Input**: Design documents from `/specs/007-assistant-runtime/`

**Prerequisites**:

- [plan.md](./plan.md)
- [spec.md](./spec.md)
- [research.md](./research.md) (R1–R16)
- [data-model.md](./data-model.md)
- [contracts/assistant-interface.md](./contracts/assistant-interface.md)
- [quickstart.md](./quickstart.md)

**Tests**: Included. The spec requires these sweeps:

- injection (SC-008);
- secret leak (SC-009);
- approvals and access control (SC-002, SC-003).

Write each story's tests first. Use the fakes from Phase 2:

- fake Telegram;
- scripted provider;
- in-process MCP servers;
- a temporary agent-memory store;
- a fake clock.

**Organization**: Grouped by user story. The order is US1 → US2 → US3, then US6 → US4 → US5 (budgets before schedules), then Polish.

**Repository**: `~/vps-mcp/personal-assistant` (the GitHub copy). Paths are relative to that repo root.

**Privacy**: fixtures use generic names only: user id `111`, bot token `123:TESTTOKEN…`, service names `web` and `worker`.

## Format: `[ID] [P?] [Story] Description`

---

## Phase 1: Setup

- [X] T001 Set up the package and its basics:
  - create `src/assistant/__init__.py` and `src/assistant/providers/__init__.py`;
  - add the console script `assistant = "assistant.cli:main"`;
  - add `anthropic>=0.40` to the dependencies in `pyproject.toml`, and `src/assistant` to the hatch packages and the mypy files; run `uv sync`;
  - create `tests/unit/assistant/__init__.py`.
- [X] T002 [P] Create `examples/assistant.toml` with the commented config from data-model §1, using generic values.

---

## Phase 2: Foundational (Blocking Prerequisites)

**⚠️ CRITICAL**: No user story can start until this phase is complete.

- [X] T003 [P] Implement `src/assistant/config.py` (data-model §1): pydantic `_Strict` models and `load(path)`.

  Validation rules:

  | Field | Rule |
  |---|---|
  | `allowed_user_ids` | 1–10 positive integers |
  | `timezone` | valid `zoneinfo` |
  | server `name` | `^[a-z][a-z0-9_]{0,15}$`, unique |
  | server `command` | non-empty list |
  | `policy` values | `auto` \| `ask` \| `never` |
  | schedule `cadence` | `^(daily\|weekdays) ([01]\d\|2[0-3]):[0-5]\d$` or `^every ([1-9]\|1\d\|2[0-4])h$` |
  | schedule `prompt` | 1–2000 chars |
  | schedule `name` | unique slug |
  | `approval_minutes` | 1–60 |
  | other limits | > 0 |
  | unknown keys | error |

  Defaults: `state_dir` from `$ASSISTANT_HOME`, otherwise `~/.local/state/assistant`. Add tests in `tests/unit/assistant/test_config.py`.
- [X] T004 [P] Implement `src/assistant/secrets.py` (R14):
  - the `SecretProvider` protocol;
  - `KeychainSecrets(service, reader)`, which runs `/usr/bin/security find-generic-password -s <service> -a <name> -w` with no shell and a 10 s timeout;
  - `FileSecrets(path)`, which refuses the file if its `mode & 0o077`;
  - `SecretMissing(name, hint)`, where the hint is the exact `security add-generic-password -U -s <service> -a <name> -w` command;
  - every loaded value is registered with `vps_mcp.remote.redact.register_secret`.

  Add tests in `tests/unit/assistant/test_secrets.py`.
- [X] T005 [P] Implement `src/assistant/state.py` (R10, data-model §2):
  - `State(path)` creates the DB with mode 0600, WAL and the schema;
  - accessors: `kv_get/set`, `conversation_current/new/end/touch`, `add_turn` (text capped at 8 KB), `recent_turns(n)`, `fold(summary)`;
  - approvals: `approval_add/get/set_status/pending`;
  - schedules: `schedule_upsert/list/set`;
  - budgets: `budget_add(day, …)/budget_get(day)`;
  - proposals: `proposal_add/get/set`;
  - audit: `audit(kind, …)/audit_since`;
  - `forget_all()`.

  Add tests in `tests/unit/assistant/test_state.py`.
- [X] T006 [P] Implement `src/assistant/providers/__init__.py`: the types `Msg`, `TextBlock`, `ToolUse(id, name, input)`, `ToolResult(id, content, is_error)`, `Reply(blocks, usage_in, usage_out, stop_reason)` and `ToolSpec(name, description, input_schema)`, and the `Provider` protocol. Then implement `src/assistant/providers/scripted.py`: `ScriptedProvider(script)`, which replays replies, records every request it receives, and can raise a provider error on demand.
- [X] T007 [P] Write `tests/assistant_fakes.py` with the shared test doubles:
  - **`FakeTelegram`**: an `httpx2.MockTransport` handler for `getUpdates`, `sendMessage`, `editMessageText`, `answerCallbackQuery` and `sendChatAction`. It keeps an update queue, recording `sent` and `edited`, and offers `user_message(text, user_id=111)` and `press(callback_data)` helpers.
  - **`make_test_servers()`**: in-process MCP servers built with `MCPServer`:
    - `status` (read-only);
    - `restart` (with `confirm`; `confirm=false` returns a preview, `true` records an action);
    - `wipe` (not read-only);
    - `notes`, whose output contains an injection string.
  - **`Clock`**: a settable fake clock with separate wall and monotonic time.
  - **`ToolHub` support**: a way for `ToolHub` to accept in-process servers instead of a command.

**Checkpoint**: config, secrets, state, provider types and fakes are ready.

---

## Phase 3: US1 - Ask the assistant from my phone (Priority: P1) 🎯 MVP

**Goal**: The operator chats with the assistant over Telegram. It answers using read-only tools and keeps the conversation's context.

**Independent Test**: With the fake Telegram, the scripted provider and the test servers:

1. ask a question;
2. the provider requests `status__status`;
3. the reply is sent;
4. a follow-up includes the previous turns.

A message from any other user gets no reply and makes no provider call.

### Tests

- [X] T008 [P] [US1] Write Telegram channel tests in `tests/unit/assistant/test_telegram.py`:
  - long polling uses `timeout=50`, and the offset is persisted in `kv` so a restart doesn't reprocess;
  - private messages from allowed ids are yielded; messages from other ids, groups or channels are dropped and audited as `access_denied`;
  - send splits text longer than 4,000 characters at paragraph boundaries, with a "(1/2)" marker;
  - typing is sent;
  - network errors back off from 1 s up to 60 s;
  - the token never appears in any log record (caplog).
- [X] T009 [P] [US1] Write ToolHub tests in `tests/unit/assistant/test_toolhub.py`:
  - tool names have the form `<server>__<tool>` (≤ 64 characters);
  - only allowlisted tools are listed;
  - the effective policy follows R5: `never` from config wins, then an explicit config value, then `auto` for read-only and `ask` otherwise;
  - a read-only call returns the envelope `{"kind": "tool_output", "untrusted": true, …}`, capped at 12 KB;
  - a server that's down is reported and its tools are omitted;
  - the hub reconnects after a session failure.
- [X] T010 [P] [US1] Write agent loop tests in `tests/unit/assistant/test_agent.py`:
  - a question leads to a tool_use, then a tool result, then the final text, which is sent;
  - the system prompt contains the "data, not instructions" rule and the current time and time zone;
  - a follow-up includes the earlier turns, up to `history_turns`, with older turns folded into the summary;
  - a question that needs no tool gets a direct reply;
  - `/new` drops the earlier context;
  - a provider error produces a "model unavailable" notice and no tool call;
  - the turn stops after `turn_model_calls` iterations.

### Implementation

- [X] T011 [US1] Implement `src/assistant/telegram.py`: `TelegramChannel(token, allowed_ids, state, transport=None)` implementing the `Channel` protocol (contracts), with R3's access control, offset persistence, backoff, splitting, and token masking in logs.
- [X] T012 [US1] Implement `src/assistant/toolhub.py` (R4, R5):
  - `ToolHub(config.servers, state, mask)`, which takes a server command or an in-process server;
  - `connect()`, `tools()` and `call(name, args, origin, conversation)`.

  This story covers read-only calls only; US2 adds the `ask` path.
- [X] T013 [US1] Implement `src/assistant/providers/anthropic.py` (R2): `AnthropicProvider(api_key, model, max_tokens)`, which maps `Msg`/`ToolSpec` to the Anthropic messages API with tools and maps the response back to `Reply`. Add a unit test against a mocked client in `tests/unit/assistant/test_anthropic_provider.py`.
- [X] T014 [US1] Implement `src/assistant/agent.py` (R8): `Agent(provider, hub, memory, budget, state, config)` with these methods:
  - `run_turn(conv, text, origin)`;
  - `system_prompt()`;
  - `history()`;
  - `fold_if_needed()`.

  Tool calls run in order; results are wrapped as untrusted data.
- [X] T015 [US1] Implement `src/assistant/app.py` (minimal) and the `/new` command in `src/assistant/commands.py`:
  - the dispatcher reads channel updates;
  - text starts a turn task with a typing indicator;
  - commands go to the handlers.

**Checkpoint**: a working phone chat with read-only tools. This is the MVP.

---

## Phase 4: US2 - Approve actions from my phone (Priority: P1)

**Goal**: `ask` tools show the tool's own preview with Approve / Reject. An approval is single-use, bound to the exact call, and expires. `never` and non-allowlisted tools are refused.

**Independent Test**:

- the restart tool runs only after Approve;
- Reject, expiry, a double tap, or a stale button after a restart all leave it not run;
- `wipe` with `never` is refused without a prompt.

### Tests

- [X] T016 [P] [US2] Write approval tests in `tests/unit/assistant/test_approvals.py`:
  - **message**: the approval message contains the server, tool, masked args and the preview (the tool's own `confirm=false` output, capped at 2,500 characters), with buttons whose callback data is `ap:<id>:<a|r>:<hmac8>` (≤ 64 bytes);
  - **approve**: the call runs exactly once with `confirm=true` plus the `plan_id`/`plan_hash` from the preview, the message is edited to "✅ Approved · done", and the result is reported;
  - **reject**: the model receives `{"declined": "rejected by the operator"}` and the message reads "❌ Rejected";
  - **expiry**: with the clock advanced past `approval_minutes`, the outcome is "⌛ Expired", the model gets "approval expired", and a late tap answers "expired or already used";
  - **double tap**: runs once;
  - **forged or stale HMAC**: rejected;
  - **voiding**: `/new` or `/stop` sets pending approvals to `voided`;
  - **no preview**: a tool without `confirm` shows "no preview available" and the args only;
  - **binding**: an approval for args A cannot run args B.
- [X] T017 [P] [US2] Write policy refusal tests in `tests/unit/assistant/test_policy.py`:
  - a `never` tool → refused, the model is told, and the refusal is audited;
  - a non-allowlisted tool name requested by the model → refused and audited;
  - each of several state-changing calls in one reply gets its own approval message.

### Implementation

- [X] T018 [US2] Implement `src/assistant/approvals.py` (R7):
  - `Approvals(state, channel, clock, minutes)`, with a per-process random key (`secrets.token_bytes`);
  - `request(conv, server, tool, args, preview) -> Outcome`, which awaits a future with a timeout;
  - `on_callback(data, callback_id)`, which verifies the HMAC, the status and the expiry;
  - `void_all(reason)`.
- [X] T019 [US2] Extend `ToolHub.call` in `src/assistant/toolhub.py` with the policy check:
  - **`never`**: refuse;
  - **`ask`**:
    1. call the preview (`confirm=false`) if the schema has `confirm`;
    2. call `Approvals.request`;
    3. on approval, run the exact bound call with `confirm=true` plus the plan id/hash;
  - every step is audited.
- [X] T020 [US2] Wire callbacks in `src/assistant/app.py`: `ap:` → `Approvals.on_callback`.

**Checkpoint**: the assistant can act, safely, with approval from the phone.

---

## Phase 5: US3 - Remembers across conversations, safely (Priority: P1)

**Goal**: The pinned core goes into every turn; the model can call `memory__search` and `memory__get`; `/done` and idle time produce up to 5 proposals with Save / Discard; nothing is saved without a tap.

**Independent Test**:

- a pinned memory appears in the system prompt;
- a scripted conversation, then `/done`, yields proposals; Save writes through `agent_memory` (searchable from a separate `Memory` handle), and Discard writes nothing;
- memory being unavailable gives a notice and the assistant still answers.

### Tests

- [X] T021 [P] [US3] Write memory tests in `tests/unit/assistant/test_memory.py` covering:
  - **pinned core**: included in the system prompt every turn;
  - **built-in tools**: `memory__search` and `memory__get` are listed as `auto` and return `agent_memory` results wrapped as untrusted data;
  - **no write tool**: no memory write tool is exposed to the model;
  - **`/done`**:
    - the extraction provider call (scripted JSON) produces at most `max_proposals` messages, each with type, subject and content and buttons `pr:<id>:<s|d>:<hmac8>`;
    - Save → `Memory.save` with `source_kind="task"`, `source_ref="conv:<id>"`, and `client="assistant"`;
    - a secret in a proposal is refused by the agent-memory policy, and the operator is told;
    - Discard → nothing is written;
  - **idle**: going past `idle_minutes` ends the conversation and offers proposals;
  - **memory unavailable**: the notice is included and answering still works.

### Implementation

- [X] T022 [US3] Implement `src/assistant/memory.py` (R9):
  - `AssistantMemory(config, clock)` wraps `agent_memory.Memory` with `project = config.memory.project`, `client="assistant"`, and `allow_scopes`;
  - `pinned_core()`;
  - `tools()` (the two built-in `ToolSpec`s) and `call()`;
  - `extract_proposals(provider, transcript) -> list[dict]`, using a fixed prompt, strict JSON parsing and at most N items;
  - `save(proposal, conv_id)`.
- [X] T023 [US3] Wire memory into `src/assistant/agent.py` (the system prompt, the built-in tools routed in the tool loop) and `src/assistant/commands.py` (`/done`, the idle sweep in `app.py`, and `pr:` callbacks).

**Checkpoint**: memory is shared with Claude Code and the other clients, and only you decide what gets saved.

---

## Phase 6: US6 - Stay within limits and leave a trail (Priority: P2)

**Goal**: Per-turn and per-day budgets, a complete audit trail, and `/usage`.

**Independent Test**:

- with `turn_tool_calls = 3`, a scripted run of 5 tool calls stops at 3 with a message naming the limit;
- with `day_tokens` exhausted, new turns are refused, while `/stop` and `/status` still work;
- `/usage` shows the counters;
- the audit trail holds every kind of record.

### Tests

- [X] T024 [P] [US6] Write budget and audit tests in `tests/unit/assistant/test_budget.py`:
  - per-turn limits on model calls, tool calls and tokens;
  - per-day limits on tokens, tool calls and cost (cost from `prices`), with the day roll-over in the configured time zone;
  - the message names the limit that was hit;
  - a check before each model call stops the turn when the estimate would exceed the limit;
  - audit records exist for `model_call`, `tool_call`, `approval_request`, `approval_outcome`, `refusal`, `command` and `access_denied`, with token counts;
  - no message text or tool output is stored in the audit;
  - the `/usage` output is correct.

### Implementation

- [X] T025 [US6] Implement `src/assistant/budget.py` (R15): `Budget(config, state, clock)` with `turn()`, `check_model(est)`, `check_tool()` and `record_model(in, out)`, raising `BudgetExceeded(limit, value)`. Wire it into `agent.py` and `toolhub.py`, and add `/usage` to `commands.py`.
- [X] T026 [US6] Add `assistant audit [--since 1d] [--json]` to `src/assistant/cli.py`.

---

## Phase 7: US4 - Morning briefing and other scheduled prompts (Priority: P2)

**Goal**: Recurring prompts on a schedule. Missed runs after sleep produce one summary. Scheduled runs always ask before acting.

**Independent Test**:

- a schedule due at the fake clock's time runs and sends its result;
- a state-changing tool in a scheduled run asks for approval even when its policy is `auto`;
- a wall-clock jump of 3 hours with 2 missed schedules sends exactly 1 summary with a [Run latest now] button, and nothing is replayed;
- `/schedule add|pause|resume|remove` take effect immediately.

### Tests

- [X] T027 [P] [US4] Write scheduler tests in `tests/unit/assistant/test_scheduler.py`:
  - cadence parsing and `next_run` for daily, weekdays (Friday → Monday) and every N hours, in a time zone with a DST change (no double run and no skip);
  - the tick runs due schedules once;
  - sleep detection: when wall time advances more than monotonic time by over 90 s, a single missed summary is sent;
  - the `ms:` callback runs the latest occurrence;
  - `origin="schedule"` forces `ask`;
  - config schedules are seeded into the DB, and chat edits persist;
  - the `/schedules` list shows the next run times.

### Implementation

- [X] T028 [US4] Implement `src/assistant/scheduler.py` (R11): `Cadence.parse`, `next_after(dt, tz)`, and `Scheduler(state, agent, channel, clock, tz)` with `tick()`, `detect_sleep()` and `missed_summary()`. Wire it as a task in `app.py`, and add `/schedules` and `/schedule …` to `commands.py`. In `toolhub.py`, force `ask` for `origin="schedule"` unless the tool is read-only.

---

## Phase 8: US5 - Always there, and I know when it isn't (Priority: P2)

**Goal**: The assistant is a single instance, starts at login and restarts after a crash, reconnects after network loss, handles offline messages, has a kill switch, a heartbeat and `/status`, and is installed as a service.

**Independent Test**:

- a second instance exits with code 3;
- `/stop` cancels a running turn within 5 s (fake clock) and refuses new turns until `/resume`; the stopped state persists across a simulated restart;
- messages older than `offline_max_age_min` get the "I was offline" reply;
- the heartbeat sends at its cadence;
- `/status` lists the servers, schedules, pending approvals and stop state;
- the launchd plist and the systemd unit render correctly.

### Tests

- [X] T029 [P] [US5] Write runtime tests in `tests/unit/assistant/test_runtime.py` covering:
  - **lock**: the second lock attempt fails with exit code 3;
  - **kill switch**:
    - `/stop` cancels the running turn task, voids approvals, persists the `paused` flag, refuses new turns, and still works when the budget is exhausted;
    - `/resume` lifts it;
  - **offline messages**: messages older than `offline_max_age_min` get the offline reply and no turn;
  - **restart notice**: sent on startup when the previous run did not end cleanly (an `kv.clean_shutdown` flag);
  - **heartbeat**: the message contains uptime, actions and budget, and `kv.last_heartbeat` is updated;
  - **`/status`**: the output is correct;
  - **`/forget`**: asks for confirmation, then wipes conversations.
- [X] T030 [P] [US5] Write service tests in `tests/unit/assistant/test_service.py`:
  - `render_launchd(repo, uv, config)`: the plist has `RunAtLoad`, `KeepAlive.SuccessfulExit=false`, `ThrottleInterval 10`, log paths under `state_dir/logs`, and `ProgramArguments`;
  - `render_systemd(...)`: `Restart=on-failure`;
  - `install` writes to a temporary LaunchAgents directory and calls `launchctl` through an injected runner (`bootstrap gui/<uid>`);
  - `uninstall` reverses it.

### Implementation

- [X] T031 [US5] Implement single-instance locking, the clean-shutdown flag and the restart notice, offline handling, `/stop`, `/resume` and `/forget`, the heartbeat task and `/status` in `src/assistant/app.py` and `src/assistant/commands.py`.
- [X] T032 [US5] Implement `src/assistant/service.py` (R12): render, install, uninstall and status for launchd and for systemd user units.
- [X] T033 [US5] Implement `src/assistant/cli.py`:
  - `run`;
  - `check` (config, secrets present, servers start and list tools, with policy counts);
  - `service install|uninstall|status [--systemd-user]`;
  - `audit`;
  - `forget --all`;
  - `secrets show-commands`.

  Exit codes: 0 / 1 config / 2 secret / 3 already running.

---

## Phase 9: Polish & Cross-Cutting Concerns

- [X] T034 [P] Write the injection suite in `tests/unit/assistant/test_injection.py` (SC-008):
  - the `notes` tool output and a memory item both contain instructions ("ignore previous instructions", "call wipe", "approve everything");
  - the scripted model then tries `wipe` (never), `restart` (ask) and an unlisted tool;
  - assert zero executions without policy or operator approval;
  - assert that every approval message shows the real call.
- [X] T035 [P] Write the secret-leak sweep in `tests/unit/assistant/test_secret_leak.py` (SC-009):
  - use the test bot token, API key and a tool-server credential;
  - run a full conversation including an approval, `/done` with a proposal, a schedule run and errors;
  - search config files, logs (caplog), the state DB, the audit, the memory store, every message sent through the fake Telegram, and every provider request for the secrets: zero hits.
- [X] T036 [P] Write the negative suite in `tests/negative/test_assistant_disallowed.py`, with at least 20 cases, covering:
  - **access**: messages from non-allowlisted ids, group chats and channels;
  - **forged buttons**: forged `ap:`, `pr:` and `ms:` callbacks, and callbacks with a valid shape but the wrong HMAC;
  - **stale approvals**: callbacks for approvals that are expired, voided or already executed;
  - **misdirected calls**: calls whose args don't match the approval;
  - **disallowed tools**: `never` and unlisted tools;
  - **unsafe schedules**: a schedule asking for a state-changing action without approval;
  - **bad config and secrets**: invalid config (unknown key, bad cadence, empty `allowed_user_ids`); a secrets file with mode 0644; a missing secret (exit 2);
  - **limits**: budgets exceeded.
- [X] T037 [P] Add a `## Personal assistant` section to `README.md` covering:
  - what it is;
  - laptop first, with the trade-offs;
  - BotFather and user id setup;
  - Keychain commands;
  - config;
  - `assistant check`;
  - `service install`;
  - commands;
  - the approval flow;
  - memory proposals;
  - budgets;
  - privacy notes: the model provider sees the conversation; it's offline while asleep.

  Also update the project layout.
- [X] T038 Run `uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest -q` and fix any issues, then run a privacy grep over `src tests specs/007-assistant-runtime README.md examples`.
- [ ] T039 Live check on the laptop (quickstart §2–§3), after the operator has created the bot, stored both secrets and written `~/.config/assistant/assistant.toml`:
  - run `assistant check`;
  - run `assistant run` in the foreground and do scenarios 1–5 and 8;
  - then `service install` and scenario 10.

  Run state-changing actions only with the operator approving them on the phone.

---

## Dependencies & Execution Order

- Phase 1 → Phase 2, which blocks everything else.
- US1 needs Phase 2.
- US2 needs US1 (ToolHub, agent loop, app).
- US3 needs US1. It can run in parallel with US2, but both touch `agent.py` and `commands.py`, so in practice they run one after the other.
- US6 needs US2 (audit of approvals).
- US4 needs US6 and US2 (scheduled runs use budgets and force `ask`).
- US5 needs US1. It is done last because it touches `app.py`.
- Polish comes last.

## Parallel Opportunities

| Phase | Can run in parallel |
|---|---|
| Phase 2 | T003, T004, T005, T006 and T007 (all different files) |
| US1 | T008, T009 and T010 (tests); T013 alongside T011 and T012 |
| US2 | T016 and T017 |
| US5 | T029 and T030 |
| Polish | T034, T035, T036 and T037 |

## Implementation Strategy

1. **MVP**: Phases 1–3. A phone chat with read-only tools, already useful ("is everything OK on the server?").
2. **US2 (approvals)**: makes it safe for it to act.
3. **US3 (memory)**: makes it personal and shared with the other clients.
4. **US6, then US4, then US5**: budgets, schedules, and always-on reliability.
5. **Polish**, then the live check on the laptop.
