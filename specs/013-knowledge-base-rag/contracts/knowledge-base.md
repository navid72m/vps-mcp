# Contracts: 013 Knowledge Base Tools

## Built-in Tools

### 1. `kb__search`
- **Description**: Search passages across ingested books, research papers, and PDFs using semantic similarity.
- **Parameters**:
  - `query` (string, required): Natural language search query.
  - `top_k` (integer, default=5, max=20): Number of top matching passages to return.
  - `document` (string, optional): Filter results to a specific document title, author, or doc ID.
- **Output**: Ranked list of matching chunks with document title, page number, relevance score, and untrusted text envelope.

### 2. `kb__ask`
- **Description**: Answer questions based strictly on context retrieved from the Knowledge Base (books, papers, PDFs).
- **Parameters**:
  - `query` (string, required): Question to answer.
  - `document` (string, optional): Restrict context to a specific book, paper, or document.
  - `top_k` (integer, default=5): Number of supporting chunks to retrieve.
- **Output**: Grounded answer with source document citations and supporting text snippets.

### 3. `kb__list`
- **Description**: List all books, papers, and PDFs currently indexed in the Knowledge Base.
- **Parameters**: none.
- **Output**: Array of indexed documents with title, author, chunk count, file size, and date added.

### 4. `kb__read`
- **Description**: Read consecutive chunks or sections from an indexed book or paper.
- **Parameters**:
  - `document` (string, required): Document title or ID to read from.
  - `chunk_index` (integer, default=0): Starting chunk offset.
  - `limit` (integer, default=3, max=10): Number of consecutive chunks to return.
- **Output**: Sequential text chunks with chunk index and page numbers.

### 5. `kb__add`
- **Description**: Ingest a new book, paper, or PDF file into the Knowledge Base.
- **Parameters**:
  - `path` (string, required): Absolute or relative filesystem path to the PDF, DOCX, TXT, or Markdown document.
  - `title` (string, optional): Human-friendly title (defaults to filename).
  - `author` (string, optional): Author name if known.
  - `tags` (array of strings, optional): Categorical tags.
- **Output**: Confirmation with assigned document ID, total extracted chunks, and processing summary.

### 6. `kb__remove`
- **Description**: Remove an indexed document and its vector embeddings from the Knowledge Base. Requires operator approval.
- **Parameters**:
  - `document` (string, required): Document title or ID to remove.
- **Policy**: Gated with interactive approval button.
- **Output**: Confirmation that document and chunks were deleted.
