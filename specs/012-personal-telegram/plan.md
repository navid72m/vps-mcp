# Implementation Plan: 012 Personal Telegram Account Access

## Phase 1: Dependencies & Configuration
- Add `telethon` dependency.
- Define `TelegramUserSection` in `src/assistant/config.py`.
- Update `AUDIT_KINDS` in `src/assistant/state.py` to include `"telegram_user"`.
- Update `BUILTIN_PREFIXES` and `BUILTIN_SETS` in `src/assistant/toolsets.py` to add `"telegram_user"` / `"tg_user"`.

## Phase 2: Telethon Wrapper & Account Operations
- Create `src/assistant/telegram_user.py`:
  - `TelegramUserClient`: Wraps `telethon.TelegramClient` with lazy connect/disconnect and error handling.
  - Implement `list_dialogs()`, `search_messages()`, `read_history()`, `send_message()`, and `save_to_saved()`.
  - Untrusted prompt-injection envelope wrapping for all incoming text.
  - Safe entity resolution (`"me"`, usernames, titles, IDs).

## Phase 3: Tool Wiring, Approvals & Commands
- Implement tool specs in `src/assistant/app.py`:
  - `tg_user_specs()` registering `tg_user__inbox`, `tg_user__search`, `tg_user__read`, `tg_user__send`, `tg_user__save`.
  - Gated approval flow for `tg_user__send`.
- Add Telegram slash command `/tg [unread|search <q>]` in `src/assistant/commands.py`.

## Phase 4: CLI Authentication Helper
- Implement `assistant tg-auth` command in `src/assistant/cli.py`:
  - Interactive login prompt: phone number -> SMS/Telegram code -> 2FA password (if enabled).
  - Securely writes session file.

## Phase 5: Testing & Verification
- Unit tests for `telegram_user.py` using mock Telethon client.
- Integration tests in `test_telegram_user_runtime.py` verifying tool specs, approval gating, and slash commands.
- Run full test suite to guarantee 0 regressions.
