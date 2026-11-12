# Quickstart: Google Calendar and Email

## 1. Create Google OAuth Credentials

1. Go to [Google Cloud Console](https://console.cloud.google.com/).
2. Create or select a project.
3. Enable **Google Calendar API** and **Gmail API**.
4. Go to **APIs & Services > Credentials**.
5. Click **Create Credentials > OAuth client ID**.
6. Select Application type: **Desktop app**.
7. Download JSON and save as `~/.config/assistant/google_client.json` (or set client ID and secret in Keychain).

## 2. Authorize the Assistant

Run the CLI command:
```bash
assistant google-auth
```
This opens the authorization URL in your browser. Sign in, grant Calendar and Gmail permissions, and paste the code. The refresh token is saved securely into your macOS Keychain under service `vps-mcp-assistant`.

## 3. Configure `assistant.toml`

Add to `~/.config/assistant/assistant.toml`:
```toml
[google]
enabled = true
credentials_path = "~/.config/assistant/google_client.json"
token_secret = "google_refresh_token"
```

## 4. Test on Telegram

- Send `/agenda` to see your upcoming meetings.
- Send `/inbox` to see unread emails.
- Chat naturally: *"Schedule a 30m sync with Alex tomorrow at 10 AM"* or *"Check my inbox for emails from Sara"*.
