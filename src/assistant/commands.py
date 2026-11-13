"""Chat commands (contracts/assistant-interface.md)."""

from __future__ import annotations

import asyncio
import contextlib
import re
from typing import TYPE_CHECKING

from assistant import files
from assistant.config import SLUG_RE
from assistant.scheduler import Cadence

if TYPE_CHECKING:
    from assistant.app import Assistant

HELP = (
    "Commands: /new · /done · /status · /usage · /remind <in 15m|at 14:00> <text> · /schedules · "
    "/schedule add <name> <cadence> <prompt> | pause|resume|remove <name> · /agenda [today|tomorrow|week] · "
    "/inbox · /tg [unread|chats|search <q>] · /ls [path] · /cat <path> · /send <path> · /stop · /resume · /forget · /model [name] · "
    "/tools · /me [all|export] · /interview"
)


async def dispatch(a: Assistant, chat_id: int, text: str) -> None:
    cmd, _, rest = text.partition(" ")
    cmd = cmd.split("@", 1)[0].lower()
    a.state.audit("command", origin="chat", target=cmd)
    handler = {
        "/start": help_,
        "/help": help_,
        "/new": new,
        "/done": done,
        "/status": status,
        "/usage": usage,
        "/remind": remind,
        "/schedules": schedules,
        "/schedule": schedule,
        "/ls": ls,
        "/cat": cat,
        "/send": send_cmd,
        "/stop": stop,
        "/resume": resume,
        "/forget": forget,
        "/model": model,
        "/tools": tools,
        "/me": me,
        "/interview": interview,
        "/agenda": agenda,
        "/inbox": inbox,
        "/tg": tg,
        "/cancel": cancel,
    }.get(cmd)
    if handler is None:
        await a.channel.send(chat_id, f"Unknown command. {HELP}")
        return
    await handler(a, chat_id, rest.strip())


async def help_(a: Assistant, chat_id: int, rest: str) -> None:
    await a.channel.send(chat_id, f"Hi, I'm {a.config.assistant.name}. Ask me anything.\n{HELP}")


async def _end_current(a: Assistant, chat_id: int) -> int:
    conv = a.state.conversation_current(chat_id)
    a.approvals.void_all()
    if conv is None:
        return 0
    return await a.end_and_propose(chat_id, str(conv["id"]))


async def new(a: Assistant, chat_id: int, rest: str) -> None:
    n = await _end_current(a, chat_id)
    a.state.conversation_new(chat_id, a.clock.wall().isoformat())
    await a.channel.send(
        chat_id, "🆕 New conversation." + (f" ({n} memory proposal(s) above.)" if n else "")
    )


async def done(a: Assistant, chat_id: int, rest: str) -> None:
    n = await _end_current(a, chat_id)
    await a.channel.send(
        chat_id,
        f"✅ Conversation ended. {n} memory proposal(s)."
        if n
        else "✅ Conversation ended."
        + ("" if a.agent.proposal_error else " Nothing worth remembering."),
    )


async def status(a: Assistant, chat_id: int, rest: str) -> None:
    now = a.clock.wall()
    up = now - a.started
    lines = [
        f"🤖 {a.config.assistant.name} · up {int(up.total_seconds() // 60)} min"
        + (" · ⏸ STOPPED" if a.paused else "")
    ]
    for s in a.hub.status():
        state = "up" if s["up"] else f"down ({s['error']})"
        lines.append(
            f"• {s['server']}: {state}, {s['tools']} tools "
            f"({s['auto']} auto, {s['ask']} ask, {s['never']} never)"
        )
    lines.append(f"• memory: {'available' if a.memory.available else 'unavailable'}")
    pend = a.state.approvals_pending()
    lines.append(f"• pending approvals: {len(pend)}")
    sch = [s for s in a.state.schedules() if s["enabled"]]
    if sch:
        nxt = min(sch, key=lambda s: str(s["next_run"]))
        lines.append(f"• next schedule: {nxt['name']} at {str(nxt['next_run'])[:16]}")
    lines.append(f"• last heartbeat: {(a.state.kv_get('last_heartbeat') or 'never')[:16]}")
    await a.channel.send(chat_id, "\n".join(lines))


