# Requirements Checklist: 013 Knowledge Base RAG

- [ ] Complete separation between `agent_memory` (personal facts) and `knowledge_base` (books, papers, PDFs)
- [ ] Ingest PDF, DOCX, TXT, and Markdown files
- [ ] Smart chunking (paragraphs/sentences with overlap) and metadata tracking (page, title, source)
- [ ] Persistent vector indexing with sub-millisecond similarity search
- [ ] Prompt injection protection via untrusted data envelopes for all retrieved chunks
- [ ] Direct citations (document title, page, chunk) in answers
- [ ] Built-in tools: `kb__search`, `kb__ask`, `kb__list`, `kb__read`, `kb__add`, `kb__remove`
- [ ] Interactive approval gating for destructive operations (`kb__remove`)
- [ ] Slash command `/kb` supporting `list`, `search`, `ask`, and `add`
- [ ] CLI command `assistant kb`
- [ ] Full test coverage and 0 test suite regressions
