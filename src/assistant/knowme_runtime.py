"""Knows me, wired into the running assistant: tools, buttons, /me, export, interview (009)."""

from __future__ import annotations

import json
import logging
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_memory import AgentMemoryError
from assistant import knowme
from assistant.state import new_id

if TYPE_CHECKING:
    from assistant.app import Assistant

log = logging.getLogger(__name__)
INTERVIEW_KEY = "interview"
PAGE = 10


def _audit(a: Assistant, conversation: str, target: str, outcome: str) -> None:
    a.state.audit(
        "memory_save", conversation=conversation or None, target=target, outcome=outcome[:200]
    )


def _undo_expiry(a: Assistant) -> str:
    return (a.clock.wall() + timedelta(hours=knowme.UNDO_HOURS)).isoformat()


def interview_state(a: Assistant) -> dict[str, Any]:
    return a.state.kv_json(INTERVIEW_KEY, knowme.interview_default())


# ------------------------------------------------------------------------ tools


async def call(
    a: Assistant, name: str, args: dict[str, Any], chat_id: int, conversation: str, origin: str
) -> tuple[str, bool]:
    if not a.memory.available:
        return json.dumps({"outcome": "refused", "why": "memory is unavailable"}), True
    if name == "me__remember":
        return await remember(a, args, chat_id, conversation, origin)
    if name == "me__forget":
        return await forget_ask(a, str(args.get("query", "")), chat_id)
    if name == "me__interview":
        return interview_step(a, str(args.get("action", "next")))
    return json.dumps({"error": f"unknown tool {name}"}), True


async def remember(
    a: Assistant, args: dict[str, Any], chat_id: int, conversation: str, origin: str
) -> tuple[str, bool]:
    fact = str(args.get("fact", "")).strip()[: knowme.FACT_CHARS]
    sensitive = bool(args.get("sensitive"))
    if knowme.looks_secret(fact):
        _audit(a, conversation, "secret", "refused: looks like a password or key")
        return json.dumps(
            {"outcome": "refused", "why": "that looks like a password or key: never stored"}
        ), True
    try:
        subject = knowme.fact_subject(
            str(args.get("category", "other")), str(args.get("key", "")), args.get("person")
        )
    except knowme.KnowMeError as exc:
        return json.dumps({"outcome": "refused", "why": str(exc)}), True
    category, person, key = knowme.parse_subject(subject)
    existing = a.memory.fact_by_subject(subject)
    st = interview_state(a)
    ok, why = knowme.may_save_now(
        fact,
        a.agent.turn_text,
        origin=origin,
        correcting=existing is not None,
        interview_active=bool(st.get("active")),
        sensitive=sensitive,
    )
    if not ok:
        if not sensitive:  # sensitive facts are never proposed (FR-017)
            payload = {
                "type": "semantic",
                "subject": subject,
                "category": category,
                "title": key.replace("-", " "),
                "content": fact,
                "confidence": "inferred",
                "why": "mentioned in conversation",
                "pin": False,
            }
            a.pending_suggestions.setdefault(conversation, []).append(payload)
        _audit(a, conversation, subject, f"suggested: {why}")
        return json.dumps({"outcome": "suggested", "why": why}), False
    tags = [knowme.PROFILE_TAG, category, "remember" if not st.get("active") else "interview"]
    if sensitive:
        tags.append(knowme.SENSITIVE_TAG)
    importance = 5 if category == "identity" else 3
    try:
        if existing is not None and existing.content.strip() == fact:
            return json.dumps({"outcome": "already known", "subject": subject}), False
        if existing is not None:
            res = a.memory.supersede_fact(existing.id, fact, "corrected by the operator")
            token = a.state.undo_add(
                "updated",
                str(res.get("new_id") or ""),
                _undo_expiry(a),
                previous_id=existing.id,
                previous_content=existing.content,
            )
            text = f"📝 Updated: {fact} (was: {existing.content})"
            result = {"outcome": "updated", "subject": subject, "was": existing.content}
        else:
            title = f"{person.title()}: {key}" if person else key.replace("-", " ")
            res = a.memory.save_fact(subject, title, fact, tags, importance)
            token = a.state.undo_add("saved", str(res.get("id") or ""), _undo_expiry(a))
            text = f"📝 Saved: {fact}"
            result = {"outcome": "saved", "subject": subject}
    except AgentMemoryError as err:
        _audit(a, conversation, subject, f"refused: {err.message}")
        return json.dumps({"outcome": "refused", "why": err.message}), True
    if sensitive:
        text += " (kept as sensitive: not in your everyday profile)"
    _audit(a, conversation, subject, result["outcome"])
    await a.channel.send(chat_id, text, [("↩️ Undo", a.signer.sign(f"un:{token}"))])
    return json.dumps(result), False


