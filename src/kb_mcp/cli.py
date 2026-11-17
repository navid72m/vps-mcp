"""`kb-mcp` CLI: serve | list | add | search | remove."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from kb_mcp.server import build_server, default_kb_storage_dir


def _print(obj: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))
        return
    if isinstance(obj, dict) and "documents" in obj:
        print(f"Total documents: {obj.get('total', len(obj['documents']))}\n")
        for d in obj["documents"]:
            print(f"• {d['title']} ({d['chunk_count']} chunks, {d['file_size']:,} bytes)")
            print(f"  ID: {d['doc_id']} | Source: {d['filename']} | Ingested: {d['ingested_at']}")
        return
    if isinstance(obj, dict) and "results" in obj:
        print(f"Found {len(obj['results'])} relevant passage(s) for {obj.get('query')!r}:\n")
        for idx, r in enumerate(obj["results"], 1):
            page_info = f"page {r['page']}" if r.get("page") else f"chunk {r['chunk_index']}"
            print(f"[{idx}] {r['title']} ({page_info}, score: {r['score']})")
            # Unpack excerpt lines
            lines = r["text"].splitlines()
            for line in lines[:8]:
                print(f"    {line}")
            if len(lines) > 8:
                print("    ...")
            print()
        return
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def cmd_serve(args: argparse.Namespace) -> int:
    server = build_server(args.storage_dir)
    server.run("stdio")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    from assistant.knowledge_base import KnowledgeBase

    dir_path = Path(args.storage_dir).expanduser().resolve() if args.storage_dir else default_kb_storage_dir()
    kb = KnowledgeBase(dir_path)
    docs = kb.list_documents()
    _print({"total": len(docs), "documents": docs}, args.json)
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    from assistant.knowledge_base import KnowledgeBase

    dir_path = Path(args.storage_dir).expanduser().resolve() if args.storage_dir else default_kb_storage_dir()
    kb = KnowledgeBase(dir_path)
    tags = [t.strip() for t in args.tags.split(",")] if args.tags else None
    res = kb.ingest_file(args.path, title=args.title or "", author=args.author or "", tags=tags)
    _print(res, args.json)
    return 0 if res.get("ok") else 1


def cmd_search(args: argparse.Namespace) -> int:
    from assistant.knowledge_base import KnowledgeBase

    dir_path = Path(args.storage_dir).expanduser().resolve() if args.storage_dir else default_kb_storage_dir()
    kb = KnowledgeBase(dir_path)
    res = kb.search(args.query, top_k=args.top_k, document=args.document or "")
    _print({"query": args.query, "results": res}, args.json)
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    from assistant.knowledge_base import KnowledgeBase

    dir_path = Path(args.storage_dir).expanduser().resolve() if args.storage_dir else default_kb_storage_dir()
    kb = KnowledgeBase(dir_path)
    res = kb.remove_document(args.document)
    _print(res, args.json)
    return 0 if res.get("ok") else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="kb-mcp",
        description="Knowledge Base MCP Server: Dense Semantic RAG for Books, Research Papers, and PDFs",
    )
    p.add_argument(
        "--storage-dir",
        default=None,
        help="path to knowledge base storage folder (default: $KB_STORAGE_DIR or ~/.local/state/assistant/knowledge_base)",
    )

    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("serve", help="run the MCP server on stdio")
    s.set_defaults(func=cmd_serve)

    ls = sub.add_parser("list", help="list all indexed documents")
    ls.add_argument("--json", action="store_true", help="output JSON")
    ls.set_defaults(func=cmd_list)

    a = sub.add_parser("add", help="ingest and index a PDF, markdown, or text document")
    a.add_argument("path", help="path to document file")
    a.add_argument("--title", default=None, help="document title")
    a.add_argument("--author", default=None, help="author or publisher")
    a.add_argument("--tags", default=None, help="comma-separated tags")
    a.add_argument("--json", action="store_true", help="output JSON")
    a.set_defaults(func=cmd_add)

    q = sub.add_parser("search", help="dense semantic vector search over indexed books and papers")
    q.add_argument("query", help="natural language search query")
    q.add_argument("--top-k", type=int, default=5, help="maximum excerpts to retrieve")
    q.add_argument("--document", default=None, help="filter by document title, filename or doc_id")
    q.add_argument("--json", action="store_true", help="output JSON")
    q.set_defaults(func=cmd_search)

    rm = sub.add_parser("remove", help="remove a document from the knowledge base")
    rm.add_argument("document", help="document ID, exact title, or filename to delete")
    rm.add_argument("--json", action="store_true", help="output JSON")
    rm.set_defaults(func=cmd_remove)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
