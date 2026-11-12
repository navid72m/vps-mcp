"""US4 scheduler, US5 runtime, service, provider mapping (T013, T027, T029, T030)."""

from __future__ import annotations

import asyncio
import plistlib
import subprocess
import zoneinfo
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from assistant import service
from assistant.app import AlreadyRunning, InstanceLock
from assistant.providers import Msg, TextBlock, ToolResult, ToolSpec, ToolUse
from assistant.providers.anthropic import AnthropicProvider, to_api
from assistant.providers.scripted import ScriptedProvider
from assistant.scheduler import Cadence
from tests.assistant_fakes import harness, say, use

BERLIN = zoneinfo.ZoneInfo("Europe/Berlin")

# ---------------------------------------------------------------- cadence


def test_cadence_next_runs() -> None:
    fri = datetime(2026, 11, 13, 9, 0, tzinfo=BERLIN)  # Friday 09:00

    def nxt(c: str, d: datetime) -> datetime:
        return Cadence.parse(c).next_after(d, BERLIN).astimezone(BERLIN)

    assert nxt("weekdays 08:00", fri).weekday() == 0  # Monday
    assert nxt("daily 08:00", fri).day == 14
    assert nxt("every 4h", fri).hour == 13
    # DST end in Europe/Berlin: 2026-10-25; a daily 08:00 is not doubled or skipped
    before = datetime(2026, 10, 24, 9, 0, tzinfo=BERLIN)
    n1 = nxt("daily 08:00", before)
    n2 = nxt("daily 08:00", n1)
    assert (n1.day, n1.hour, n2.day, n2.hour) == (25, 8, 26, 8)
    with pytest.raises(ValueError):
        Cadence.parse("hourly")


# --------------------------------------------------------------- scheduler


async def test_schedule_runs_and_forces_ask(tmp_path: Path) -> None:
    p = ScriptedProvider(
        [use("ops__status"), use("ops__kick", {"name": "x"}, uid="k"), say("Briefing done.")]
    )
    h = await harness(
        tmp_path,
        p,
        schedule=[{"name": "brief", "cadence": "every 1h", "prompt": "Morning briefing"}],
        server=[{"name": "ops", "command": ["x"], "policy": {"kick": "auto", "wipe": "never"}}],
    )
    h.a.seed_schedules()
    h.clock.advance(3700)
    await h.a.tick()
    for _ in range(300):
        if any(t.startswith("🔐") for t in h.tg.texts()):
            break
        await asyncio.sleep(0.01)
    ask = next(t for t in h.tg.texts() if t.startswith("🔐"))
    assert "(scheduled run)" in ask  # kick is 'auto' for chat, but schedules always ask
    h.clock.advance(11 * 60)
    for t in list(h.a.background):
        await t
    assert "⏰ brief" in h.tg.texts() and h.ops["restarted"] == []
    assert "Briefing done." in h.tg.texts()


async def test_sleep_gap_sends_one_missed_summary(tmp_path: Path) -> None:
    h = await harness(
        tmp_path,
        ScriptedProvider(),
        schedule=[
            {"name": "a", "cadence": "every 1h", "prompt": "x"},
            {"name": "b", "cadence": "every 2h", "prompt": "y"},
        ],
    )
    h.a.seed_schedules()
    h.clock.advance(3 * 3600, sleep=True)
    await h.a.tick()
    missed = [t for t in h.tg.texts() if "missed" in t]
    assert len(missed) == 1 and "a" in missed[0] and "b" in missed[0]
    assert not h.a.background  # nothing replayed
    assert all(s["next_run"] > h.clock.wall().isoformat() for s in h.state.schedules())
    assert h.tg.buttons()[0][0].startswith("▶️ Run")


async def test_schedule_commands(tmp_path: Path) -> None:
    h = await harness(tmp_path, ScriptedProvider())
    await h.say("/schedule add morning weekdays 08:00 Give me a briefing")
    assert h.tg.texts()[-1].startswith("⏰ morning: weekdays 08:00")
    await h.say("/schedule pause morning")
    assert h.state.schedules()[0]["enabled"] == 0
    await h.say("/schedules")
    assert "⏸ morning" in h.tg.texts()[-1]
    await h.say("/schedule add bad hourly x")
    assert h.tg.texts()[-1].startswith("⚠️")
    await h.say("/schedule remove morning")
    assert h.state.schedules() == []


# ----------------------------------------------------------------- runtime


def test_single_instance(tmp_path: Path) -> None:
    a = InstanceLock(tmp_path / "x.lock")
    a.acquire()
    with pytest.raises(AlreadyRunning):
        InstanceLock(tmp_path / "x.lock").acquire()
    a.release()
    InstanceLock(tmp_path / "x.lock").acquire()


async def test_stop_cancels_and_blocks_until_resume(tmp_path: Path) -> None:
    p = ScriptedProvider([use("ops__restart", {"service": "web"}), say("never sent")])
    h = await harness(tmp_path, p)
    await h.say("Restart web")
    for _ in range(300):
        if any(t.startswith("🔐") for t in h.tg.texts()):
            break
        await asyncio.sleep(0.01)
    await h.say("/stop")
    assert "Stopped" in h.tg.texts()[-1] and h.ops["restarted"] == []
    assert h.state.approvals_pending() == []
    await h.say("are you there?")
    assert "I'm stopped" in h.tg.texts()[-1]
    await h.say("/status")
    assert "STOPPED" in h.tg.texts()[-1]
    await h.say("/resume")
    assert h.state.kv_get("paused") == "0"


