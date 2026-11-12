# Quickstart: Web and Media

## Setup

1. Get a free Brave Search API key at https://brave.com/search/api/.
2. In your own terminal: `security add-generic-password -U -s vps-mcp-assistant -a brave_api_key -w`
3. `uv run assistant check` → `web: search ok (Brave)`.

## Automated

`uv run pytest -q tests/unit/pa/test_web.py tests/unit/pa/test_media.py`:

- the SSRF guard refuses private addresses, including after redirects;
- HTML, PDF and docx become text;
- a photo reaches the model once;
- an injection inside a document triggers no save.

## Live, on the phone

| Send | Expected |
|---|---|
| "Who is Anil Seth and what's his latest book?" | Answer with source links |
| a news link + "summarise this" | Summary from the page |
| "read http://192.168.1.1" | Refused (private address) |
| screenshot of an error + "what's wrong?" | Explanation |
| PDF letter + "what do they want and by when?" | Deadline and amount |
| a scanned PDF | "I can't read scanned pages yet; send photos" |
