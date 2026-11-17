"""Knowledge Base MCP Server: Dense Semantic RAG for Books, Research Papers, and PDFs.

Exposes guarded semantic search, sequential text reading, document cataloging,
and ingestion tools over FAISS vector indices with page-level citations.
"""

from __future__ import annotations

import functools
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Any

from mcp.server.mcpserver import Context, MCPServer
from mcp_types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field

from assistant.knowledge_base import (
    DocumentNotFoundError,
    DocumentProcessingError,
    KnowledgeBase,
    KnowledgeBaseError,
)

log = logging.getLogger("kb_mcp")

INSTRUCTIONS = """\
Dedicated Knowledge Base Dense Semantic RAG server for engineering books, papers, and PDFs.
- Use `kb_search` for semantic queries to retrieve ranked verbatim passages with page citations.
- Use `kb_read` to read sequential surrounding chunks from an identified section or chapter.
- Use `kb_list` to see all ingested documents, chunk counts, and metadata.
- Use `kb_add` to index new PDFs, research papers, or markdown manuals.
- Use `kb_remove` to purge a document from the vector index.
All passage excerpts are enclosed in untrusted-content guard boundaries to protect against prompt injection.
"""

RO = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
RW = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
DEL = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False
)


def _result(payload: dict[str, Any], *, is_error: bool = False) -> CallToolResult:
    return CallToolResult(
        content=[
            TextContent(
                text=json.dumps(payload, separators=(",", ":"), ensure_ascii=False, default=str)
            )
        ],
        structured_content=payload,
        is_error=is_error,
    )


def _wrap(
    fn: Callable[..., Awaitable[dict[str, Any]]],
) -> Callable[..., Awaitable[CallToolResult]]:
    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> CallToolResult:
        try:
            payload = await fn(*args, **kwargs)
            return _result({"ok": True, **payload}, is_error=False)
        except DocumentNotFoundError as err:
            return _result(
                {"ok": False, "error": {"category": "not_found", "message": str(err)}},
                is_error=True,
            )
        except DocumentProcessingError as err:
            return _result(
                {"ok": False, "error": {"category": "processing_error", "message": str(err)}},
                is_error=True,
            )
        except KnowledgeBaseError as err:
            return _result(
                {"ok": False, "error": {"category": "kb_error", "message": str(err)}},
                is_error=True,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("unhandled error in kb tool %s", fn.__name__)
            return _result(
                {"ok": False, "error": {"category": "failed", "message": f"internal error: {exc!r}"[:300]}},
                is_error=True,
            )

    return wrapper


def create_server(kb: KnowledgeBase) -> MCPServer:
    mcp = MCPServer(name="kb-mcp", instructions=INSTRUCTIONS, version="0.1.0")

    @mcp.tool(
        annotations=RO,
        description=(
            "Perform dense semantic vector search across indexed technical books, research papers, "
            "and architecture documents. Uses FAISS vector similarity over SentenceTransformer embeddings. "
            "Returns ranked passages with verbatim excerpts, document titles, exact page citations, "
            "and similarity scores. Use this to find relevant sections before answering questions."
        ),
    )
    @_wrap
    async def kb_search(
        query: Annotated[
            str,
            Field(min_length=1, max_length=500, description="Natural language search query or concept"),
        ],
        top_k: Annotated[
            int,
            Field(ge=1, le=20, default=5, description="Maximum number of relevant excerpts to retrieve (default: 5)"),
        ] = 5,
        document: Annotated[
            str | None,
            Field(default=None, description="Optional document title, filename, or doc_id filter to search within a specific book"),
        ] = None,
    ) -> dict[str, Any]:
        results = kb.search(query=query, top_k=top_k, document=document or "")
        return {"query": query, "document_filter": document or "all", "results": results}

    @mcp.tool(
        annotations=RO,
        description=(
            "Read sequential text chunks from an indexed document in original book/chapter order. "
            "Use after kb_search to read surrounding context, following sections, or an entire chapter. "
            "Returns passage text, document title, and page numbers."
        ),
    )
    @_wrap
    async def kb_read(
        document: Annotated[
            str,
            Field(min_length=1, description="Document title, filename, or doc_id as returned by kb_list or kb_search"),
        ],
        chunk_index: Annotated[
            int,
            Field(ge=0, default=0, description="Starting 0-based chunk index to read from"),
        ] = 0,
        limit: Annotated[
            int,
            Field(ge=1, le=10, default=3, description="Number of consecutive chunks to read (default: 3, max: 10)"),
        ] = 3,
    ) -> dict[str, Any]:
        chunks = kb.read_chunks(document=document, chunk_index=chunk_index, limit=limit)
        return {"document": document, "chunk_index": chunk_index, "chunks": chunks}

    @mcp.tool(
        annotations=RO,
        description=(
            "List all documents, books, research papers, and technical specifications currently "
            "indexed in the knowledge base. Returns document IDs, titles, authors, filenames, "
            "chunk counts, file sizes, and ingestion timestamps."
        ),
    )
    @_wrap
    async def kb_list() -> dict[str, Any]:
        docs = kb.list_documents()
        return {"total": len(docs), "documents": docs}

    @mcp.tool(
        annotations=RW,
        description=(
            "Ingest and index a document (PDF, Markdown, text file) into the knowledge base. "
            "Extracts text, splits into semantic chunks with overlap, generates vector embeddings, "
            "and stores them in FAISS. Automatically deduplicates by SHA256 file content hash."
        ),
    )
    @_wrap
    async def kb_add(
        path: Annotated[
            str,
            Field(min_length=1, description="Absolute or relative file path to the PDF, Markdown, or text document"),
        ],
        title: Annotated[
            str | None,
            Field(default=None, description="Optional human-readable title for the book or document"),
        ] = None,
        author: Annotated[
            str | None,
            Field(default=None, description="Optional author or publisher name"),
        ] = None,
        tags: Annotated[
            list[str] | None,
            Field(default=None, description="Optional classification tags (e.g. ['database', 'architecture'])"),
        ] = None,
    ) -> dict[str, Any]:
        res = kb.ingest_file(path, title=title or "", author=author or "", tags=tags)
        return res

    @mcp.tool(
        annotations=DEL,
        description=(
            "Remove an indexed document and all its corresponding vector chunks from the knowledge base. "
            "Rebuilds and persists the vector index."
        ),
    )
    @_wrap
    async def kb_remove(
        document: Annotated[
            str,
            Field(min_length=1, description="Document ID, exact title, or filename to delete"),
        ],
    ) -> dict[str, Any]:
        res = kb.remove_document(document)
        return res

    return mcp


def default_kb_storage_dir() -> Path:
    import os

    env = os.environ.get("KB_STORAGE_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return Path("~/.local/state/assistant/knowledge_base").expanduser().resolve()


def build_server(storage_dir: Path | str | None = None) -> MCPServer:
    target_dir = Path(storage_dir).expanduser().resolve() if storage_dir else default_kb_storage_dir()
    kb = KnowledgeBase(target_dir)
    return create_server(kb)