async def test_restart_notice_and_voided_approvals(tmp_path: Path) -> None:
    h = await harness(tmp_path, ScriptedProvider())
    assert not any("restarted" in t for t in h.tg.texts())
    h.state.approval_add(
        id="aold",
        conversation="c",
        server="ops",
        tool="restart",
        args_hash="x",
        args_json="{}",
        preview="",
        created="",
        expires="9999",
        message_id=1,
        status="pending",
    )
    h2 = await harness(tmp_path, ScriptedProvider())  # previous run never shut down cleanly
    assert any("restarted after an unexpected stop" in t for t in h2.tg.texts())
    assert h2.state.approval_get("aold")["status"] == "voided"  # type: ignore[index]


async def test_heartbeat_status_usage_forget(tmp_path: Path) -> None:
    h = await harness(
        tmp_path,
        ScriptedProvider([say("hi")]),
        assistant={
            "name": "Test",
            "timezone": "Europe/Berlin",
            "state_dir": str(tmp_path / "st"),
            "heartbeat": "every 1h",
        },
    )
    await h.a.tick()
    h.clock.advance(3700)
    await h.a.tick()
    assert any(t.startswith("💓 Alive") for t in h.tg.texts())
    await h.say("/status")
    st = h.tg.texts()[-1]
    assert "ops: up" in st and "memory: available" in st and "last heartbeat" in st
    await h.say("hello")
    await h.finish()
    await h.say("/forget")
    data = h.tg.buttons()[0][1]
    await h.press(data)
    assert h.state.recent_turns(h.a.conversation(111), 10) == []


async def test_idle_conversation_ends(tmp_path: Path) -> None:
    p = ScriptedProvider([say("ok"), say('{"memories": []}')])
    h = await harness(tmp_path, p)
    await h.say("hello")
    await h.finish()
    h.clock.advance(31 * 60)
    await h.a.tick()
    assert h.state.conversation_current(111) is None


# ----------------------------------------------------------------- service


def test_service_render_and_install(tmp_path: Path) -> None:
    plist = plistlib.loads(
        service.render_launchd("/opt/uv", Path("/repo"), Path("/c.toml"), tmp_path)
    )
    assert plist["RunAtLoad"] and plist["KeepAlive"]["SuccessfulExit"] is False
    assert plist["ThrottleInterval"] == 10
    assert plist["ProgramArguments"][-3:] == ["run", "--config", "/c.toml"]
    assert plist["StandardErrorPath"].startswith(str(tmp_path / "logs"))
    unit = service.render_systemd("/opt/uv", Path("/repo"), None)
    assert "Restart=on-failure" in unit and "assistant run" in unit
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    path = service.install(
        "/opt/uv", Path("/repo"), None, tmp_path, agents_dir=tmp_path / "LA", runner=runner
    )
    assert Path(path).exists() and calls[-1][:2] == ["launchctl", "bootstrap"]
    service.uninstall(agents_dir=tmp_path / "LA", runner=runner)
    assert not Path(path).exists()


# ---------------------------------------------------------------- provider


async def test_anthropic_mapping() -> None:
    msgs = [
        Msg("user", [TextBlock("hi")]),
        Msg("assistant", [ToolUse("t1", "ops__status", {})]),
        Msg("user", [ToolResult("t1", "{}")]),
    ]
    api = to_api(msgs)
    assert api[1]["content"][0]["type"] == "tool_use"
    assert api[2]["content"][0]["type"] == "tool_result"
    seen: dict[str, Any] = {}

    class Client:
        class messages:  # noqa: N801
            @staticmethod
            async def create(**kw: Any) -> Any:
                seen.update(kw)
                return SimpleNamespace(
                    content=[
                        SimpleNamespace(type="text", text="ok"),
                        SimpleNamespace(type="tool_use", id="x", name="n", input={"a": 1}),
                    ],
                    usage=SimpleNamespace(input_tokens=5, output_tokens=7),
                    stop_reason="tool_use",
                )

    prov = AnthropicProvider("key", "claude-sonnet-5", client=Client())
    r = await prov.complete(
        system="s", messages=msgs, tools=[ToolSpec("n", "d", {"type": "object"})], max_tokens=10
    )
    assert r.text == "ok" and r.tool_uses[0].input == {"a": 1} and r.usage_out == 7
    assert seen["tools"][0]["name"] == "n" and seen["model"] == "claude-sonnet-5"
    assert datetime.now(UTC)


async def test_sigterm_is_a_clean_shutdown(tmp_path: Path) -> None:
    import os
    import signal

    h = await harness(tmp_path)
    h.state.kv_set("clean_shutdown", "0")

    async def updates() -> Any:
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.sleep(5)
        yield None

    async def started() -> None:  # the harness already started it
        return None

    h.a.channel.updates = updates  # type: ignore[method-assign]
    h.a.start = started  # type: ignore[method-assign]
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(asyncio.ensure_future(h.a.run()), 3)
    assert h.state.kv_get("clean_shutdown") == "1"