async def usage(a: Assistant, chat_id: int, rest: str) -> None:
    await a.channel.send(chat_id, "📊 " + a.budget.usage_text())


async def remind(a: Assistant, chat_id: int, rest: str) -> None:
    parts = rest.strip().split(maxsplit=2)
    if len(parts) < 3 or parts[0] not in ("in", "at"):
        await a.channel.send(
            chat_id,
            "usage: /remind in <Ns|Nm|Nh|Nd> <text> or /remind at <HH:MM> <text>\n"
            "example: /remind in 15m Check the server\n"
            "example: /remind at 14:30 Join project meeting",
        )
        return
    cadence = f"{parts[0]} {parts[1]}"
    prompt = parts[2].strip()
    now = a.clock.wall()
    try:
        cad = Cadence.parse(cadence, now=now, tz=a.tz)
        nxt = cad.next_after(now, a.tz)
        if nxt is None:
            await a.channel.send(chat_id, "⚠️ Reminder time is already in the past.")
            return
    except Exception as exc:  # noqa: BLE001
        await a.channel.send(chat_id, f"⚠️ {exc}")
        return
    first_word = re.sub(r"[^a-z0-9]", "", prompt.lower().split()[0])[:12] if prompt else "reminder"
    name = f"{first_word or 'remind'}-{now.strftime('%H%M%S')}"
    cad_stored = f"once {nxt.isoformat()}"
    a.state.schedule_upsert(name, cad_stored, prompt[:2000], True, nxt.isoformat(), "chat")
    a.state.schedule_set(name, enabled=1, next_run=nxt.isoformat())
    local_time = nxt.astimezone(a.tz).strftime("%H:%M")
    await a.channel.send(chat_id, f'⏰ Reminder set for {local_time} ({cadence}): "{prompt}"')


async def schedules(a: Assistant, chat_id: int, rest: str) -> None:
    rows = a.state.schedules()
    if not rows:
        await a.channel.send(
            chat_id,
            "No schedules or reminders.\n"
            "Add one:\n"
            "• /remind in 15m Check the server\n"
            "• /schedule add morning weekdays 08:00 Give me a briefing\n"
            "• /schedule add backup cron 0 3 * * * Run backup",
        )
        return
    lines = [
        f"{'▶️' if s['enabled'] else '⏸'} {s['name']} · "
        f"{'one-off reminder' if s['cadence'].startswith('once ') else s['cadence']} · next "
        f"{str(s['next_run'])[:16] if s['next_run'] else 'done'}\n   {s['prompt'][:80]}"
        for s in rows
    ]
    await a.channel.send(chat_id, "\n".join(lines))


async def schedule(a: Assistant, chat_id: int, rest: str) -> None:
    action, _, args = rest.partition(" ")
    if action == "add":
        name, _, more = args.partition(" ")
        parts = more.split()
        if parts and parts[0] == "cron" and len(parts) >= 6:
            cadence = " ".join(parts[:6])
            prompt = " ".join(parts[6:])
        elif parts and parts[0] in ("daily", "weekdays", "in", "at", "every") and len(parts) >= 2:
            cadence = " ".join(parts[:2])
            prompt = " ".join(parts[2:])
        else:
            cadence = parts[0] if parts else ""
            prompt = " ".join(parts[1:])
        now = a.clock.wall()
        try:
            if not SLUG_RE.fullmatch(name) or not prompt.strip():
                raise ValueError("usage: /schedule add <name> <cadence> <prompt>")
            cad = Cadence.parse(cadence, now=now, tz=a.tz)
            nxt = cad.next_after(now, a.tz)
            if nxt is None:
                raise ValueError(f"cadence '{cadence}' has no upcoming execution")
        except (ValueError, RuntimeError) as exc:
            await a.channel.send(chat_id, f"⚠️ {exc}")
            return
        cad_stored = f"once {nxt.isoformat()}" if cad.kind == "once" else cadence
        a.state.schedule_upsert(
            name, cad_stored, prompt.strip()[:2000], True, nxt.isoformat(), "chat"
        )
        a.state.schedule_set(name, enabled=1, next_run=nxt.isoformat())
        await a.channel.send(chat_id, f"⏰ {name}: {cadence}, next {nxt.isoformat()[:16]}")
        return
    names = {s["name"] for s in a.state.schedules()}
    name = args.strip()
    if action in ("pause", "resume", "remove") and name in names:
        if action == "remove":
            a.state.schedule_remove(name)
        else:
            a.state.schedule_set(name, enabled=int(action == "resume"))
        await a.channel.send(chat_id, f"⏰ {name}: {action}d")
        return
    await a.channel.send(chat_id, "usage: /schedule add|pause|resume|remove <name> ...")


