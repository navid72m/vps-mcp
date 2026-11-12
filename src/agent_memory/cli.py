"""`agent-memory` CLI: serve | save --operator | search | doctor | migrate-004 | bench."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from agent_memory.errors import AgentMemoryError
from agent_memory.store import Memory


def _mem(args: argparse.Namespace, **kw: Any) -> Memory:
    return Memory.open(
        args.store,
        project=getattr(args, "project", None),
        environment=getattr(args, "environment", None),
        client=getattr(args, "client", None) or "cli",
        **kw,
    )


def _print(obj: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))
        return
    if isinstance(obj, dict) and "groups" in obj:
        print(
            f"{obj['returned']} of {obj['total']} for {obj['query']!r} in {', '.join(obj['scope'])}"
        )
        for group, items in obj["groups"].items():
            print(f"\n{group}:")
            for it in items:
                flags = f"  [{', '.join(it['flags'])}]" if it.get("flags") else ""
                print(
                    f"  - {it['subject']}: {it['title']} ({it['confidence']}, {it['age']})"
                    f"{flags}\n      {it['content'].splitlines()[0][:160]}"
                )
        if obj.get("message"):
            print(obj["message"])
        return
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def cmd_serve(args: argparse.Namespace) -> int:
    from agent_memory.server import create_server

    mem = Memory.open(
        args.store,
        project=args.project,
        environment=args.environment,
        task=args.task,
        client=args.client or "mcp",
        allow_scopes=args.allow_scope,
    )
    create_server(mem).run("stdio")
    return 0


def cmd_save(args: argparse.Namespace) -> int:
    if not args.operator:
        print("error: the CLI saves as the operator; pass --operator to confirm", file=sys.stderr)
        return 2
    content = Path(args.file).read_text() if args.file else args.content
    mem = Memory.open(args.store, client="operator", allow_scopes=["*"])
    res = mem.save(
        {
            "type": args.type,
            "scope": args.scope,
            "subject": args.subject,
            "title": args.title,
            "content": content,
            "importance": args.importance,
            "pinned": args.pinned,
            "tags": args.tag or [],
            "alternatives": args.alternative or [],
        },
        operator=True,
    )
    _print(res, args.json)
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    mem = _mem(args, read_only=True)
    _print(mem.search(args.query, limit=args.limit, include_history=args.history), args.json)
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    mem = Memory.open(args.store, client="doctor", allow_scopes=["*"])
    if args.rebuild_index:
        mem.index.rebuild()
    report = mem.doctor()
    _print(report, True)
    return 1 if report["malformed"] else 0


def cmd_migrate(args: argparse.Namespace) -> int:
    from agent_memory import migrate004

    items = migrate004.plan(Path(args.folder).expanduser(), args.project, args.environment)
    for line in migrate004.describe(items):
        print(line)
    if not args.apply:
        print(f"\ndry run: {len(items)} note(s); re-run with --apply to write them")
        return 0
    mem = Memory.open(
        args.store,
        project=args.project,
        environment=args.environment,
        client="migrate-004",
        allow_scopes=["*"],
    )
    for r in migrate004.apply(mem, items):
        print(f"{r['note']}: {r['outcome']} {r.get('id', '')}")
    return 0


def cmd_bench(args: argparse.Namespace) -> int:
    from agent_memory.bench import runner

    data = None
    if args.fixtures and args.fixtures != "builtin":
        data = json.loads(Path(args.fixtures).read_text())
    report = runner.run(k=args.k, data=data)
    print(json.dumps(report, indent=2) if args.json else runner.table(report))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agent-memory", description="Managed memory for agents")
    p.add_argument(
        "--store", help="store folder (default $AGENT_MEMORY_HOME or ~/.local/share/agent-memory)"
    )
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("serve", help="run the MCP server on stdio")
    s.add_argument("--project")
    s.add_argument("--environment")
    s.add_argument("--task")
    s.add_argument("--client")
    s.add_argument(
        "--allow-scope",
        action="append",
        default=[],
        help="extra scopes this session may read/write (fnmatch pattern)",
    )
    s.set_defaults(func=cmd_serve)

    v = sub.add_parser("save", help="save a memory as the operator (trusted)")
    v.add_argument("--operator", action="store_true")
    v.add_argument(
        "--type", required=True, choices=["episodic", "semantic", "procedural", "decision"]
    )
    v.add_argument("--scope", required=True)
    v.add_argument("--subject", required=True)
    v.add_argument("--title", required=True)
    g = v.add_mutually_exclusive_group(required=True)
    g.add_argument("--content")
    g.add_argument("--file")
    v.add_argument("--importance", type=int, default=3)
    v.add_argument("--pinned", action="store_true")
    v.add_argument("--tag", action="append")
    v.add_argument("--alternative", action="append")
    v.add_argument("--json", action="store_true")
    v.set_defaults(func=cmd_save)

    q = sub.add_parser("search", help="search memories")
    q.add_argument("query")
    q.add_argument("--project")
    q.add_argument("--environment")
    q.add_argument("--limit", type=int, default=8)
    q.add_argument("--history", action="store_true")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_search)

    d = sub.add_parser("doctor", help="store health: counts, malformed files, warnings")
    d.add_argument("--rebuild-index", action="store_true")
    d.set_defaults(func=cmd_doctor)

    m = sub.add_parser("migrate-004", help="import a vps-mcp feature-004 memory folder")
    m.add_argument("folder")
    m.add_argument("--project", required=True)
    m.add_argument("--environment")
    m.add_argument("--apply", action="store_true")
    m.set_defaults(func=cmd_migrate)

    b = sub.add_parser("bench", help="run the memory benchmark (read-only)")
    b.add_argument("--fixtures", default="builtin")
    b.add_argument("--k", type=int, default=8)
    b.add_argument("--json", action="store_true")
    b.set_defaults(func=cmd_bench)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except AgentMemoryError as err:
        print(f"error ({err.category}): {err.message}", file=sys.stderr)
        return 1
