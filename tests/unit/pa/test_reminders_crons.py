"""Tests for reminders, one-off timers, crons, and schedule tools."""

from __future__ import annotations

import zoneinfo
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from assistant.providers.scripted import ScriptedProvider
from assistant.scheduler import Cadence
from tests.assistant_fakes import harness, say, use


def test_cron_parsing_and_next_run() -> None:
    tz = zoneinfo.ZoneInfo("UTC")
    now = datetime(2026, 11, 12, 10, 0, 0, tzinfo=UTC)

    # Every 15 minutes
    cad = Cadence.parse("cron */15 * * * *")
    assert cad.kind == "cron"
    assert cad.cron_expr == "*/15 * * * *"
    nxt = cad.next_after(now, tz)
    assert nxt is not None
    assert nxt == datetime(2026, 11, 12, 10, 15, 0, tzinfo=UTC)

    # Weekdays at 09:00 (Thursday -> Friday)
    cad_wd = Cadence.parse("cron 0 9 * * 1-5")
    nxt_wd = cad_wd.next_after(now, tz)
    assert nxt_wd is not None
    assert nxt_wd == datetime(2026, 11, 13, 9, 0, 0, tzinfo=UTC)


def test_every_minutes_parsing_and_next_run() -> None:
    tz = zoneinfo.ZoneInfo("UTC")
    now = datetime(2026, 11, 12, 10, 7, 0, tzinfo=UTC)
    cad = Cadence.parse("every 15m")
    assert cad.kind == "every" and cad.minutes == 15
    nxt = cad.next_after(now, tz)
    assert nxt is not None
    assert nxt == datetime(2026, 11, 12, 10, 15, 0, tzinfo=UTC)


def test_reminder_relative_timer_and_expiry() -> None:
    tz = zoneinfo.ZoneInfo("UTC")
    now = datetime(2026, 11, 12, 12, 0, 0, tzinfo=UTC)
    cad = Cadence.parse("in 20m", now=now, tz=tz)
    assert cad.kind == "once"
    assert cad.target == datetime(2026, 11, 12, 12, 20, 0, tzinfo=UTC)

    # Before target: returns target
    assert cad.next_after(now, tz) == datetime(2026, 11, 12, 12, 20, 0, tzinfo=UTC)

    # After target: returns None (no next run)
    past = now + timedelta(minutes=25)
    assert cad.next_after(past, tz) is None


def test_reminder_at_time() -> None:
    tz = zoneinfo.ZoneInfo("UTC")
    now = datetime(2026, 11, 12, 10, 0, 0, tzinfo=UTC)
    cad = Cadence.parse("at 14:30", now=now, tz=tz)
    assert cad.kind == "once"
    assert cad.target == datetime(2026, 11, 12, 14, 30, 0, tzinfo=UTC)

    # If the time is already past today, schedule for tomorrow
    cad_tomorrow = Cadence.parse("at 09:00", now=now, tz=tz)
    assert cad_tomorrow.target == datetime(2026, 11, 13, 9, 0, 0, tzinfo=UTC)


def test_invalid_cadences_raise() -> None:
    with pytest.raises(ValueError, match="cadence must be"):
        Cadence.parse("hourly")
    with pytest.raises(ValueError, match="cadence must be"):
        Cadence.parse("in 0m")
    with pytest.raises(ValueError, match="cadence must be"):
        Cadence.parse("cron * * *")