async def stop(a: Assistant, chat_id: int, rest: str) -> None:
    a.state.kv_set("paused", "1")
    a.pending.clear()
    voided = a.approvals.void_all()
    cancelled = 0
    for task in list(a.turns.values()) + list(a.background):
        if not task.done():
            task.cancel()
            cancelled += 1
    for task in list(a.turns.values()):
        with contextlib.suppress(BaseException):
            await task
    await a.channel.send(
        chat_id,
        f"⏹ Stopped. Cancelled {cancelled} task(s), voided {voided} "
        "approval(s). Nothing runs until /resume.",
    )


async def resume(a: Assistant, chat_id: int, rest: str) -> None:
    a.state.kv_set("paused", "0")
    await a.channel.send(chat_id, "▶️ Resumed.")


async def forget(a: Assistant, chat_id: int, rest: str) -> None:
    await a.channel.send(
        chat_id,
        "Delete all stored conversation history? (memory is not affected)",
        [("🧹 Yes, forget", a.signer.sign("fg:all"))],
    )


async def model(a: Assistant, chat_id: int, rest: str) -> None:
    if a.router is None:
        await a.channel.send(chat_id, "Only one model is configured (add [models.<name>]).")
        return
    if rest:
        await a.channel.send(chat_id, a.switch_model(rest.split()[0].lower()))
        return
    lines = [
        f"{'▶️' if alias == a.router.current else '   '} {alias}: {m.name} ({m.where})"
        for alias, m in a.router.profiles.items()
    ]
    buttons = [
        (alias, a.signer.sign(f"md:{alias}"))
        for alias in a.router.profiles
        if alias != a.router.current
    ]
    await a.channel.send(chat_id, "Models:\n" + "\n".join(lines), buttons)


async def ls(a: Assistant, chat_id: int, rest: str) -> None:
    path = rest.strip() or "~"
    res = await asyncio.to_thread(files.files_list, path, max_depth=1)
    if not res.get("ok"):
        await a.channel.send(chat_id, f"⚠️ Error: {res.get('error', 'failed to list directory')}")
        return
    entries = res.get("entries", [])
    if not entries:
        await a.channel.send(chat_id, f"📁 `{res.get('path')}` is empty.")
        return
    lines = [f"📁 `{res.get('path')}` ({len(entries)} items):"]
    for e in entries[:40]:
        icon = "📁" if e["type"] == "directory" else "📄"
        size_str = f" ({e['size_bytes']:,} B)" if e["type"] == "file" else ""
        lines.append(f"{icon} `{e['name']}`{size_str}")
    if len(entries) > 40:
        lines.append(f"... and {len(entries) - 40} more")
    await a.channel.send(chat_id, "\n".join(lines))


