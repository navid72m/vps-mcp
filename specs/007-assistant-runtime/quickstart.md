# Quickstart / Validation: Personal Assistant Runtime

## 1. Automated checks (no Telegram, no model, no VPS)

```bash
cd ~/vps-mcp/personal-assistant
uv run pytest tests/unit/assistant tests/negative/test_assistant_disallowed.py -q
uv run ruff check && uv run mypy
```

All tests and checks must pass. They run against:
- a fake Telegram server (an `httpx2` mock transport);
- a scripted model provider;
- in-process MCP test servers;
- a temporary agent-memory store;
- a fake clock.

## 2. One-time setup

1. **Create a Telegram bot.** In Telegram, message **@BotFather**, send `/newbot`, and copy the bot token. To find your own user id, message **@userinfobot**.
2. **Store the secrets.** Run these in your own Terminal; each one prompts for the value, so it never appears on screen:

   ```bash
   security add-generic-password -U -s vps-mcp-assistant -a telegram_bot_token -w
   security add-generic-password -U -s vps-mcp-assistant -a anthropic_api_key -w
   ```

3. **Write the config.** Copy `examples/assistant.toml` to `~/.config/assistant/assistant.toml`, then set `allowed_user_ids`, `timezone`, and the vps server's `command`.
4. **Check the setup:**

   ```bash
   uv run assistant check
   ```

   It should print `config ok`, `secrets ok`, and `vps: N tools (M auto, K ask, J never)`.
5. **Install and start the background service:**

   ```bash
   uv run assistant service install
   ```

   `assistant service status` should then show it running.

## 3. Live scenarios (SC-001 – SC-006)

| # | Do | Expect |
|---|---|---|
| 1 | From your phone: "Is everything OK on the server?" | A "typing…" indicator within 3 s, then an answer built from `vps__list_services`, in under 30 s |
| 2 | "And the disk?" | It uses the earlier context and calls `get_resource_usage` |
| 3 | "Restart worker" | An approval message showing the preview. ❌ Reject: nothing changes. Ask again and ✅ Approve: it restarts and reports the result |
| 4 | Tap an old Approve button again | Nothing happens; it answers "expired or already used" |
| 5 | Message the bot from another Telegram account | No reply; `assistant audit` shows `access_denied` |
| 6 | `/schedule add test every 1h What changed on the server?` with the time set 2 min ahead (or `/schedules` → Run now) | The briefing arrives |
| 7 | Close the lid past a scheduled time, then open it | One "missed: …" note with a [Run latest now] button |
| 8 | `/stop` during a long answer | Activity stops within 5 s; new messages are refused until `/resume` |
| 9 | `/done` after a conversation where a decision was made | Up to 5 proposals. Save one: `agent-memory search` from Claude Code finds it |
| 10 | `kill -9` the assistant process | Back within 60 s (launchd), and you get a "restarted" note |

## 4. Safety sweeps (SC-008, SC-009)

- `uv run pytest tests/unit/assistant/test_injection.py`: injected instructions in tool output and in memory never cause an unapproved call.
- `uv run pytest tests/unit/assistant/test_secret_leak.py`: the test secrets don't appear in the config, logs, audit, DB, memory or any message sent.