async def test_agent_can_use_schedule_tools(tmp_path: Path) -> None:
    p = ScriptedProvider(
        [
            use(
                "schedule__add", {"name": "remind_pie", "cadence": "in 15m", "prompt": "Check pie"}
            ),
            say("I set a reminder to check the pie in 15 minutes."),
            use("schedule__list", {}),
            say("You have one reminder set."),
            use("schedule__pause", {"name": "remind_pie"}),
            say("Paused."),
            use("schedule__resume", {"name": "remind_pie"}),
            say("Resumed."),
            use("schedule__remove", {"name": "remind_pie"}),
            say("Reminder removed."),
        ]
    )
    h = await harness(tmp_path, p)

    # 1. Add reminder
    await h.say("Remind me in 15m to check pie")
    await h.finish()
    assert any("set a reminder" in t for t in h.tg.texts())
    scheds = h.state.schedules()
    assert len(scheds) == 1
    assert scheds[0]["name"] == "remind_pie"
    assert scheds[0]["enabled"] == 1
    assert "Check pie" in scheds[0]["prompt"]

    # 2. List reminders
    await h.say("What reminders do I have?")
    await h.finish()
    assert any("one reminder set" in t for t in h.tg.texts())

    # 3. Pause
    await h.say("Pause the pie reminder")
    await h.finish()
    assert h.state.schedules()[0]["enabled"] == 0

    # 4. Resume
    await h.say("Resume the pie reminder")
    await h.finish()
    assert h.state.schedules()[0]["enabled"] == 1

    # 5. Remove
    await h.say("Delete the pie reminder")
    await h.finish()
    assert h.state.schedules() == []


async def test_one_off_reminder_executes_and_completes(tmp_path: Path) -> None:
    p = ScriptedProvider([say("Here is your reminder: Check oven")])
    h = await harness(tmp_path, p)

    # Schedule reminder 1 minute ahead
    now = h.clock.wall()
    cad = Cadence.parse("in 60s", now=now, tz=h.a.tz)
    nxt = cad.next_after(now, h.a.tz)
    assert nxt is not None
    h.state.schedule_upsert(
        "check_oven", f"once {nxt.isoformat()}", "Check oven", True, nxt.isoformat(), "test"
    )

    # Advance clock past reminder time
    h.clock.advance(70)
    await h.a.tick()

    # Wait for turn task
    for t in list(h.a.background):
        await t

    # Verify notification was sent
    texts = h.tg.texts()
    assert any("⏰ check_oven" in t for t in texts)
    assert any("Check oven" in t for t in texts)

    # Verify schedule is now disabled with no next_run
    row = h.state.schedules()[0]
    assert row["enabled"] == 0
    assert row["next_run"] is None

    # Tick again: should NOT run again
    count_before = len(h.tg.texts())
    h.clock.advance(60)
    await h.a.tick()
    for t in list(h.a.background):
        await t
    assert len(h.tg.texts()) == count_before


async def test_cron_executes_and_reschedules(tmp_path: Path) -> None:
    p = ScriptedProvider([say("Server health check passed.")])
    h = await harness(tmp_path, p)

    # Schedule cron every 5 minutes: cron */5 * * * *
    now = h.clock.wall()
    cad = Cadence.parse("cron */5 * * * *")
    nxt = cad.next_after(now, h.a.tz)
    assert nxt is not None
    h.state.schedule_upsert(
        "health_check", "cron */5 * * * *", "Check server health", True, nxt.isoformat(), "test"
    )

    # Advance clock to next run
    delta_s = (nxt - now).total_seconds() + 2
    h.clock.advance(delta_s)
    await h.a.tick()

    for t in list(h.a.background):
        await t

    # Verify executed
    assert any("⏰ health_check" in t for t in h.tg.texts())

    # Verify schedule is still enabled with future next_run
    row = h.state.schedules()[0]
    assert row["enabled"] == 1
    assert row["next_run"] is not None
    assert row["next_run"] > h.clock.wall().isoformat()


async def test_remind_and_schedule_commands(tmp_path: Path) -> None:
    h = await harness(tmp_path, ScriptedProvider())

    # /remind command
    await h.say("/remind in 15m Take laundry out")
    assert any("⏰ Reminder set" in t for t in h.tg.texts())
    assert any("Take laundry out" in t for t in h.tg.texts())

    # /schedule add with cron
    await h.say("/schedule add vps-cron cron */10 * * * * Run disk check")
    assert any("⏰ vps-cron: cron */10 * * * *" in t for t in h.tg.texts())

    # /schedules listing
    await h.say("/schedules")
    last = h.tg.texts()[-1]
    assert "vps-cron" in last
    assert "cron */10 * * * *" in last
    assert "Take laundry out" in last
