"""Unit tests for kb-mcp standalone MCP server."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest

from assistant.knowledge_base import KnowledgeBase
from kb_mcp.server import build_server, create_server
from tests.conftest import SessionPerCall, call


class MockEmbedder:
    def __init__(self, dimension: int = 384) -> None:
        self.dimension = dimension

    def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.empty((0, self.dimension), dtype="float32")
        vecs = []
        for t in texts:
            h = hash(t)
            np.random.seed(abs(h) % (2**32))
            v = np.random.randn(self.dimension).astype("float32")
            norm = np.linalg.norm(v)
            vecs.append(v / norm if norm > 0 else v)
        return np.vstack(vecs).astype("float32")


@pytest.fixture
def mock_kb(tmp_path: Path) -> KnowledgeBase:
    return KnowledgeBase(tmp_path / "kb_store", embedder=MockEmbedder())


@pytest.fixture
def kb_client(mock_kb: KnowledgeBase) -> SessionPerCall:
    server = create_server(mock_kb)
    return SessionPerCall(server)


@pytest.mark.asyncio
async def test_kb_server_tool_registration(kb_client: SessionPerCall):
    tools = await kb_client.list_tools()
    names = {t.name for t in tools.tools}
    expected = {"kb_search", "kb_read", "kb_list", "kb_add", "kb_remove"}
    assert names == expected
    assert len(names) == 5

    # Verify annotations
    tool_map = {t.name: t for t in tools.tools}
    assert tool_map["kb_search"].annotations.read_only_hint is True
    assert tool_map["kb_read"].annotations.read_only_hint is True
    assert tool_map["kb_list"].annotations.read_only_hint is True
    assert tool_map["kb_add"].annotations.read_only_hint is False
    assert tool_map["kb_remove"].annotations.destructive_hint is True


@pytest.mark.asyncio
async def test_kb_list_empty(kb_client: SessionPerCall):
    res = await call(kb_client, "kb_list")
    assert res["ok"] is True
    assert res["total"] == 0
    assert res["documents"] == []


@pytest.mark.asyncio
async def test_kb_add_and_search_and_read(kb_client: SessionPerCall, tmp_path: Path):
    doc_file = tmp_path / "ddia_notes.txt"
    doc_file.write_text(
        "Chapter 1: Reliable, Scalable, and Maintainable Applications.\n\n"
        "Fault-tolerant systems must handle hardware crashes and network partitions.\n\n"
        "Chapter 2: Data Models and Query Languages: Relational vs Document models."
    )

    # Ingest document
    add_res = await call(
        kb_client,
        "kb_add",
        path=str(doc_file),
        title="DDIA Notes",
        author="Martin Kleppmann",
        tags=["database", "architecture"],
    )
    assert add_res["ok"] is True
    assert add_res["title"] == "DDIA Notes"
    assert add_res["chunk_count"] >= 1
    doc_id = add_res["doc_id"]

    # List documents
    list_res = await call(kb_client, "kb_list")
    assert list_res["ok"] is True
    assert list_res["total"] == 1
    assert list_res["documents"][0]["doc_id"] == doc_id
    assert list_res["documents"][0]["title"] == "DDIA Notes"

    # Search document
    search_res = await call(kb_client, "kb_search", query="fault-tolerant systems")
    assert search_res["ok"] is True
    assert len(search_res["results"]) > 0
    first = search_res["results"][0]
    assert first["title"] == "DDIA Notes"
    assert "score" in first
    assert "<reference_content>" in first["text"]

    # Read sequential chunks
    read_res = await call(kb_client, "kb_read", document=doc_id, chunk_index=0, limit=2)
    assert read_res["ok"] is True
    assert read_res["document"] == doc_id
    assert len(read_res["chunks"]) >= 1


@pytest.mark.asyncio
async def test_kb_remove(kb_client: SessionPerCall, tmp_path: Path):
    doc_file = tmp_path / "temporary.txt"
    doc_file.write_text("Temporary contents for deletion test.")

    add_res = await call(kb_client, "kb_add", path=str(doc_file), title="Temporary")
    doc_id = add_res["doc_id"]

    # Remove
    del_res = await call(kb_client, "kb_remove", document=doc_id)
    assert del_res["ok"] is True
    assert del_res["doc_id"] == doc_id
    assert del_res["removed_chunks"] >= 1

    # Verify empty list
    list_res = await call(kb_client, "kb_list")
    assert list_res["total"] == 0


@pytest.mark.asyncio
async def test_kb_not_found_handling(kb_client: SessionPerCall):
    res = await call(kb_client, "kb_read", document="nonexistent_doc")
    assert res["ok"] is False
    assert res["error"]["category"] == "not_found"


def test_kb_cli_parser():
    from kb_mcp.cli import build_parser

    p = build_parser()
    args_serve = p.parse_args(["serve"])
    assert args_serve.command == "serve"

    args_add = p.parse_args(["add", "test.pdf", "--title", "Book", "--tags", "a,b"])
    assert args_add.command == "add"
    assert args_add.path == "test.pdf"
    assert args_add.title == "Book"
    assert args_add.tags == "a,b"

    args_search = p.parse_args(["search", "query concept", "--top-k", "8"])
    assert args_search.command == "search"
    assert args_search.query == "query concept"
    assert args_search.top_k == 8

    args_rm = p.parse_args(["remove", "doc_123"])
    assert args_rm.command == "remove"
    assert args_rm.document == "doc_123"


def test_build_server(tmp_path: Path):
    s = build_server(tmp_path / "custom_kb")
    assert len(s._tool_manager.list_tools()) == 5
