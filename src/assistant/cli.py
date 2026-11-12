"""`assistant` CLI: run | check | service | audit | forget | secrets (contracts)."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from assistant import config as cfgmod
from assistant import service
from assistant.secrets import SecretMissing, provider_for
from assistant.state import State

EXIT_CONFIG, EXIT_SECRET, EXIT_RUNNING = 1, 2, 3
REPO = Path(__file__).resolve().parent.parent.parent


def _load(args: argparse.Namespace) -> cfgmod.Config:
    return cfgmod.load(args.config)


def _names(cfg: cfgmod.Config) -> list[str]:
    return ["telegram_bot_token", *cfg.model.secret_names]


def _secrets(cfg: cfgmod.Config) -> dict[str, str]:
    prov = provider_for(cfg.secrets.provider, cfg.secrets.service, cfg.secrets.file)
    return {n: prov.get(n) for n in _names(cfg)}


def make_provider(m: cfgmod.ModelSection, secret: Callable[[str], str]):  # type: ignore[no-untyped-def]
    key = secret(m.api_key_secret) if m.api_key_secret else ""
    if m.provider == "openai":
        from assistant.providers.openai_compat import OpenAICompatProvider

        return OpenAICompatProvider(
            m.base_url, m.name, key, timeout=m.timeout_s, reasoning_effort=m.reasoning_effort
        )
    from assistant.providers.anthropic import AnthropicProvider

    return AnthropicProvider(key, m.name)


def make_router(cfg: cfgmod.Config, state: State):  # type: ignore[no-untyped-def]
    from assistant.providers.router import ModelRouter

    prov = provider_for(cfg.secrets.provider, cfg.secrets.service, cfg.secrets.file)
    router = ModelRouter(cfg.profiles, lambda m: make_provider(m, prov.get), cfg.model.alias)
    chosen = state.kv_get("model")  # the last /model choice survives restarts
    if chosen and chosen != router.current:
        try:
            router.switch(chosen)
        except (KeyError, SecretMissing):
            state.kv_set("model", router.current)
    return router


def build(cfg: cfgmod.Config, secrets: dict[str, str]):  # type: ignore[no-untyped-def]
    from assistant.app import Assistant
    from assistant.memory import AssistantMemory
    from assistant.telegram import TelegramChannel
    from assistant.toolhub import ToolHub
    from vps_mcp.remote.redact import scrub

    state = State(cfg.state_dir / "assistant.db")
    channel = TelegramChannel(secrets["telegram_bot_token"], cfg.telegram.allowed_user_ids, state)
    hub = ToolHub(cfg.server, state, mask=lambda s: scrub(s, []))
    return Assistant(
        cfg,
        provider=make_router(cfg, state),
        channel=channel,
        state=state,
        hub=hub,
        memory=AssistantMemory(cfg),
    )


def cmd_run(args: argparse.Namespace) -> int:
    from assistant.app import AlreadyRunning, InstanceLock
    from vps_mcp.remote.redact import install_redaction

    cfg = _load(args)
    secrets = _secrets(cfg)
    for name in ("assistant", "httpx2", "httpcore", "mcp", "anthropic", "agent_memory"):
        install_redaction(logging.getLogger(name), [])
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    lock = InstanceLock(cfg.state_dir / "assistant.lock")
    try:
        lock.acquire()
    except AlreadyRunning as exc:
        print(f"assistant is already running (pid {exc})", file=sys.stderr)
        return EXIT_RUNNING
    try:
        asyncio.run(build(cfg, secrets).run())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        lock.release()
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    cfg = _load(args)
    print(f"config ok: {args.config or cfgmod.default_path()}")
    _secrets(cfg)
    print("secrets ok")

    async def servers() -> None:
        from assistant.toolhub import ToolHub

        state_db = State(cfg.state_dir / "assistant.db")
        hub = ToolHub(cfg.server, state_db)
        await hub.connect()
        for s in hub.status():
            state = "ok" if s["up"] else f"DOWN ({s['error']})"
            print(
                f"{s['server']}: {state}, {s['tools']} tools ({s['auto']} auto, {s['ask']} ask, "
                f"{s['never']} never)"
            )
        tools_summary(cfg, hub, state_db)
        await hub.close()

    asyncio.run(servers())
    from assistant.memory import AssistantMemory

    mem = AssistantMemory(cfg)
    print(f"memory: {'ok' if mem.available else 'unavailable ' + str(mem.error)}")
    if cfg.web.enabled:
        prov = provider_for(cfg.secrets.provider, cfg.secrets.service, cfg.secrets.file)
        try:
            prov.get(cfg.web.key_name)
            print(f"web: search ok ({cfg.web.search_provider}), page reading ok")
        except SecretMissing:
            print(
                f"web: page reading ok; search not set up (store a {cfg.web.search_provider} "
                f"API key: security add-generic-password -U -s {cfg.secrets.service} "
                f"-a {cfg.web.key_name} -w)"
            )
    return 0


class _Quiet:
    """Stands in for Telegram when only the tool catalog is needed."""

    async def send(self, *a: object, **k: object) -> int:
        return 0


def tools_summary(cfg: cfgmod.Config, hub: object, state_db: State) -> None:
    from typing import Any, cast

    from assistant.app import Assistant
    from assistant.memory import AssistantMemory
    from assistant.toolsets import spec_chars

    a = Assistant(
        cfg,
        provider=make_router(cfg, state_db),
        channel=cast(Any, _Quiet()),
        state=state_db,
        hub=cast(Any, hub),
        memory=AssistantMemory(cfg),
    )
    cat = a.agent.catalog()
    full = spec_chars(a.agent.all_specs())
    if not cfg.tools.lazy:
        print(f"tools: lazy off · all tools on every call · ~{full // 4 / 1000:.1f}k tokens")
        return
    size = cat.size()
    print(
        f"tools: lazy on · {len(cat.core)} core · {len(cat.sets)} toolsets · tool section "
        f"~{size // 4 / 1000:.1f}k tokens (all: ~{full // 4 / 1000:.1f}k, {100 * size // max(1, full)}%)"
    )
    for w in cat.warnings:
        print(f"warning: {w}")


def cmd_service(args: argparse.Namespace) -> int:
    cfg = _load(args)
    if args.action == "status":
        print(service.status())
        return 0
    if args.action == "uninstall":
        print("removed", service.uninstall(systemd=args.systemd_user))
        return 0
    uv = shutil.which("uv") or "uv"
    conf = Path(args.config).expanduser().resolve() if args.config else None
    print("installed", service.install(uv, REPO, conf, cfg.state_dir, systemd=args.systemd_user))
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    cfg = _load(args)
    unit = args.since[-1]
    n = int(args.since[:-1])
    delta = {"h": timedelta(hours=n), "d": timedelta(days=n)}.get(unit, timedelta(days=1))
    rows = State(cfg.state_dir / "assistant.db").audit_since(
        (datetime.now(UTC) - delta).isoformat(timespec="seconds")
    )
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        for r in rows:
            print(
                f"{r['ts'][:19]} {r['origin']:8} {r['kind']:16} {r['server'] or '':8} "
                f"{r['tool'] or '':22} {r['outcome']}"
            )
    return 0


def cmd_forget(args: argparse.Namespace) -> int:
    cfg = _load(args)
    if not args.all:
        print("pass --all to delete all stored conversation history", file=sys.stderr)
        return EXIT_CONFIG
    n = State(cfg.state_dir / "assistant.db").forget_all()
    print(f"forgot {n} conversation(s)")
    return 0


def cmd_secrets(args: argparse.Namespace) -> int:
    cfg = _load(args)
    prov = provider_for(cfg.secrets.provider, cfg.secrets.service, cfg.secrets.file)
    names = list(
        dict.fromkeys([*_names(cfg), *(n for m in cfg.models.values() for n in m.secret_names)])
    )
    for n in names:
        print(prov.hint(n))
    return 0


def cmd_knowme(args: argparse.Namespace) -> int:
    """Re-file memories saved before feature 009 (`operator.*`) under `me.<category>.*`."""
    from assistant import knowme
    from assistant.memory import AssistantMemory

    cfg = _load(args)
    mem = AssistantMemory(cfg)
    if not mem.available:
        print(f"memory unavailable: {mem.error}", file=sys.stderr)
        return EXIT_CONFIG
    legacy = [f for f in mem.facts() if f.subject.startswith("operator.")]
    if not legacy:
        print("nothing to migrate")
        return 0
    for f in legacy:
        rest = knowme.slug(f.subject.split(".", 1)[1].replace(".", "-"))
        new = f"me.{f.category}.{rest}"
        print(f"{f.subject} -> {new}" + (" (sensitive)" if f.sensitive else ""))
        if args.apply:
            tags = [knowme.PROFILE_TAG, f.category, "migrated"]
            if f.sensitive:
                tags.append(knowme.SENSITIVE_TAG)
            mem.save_fact(new, f.title, f.content, tags, f.importance)
            mem.forget_fact(f.id, f"re-filed as {new}")
    if not args.apply:
        print("dry run: add --apply to re-file them")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="assistant", description="Personal assistant runtime")
    p.add_argument("--config", help="assistant.toml (default ~/.config/assistant/assistant.toml)")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="run in the foreground").set_defaults(func=cmd_run)
    sub.add_parser("check", help="validate config, secrets, servers").set_defaults(func=cmd_check)
    s = sub.add_parser("service", help="install/uninstall/status of the background service")
    s.add_argument("action", choices=["install", "uninstall", "status"])
    s.add_argument("--systemd-user", action="store_true")
    s.set_defaults(func=cmd_service)
    a = sub.add_parser("audit", help="print the audit trail")
    a.add_argument("--since", default="1d")
    a.add_argument("--json", action="store_true")
    a.set_defaults(func=cmd_audit)
    f = sub.add_parser("forget", help="delete stored conversation history")
    f.add_argument("--all", action="store_true")
    f.set_defaults(func=cmd_forget)
    k = sub.add_parser("knowme", help="profile maintenance (feature 009)")
    k.add_argument("action", choices=["migrate"])
    k.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    k.set_defaults(func=cmd_knowme)
    sub.add_parser("secrets", help="print the commands that store the secrets").set_defaults(
        func=cmd_secrets
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except cfgmod.ConfigError as exc:
        for e in exc.errors:
            print(f"config error: {e}", file=sys.stderr)
        return EXIT_CONFIG
    except SecretMissing as exc:
        print(f"missing secret: {exc}", file=sys.stderr)
        return EXIT_SECRET
