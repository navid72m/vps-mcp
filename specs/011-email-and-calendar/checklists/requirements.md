# Requirements Checklist: Email and Google Calendar

- [x] Single unified Google OAuth2 authorization for Calendar and Gmail
- [x] Zero heavy SDKs: pure async `httpx2` client
- [x] Calendar events listing, creation, and deletion
- [x] Email unread/inbox listing, search, safe reading, drafting, and sending
- [x] Untrusted data envelope on all external email/calendar text
- [x] Operator approval gating (`ask`) for sending emails and deleting events
- [x] Lazy tool loading integration (`calendar` and `email` toolsets)
- [x] Direct Telegram slash commands (`/agenda`, `/inbox`)
- [x] Secrets stored strictly in macOS Keychain
