# Data Model: Web and Media

## Attachment (in memory, per incoming message)

| Field | Type | Notes |
|---|---|---|
| kind | `photo` \| `document` | |
| file_id | str | Telegram file id |
| name | str | the document's file name, or `photo.jpg` |
| mime | str | e.g. `image/jpeg`, `application/pdf` |
| size | int | bytes, as Telegram reports it. Over 20 MB is refused before downloading. |

## ImageBlock (provider message content)

| Field | Type | Notes |
|---|---|---|
| media_type | str | `image/jpeg`, `image/png`, `image/webp` or `image/gif` |
| data | str | base64. Used once, in the turn the image arrived. |

## Config additions

```toml
[web]
enabled = true
search_key_secret = "brave_api_key"   # Keychain entry
max_results = 5                       # 1–10
read_chars = 12000
timeout_s = 20

[model]          # and every [models.*]
vision = true    # false: the model can't see images (the bot says so)
```

## Audit kinds

- `web`: target `search` or `read <host>`; outcome `ok N results`, `ok N chars`, `refused: private address`, …
- `media`: target `photo` or `document <type>`; outcome `ok N bytes`, `refused: too large`, …
