# Tasks: 012 Personal Telegram Account Access

- [x] T001: Add `telethon` dependency and update pyproject.toml
- [x] T002: Add `TelegramUserSection` to `src/assistant/config.py` and register in Config
- [x] T003: Update `src/assistant/state.py` AUDIT_KINDS with `"telegram_user"`
- [x] T004: Update `src/assistant/toolsets.py` with `tg_user__*` and `"telegram-user"` toolset
- [x] T005: Create `src/assistant/telegram_user.py` with Telethon wrapper and untrusted envelopes
- [x] T006: Add `tg_user_specs()` and tool execution wiring in `src/assistant/app.py`
- [x] T007: Add `/tg` slash command in `src/assistant/commands.py`
- [x] T008: Add `assistant tg-auth` command in `src/assistant/cli.py`
- [x] T009: Add unit tests for `telegram_user.py` and runtime integration tests
- [x] T010: Verify full test suite passes with 0 failures and update user documentation
