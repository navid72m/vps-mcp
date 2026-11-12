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
    "/schedule add <name> <cadence> <prompt> | pause|resume|remove <name> · /ls [path] · /cat <path> · "
    "/stop · /resume · /forget · /model [name]"
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
        "/stop": stop,
        "/resume": resume,
        "/forget": forget,
        "/model": model,
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
        else "✅ Conversation ended. Nothing worth remembering.",
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
