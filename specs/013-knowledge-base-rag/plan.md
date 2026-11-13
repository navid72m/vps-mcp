# Implementation Plan: 013 Knowledge Base RAG

## Phase 1: Dependencies & Configuration
- Add necessary dependencies to `personal-assistant` (`numpy`, `sentence-transformers`, `faiss-cpu`, or `fastembed`).
- Define `KnowledgeBaseSection` in `src/assistant/config.py`:
  - `enabled`: bool (default false)
  - `storage_dir`: str (default `~/.local/state/assistant/knowledge_base`)
  - `embedding_model`: str (default `sentence-transformers/all-MiniLM-L6-v2`)
  - `chunk_size`: int (default 1000)
  - `chunk_overlap`: int (default 200)
  - `top_k`: int (default 5)
- Add `"knowledge_base"` to `AUDIT_KINDS` in `src/assistant/state.py`.
- Register `"knowledge_base"` in `BUILTIN_SETS` and `kb__*` in `BUILTIN_PREFIXES` in `src/assistant/toolsets.py`.

## Phase 2: Core Knowledge Base Engine
- Create `src/assistant/knowledge_base.py`:
  - `KnowledgeBase`: Manages the manifest, document storage, and vector index.
  - Adapted `DocumentProcessor` supporting PDF (PyMuPDF / pypdf), DOCX, TXT, Markdown, with smart paragraph and sentence chunking.
  - Persistent vector store adapted from `pdf`'s `FaissVectorDatabase` with fallback keyword search.
  - Ingestion workflow: file reading -> text extraction -> chunking -> embedding -> index update -> manifest save.
  - Retrieval workflow: query embedding -> FAISS search -> metadata filtering -> untrusted envelope wrapping.

## Phase 3: Assistant Runtime Wiring & Approvals
- In `src/assistant/app.py`:
  - Register `kb_specs()`: `kb__search`, `kb__ask`, `kb__list`, `kb__read`, `kb__add`, `kb__remove`.
  - Wire `kb_tool` execution dispatcher.
  - Gate `kb__remove` with `self.approvals.request(...)`.
  - Add Telegram document attachment handler: when a user sends a PDF file to the Telegram bot, offer to index it into the Knowledge Base.

## Phase 4: Slash Commands & CLI
- In `src/assistant/commands.py`:
  - Implement `/kb [list | search <q> | ask <q> | add <path>]`.
- In `src/assistant/cli.py`:
  - Implement `assistant kb` CLI commands (`list`, `add <path>`, `search <q>`, `remove <id>`).

## Phase 5: Verification & Quality Assurance
- Unit tests: document chunking, PDF extraction, vector store persistence, manifest updates.
- Runtime tests: tool execution, approval gating on `kb__remove`, slash command execution.
- Full pytest test suite with 0 failures and complete linter clean pass.
