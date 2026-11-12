# Tasks: Web and Media

**Input**: `/specs/010-web-and-media/` (plan, spec, research, data-model, contracts, quickstart)

**Tests**: included. The SSRF guard (SC-002) and injection safety (SC-005) must be proven.

## Phase 1: Setup

- [ ] T001 Add the `pypdf` dependency in `pyproject.toml`/`uv.lock`. Add the config sections in `src/assistant/config.py`:
  - `WebSection` (`enabled=true`, `search_key_secret="brave_api_key"`, `max_results` 1–10 default 5, `read_chars` default 12000, `timeout_s` default 20);
  - `ModelSection.vision: bool = True`.
- [ ] T002 Add the audit kinds `web` and `media` in `src/assistant/state.py`.

## Phase 2: Foundational

- [ ] T003 Add `ImageBlock(media_type, data)` in `src/assistant/providers/__init__.py`, and map it:
  - in `providers/openai_compat.py`, a user content list of text and `image_url` data-URL parts;
  - in `providers/anthropic.py`, a base64 image block;
  - the scripted provider passes it through.
- [ ] T004 [P] Provider mapping tests in `tests/unit/pa/test_media.py`.

## Phase 3: User Story 1 - Look things up (P1)

- [ ] T005 [US1] `src/assistant/web.py`: `search(query, count, key, transport)` over the Brave API, returning `[{title, url, snippet, age}]`, with a clear error when there's no key (FR-005) and the key registered for redaction.
- [ ] T006 [US1] The `web__search` / `web__read` tool specs and dispatch as built-ins (`src/assistant/app.py`), using the untrusted envelope, the audit kind `web`, and a missing-key hint.
- [ ] T007 [US1] The built-in toolset `web` with keywords, plus the rule "a message containing a URL loads `web`", in `src/assistant/toolsets.py` and `src/assistant/agent.py` prefetch.
- [ ] T008 [P] [US1] Tests: search results are mapped, the missing key gives the hint, and an injection in a snippet stays inside the envelope.

## Phase 4: User Story 2 - Read this page (P1)

- [ ] T009 [US2] `web.read(url, …)` in `src/assistant/web.py`:
  - the scheme check;
  - resolve, then reject non-public addresses (private, loopback, link-local, multicast, reserved, unspecified, IPv4-mapped);
  - manual redirects, at most 3, each re-checked;
  - 2 MB / 20 s caps;
  - HTML to text (the parser drops script, style, nav, footer and header, and keeps the title), PDF to text, JSON and text as they are, anything else unsupported;
  - `read_chars` cap, noting a cut.
- [ ] T010 [P] [US2] Tests: refusals for localhost, 10/8, 192.168/16, 169.254.169.254, `::1`, and a redirect to a private address; HTML, PDF and JSON extraction; the size cap.

## Phase 5: User Story 3 - Photos (P1)

- [ ] T011 [US3] In `src/assistant/telegram.py`:
  - parse `photo` (the largest size with its longest side ≤ 1,280 px) and `document` into `Message.attachments` (kind, file_id, name, mime, size), with the caption as the text;
  - `download(file_id)` through `getFile` plus a file URL, with the 20 MB limit (the token is never logged).
- [ ] T012 [US3] In `src/assistant/app.py`:
  - pending turns carry `(text, attachments)`, so albums and photo-plus-text become one turn;
  - the vision check, with a refusal naming the first `vision=true` profile;
  - download and build the `ImageBlock`s;
  - audit `media`.
- [ ] T013 [US3] In `src/assistant/agent.py`, `run_turn(..., images=[], documents=[])`:
  - the images go in the user message of this turn only;
  - history stores "[photo] caption";
  - `turn_text` stays the caption.
- [ ] T014 [P] [US3] Tests: a photo with a caption reaches the model once, and the next turn has the note but no image; the vision refusal; an album becomes one turn.

## Phase 6: User Story 4 - Documents (P2)

- [ ] T015 [US4] `src/assistant/media.py`: `document_text(name, mime, data) -> (text, pages, cut, problem)` for PDF (pypdf, ≤ 50 pages, detecting scans), .docx (zipfile XML) and text types. Also unsupported types and the 15,000-character cap.
- [ ] T016 [US4] Wire documents into turns (`app.py` and `agent.py`): `[attached file …] <document>…</document>` appended for the model only, a scan, unsupported or too-large reply, audit `media`. Image documents go through the photo path.
- [ ] T017 [P] [US4] Tests: PDF, docx and text extraction; scan detection; the cap; an injection in a document doesn't pass the remember gate (SC-005).

## Phase 7: Polish

- [ ] T018 `assistant check` prints `web: search ok (Brave)` or `web: search not set up (store brave_api_key)`, in `src/assistant/cli.py`.
- [ ] T019 [P] README: web search and reading, photos, documents, the privacy notes, the Brave key setup.
- [ ] T020 Run ruff, ruff format, mypy and the full pytest suite, and the privacy grep.
- [ ] T021 Live check (quickstart), with the operator, after the Brave key is stored.

## Dependencies

- **Setup**: T001–T002, then the provider foundation (T003–T004).
- **US1 and US2** (web) and **US3 and US4** (media) are independent of each other.
- **US4** reuses US3's download path.
- **Polish**: last.

## MVP

- **US1 + US3**: search and photos, the two most frequent needs.
- **Then**: US2 (links) and US4 (documents).