async def cat(a: Assistant, chat_id: int, rest: str) -> None:
    path = rest.strip()
    if not path:
        await a.channel.send(chat_id, "Usage: `/cat <path>` (e.g. `/cat ~/notes.txt`)")
        return
    res = await asyncio.to_thread(files.files_read, path, 1, 60)
    if not res.get("ok"):
        await a.channel.send(chat_id, f"⚠️ Error: {res.get('error', 'failed to read file')}")
        return
    if res.get("is_binary"):
        await a.channel.send(
            chat_id,
            f"📄 `{res.get('path')}`: Binary file ({res.get('size_bytes', 0):,} bytes)",
        )
        return
    content = res.get("content", "")
    total = res.get("total_lines", 0)
    header = f"📄 `{res.get('path')}` ({total} lines):"
    snippet = content[:3500]
    await a.channel.send(chat_id, f"{header}\n```\n{snippet}\n```")


async def send_cmd(a: Assistant, chat_id: int, rest: str) -> None:
    path = rest.strip()
    if not path:
        await a.channel.send(chat_id, "Usage: `/send <path>` (e.g. `/send ~/Downloads/book.pdf`)")
        return
    p = files.resolve_path(path)
    if not p.is_file():
        # Search Downloads or Desktop for fuzzy match
        matches = files.files_search(path="~/Downloads", query=path, max_results=1).get(
            "matches", []
        )
        if matches:
            p = files.resolve_path(matches[0]["path"])
        else:
            await a.channel.send(chat_id, f"⚠️ File not found: {p}")
            return
    await a.channel.send(chat_id, f"Uploading `{p.name}`...")
    try:
        await a.channel.send_document(chat_id, p)
    except Exception as exc:  # noqa: BLE001
        await a.channel.send(chat_id, f"⚠️ Failed to upload file: {exc}")


async def tools(a: Assistant, chat_id: int, rest: str) -> None:
    await a.channel.send(chat_id, tools_text(a, chat_id))


def tools_text(a: Assistant, chat_id: int) -> str:
    from assistant.toolsets import spec_chars

    agent = a.agent
    if not agent.lazy:
        n = len(agent.all_specs())
        size = spec_chars(agent.all_specs())
        return (
            f"🧰 Lazy loading is off: all {n} tools on every call · ~{size // 4 / 1000:.1f}k tokens"
        )
    cat = agent.catalog()
    conv = a.state.conversation_current(chat_id)
    cid = str(conv["id"]) if conv else ""
    active = a.state.toolsets_active(cid) if cid else {}
    turn = a.state.operator_turn(cid) if cid else 0
    size, full = cat.size(active), cat.full_size()
    lines = [
        f"🧰 Tools: {len(cat.core)} core + {len(active)} loaded toolset(s) · "
        f"~{size // 4 / 1000:.1f}k tokens (everything: ~{full // 4 / 1000:.1f}k)",
        "Core: " + ", ".join(cat.core),
    ]
    idle = []
    for name, ts in cat.sets.items():
        row = active.get(name)
        if row is None:
            idle.append(f"{name} ({len(ts.tools)})" + ("" if ts.available else " [down]"))
            continue
        used = int(row["last_used_turn"])
        ago = turn - max(int(row["loaded_turn"]), used)
        when = "this turn" if ago <= 0 else f"{ago} turn(s) ago"
        state = "used" if used else "not used yet"
        lines.append(f"▶️ {name} ({len(ts.tools)}): {row['reason']} · {when} · {state}")
    if idle:
        lines.append("Not loaded: " + " · ".join(idle))
    return "\n".join(lines)


async def me(a: Assistant, chat_id: int, rest: str) -> None:
    from assistant import knowme_runtime

    await knowme_runtime.show_me(a, chat_id, rest)


async def interview(a: Assistant, chat_id: int, rest: str) -> None:
    from assistant import knowme_runtime

    await knowme_runtime.interview_cmd(a, chat_id, rest)


async def cancel(a: Assistant, chat_id: int, rest: str) -> None:
    await a.channel.send(chat_id, "OK, nothing changed.")


