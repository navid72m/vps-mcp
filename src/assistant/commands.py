"""Chat commands (contracts/assistant-interface.md)."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from assistant.config import SLUG_RE
from assistant.scheduler import Cadence

if TYPE_CHECKING:
    from assistant.app import Assistant

HELP = (
    "Commands: /new · /done · /status · /usage · /schedules · "
    "/schedule add <name> <cadence> <prompt> | pause|resume|remove <name> · /stop · /resume · "
    "/forget · /model [name]"
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
        "/schedules": schedules,
        "/schedule": schedule,
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


async def schedules(a: Assistant, chat_id: int, rest: str) -> None:
    rows = a.state.schedules()
    if not rows:
        await a.channel.send(
            chat_id,
            "No schedules. Add one: /schedule add morning weekdays 08:00 Give me a briefing.",
        )
        return
    lines = [
        f"{'▶️' if s['enabled'] else '⏸'} {s['name']} · {s['cadence']} · next "
        f"{str(s['next_run'])[:16]}\n   {s['prompt'][:80]}"
        for s in rows
    ]
    await a.channel.send(chat_id, "\n".join(lines))


async def schedule(a: Assistant, chat_id: int, rest: str) -> None:
    action, _, args = rest.partition(" ")
    if action == "add":
        name, _, more = args.partition(" ")
        parts = more.split(" ")
        cadence = " ".join(parts[:2]) if parts and parts[0] in ("daily", "weekdays") else parts[0]
        prompt = " ".join(parts[2:] if parts and parts[0] in ("daily", "weekdays") else parts[1:])
        try:
            if not SLUG_RE.fullmatch(name) or not prompt.strip():
                raise ValueError(
                    "usage: /schedule add <name> <daily HH:MM|weekdays HH:MM|every Nh> <prompt>"
                )
            nxt = Cadence.parse(cadence).next_after(a.clock.wall(), a.tz)
        except ValueError as exc:
            await a.channel.send(chat_id, f"⚠️ {exc}")
            return
        a.state.schedule_upsert(name, cadence, prompt.strip()[:2000], True, nxt.isoformat(), "chat")
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
