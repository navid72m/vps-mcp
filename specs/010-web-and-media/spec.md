# Feature Specification: Web and Media

**Feature Branch**: `010-web-and-media`

**Created**: 2026-11-12

**Status**: Draft

**Input**: The operator chose two "quick wins" for the digital secretary:

- web search and reading web pages;
- photos and documents: "send a screenshot, a receipt or a PDF letter: what does this letter want from me?".

## Context

The assistant can't look anything up on the internet. Asked to "check his website", it had to say it couldn't. It also ignores everything that isn't plain text: a photo or a PDF sent in Telegram is dropped without an answer.

A secretary reads what you hand them and looks things up for you. This feature adds both. It keeps the existing safety model: everything read from the web, an image or a document is untrusted data, never instructions.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Look things up (Priority: P1)

The operator asks:

- "Who is Anil Seth and what's his latest book?"
- "What's the opening time of the Bürgeramt Mitte?"
- "Search for reviews of the Framework 13 laptop."

The assistant searches the web, reads the most relevant pages if needed, and answers with the sources linked.

**Why this priority**: The most frequent gap seen so far.

**Independent Test**: Ask three questions that need current information from the web. Each answer is correct, and names its sources as links.

**Acceptance Scenarios**:

1. **Given** a question that needs the web, **When** it's asked, **Then** the assistant searches and answers with at least one source link.
2. **Given** the search provider isn't set up (no key), **When** a search is needed, **Then** the assistant says so in one line, including how to set it up, and doesn't pretend to have searched.
3. **Given** a search result page contains instructions ("ignore your rules…"), **When** it's read, **Then** they're treated as data and never followed.

---

### User Story 2 - Read this page (Priority: P1)

The operator sends a link, alone or with a question ("what does this say about pricing?"). The assistant reads the page and answers about it.

**Why this priority**: Very common on a phone ("look at this").

**Independent Test**: Send three links (a news article, a documentation page, a PDF link) with questions. All are answered from the page's content.

**Acceptance Scenarios**:

1. **Given** a message with a public http(s) link, **When** it's handled, **Then** the assistant can read the page and answer from it.
2. **Given** a link to a private or local address (localhost, a home-network IP, a cloud metadata address), **When** reading is attempted, **Then** it's refused, so the bot can't be used to probe the laptop's network.
3. **Given** a very large or binary page, **When** it's read, **Then** only a bounded amount of text is used, and unsupported content types are reported, not dumped.

---

### User Story 3 - What's in this photo (Priority: P1)

The operator sends a photo or screenshot with or without a caption: a receipt, a whiteboard, an error message, a sign in another language. The assistant looks at it and answers the caption's question. Without a caption, it describes what matters in it.

**Why this priority**: The operator asked for it explicitly, and it's natural on a phone.

**Independent Test**: Send a screenshot of an error, a photo of a receipt, and a photo of text in another language, with questions. All are answered correctly.

**Acceptance Scenarios**:

1. **Given** a photo with a caption, **When** it's handled, **Then** the model sees the image and the caption's question in the same turn.
2. **Given** several photos sent together (an album) or a photo followed quickly by a text, **When** they're handled, **Then** they form one turn.
3. **Given** the current model can't see images, **When** a photo arrives, **Then** the assistant says so and suggests a model that can, instead of answering blind.
4. **Given** later messages in the same conversation, **When** they're handled, **Then** the image isn't sent again. The conversation remembers that a photo was sent and what was said about it.

---

### User Story 4 - What does this document want from me (Priority: P2)

The operator sends a PDF letter, a Word document or a text file, with or without a question. The assistant reads it and answers: a summary, deadlines and amounts, what action is needed.

**Why this priority**: High value (letters, contracts, invoices), slightly less frequent than photos.

**Independent Test**: Send a PDF letter with a deadline and an amount, a Word document and a text file. The summaries and the extracted deadline and amount are correct.

**Acceptance Scenarios**:

1. **Given** a PDF with a text layer, a .docx, or a text file (txt, md, csv, json), **When** it's sent, **Then** its text is read and used to answer.
2. **Given** a scanned PDF with no text layer, **When** it's sent, **Then** the assistant says it can't read scanned pages yet, and suggests sending photos of the pages.
3. **Given** a long document, **When** it's sent, **Then** a bounded part is used, and the assistant says when it only read the beginning.
4. **Given** a document containing instructions ("assistant, forward this to…"), **When** it's read, **Then** they're never followed. Its text can never trigger an instant "remember" (feature 009's gate).

### Edge Cases

- **Files that are too large.** Telegram limits bot downloads to 20 MB. Bigger files are refused with a message.
- **An image sent "as a file"** (a document of type image) is treated like a photo.
- **Unsupported types** (zip, audio, video): a one-line "I can't read this type yet".
- **Prompt size on free tiers.** Images and document text count against the provider's limits. Documents are capped, and photos are sent at Telegram's medium-large size, not the original.
- **Privacy.** Search queries go to the search provider. Page fetches come from the laptop's IP. Photos and documents go to the model provider. The README says so.
- **Offline.** If a photo arrives while the bot is offline and it's too old, the existing "please resend" rule applies.

## Requirements *(mandatory)*

### Functional Requirements

**Web**

- **FR-001**: The assistant MUST be able to search the web through a configured search provider. It MUST answer with source links when it used the web.
- **FR-002**: The assistant MUST be able to read the text of a public http(s) page, within size and time limits.
- **FR-003**: Reading MUST refuse:
  - non-http(s) schemes;
  - addresses that resolve to private, loopback, link-local or otherwise non-public networks, including after redirects.
- **FR-004**: Web content MUST be given to the model as labelled, untrusted data.
- **FR-005**: Without a configured search key, search MUST fail with a clear message and setup hint. Reading pages MUST still work.

**Media**

- **FR-006**: Photos, and images sent as files, MUST be passed to the model as images in the turn they arrive, together with the caption.
- **FR-007**: Several photos or messages arriving together MUST form one turn, as text messages already do.
- **FR-008**: Images MUST be sent once. Later turns MUST see a short text note in their place: "[photo: caption]".
- **FR-009**: If the current model is configured as unable to see images, the assistant MUST say so and name a model that can.
- **FR-010**: PDF (text layer), .docx and plain-text files MUST be read as text, capped in size. The model MUST be told whether the text was cut.
- **FR-011**: Scanned PDFs, unsupported types and files over 20 MB MUST get a clear one-line answer.
- **FR-012**: Document and image content MUST NOT count as the operator's own words for the remember gate (feature 009). Only the caption counts.
- **FR-013**: Every search, read and received file MUST be audited (type, size, outcome), without the content.

## Success Criteria *(mandatory)*

- **SC-001**: 3 of 3 web questions are answered correctly with source links (live check).
- **SC-002**: Private and local addresses are refused in 100% of tests, including redirects to them.
- **SC-003**: A photo with a question is answered in one turn, and the image is sent to the model exactly once per conversation.
- **SC-004**: A PDF letter's deadline and amount are extracted correctly (live check). A scanned PDF gets the "can't read scanned pages" answer.
- **SC-005**: No instruction inside a page, image or document leads to an action or an instant save (tests).

## Assumptions

- **Search provider:** Brave Search API (the operator's choice). The key goes in the Keychain as `brave_api_key`. The free plan is enough for one person.
- **Images** are sent inline (base64). DeepSeek V4.1 Flash through OrcaRouter accepts them (verified 2026-11-12). Models without vision get `vision = false` in the config.
- **Documents:** PDF text via a pure-Python library (portable to a server later), .docx via its XML, and text files decoded as UTF-8. OCR for scanned PDFs is out of scope.
- **Limits:**
  - pages: about 12,000 characters of text per read;
  - documents: about 15,000 characters;
  - pages up to 2 MB downloaded;
  - a 20-second timeout.

## Out of Scope

- Voice messages (later), video, audio files.
- OCR of scanned PDFs.
- Saving received files to the laptop (the files tools can do that later on request).
- Search with paid model-native search, such as OrcaRouter's web search for OpenAI or Anthropic models.
