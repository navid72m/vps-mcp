# Research: 013 Knowledge Base RAG for Books, Papers & PDFs

## 1. Study of the Cloned `pdf` Repository
The cloned `pdf` repository (`/Users/seyednavidmirnourilangeroudi/vps-mcp/pdf`) provides:

1. **`document/processor.py`**:
   - `DocumentProcessor`: robust multi-format text extraction:
     - PDF: uses `PyMuPDF` (`fitz`), with fallbacks to `PyPDF2` or `pypdf`.
     - DOCX: uses `docx.Document`.
     - TXT / Markdown: native standard library.
   - Chunking strategies:
     - `paragraph`: splits on `\n\n` or sentence clusters, ideal for books and papers.
     - `sentence`: groups sentences with configurable overlap.
     - `character`: character slicing with boundary preservation.
     - `auto`: chooses paragraph for shorter documents, character for large texts.
   - Metadata enrichment: attaches `source`, `chunk_index`, `total_chunks`, `char_count`.

2. **`storage/vector_db.py`**:
   - `FaissVectorDatabase`:
     - Built on `faiss-cpu`.
     - Full persistence: `.save(directory)` writes `faiss_index.bin`, `documents.json`, `mappings.json`, `metadata.json`.
     - `.load(directory)` reloads index and all documents.
   - `KeywordVectorDatabase`:
     - Fallback keyword/BM25 vector database without requiring FAISS.

3. **`embedding/model.py`**:
   - `SentenceTransformerEmbedding`: embeds texts via SentenceTransformers (`all-MiniLM-L6-v2`, `bge-small-en-v1.5`, etc.).
   - Normalization: L2 normalization enabled by default for cosine similarity.

4. **`rag/pipeline.py` & `rag/interfaces.py`**:
   - Clean structural protocols: `Embedder`, `DocumentStore`, `Retriever`, `Generator`.
   - Pipeline handles file addition, batch embedding, chunk storage, vector search, and extractive/generative question answering.

---

## 2. Integration Strategy for Personal Assistant

### Why Separate from Memory?
- `agent_memory` is an episodic/semantic memory store for operator facts ("Navid lives in Rasht", "insulin routine"). It uses small markdown files and FTS5 keyword indexing.
- Long-form literature (e.g., a 400-page textbook or a 30-page ML paper) would bloat personal memory with thousands of unrelated facts and degrade recall of operator preferences.
- A dedicated **Knowledge Base** with vector embeddings, chunk-level citations, and document-level management gives optimal retrieval performance without cluttering the assistant's personal memory.

### Embedding Engine Selection
- The assistant can run `sentence-transformers` (e.g. `all-MiniLM-L6-v2`, 384 dimensions), or lightweight ONNX / API embeddings.
- Storing vectors in FAISS ensures sub-millisecond retrieval even across dozens of full-length books.

### PDF Parsing in macOS Environment
- `pypdf` is already installed in `personal-assistant` venv.
- `pdfplumber` or `PyMuPDF` can be installed for advanced table/formula extraction from scientific papers.
- `DocumentProcessor` will support `pypdf` as primary/fallback so PDF parsing works reliably.
