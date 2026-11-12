# Contract: Web and Media

## `web__search`

- **Input**: `{"query": "…", "count": 5}`
- **Result**, in the untrusted envelope: `{"results": [{"title", "url", "snippet", "age"}]}`
- **Errors**:
  - `{"error": "web search is not set up: store a Brave Search API key with security add-generic-password -U -s <service> -a brave_api_key -w"}`
  - an HTTP error, or rate limiting.

## `web__read`

- **Input**: `{"url": "https://…"}`
- **Result**: `{"url", "final_url", "title", "type": "html|pdf|text|json", "text", "cut": bool}`
- **Refusals**: `refused: only http(s)`, `refused: private or local address`, `refused: too many redirects`, `unsupported content type <mime>`

## Incoming messages

| Telegram | Turn |
|---|---|
| photo (+ caption) | caption text + ImageBlock (largest size with longest side ≤ 1,280) |
| document `image/*` | treated as a photo |
| document pdf / docx / text | caption + `[attached file …] <document>text</document>` |
| other document | "I can't read <type> files yet." |
| file > 20 MB | "That file is too large for me (Telegram lets bots download up to 20 MB)." |
| photo, current model `vision = false` | "My current model (<alias>) can't see images. /model <first vision model> can." |

## History

Stored as `[photo] <caption>` or `[file: name.pdf] <caption>`. Images are never re-sent.