async def forget_ask(a: Assistant, query: str, chat_id: int) -> tuple[str, bool]:
    q = knowme.words(query)
    matches = [
        f for f in a.memory.facts() if q & knowme.words(f"{f.subject} {f.title} {f.content}")
    ][:5]
    if not matches:
        return json.dumps({"outcome": "nothing matches", "query": query}), False
    for f in matches:
        await a.channel.send(
            chat_id, f"Forget this? {f.content}", [("🗑 Forget", a.signer.sign(f"fg1:{f.id}"))]
        )
    return json.dumps({"outcome": "asked the operator to confirm", "matches": len(matches)}), False


def interview_step(a: Assistant, action: str) -> tuple[str, bool]:
    st = interview_state(a)
    if not st.get("active"):
        return json.dumps({"outcome": "the interview is not running"}), False
    cur = st.get("current")
    if action == "stop":
        st["active"] = False
        a.state.kv_set_json(INTERVIEW_KEY, st)
        return json.dumps({"outcome": "stopped; /interview continues it later"}), False
    if cur:
        st["done" if action == "next" else "skipped"] = [
            *st.get("done" if action == "next" else "skipped", []),
            cur,
        ]
    nxt = knowme.interview_next(st)
    if nxt is None:
        st.update(active=False, current="")
        a.state.kv_set_json(INTERVIEW_KEY, st)
        return json.dumps(
            {"done": True, "say": "thank the operator; /me shows what you know"}
        ), False
    st["current"] = nxt[0]
    a.state.kv_set_json(INTERVIEW_KEY, st)
    return json.dumps({"topic": nxt[0], "ask_about": nxt[1]}), False


# ---------------------------------------------------------------------- buttons


async def on_undo(a: Assistant, token: str) -> str:
    row = a.state.undo_take(token, a.clock.wall().isoformat())
    if row is None:
        return "Undo expired or already used."
    try:
        if row["action"] == "saved":
            a.memory.forget_fact(row["memory_id"], "undone by the operator")
        elif row["action"] == "updated":
            a.memory.supersede_fact(row["memory_id"], row["previous_content"], "undone")
        elif row["action"] == "forgotten":
            await _restore(a, row["memory_id"], row["previous_content"])
    except AgentMemoryError as err:
        return f"Couldn't undo: {err.message}"
    _audit(a, "", row["memory_id"], f"undo {row['action']}")
    return "↩️ Undone."


async def _restore(a: Assistant, mem_id: str, content: str) -> None:
    assert a.memory.mem is not None
    old = a.memory.mem.get(mem_id) if mem_id else {}
    rec = old.get("memory") if isinstance(old, dict) else None
    subject = (rec or {}).get("subject") or mem_id
    tags = list((rec or {}).get("tags") or [knowme.PROFILE_TAG])
    a.memory.save_fact(subject, (rec or {}).get("title") or subject, content, tags)


async def on_forget(a: Assistant, mem_id: str) -> tuple[str, list[tuple[str, str]]]:
    """Forget one fact (after the operator's tap). Returns the text and an Undo button."""
    fact = next((f for f in a.memory.facts() if f.id == mem_id), None)
    if fact is None:
        return "Already gone.", []
    try:
        a.memory.forget_fact(mem_id, "forgotten at the operator's request")
    except AgentMemoryError as err:
        return f"Couldn't forget: {err.message}", []
    token = a.state.undo_add("forgotten", mem_id, _undo_expiry(a), previous_content=fact.content)
    _audit(a, "", fact.subject, "forgotten")
    return f"🗑 Forgotten: {fact.content}", [("↩️ Undo", a.signer.sign(f"un:{token}"))]


def start_edit(a: Assistant, chat_id: int, mem_id: str) -> str:
    fact = next((f for f in a.memory.facts() if f.id == mem_id), None)
    if fact is None:
        return "That fact is gone."
    a.state.kv_set(f"pending_edit:{chat_id}", mem_id)
    return f"✏️ Send the new text for: {fact.content}\n(/cancel to keep it)"


