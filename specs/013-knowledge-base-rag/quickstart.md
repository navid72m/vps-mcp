# Quickstart: 013 Knowledge Base RAG

## 1. Enable in `assistant.toml`
Add the following to `~/.config/assistant/assistant.toml`:

```toml
[knowledge_base]
enabled = true
storage_dir = "~/.local/state/assistant/knowledge_base"
embedding_model = "sentence-transformers/all-MiniLM-L6-v2"
chunk_size = 1000
chunk_overlap = 200
top_k = 5
```

## 2. Ingesting Books & Papers

### Via CLI:
```bash
assistant kb add ~/Downloads/Attention_Is_All_You_Need.pdf --title "Attention Is All You Need" --author "Vaswani et al."
```

### Via Telegram Bot Slash Command:
```
/kb add /path/to/my_book.pdf
```

### Via Natural Language in Telegram Bot:
```
این مقاله رو به دانشنامه اضافه کن: /Users/.../paper.pdf
```

## 3. Querying & Reading Books

```
/kb list
/kb search multi-head attention
/kb ask What is the learning rate used in the transformer paper?
```
Or simply ask in Telegram chat:
> *"توی کتاب معماری کامپیوتر درباره پایپ‌لاین چی نوشته؟"*
