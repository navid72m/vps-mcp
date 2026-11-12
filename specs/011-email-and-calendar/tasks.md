# Tasks: Email and Google Calendar

- [ ] **T001**: Define `GoogleConfig` in `src/assistant/config.py`.
- [ ] **T002**: Implement `src/assistant/google_auth.py` for token management and OAuth refresh.
- [ ] **T003**: Implement Google Calendar client in `src/assistant/calendar.py` (`calendar__events`, `calendar__create`, `calendar__delete`).
- [ ] **T004**: Implement Gmail client in `src/assistant/gmail.py` (`email__inbox`, `email__search`, `email__read`, `email__draft`, `email__send`).
- [ ] **T005**: Add `calendar` and `email` toolsets in `src/assistant/toolsets.py`.
- [ ] **T006**: Wire tools and approvals into `src/assistant/app.py`.
- [ ] **T007**: Add `/agenda` and `/inbox` slash commands in `src/assistant/commands.py`.
- [ ] **T008**: Add `assistant google-auth` CLI command in `src/assistant/cli.py`.
- [ ] **T009**: Write unit tests in `tests/unit/pa/test_google_auth.py`, `tests/unit/pa/test_calendar.py`, and `tests/unit/pa/test_gmail.py`.
- [ ] **T010**: Verify full test suite passes with `ruff` and `mypy` clean.
