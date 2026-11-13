# Tasks: 013 Knowledge Base RAG for Books, Papers & PDFs

- [x] T001: Install RAG dependencies (`numpy`, `faiss-cpu`, `sentence-transformers` / `fastembed`) in `personal-assistant`
- [x] T002: Add `KnowledgeBaseSection` in `src/assistant/config.py` and register in `Config`
- [x] T003: Update `AUDIT_KINDS` in `src/assistant/state.py` with `"knowledge_base"`
- [x] T004: Update `BUILTIN_PREFIXES` and `BUILTIN_SETS` in `src/assistant/toolsets.py`
- [x] T005: Create `src/assistant/knowledge_base.py` adapting DocumentProcessor, FaissVectorStore, and Manifest
- [x] T006: Add `kb_specs()` and tool execution in `src/assistant/app.py` with approval gating on `kb__remove`
- [x] T007: Add `/kb` slash command in `src/assistant/commands.py`
- [x] T008: Add `assistant kb` CLI management in `src/assistant/cli.py`
- [x] T009: Add unit tests in `tests/unit/pa/test_knowledge_base.py` and runtime tests in `test_knowledge_base_runtime.py`
- [x] T010: Run full test suite, verify 0 regressions, and document quickstart
