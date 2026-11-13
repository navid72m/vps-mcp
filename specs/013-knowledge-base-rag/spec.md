# Feature Specification: 013 Knowledge Base RAG for Books, Papers & PDFs

## 1. Overview & Objective
The personal assistant currently has long-term user memory (`agent_memory`) for personal facts, identity, operator preferences, and daily routines.
This feature introduces a separate, dedicated **Knowledge Base (RAG)** system tailored for storing, indexing, and querying long-form reference materials—specifically **books, academic papers, research PDFs, and technical documentation**.

The Knowledge Base is cleanly isolated from personal memory:
- **Personal Memory (`agent_memory`)**: Operator profile, preferences, project facts, daily reminders, habits.
- **Knowledge Base (`knowledge_base`)**: Ingested books, scientific papers, manuals, PDFs, and articles.

The implementation adapts the modular architecture from the `pdf` repository (`DocumentProcessor`, `VectorDatabase`, `RAGPipeline`, `Embedder`, and chunking strategies) into a native assistant module with persistence, Telegram tools, and slash commands.

---

## 2. User Scenarios & Acceptance Criteria

### US1: Ingesting Books, Papers, and PDFs
- **Scenario**: The operator asks the assistant to index a research paper or book:
  - *"این کتاب رو به دانش‌نامه اضافه کن: ~/Downloads/deep_learning.pdf"*
  - Or via slash command: `/kb add ~/Documents/Attention_Is_All_You_Need.pdf`
  - Or by sending a PDF file directly in Telegram chat.
- **Criteria**:
  - The document is processed using `DocumentProcessor`, extracting text, sections, and page numbers.
  - Text is chunked using smart paragraph or sentence chunking with overlap.
  - Chunks are embedded and stored in the persistent vector database.
  - Document metadata (title, author, chunk count, file size, hash, added date) is stored in `manifest.json`.

### US2: Searching Knowledge Base
- **Scenario**: The operator asks *"Search my papers for transformer self-attention complexity"* or uses `/kb search self-attention`.
- **Criteria**:
  - Semantic vector search matches relevant chunks across books and papers.
  - Optional filter by document title or author.
  - Returns formatted excerpts with citations (title, page/section number, relevance score).

### US3: Grounded Question Answering (Q&A)
- **Scenario**: The operator asks *"According to the paper 'Attention is All You Need', what is the multi-head attention formula?"*
- **Criteria**:
  - Assistant retrieves relevant chunks via `kb__search` / `kb__ask`.
  - Answers strictly using the retrieved context, citing document title and page/section.
  - If information is not in the knowledge base, explicitly states so without hallucinating.

### US4: Browsing & Managing Documents
- **Scenario**: The operator uses `/kb list` or asks *"What books do I have in my knowledge base?"*.
- **Criteria**:
  - Lists all indexed books and papers with title, author, chunk count, and date added.
  - Allows removing documents via `kb__remove` (with approval gating).

---

## 3. Architecture & Data Isolation

```
~/.local/state/assistant/
├── memory/                  <-- PERSONAL MEMORY (agent_memory: facts, profile, habits)
│   ├── facts/
│   └── index.db
│
└── knowledge_base/          <-- REFERENCE KNOWLEDGE BASE (books, papers, PDFs)
    ├── manifest.json        <-- Registry of indexed books, papers, PDFs
    ├── docs/                <-- Stored or linked original documents
    └── index/               <-- Persistent Vector Store (FAISS index + metadata)
        ├── faiss_index.bin
        ├── documents.json
        ├── mappings.json
        └── metadata.json
```

---

## 4. Built-in Tools
1. `kb__search(query: str, top_k: int = 5, document: str = "")`: Semantic search across books/papers.
2. `kb__ask(query: str, document: str = "")`: Q&A grounded in retrieved document chunks.
3. `kb__list()`: List all indexed documents.
4. `kb__read(document: str, chunk_index: int = 0, limit: int = 3)`: Read consecutive chunks from a document.
5. `kb__add(path: str, title: str = "", author: str = "")`: Ingest a PDF, paper, or book into the knowledge base.
6. `kb__remove(document: str)`: Remove a document and its chunks from the knowledge base (approval required).

---

## 5. Security & Safety
1. **Prompt Injection Defense**: Text extracted from external PDFs/papers is wrapped in untrusted data envelopes when passed to the LLM.
2. **Approval Gating**: Deletion (`kb__remove`) requires operator approval button.
3. **Resource Protection**: Ingestion chunk sizes and batch processing prevent memory exhaustion on large books.
