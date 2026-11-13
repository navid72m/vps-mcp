"""`assistant` CLI: run | check | service | audit | forget | secrets (contracts)."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
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
    names = ["telegram_bot_token", *cfg.model.secret_names]
    if cfg.telegram_user.enabled:
        names.extend([cfg.telegram_user.api_id_secret, cfg.telegram_user.api_hash_secret])
    return names


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


def cmd_google_auth(args: argparse.Namespace) -> int:
    import http.server
    import subprocess
    import time
    import urllib.parse
    import webbrowser
    from typing import Any

    from assistant import google_auth
    from assistant.secrets import provider_for

    cfg = _load(args)
    prov = provider_for(cfg.secrets.provider, cfg.secrets.service, cfg.secrets.file)

    client_id = ""
    client_secret = ""
    path = Path(cfg.google.credentials_path).expanduser()
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            info = raw.get("installed") or raw.get("web") or raw
            client_id = str(info.get("client_id", "")).strip()
            client_secret = str(info.get("client_secret", "")).strip()
            print(f"Loaded client credentials from {path}")
        except Exception as exc:
            print(f"Warning: could not read {path}: {exc}", file=sys.stderr)

    if not client_id:
        with contextlib.suppress(Exception):
            client_id = prov.get(cfg.google.client_id_secret)
    if not client_secret:
        with contextlib.suppress(Exception):
            client_secret = prov.get(cfg.google.client_secret_secret)

    if not client_id or not client_secret:
        print(
            "Error: Google client credentials not found.\n"
            f"1. Download OAuth client ID JSON from Google Cloud Console to {cfg.google.credentials_path}, OR\n"
            f"2. Store secrets: {prov.hint(cfg.google.client_id_secret)} and {prov.hint(cfg.google.client_secret_secret)}",
            file=sys.stderr,
        )
        return EXIT_CONFIG

    class _OAuthServer(http.server.HTTPServer):
        auth_code: str | None = None
        auth_error: str | None = None

    class _OAuthCallbackHandler(http.server.BaseHTTPRequestHandler):
        server: _OAuthServer

        def do_GET(self) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            if "code" in qs:
                self.server.auth_code = qs["code"][0]
                html = (
                    "<!DOCTYPE html><html><head><title>Authorization Successful</title>"
                    "<style>body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; "
                    "display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; background: #f8f9fa; color: #202124; }"
                    ".card { background: white; padding: 40px; border-radius: 12px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); text-align: center; max-width: 440px; }"
                    "h1 { color: #1e8e3e; font-size: 24px; margin-bottom: 12px; }"
                    "p { color: #5f6368; line-height: 1.5; }</style></head>"
                    "<body><div class='card'>"
                    "<h1>✓ Authorization Successful</h1>"
                    "<p>Google Calendar and Gmail access granted. You can close this browser tab and return to your terminal.</p>"
                    "</div></body></html>"
                )
            else:
                self.server.auth_error = qs.get("error", ["authorization_denied"])[0]
                html = f"<!DOCTYPE html><html><body><h1>Authorization Failed</h1><p>{self.server.auth_error}</p></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = _OAuthServer(("127.0.0.1", 0), _OAuthCallbackHandler)
    server.timeout = 1.0
    port = server.server_address[1]
    redirect_uri = f"http://localhost:{port}/"
    url = google_auth.make_auth_url(client_id, redirect_uri=redirect_uri)

    print("\n1. Opening your browser to authorize with Google...")
    print(f"   {url}\n")
    with contextlib.suppress(Exception):
        webbrowser.open(url)

    print("2. Sign in with your Google account, grant Calendar & Gmail permissions.")
    print("   Waiting for browser callback (or press Ctrl+C to paste code manually)...")

    code = ""
    start = time.time()
    try:
        while not server.auth_code and not server.auth_error and (time.time() - start < 180.0):
            server.handle_request()
        if server.auth_code:
            code = server.auth_code
            print("\n✓ Authorization code received from browser callback!")
        elif server.auth_error:
            print(f"\nError from Google: {server.auth_error}", file=sys.stderr)
            server.server_close()
            return EXIT_CONFIG
    except KeyboardInterrupt:
        print("\n\nBrowser wait canceled by user.")
    finally:
        server.server_close()

    if not code:
        try:
            raw = input("Paste the authorization code or redirected URL here: ").strip()
            if "?" in raw and "code=" in raw:
                qs = urllib.parse.parse_qs(urllib.parse.urlsplit(raw).query)
                code = qs.get("code", [""])[0]
            else:
                code = raw
        except (KeyboardInterrupt, EOFError):
            print("\nAborted.")
            return 1

    if not code:
        print("Error: empty authorization code.", file=sys.stderr)
        return EXIT_CONFIG

    print("\nExchanging code for refresh token...")
    try:
        refresh_token = asyncio.run(
            google_auth.exchange_auth_code(
                client_id, client_secret, code, redirect_uri=redirect_uri
            )
        )
    except Exception as exc:
        print(f"Error exchanging code: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    if cfg.secrets.provider == "keychain":
        security_bin = shutil.which("security") or "/usr/bin/security"
        proc = subprocess.run(
            [
                security_bin,
                "add-generic-password",
                "-U",
                "-s",
                cfg.secrets.service,
                "-a",
                cfg.google.token_secret,
                "-w",
                refresh_token,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            print(f"✓ Saved refresh token to macOS Keychain ({cfg.google.token_secret})")
            return 0

    print(f"✓ Obtained refresh token! Please store it using:\n{prov.hint(cfg.google.token_secret)}")
    return 0


def cmd_tg_auth(args: argparse.Namespace) -> int:
    import asyncio

    from assistant.secrets import provider_for
    from assistant.telegram_user import TelegramCredentials

    cfg = _load(args)
    prov = provider_for(cfg.secrets.provider, cfg.secrets.service, cfg.secrets.file)

    try:
        creds = TelegramCredentials.load(cfg, prov.get)
    except Exception as exc:
        print(f"Error loading credentials: {exc}", file=sys.stderr)
        return 1

    from telethon import TelegramClient  # type: ignore[import-untyped]

    sess_str = str(creds.session_path)
    if sess_str.endswith(".session"):
        sess_str = sess_str[:-8]
    creds.session_path.parent.mkdir(parents=True, exist_ok=True)

    async def _auth() -> None:
        client = TelegramClient(sess_str, creds.api_id, creds.api_hash)
        await client.start()
        me = await client.get_me()
        first = getattr(me, "first_name", "") or ""
        last = getattr(me, "last_name", "") or ""
        username = f"@{me.username}" if getattr(me, "username", None) else ""
        name = f"{first} {last}".strip()
        print(
            f"✅ Successfully authenticated as {name} ({username}) [ID: {getattr(me, 'id', 'unknown')}]"
        )
        print(f"Session saved to {creds.session_path}")
        await client.disconnect()

    try:
        asyncio.run(_auth())
        return 0
    except Exception as exc:
        print(f"Authentication failed: {exc}", file=sys.stderr)
        return 1


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
    sub.add_parser(
        "google-auth", help="authorize Google Calendar and Gmail via OAuth 2.0"
    ).set_defaults(func=cmd_google_auth)
    sub.add_parser(
        "tg-auth", help="authorize personal Telegram account via MTProto session"
    ).set_defaults(func=cmd_tg_auth)
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
