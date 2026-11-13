# Data Model: 013 Knowledge Base RAG

## 1. Document Entity (Manifest Entry)
Stored in `manifest.json` under `~/.local/state/assistant/knowledge_base/manifest.json`.

```json
{
  "id": "doc_8f1b2c3d",
  "title": "Attention Is All You Need",
  "author": "Vaswani et al.",
  "filename": "attention_is_all_you_need.pdf",
  "path": "/Users/seyednavidmirnourilangeroudi/.local/state/assistant/knowledge_base/docs/attention_is_all_you_need.pdf",
  "file_hash": "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
  "file_size": 2215840,
  "chunk_count": 42,
  "ingested_at": "2026-11-13T10:30:00Z",
  "tags": ["deep-learning", "transformers", "paper"]
}
```

---

## 2. Chunk Entity
Stored in the vector index (`documents.json` and FAISS index):

```json
{
  "id": "chunk_001_8f1b2c3d",
  "text": "The dominant sequence transduction models are based on complex recurrent or convolutional neural networks...",
  "metadata": {
    "doc_id": "doc_8f1b2c3d",
    "title": "Attention Is All You Need",
    "chunk_index": 0,
    "total_chunks": 42,
    "page": 1,
    "source": "attention_is_all_you_need.pdf"
  },
  "embedding": [0.012, -0.045, 0.089, "..."]
}
```

---

## 3. Search Result Entity
Returned to the agent / slash commands:

```json
{
  "chunk_id": "chunk_001_8f1b2c3d",
  "doc_id": "doc_8f1b2c3d",
  "title": "Attention Is All You Need",
  "page": 1,
  "score": 0.892,
  "text": "The dominant sequence transduction models..."
}
```
