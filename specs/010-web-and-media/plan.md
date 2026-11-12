# Implementation Plan: Web and Media

**Branch**: `010-web-and-media` | **Date**: 2026-11-12 | **Spec**: [spec.md](./spec.md)

## Summary

**Web**: a new module `assistant/web.py` with two built-in tools, in a lazily loaded `web` toolset:
- `web__search` uses the Brave Search API, with the key from the Keychain;
- `web__read` fetches a public page and turns it into text.

Fetching is SSRF-safe:
- http(s) only;
- every hop's resolved address must be public, with redirects followed by hand (at most 3);
- the downloaded size and the time are capped;
- HTML is reduced to text with the standard library's parser, PDFs are read with `pypdf`.

Results go to the model in the same untrusted envelope as other tools.

**Media**:
- Telegram messages may now carry a photo (the largest size up to about 1,280 px) or a document. The file is downloaded with `getFile` (20 MB limit).
- A new content block `ImageBlock` travels through the provider layer:
  - OpenAI-compatible providers get an `image_url` data URL;
  - Anthropic gets a base64 image block.
- Documents (PDF, .docx, text files) become a capped text block, marked as untrusted data, in the operator's message.
- Images are only in the turn they arrive in. History stores "[photo: caption]".
- Models with `vision = false` get a polite refusal that names a model that can see images.
- The caption alone is the operator's text for the remember gate (009).

## Technical Context

**Language/Version**: Python 3.12+, in the same package.

**Primary Dependencies**:
- new: `pypdf` (pure Python, portable);
- reused: `httpx2`, and the stdlib `html.parser`, `ipaddress`, `socket`, `zipfile`, `base64`.

**Storage**: none new. Audit rows only.

**Testing**: pytest with FakeTelegram, now with `getFile` and file downloads, and `httpx2.MockTransport` for Brave and the pages. A fake resolver drives the SSRF tests.

**Constraints**:
- free-tier prompt caps: pages about 12,000 characters, documents about 15,000, photos at medium size;
- no content in the audit.

## Constitution Check

The constitution is still the template. The project's standing rules apply:

| Rule | How |
|---|---|
| Untrusted text is data | Web, image and document content all go to the model inside the untrusted envelope, and none of it counts as the operator's words for the remember gate (FR-012). |
| Least privilege | Reading is limited to public http(s) (SSRF guard). Nothing is written to disk except a temporary download, deleted after use. |
| Secrets | The Brave key is in the Keychain and registered for redaction. |
| Audit | Searches, reads and received files are audited by type, size and outcome. |

No violations.

## Project Structure

```text
src/assistant/
├── web.py          # NEW: search (Brave), safe fetch, html/pdf → text, tool specs
├── media.py        # NEW: Telegram files → ImageBlock / document text; pdf, docx, text
├── providers/__init__.py      # ImageBlock
├── providers/openai_compat.py # user content parts with image_url
├── providers/anthropic.py     # image blocks
├── telegram.py     # Message.photo / document; get_file + download
├── app.py          # pending turns carry attachments; vision check; audit
├── agent.py        # run_turn(images=…); history note "[photo: …]"
├── config.py       # [web] section; model.vision
├── toolsets.py     # built-in "web" toolset (keywords, URLs)
└── cli.py          # check: web search key present?
tests/unit/pa/test_web.py, test_media.py
```
