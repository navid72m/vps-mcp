"""/done memory proposals with models that think out loud (live failure, 2026-11-12)."""

from __future__ import annotations

from pathlib import Path

from assistant.memory import parse_proposals
from assistant.providers import Reply, TextBlock
from assistant.providers.scripted import ScriptedProvider
from tests.assistant_fakes import harness, say

GOOD = (
    '{"memories": [{"type": "semantic", "subject": "operator.profile", "title": "Name", '
    '"content": "The operator is Sam.", "confidence": "verified", "why": "identity"}]}'
)
THINKING = (
    "We need to propose durable memories. The transcript is mostly a pasted profile. "
    'Format: {"memories": [{"type": "semantic", "subject": "x", "title": "t", "content": "c"}]} '
    "Let me write the final answer.\n\n" + GOOD
)
CUT_OFF = 'We need to propose durable memories. {"memories": [{"type": "semantic", "subject": "a'


def test_parse_takes_the_last_complete_list() -> None:
    props = parse_proposals(THINKING, 5)
    assert props is not None and [p["subject"] for p in props] == ["operator.profile"]
    assert parse_proposals('```json\n{"memories": []}\n```', 5) == []  # valid, nothing to keep
    assert parse_proposals(CUT_OFF, 5) is None
    assert parse_proposals("I think nothing matters here.", 5) is None


async def test_done_with_thinking_model_still_proposes(tmp_path: Path) -> None:
    p = ScriptedProvider([say("Noted."), Reply([TextBlock(THINKING)], 900, 300)])
    h = await harness(tmp_path, p)
    await h.say("My name is Sam, remember it")
    await h.finish()
    await h.say("/done")
    assert any(t.startswith("💡 Remember this?") and "Sam" in t for t in h.tg.texts())
    assert h.tg.texts()[-1] == "✅ Conversation ended. 1 memory proposal(s)."
    calls = [r for r in h.state.audit_since("2000-01-01") if r["outcome"] == "proposals"]
    assert (calls[-1]["tokens_in"], calls[-1]["tokens_out"]) == (900, 300)  # usage recorded
    assert p.requests[-1].max_tokens == 4000


async def test_unusable_answer_is_reported_not_hidden(tmp_path: Path) -> None:
    cut = Reply([TextBlock(CUT_OFF)], 900, 4000, "max_tokens")
    p = ScriptedProvider([say("Noted."), cut])
    h = await harness(tmp_path, p)
    await h.say("My name is Sam, remember it")
    await h.finish()
    await h.say("/done")
    texts = h.tg.texts()
    assert any("couldn't turn this conversation into memory proposals" in t for t in texts)
    assert any("cut off at the length limit" in t for t in texts)
    assert texts[-1] == "✅ Conversation ended."  # not "Nothing worth remembering"
    audit = [r for r in h.state.audit_since("2000-01-01") if r["kind"] == "model_call"]
    assert audit[-1]["outcome"].startswith("proposals failed:")


async def test_long_paste_is_stored_whole_and_not_prefetched(tmp_path: Path) -> None:
    from tests.unit.pa.test_lazy_flow import OPS

    paste = ("# Profile\nI restart and deploy services all day. " * 400)[:20000]
    p = ScriptedProvider([say("Got it.")])
    h = await harness(tmp_path, p, server=[OPS], tools={"lazy": True})
    await h.say(paste)
    await h.finish()
    assert "ops__restart" not in [t.name for t in p.requests[0].tools]  # no keyword prefetch
    conv = h.state.conversation_current(h.a.owner_chat)
    assert conv is not None
    stored = h.state.recent_turns(str(conv["id"]), 2)[0]["text"]
    assert stored == paste.strip() and len(stored) > 19000  # not cut at 8 KB


IDENTITY = (
    '{"memories": [{"type": "semantic", "subject": "operator.identity", "title": "Who the '
    'operator is", "content": "The operator is Sam Rivera, a backend engineer in Berlin.", '
    '"confidence": "verified", "pin": true}, {"type": "semantic", "subject": "operator.stack", '
    '"title": "Stack", "content": "Sam works with Python and Postgres.", "pin": false}]}'
)


def test_at_most_two_pins() -> None:
    many = (
        '{"memories": ['
        + ", ".join(
            f'{{"type": "semantic", "subject": "a.{i}", "title": "t", "content": "c", "pin": true}}'
            for i in range(4)
        )
        + "]}"
    )
    props = parse_proposals(many, 5)
    assert props is not None and [p["pin"] for p in props] == [True, True, False, False]
    assert parse_proposals(GOOD, 5)[0]["pin"] is False  # type: ignore[index]


async def test_pinned_identity_is_known_in_the_next_conversation(tmp_path: Path) -> None:
    p = ScriptedProvider(
        [say("Nice to meet you."), Reply([TextBlock(IDENTITY)], 500, 200), say("You're Sam.")]
    )
    h = await harness(tmp_path, p)
    await h.say("I'm Sam Rivera, a backend engineer in Berlin")
    await h.finish()
    await h.say("/done")
    ident = next(i for i, t in enumerate(h.tg.texts()) if "operator.identity" in t)
    assert "📌 Pinned" in h.tg.texts()[ident]
    other = next(i for i, t in enumerate(h.tg.texts()) if "operator.stack" in t)
    assert "📌" not in h.tg.texts()[other]
    save = next(d for label, d in h.tg.buttons(ident) if label.startswith("💾"))
    await h.press(save)
    await h.say("do you remember my name?")  # a new conversation
    await h.finish()
    system = p.requests[-1].system
    assert "Sam Rivera" in system  # pinned: in the prompt, no search needed
    assert "Never claim to have searched" in system
