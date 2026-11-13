# Quickstart: Personal Telegram Account Access

## 1. Get Telegram API Credentials
1. Log in to [my.telegram.org](https://my.telegram.org) with your phone number.
2. Go to **API development tools**.
3. Create an application (App title: e.g. `vps-mcp-personal`, Short name: `vpsmcp`).
4. Copy your **api_id** (integer) and **api_hash** (string).

## 2. Store Credentials
Store them securely in your macOS Keychain:
```bash
security add-generic-password -U -s vps-mcp-assistant -a telegram_api_id -w "<your_api_id>"
security add-generic-password -U -s vps-mcp-assistant -a telegram_api_hash -w "<your_api_hash>"
```

## 3. Authorize Your Telegram Account
Run the CLI authentication command:
```bash
cd ~/vps-mcp/personal-assistant
uv run assistant tg-auth
```
- Enter your phone number with country code (e.g. `+98912...`).
- Enter the login code Telegram sends to your official app.
- Enter your 2-step verification password if enabled.
- The session is saved securely to `~/.config/assistant/telegram_user.session`.

## 4. Enable in `assistant.toml`
Add to `~/.config/assistant/assistant.toml`:
```toml
[telegram_user]
enabled = true
```

## 5. Restart Assistant Service
```bash
launchctl kickstart -k gui/$(id -u)/io.github.vps-mcp.assistant
```
