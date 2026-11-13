"""Integration tests for Knowledge Base RAG tools, approvals, slash commands, and CLI in runtime."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from assistant.cli import cmd_kb
from assistant.knowledge_base import KnowledgeBase
from tests.assistant_fakes import harness, say, use
from tests.unit.pa.test_knowledge_base import MockEmbedder


@pytest.fixture
def mock_kb(tmp_path: Path) -> KnowledgeBase:
    return KnowledgeBase(
        tmp_path / "kb_data",
        chunk_size=100,
        chunk_overlap=20,
        embedder=MockEmbedder(),
    )


@pytest.mark.asyncio
async def test_tool_registration(tmp_path: Path) -> None:
    # 1. Disabled
    h_off = await harness(tmp_path, knowledge_base={"enabled": False})
    names_off = [s.name for s in h_off.a.agent.builtin_specs]
    assert not any(n.startswith("kb__") for n in names_off)

    # 2. Enabled
    h_on = await harness(tmp_path, knowledge_base={"enabled": True})
    names_on = [s.name for s in h_on.a.agent.builtin_specs]
    assert "kb__search" in names_on
    assert "kb__ask" in names_on
    assert "kb__list" in names_on
    assert "kb__read" in names_on
    assert "kb__add" in names_on
    assert "kb__remove" in names_on


@pytest.mark.asyncio
async def test_runtime_tools_execution(tmp_path: Path, mock_kb: KnowledgeBase) -> None:
    h = await harness(tmp_path, knowledge_base={"enabled": True})
    h.a.kb = mock_kb

    doc_file = tmp_path / "quantum.txt"
    doc_file.write_text(
        "Quantum computing harnesses the phenomena of quantum mechanics.\n\n"
        "Superposition and entanglement are key resources for speedups.\n\n"
        "Qubits can represent combinations of 0 and 1 simultaneously.\n\n"
        "Applications include quantum chemistry and cryptography algorithms.\n"
    )

    # 1. kb__add
    out, is_err = await h.a.builtin(
        "kb__add",
        {"path": str(doc_file), "title": "Quantum Computing 101", "author": "Alice"},
        chat_id=111,
    )
    assert not is_err
    data = json.loads(out)
    assert data["content"]["title"] == "Quantum Computing 101"
    assert data["content"]["chunk_count"] >= 2
    doc_id = data["content"]["doc_id"]

    # 2. kb__list
    out, is_err = await h.a.builtin("kb__list", {}, chat_id=111)
    assert not is_err
    data = json.loads(out)
    docs = data["content"]["documents"]
    assert len(docs) == 1
    assert docs[0]["doc_id"] == doc_id
    assert docs[0]["author"] == "Alice"

    # 3. kb__search
    out, is_err = await h.a.builtin(
        "kb__search",
        {"query": "superposition and qubits", "top_k": 3},
        chat_id=111,
    )
    assert not is_err
    data = json.loads(out)
    results = data["content"]["results"]
    assert len(results) > 0
    assert "[untrusted reference excerpt" in results[0]["text"]
    assert "Quantum Computing 101" in results[0]["title"]

    # 4. kb__ask
    out, is_err = await h.a.builtin(
        "kb__ask",
        {"query": "what are the applications of qubits?", "top_k": 2},
        chat_id=111,
    )
    assert not is_err
    data = json.loads(out)
    content = data["content"]
    assert content["query"] == "what are the applications of qubits?"
    assert len(content["retrieved_passages"]) > 0

    # 5. kb__read
    out, is_err = await h.a.builtin(
        "kb__read",
        {"document": "Quantum Computing 101", "chunk_index": 0, "limit": 2},
        chat_id=111,
    )
    assert not is_err
    data = json.loads(out)
    chunks = data["content"]["chunks"]
    assert len(chunks) >= 1
    assert chunks[0]["chunk_index"] == 0

    # 6. knowledge_base__search alias
    out, is_err = await h.a.builtin(
        "knowledge_base__search",
        {"query": "qubits", "top_k": 1},
        chat_id=111,
    )
    assert not is_err
    data = json.loads(out)
    assert len(data["content"]["results"]) > 0


@pytest.mark.asyncio
async def test_remove_approval_flow(tmp_path: Path, mock_kb: KnowledgeBase) -> None:
    h = await harness(tmp_path, knowledge_base={"enabled": True})
    h.a.kb = mock_kb

    doc_file = tmp_path / "paper.txt"
    doc_file.write_text("Artificial Intelligence and Reinforcement Learning.")
    mock_kb.ingest_file(str(doc_file), title="AI Paper")

    assert len(mock_kb.list_documents()) == 1

    h.provider.add(
        use("kb__remove", {"document": "AI Paper"}),
        say("Document AI Paper has been removed from knowledge base."),
    )

    task = asyncio.create_task(h.say("Please delete AI Paper from the knowledge base"))
    for _ in range(200):
        if h.tg.sent and h.tg.buttons():
            break
        await asyncio.sleep(0.01)

    # Verify approval prompt was sent
    assert any(
        "Delete document 'AI Paper' and all its vector embeddings" in m["text"] for m in h.tg.sent
    )
    data = next(d for label, d in h.tg.buttons() if label.startswith("✅"))
    await h.press(data)
    await h.finish()
    await task

    # Document should now be deleted from KB
    assert len(mock_kb.list_documents()) == 0
    assert any("Document AI Paper has been removed" in m["text"] for m in h.tg.sent)


@pytest.mark.asyncio
async def test_remove_approval_rejection(tmp_path: Path, mock_kb: KnowledgeBase) -> None:
    h = await harness(tmp_path, knowledge_base={"enabled": True})
    h.a.kb = mock_kb

    doc_file = tmp_path / "paper2.txt"
    doc_file.write_text("Robotics and Automation.")
    mock_kb.ingest_file(str(doc_file), title="Robotics 101")

    assert len(mock_kb.list_documents()) == 1

    h.provider.add(
        use("kb__remove", {"document": "Robotics 101"}),
        say("Removal cancelled."),
    )

    task = asyncio.create_task(h.say("Remove Robotics 101"))
    for _ in range(200):
        if h.tg.sent and h.tg.buttons():
            break
        await asyncio.sleep(0.01)

    data = next(d for label, d in h.tg.buttons() if label.startswith("❌"))
    await h.press(data)
    await h.finish()
    await task

    # Document should NOT be deleted
    assert len(mock_kb.list_documents()) == 1


@pytest.mark.asyncio
async def test_slash_command_kb(tmp_path: Path, mock_kb: KnowledgeBase) -> None:
    # 1. Enabled
    h = await harness(tmp_path, knowledge_base={"enabled": True})
    h.a.kb = mock_kb

    # /kb list empty
    await h.say("/kb list")
    await h.finish()
    assert any("Knowledge Base is empty" in m["text"] for m in h.tg.sent)

    # Ingest document
    doc_file = tmp_path / "math.txt"
    doc_file.write_text("Calculus and Linear Algebra are fundamental mathematics.")
    mock_kb.ingest_file(str(doc_file), title="Math Fundamentals")

    # /kb list with doc
    await h.say("/kb")
    await h.finish()
    assert any("Math Fundamentals" in m["text"] for m in h.tg.sent)

    # /kb search query
    await h.say("/kb search calculus")
    await h.finish()
    assert any("Knowledge Base Results for 'calculus'" in m["text"] for m in h.tg.sent)

    # /kb add path
    doc_file2 = tmp_path / "physics.txt"
    doc_file2.write_text("Classical mechanics and relativity.")
    await h.say(f"/kb add {doc_file2}")
    await h.finish()
    assert any("Successfully ingested" in m["text"] for m in h.tg.sent)

    # /kb unknown
    await h.say("/kb invalid")
    await h.finish()
    assert any("Usage: /kb [list | search <query> | add <path>]" in m["text"] for m in h.tg.sent)

    # 2. Disabled
    h_off = await harness(tmp_path, knowledge_base={"enabled": False})
    await h_off.say("/kb")
    await h_off.finish()
    assert any("Knowledge Base is not enabled" in m["text"] for m in h_off.tg.sent)


def test_cli_kb(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(
        "assistant.knowledge_base.SentenceTransformerEmbedder",
        MockEmbedder,
    )

    cfg_file = tmp_path / "assistant.toml"
    kb_dir = tmp_path / "kb_cli"
    cfg_file.write_text(
        f"""
[assistant]
name = "Tester"

[telegram]
allowed_user_ids = [111]

[knowledge_base]
enabled = true
storage_dir = "{kb_dir}"
"""
    )

    doc_file = tmp_path / "cli_doc.txt"
    doc_file.write_text("CLI testing document for Knowledge Base integration.")

    # 1. assistant kb add
    args_add = argparse.Namespace(
        config=cfg_file,
        action="add",
        path=str(doc_file),
        title="CLI Book",
        author="Tester",
    )
    assert cmd_kb(args_add) == 0

    # 2. assistant kb list
    args_list = argparse.Namespace(
        config=cfg_file,
        action="list",
    )
    assert cmd_kb(args_list) == 0

    # 3. assistant kb search
    args_search = argparse.Namespace(
        config=cfg_file,
        action="search",
        query="testing",
        top_k=3,
    )
    assert cmd_kb(args_search) == 0

    # 4. assistant kb remove
    args_rem = argparse.Namespace(
        config=cfg_file,
        action="remove",
        doc="CLI Book",
    )
    assert cmd_kb(args_rem) == 0