async def agenda(a: Assistant, chat_id: int, rest: str) -> None:
    from datetime import timedelta

    from assistant import calendar

    if not a.config.google.enabled:
        await a.channel.send(chat_id, "Google Calendar is not enabled in assistant.toml.")
        return

    now = a.clock.wall()
    tz = a.tz
    target = rest.strip().lower()
    if target == "tomorrow":
        tomorrow = (now + timedelta(days=1)).astimezone(tz)
        time_min = tomorrow.strftime("%Y-%m-%d")
        time_max = (tomorrow + timedelta(days=1)).strftime("%Y-%m-%d")
        heading = "Tomorrow's Agenda"
    elif target == "week":
        time_min = now.astimezone(tz).strftime("%Y-%m-%d")
        time_max = (now + timedelta(days=7)).astimezone(tz).strftime("%Y-%m-%d")
        heading = "This Week's Agenda"
    else:
        today = now.astimezone(tz)
        time_min = today.strftime("%Y-%m-%d")
        time_max = (today + timedelta(days=1)).strftime("%Y-%m-%d")
        heading = "Today's Agenda"

    try:
        auth = a.google_auth()
        events = await calendar.list_events(
            auth,
            time_min=time_min,
            time_max=time_max,
            max_results=a.config.google.max_events,
            timezone=a.config.assistant.timezone,
        )
        text = calendar.format_agenda(events, heading)
        await a.channel.send(chat_id, text)
    except Exception as exc:
        await a.channel.send(chat_id, f"⚠️ Calendar error: {exc}")


async def inbox(a: Assistant, chat_id: int, rest: str) -> None:
    from assistant import gmail

    if not a.config.google.enabled:
        await a.channel.send(chat_id, "Gmail is not enabled in assistant.toml.")
        return

    try:
        auth = a.google_auth()
        msgs = await gmail.list_inbox(
            auth, unread_only=True, max_results=a.config.google.max_emails
        )
        text = gmail.format_inbox(msgs, "Unread Inbox")
        await a.channel.send(chat_id, text)
    except Exception as exc:
        await a.channel.send(chat_id, f"⚠️ Gmail error: {exc}")


async def tg(a: Assistant, chat_id: int, rest: str) -> None:
    from assistant import telegram_user

    if not a.config.telegram_user.enabled:
        await a.channel.send(
            chat_id, "Personal Telegram integration is not enabled in assistant.toml."
        )
        return

    subcmd, _, arg = rest.partition(" ")
    subcmd = subcmd.lower().strip()
    arg = arg.strip()

    try:
        client = a.telegram_user_client()
        if subcmd in ("", "unread", "inbox"):
            dialogs = await client.list_inbox(
                unread_only=True, limit=a.config.telegram_user.max_messages
            )
            text = telegram_user.format_inbox(dialogs, "Unread Chats")
            await a.channel.send(chat_id, text)
        elif subcmd in ("all", "chats", "dialogs"):
            dialogs = await client.list_inbox(
                unread_only=False, limit=a.config.telegram_user.max_messages
            )
            text = telegram_user.format_inbox(dialogs, "Recent Chats")
            await a.channel.send(chat_id, text)
        elif subcmd == "search":
            if not arg:
                await a.channel.send(chat_id, "Usage: /tg search <query>")
                return
            msgs = await client.search_messages(
                query=arg, limit=a.config.telegram_user.max_messages
            )
            if not msgs:
                await a.channel.send(chat_id, f"No messages found matching '{arg}'.")
                return
            lines = [f"🔍 Found {len(msgs)} message(s) for '{arg}':\n"]
            for m in msgs:
                sender = m.get("sender_name") or "Unknown"
                chat_title = m.get("chat_title") or str(m.get("chat_id", ""))
                date = m.get("date", "")[:16]
                text_snippet = m.get("text", "").replace("\n", " ")[:120]
                lines.append(f"• **{sender}** in *{chat_title}* ({date}):\n  {text_snippet}")
            await a.channel.send(chat_id, "\n".join(lines))
        else:
            await a.channel.send(chat_id, "Usage: /tg [unread|chats|search <query>]")
    except Exception as exc:
        await a.channel.send(chat_id, f"⚠️ Telegram error: {exc}")
