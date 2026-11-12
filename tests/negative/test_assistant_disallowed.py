"""Disallowed assistant inputs (T036): refused, nothing executed."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from assistant.cli import main
from assistant.providers import Reply, ToolUse
from assistant.providers.scripted import ScriptedProvider
from tests.assistant_fakes import OWNER, TOKEN, FakeTelegram, harness, say, use


@pytest.mark.parametrize(
    "update",
    [
        {
            "message": {
                "chat": {"id": 999, "type": "private"},
                "from": {"id": 999},
                "text": "hi",
                "date": 1,
            }
        },
        {
            "message": {
                "chat": {"id": -100, "type": "group"},
                "from": {"id": OWNER},
                "text": "hi",
                "date": 1,
            }
        },
        {
            "message": {
                "chat": {"id": -100, "type": "channel"},
                "from": {"id": OWNER},
                "text": "x",
                "date": 1,
            }
        },
        {
            "callback_query": {
                "id": "q",
                "from": {"id": 999},
                "data": "ap:x:a:00000000",
                "message": {"message_id": 1, "chat": {"id": 999, "type": "private"}},
            }
        },
    ],
)
async def test_strangers_dropped(tmp_path: Path, update: dict) -> None:  # type: ignore[type-arg]
    from assistant.state import State
    from assistant.telegram import TelegramChannel

    st = State(tmp_path / "a.db")
    tg = FakeTelegram(updates=[{"update_id": 1, **update}])
    ch = TelegramChannel(TOKEN, [OWNER], st, transport=tg.transport)
    assert await ch.poll_once() == []
    await ch.aclose()


@pytest.mark.parametrize(
    "data",
    [
        "ap:aXXXX:a:00000000",
        "ap:aXXXX:a",
        "pr:p1:s:deadbeef",
        "ms:brief:12345678",
        "fg:all:00000000",
        "garbage",
        "",
        "ap:" + "a" * 60,
    ],
)
async def test_forged_callbacks(tmp_path: Path, data: str) -> None:
    h = await harness(tmp_path, ScriptedProvider())
    await h.press(data)
    assert h.ops["restarted"] == [] and h.ops["wiped"] == []
    assert (
        h.tg.answered
        and "expired" in h.tg.answered[-1]["text"]
        or "invalid" in h.tg.answered[-1]["text"]
        or "unknown" in h.tg.answered[-1]["text"]
    )


async def test_stale_and_wrong_args(tmp_path: Path) -> None:
    p = ScriptedProvider([use("ops__restart", {"service": "web"}), say("ok")])
    h = await harness(tmp_path, p)
    await h.say("restart web")
    for _ in range(300):
        if h.tg.buttons():
            break
        await asyncio.sleep(0.01)
    good = h.tg.buttons()[0][1]
    aid = good.split(":")[1]
    assert not h.a.approvals.bound(aid, "ops", "restart", {"service": "db", "confirm": True})
    h.a.approvals.void_all()
    await h.finish()
    await h.press(good)
    assert h.ops["restarted"] == []


@pytest.mark.parametrize("tool", ["ops__wipe", "nope__x", "ops__", "memory__save", "../x"])
async def test_disallowed_tools(tmp_path: Path, tool: str) -> None:
    p = ScriptedProvider([Reply([ToolUse("t", tool, {})], 1, 1, "tool_use"), say("no")])
    h = await harness(tmp_path, p)
    await h.say("do it")
    await h.finish()
    assert h.ops["wiped"] == [] and not any(t.startswith("🔐") for t in h.tg.texts())


@pytest.mark.parametrize(
    "toml",
    [
        "[telegram]\nallowed_user_ids = []\n",
        "[telegram]\nallowed_user_ids = [1]\n[[schedule]]\nname='x'\ncadence='hourly'\nprompt='p'\n",
        "[telegram]\nallowed_user_ids = [1]\n[nope]\nx=1\n",
    ],
)
def test_bad_config_exit_1(tmp_path: Path, toml: str) -> None:
    c = tmp_path / "a.toml"
    c.write_text(toml)
    assert main(["--config", str(c), "check"]) == 1


def test_missing_secret_exit_2(tmp_path: Path) -> None:
    c = tmp_path / "a.toml"
    c.write_text(
        f"[telegram]\nallowed_user_ids = [1]\n[secrets]\nprovider = 'file'\n"
        f"file = '{tmp_path / 's.toml'}'\n"
    )
    assert main(["--config", str(c), "check"]) == 2


async def test_budget_exceeded_no_calls(tmp_path: Path) -> None:
    p = ScriptedProvider([use("ops__status")])
    h = await harness(tmp_path, p, limits={"day_tool_calls": 1, "approval_minutes": 10})
    h.state.budget_add(h.a.budget.day(), tool_calls=1)
    await h.say("status?")
    await h.finish()
    assert p.requests == []