async def finish_edit(a: Assistant, chat_id: int, text: str) -> bool:
    """If an edit is pending for this chat, `text` is the new fact. Returns True if handled."""
    key = f"pending_edit:{chat_id}"
    mem_id = a.state.kv_get(key)
    if not mem_id:
        return False
    a.state.kv_del(key)
    if text.startswith("/"):
        return False  # any command cancels the edit
    fact = next((f for f in a.memory.facts() if f.id == mem_id), None)
    if fact is None:
        await a.channel.send(chat_id, "That fact is gone; nothing changed.")
        return True
    new = text.strip()[: knowme.FACT_CHARS]
    try:
        res = a.memory.supersede_fact(mem_id, new, "edited by the operator")
    except AgentMemoryError as err:
        await a.channel.send(chat_id, f"⛔ Not changed: {err.message}")
        return True
    token = a.state.undo_add(
        "updated", str(res.get("new_id") or ""), _undo_expiry(a), mem_id, fact.content
    )
    _audit(a, "", fact.subject, "edited")
    await a.channel.send(
        chat_id,
        f"📝 Updated: {new} (was: {fact.content})",
        [("↩️ Undo", a.signer.sign(f"un:{token}"))],
    )
    return True


# --------------------------------------------------------------------- commands


async def show_me(a: Assistant, chat_id: int, rest: str) -> None:
    arg = rest.strip().lower()
    if arg == "export":
        await export(a, chat_id)
        return
    facts = a.memory.facts()
    if not facts:
        await a.channel.send(
            chat_id,
            'I don\'t know anything about you yet. Tell me ("remember that …") or try /interview.',
        )
        return
    groups = knowme.card_for_phone(facts, include_sensitive=arg.startswith("all"))
    shown = sum(len(g) for _, g in groups)
    hidden = len(facts) - shown
    head = f"🪪 What I know about you ({shown} facts"
    head += f", {hidden} sensitive: /me all)" if hidden else ")"
    await a.channel.send(chat_id, head + " · tap a fact's buttons to edit or remove it")
    sent = 0
    for heading, group in groups:
        for f in group:
            if sent >= PAGE * 3:
                await a.channel.send(chat_id, "… more in /me export")
                return
            who = f"{f.person.title()}: " if f.person else ""
            await a.channel.send(
                chat_id,
                f"[{heading}] {who}{f.content}",
                [
                    ("✏️ Edit", a.signer.sign(f"ed:{f.id}")),
                    ("🗑 Remove", a.signer.sign(f"fg1:{f.id}")),
                ],
            )
            sent += 1


async def export(a: Assistant, chat_id: int) -> None:
    text = knowme.export_markdown(a.memory.facts(), a.memory.recaps(), a.clock.wall())
    with tempfile.TemporaryDirectory() as tmp:  # sent from a throwaway file, not kept (R9)
        path = Path(tmp) / "what-my-assistant-knows.md"
        path.write_text(text, encoding="utf-8")
        await a.channel.send_document(chat_id, path, "Everything I know about you")
    _audit(a, "", "export", "sent")


async def interview_cmd(a: Assistant, chat_id: int, rest: str) -> None:
    st = interview_state(a)
    arg = rest.strip().lower()
    if arg == "stop":
        st["active"] = False
        a.state.kv_set_json(INTERVIEW_KEY, st)
        await a.channel.send(chat_id, "Interview paused. /interview continues it.")
        return
    if arg == "sensitive on":
        st["sensitive_opt_in"] = True
        a.state.kv_set_json(INTERVIEW_KEY, st)
        await a.channel.send(chat_id, "OK: the interview may also ask about sensitive topics.")
        return
    nxt = knowme.interview_next(st)
    if nxt is None:
        await a.channel.send(chat_id, "We've covered everything. /me shows what I know.")
        return
    st.update(active=True, current=nxt[0])
    a.state.kv_set_json(INTERVIEW_KEY, st)
    await a.chat_turn(
        chat_id,
        "Let's do the getting-to-know-you interview. Ask me the next question.",
        "chat",
    )


def maybe_offer_interview(a: Assistant, fact_count: int) -> str:
    """One line appended to a conversation's first reply when the profile is thin (R7)."""
    st = interview_state(a)
    now = a.clock.wall()
    if not knowme.should_offer(st, fact_count, now):
        return ""
    st["last_offered"] = now.isoformat()
    a.state.kv_set_json(INTERVIEW_KEY, st)
    return "\n\n👋 I don't know much about you yet. Want a quick getting-to-know-you? /interview"


def new_proposal_id() -> str:
    return new_id("p")
