# Plan: Email and Google Calendar

## Architecture

```
[ Operator (Telegram) ]
        │
        ▼
   [ Assistant ]
   ├── Agent / Toolsets (Lazy-loaded "calendar" and "email")
   ├── Approvals Flow (for email__send and calendar__delete)
   │
   ├── GoogleAuth (In-memory token refresh via httpx2)
   │       └── Keychain ("google_refresh_token")
   │
   ├── Google Calendar API (calendar.py)
   │       ├── calendar__events
   │       ├── calendar__create
   │       └── calendar__delete
   │
   └── Gmail API (gmail.py)
           ├── email__inbox
           ├── email__search
           ├── email__read (Safe HTML stripper, untrusted banner)
           ├── email__draft
           └── email__send
```

## Implementation Phases

1. **Phase 1: GoogleAuth Token Manager & CLI Setup**:
   - `src/assistant/google_auth.py`: Token refresh, credentials parsing, CLI auth helper (`assistant google-auth`).
2. **Phase 2: Google Calendar Module**:
   - `src/assistant/calendar.py`: Event listing, creation, and deletion via Calendar API v3.
3. **Phase 3: Gmail Module**:
   - `src/assistant/gmail.py`: Inbox listing, search, safe message reading, draft creation, and message sending via Gmail API v1.
4. **Phase 4: Runtime & Wiring**:
   - `src/assistant/config.py`: Add `[google]` configuration model.
   - `src/assistant/app.py`: Register specs, route tool execution, approval callbacks.
   - `src/assistant/toolsets.py`: Add `calendar` and `email` toolsets.
   - `src/assistant/commands.py`: Add `/agenda` and `/inbox` slash commands.
5. **Phase 5: Unit & Contract Tests**:
   - Mock HTTP transport tests for `google_auth`, `calendar`, and `gmail`.
   - Test approval gating and safety checks.
