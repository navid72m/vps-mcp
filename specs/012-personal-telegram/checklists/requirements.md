# Requirements Checklist: 012 Personal Telegram Account Access

- [x] Dedicated spec files under `specs/012-personal-telegram/`
- [x] Zero plain text secrets in files; Keychain integration for `api_id` and `api_hash`
- [x] Approval gating for sending messages (`tg_user__send`)
- [x] Prompt injection defense: untrusted envelope wrapping all incoming message content
- [x] Pure async client integration using `telethon`
- [x] Telegram slash command `/tg`
- [x] CLI authentication wizard `assistant tg-auth`
- [x] Full test suite maintaining 1,081+ passing tests
