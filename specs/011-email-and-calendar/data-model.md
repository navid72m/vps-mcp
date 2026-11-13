# Data Model: Email and Google Calendar

## GoogleAuth (In-Memory Token Manager)

| Field | Type | Description |
|---|---|---|
| client_id | str | OAuth 2.0 Client ID |
| client_secret | str | OAuth 2.0 Client Secret |
| refresh_token | str | Stored in Keychain (`google_refresh_token`) |
| access_token | str \| None | Bearer token refreshed automatically in memory |
| expires_at | float | Monotonic or epoch timestamp when current token expires |

---

## CalendarEvent

| Field | Type | Description |
|---|---|---|
| id | str | Unique Google Calendar event ID |
| summary | str | Event title |
| description | str | Event notes / description |
| location | str | Meeting location or video link |
| start | str | ISO-8601 start datetime with timezone offset |
| end | str | ISO-8601 end datetime with timezone offset |
| html_link | str | Web URL to view the event in Google Calendar |
| attendees | list[str] | Email addresses of invited attendees |

---

## EmailMessage

| Field | Type | Description |
|---|---|---|
| id | str | Gmail message ID |
| thread_id | str | Gmail thread ID |
| sender | str | `From:` address and display name |
| to | str | `To:` recipients |
| subject | str | Email subject line |
| date | str | Date and time string from email header |
| snippet | str | Brief text snippet preview from Gmail |
| body_text | str | Sanitized plain text body (HTML stripped, capped at 15k chars) |
| is_unread | bool | Whether the message has the `UNREAD` label |

---

## Config Additions (`assistant.toml`)

```toml
[google]
enabled = true
credentials_path = "~/.config/assistant/google_client.json"  # OAuth client ID JSON from Cloud Console
token_secret = "google_refresh_token"                        # Keychain entry for the refresh token
client_id_secret = "google_client_id"                        # Alternative to credentials_path
client_secret_secret = "google_client_secret"                # Alternative to credentials_path
max_emails = 10
max_events = 20
```

---

## Toolset Additions (`toolsets.py`)

- `calendar`: tools `calendar__*`, keywords: `calendar, event, meeting, agenda, appointment, schedule, busy, free time, call, google calendar`.
- `email`: tools `email__*`, keywords: `email, mail, gmail, inbox, unread, send email, draft, reply to, message from`.
