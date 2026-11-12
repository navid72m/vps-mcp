# Implementation Plan: Personal Assistant Runtime

**Branch**: `007-assistant-runtime` | **Date**: 2026-11-12 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/007-assistant-runtime/spec.md`

## Summary

This adds a new `assistant` package: a background process that is its own agent.

- **Where it runs**: on the laptop, as a launchd user agent that starts at login and is restarted after crashes. A systemd unit can be rendered for running it on another machine later.
- **How it thinks**: a model-agnostic tool-use loop. The first provider is the Claude API.
- **What tools it has**: it connects as an MCP client to the configured servers (vps-mcp to start), and uses agent-memory in-process. Every turn gets the memory's pinned core, and the model can call two read-only memory tools.
- **How you talk to it**: a Telegram bot that only polls outward, restricted to your account.

Tool use is governed in layers:

1. allowlists;
2. a per-tool `auto` / `ask` / `never` policy;
3. phone approvals that show the tool's own `confirm=false` preview and are bound to the exact call, single-use and expiring.

Unattended scheduled runs always ask before any state-changing action. Long-term memory is written only through end-of-conversation proposals that you approve.

Supporting pieces:

- **Operations**: a scheduler with sleep detection, a heartbeat, budgets, a kill switch, and a single-instance lock.
- **State and audit**: SQLite state and audit storage.
- **Secrets**: Keychain secrets behind a swappable provider.

Decisions R1–R16 are in [research.md](./research.md).

## Technical Context

**Language/Version**: Python 3.12+, in the same repository.

**Primary Dependencies**:
- new: `anthropic` (model SDK);
- reused: `mcp` (client), `httpx2` (Telegram), `agent_memory` (library);
- stdlib: `zoneinfo`, `sqlite3`, `fcntl`.

**Storage**:
- state: `~/.local/state/assistant/assistant.db` (SQLite, WAL mode, file mode 0600);
- logs: `~/.local/state/assistant/logs/`;
- config: `~/.config/assistant/assistant.toml`.

**Testing**: pytest with these doubles and suites:
- a fake Telegram Bot API (`httpx2.MockTransport`);
- `ScriptedProvider`, which replays pre-written model replies including tool_use;
- in-process MCP test servers (one read-only tool, one tool with `confirm`, one never-tool);
- a temporary agent-memory store and a fake clock;
- the injection suite, the secret-leak sweep, and a negative suite.

**Target Platform**: macOS laptop now. Linux (a systemd user unit) is supported by design and checked by rendering the unit in tests.

**Project Type**: a long-running service (daemon) plus a CLI, in the existing repo.

**Performance Goals**:
- "typing…" within 3 s;
- a read-only question answered in under 30 s (dominated by model latency);
- `/stop` effective within 5 s;
- back within 60 s of a crash.

**Constraints**:
- no inbound network;
- secrets only in the Keychain or a 0600 file;
- no memory writes without your tap;
- unattended runs never act without approval;
- no tool outside the allowlists.

**Scale/Scope**: 1 operator, 1 chat, a few MCP servers, tens of tools, about 10 schedules, hundreds of turns per day at most.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

The constitution is still the unfilled template, so there are no ratified gates: **PASS**.

The project defaults still apply:
- **Least privilege**: allowlists, `ask` by default, approvals bound to the exact call.
- **Testability**: everything external sits behind a protocol with a fake.
- **Simplicity**: no web framework, no bot framework, no agent framework.
- **Observability**: an audit trail plus `/status` and `/usage`.

**Post-design re-check: PASS**, with these residual risks documented:
1. The model provider sees the conversation and the tool output: that's inherent to using a hosted model. The README states it, and a local-model adapter is the later mitigation.
2. While the laptop sleeps the assistant is offline. The spec accepts this; always-on hosting is a later step.
3. Approving from the phone is only as safe as the phone. The bot only answers allowlisted user ids, and approvals expire.

## Project Structure

### Documentation (this feature)

```text
specs/007-assistant-runtime/
├── plan.md  research.md  data-model.md  quickstart.md
├── contracts/assistant-interface.md
├── checklists/requirements.md
└── tasks.md                  # /speckit-tasks
```

### Source Code (repository root)

```text
src/assistant/
├── __init__.py
├── config.py          # pydantic models for assistant.toml + validation (data-model §1)
├── secrets.py         # SecretProvider, KeychainSecrets, FileSecrets, redaction registration (R14)
├── state.py           # SQLite schema + accessors: kv, conversations, turns, approvals, schedules,
│                      #   budgets, proposals, audit (R10, R16)
├── telegram.py        # Channel: Bot API long polling, send/edit/callbacks/typing, access control (R3)
├── providers/
│   ├── __init__.py    # Provider protocol, Msg/Reply/ToolSpec types
│   ├── anthropic.py   # AnthropicProvider (R2)
│   └── scripted.py    # ScriptedProvider for tests
├── toolhub.py         # MCP sessions, allowlists, effective policy, naming, preview + execute (R4-R6)
├── approvals.py       # approval futures, HMAC callback data, expiry, voiding (R7)
├── memory.py          # pinned core, memory__search/get built-ins, proposals extraction + save (R9)
├── budget.py          # per-turn and per-day counters, cost estimate, checks (R15)
├── agent.py           # run_turn loop, system prompt, data labelling, history folding (R8)
├── scheduler.py       # cadences, next_run, sleep detection, missed summary (R11)
├── commands.py        # /new /done /status /usage /schedules /schedule /stop /resume /forget
├── app.py             # wiring: lock, channel loop, dispatcher, scheduler/heartbeat tasks
├── service.py         # launchd plist / systemd unit install, uninstall, status (R12)
└── cli.py             # assistant run|check|service|audit|forget|secrets
examples/assistant.toml
tests/unit/assistant/  # config, secrets, state, telegram, toolhub, approvals, agent loop,
                       #   memory proposals, budget, scheduler, commands, service, injection, leak
tests/negative/test_assistant_disallowed.py
```

**Structure Decision**: `assistant` is its own package that depends on `agent_memory` and the MCP client. It reaches vps-mcp only through MCP. Each external dependency (model, chat channel, secrets, tool servers) sits behind a protocol with a fake for tests.

## Implementation Phasing (for /speckit-tasks)

1. **Foundation**: config, secrets, state DB, provider types, and the fakes (Telegram transport, scripted provider, in-process MCP servers, clock).
2. **US1 (P1)**: Telegram channel with access control, ToolHub for read-only calls, the agent loop with history, `/new`.
3. **US2 (P1)**: policy, the preview call, approvals (HMAC, expiry, voiding), and the declined result returned to the model.
4. **US3 (P1)**: pinned core in every turn, the `memory__search` and `memory__get` tools, `/done` proposals with Save / Discard.
5. **US6 (P2)**: budgets, the audit trail, `/usage`. This comes before US4 and US5 because schedules depend on budgets.
6. **US4 (P2)**: the scheduler, `/schedules` and `/schedule`, the missed-run summary, scheduled runs forced to ask.
7. **US5 (P2)**: the single-instance lock, reconnect with backoff, offline-message age limit, `/stop` and `/resume`, the heartbeat, `/status`, the launchd and systemd service, the restart notice.
8. **Polish**:
   - the injection suite and the secret-leak sweep;
   - a negative suite of at least 20 cases;
   - a README "Personal assistant" section and `examples/assistant.toml`;
   - a live check on the laptop.

## Complexity Tracking

There are no constitution violations. The residual risks above are documented.
