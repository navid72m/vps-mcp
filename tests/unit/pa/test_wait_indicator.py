"""Unit tests for the waiting time spinner and progress indicator."""

import asyncio
from pathlib import Path

import pytest

from tests.assistant_fakes import harness, say, use


@pytest.mark.asyncio
async def test_fast_response_does_not_send_spinner(tmp_path: Path) -> None:
    """When a response is fast (< delay_s), only typing action is sent, no extra placeholder."""
    h = await harness(tmp_path)
    h.provider.add(say("Hello! Quick answer."))
    h.a.wait_indicator_delay_s = 2.0

    await h.a.chat_turn(111, "hi", "chat")

    # Only 1 message sent: the final answer
    assert len(h.tg.sent) == 1
    assert h.tg.sent[0]["text"] == "Hello! Quick answer."
    # Typing indicator was invoked
    assert len(h.tg.actions) >= 1
    # No message was deleted
    assert len(h.tg.deleted) == 0


@pytest.mark.asyncio
async def test_slow_response_shows_spinner_and_deletes_on_completion(tmp_path: Path) -> None:
    """When a response takes longer than delay_s, a spinner message is sent, edited, and deleted."""
    h = await harness(tmp_path)

    # Use a low delay to trigger the spinner in tests
    h.a.wait_indicator_delay_s = 0.05
    h.a.wait_indicator_interval_s = 0.03

    async def slow_tool(*args, **kwargs):  # type: ignore[no-untyped-def]
        await asyncio.sleep(0.12)
        return "slow result", False

    h.a.agent.builtin = slow_tool  # type: ignore[assignment]
    h.provider.add(
        use("schedule__add", {"cadence": "in 1h", "prompt": "test"}),
        say("All done after waiting!"),
    )

    await h.a.chat_turn(111, "schedule something", "chat")

    # Spinner message was sent and then final message was sent
    assert len(h.tg.sent) >= 2
    # The spinner message was edited with updated progress/elapsed time
    assert len(h.tg.edited) >= 1
    # Check that edited text contained spinner or status
    assert any(
        "schedule__add" in e.get("text", "") or "Thinking" in e.get("text", "") for e in h.tg.edited
    )
    # The temporary placeholder message was deleted
    assert len(h.tg.deleted) >= 1
    # Final message is intact
    assert h.tg.sent[-1]["text"] == "All done after waiting!"


@pytest.mark.asyncio
async def test_cancelled_turn_cleans_up_spinner(tmp_path: Path) -> None:
    """If a slow turn is cancelled, the indicator is stopped and the placeholder message is deleted."""
    h = await harness(tmp_path)
    h.a.wait_indicator_delay_s = 0.05
    h.a.wait_indicator_interval_s = 0.03

    async def hang_tool(*args, **kwargs):  # type: ignore[no-untyped-def]
        await asyncio.sleep(10.0)
        return "never", False

    h.a.agent.builtin = hang_tool  # type: ignore[assignment]
    h.provider.add(use("schedule__add", {"cadence": "in 1h", "prompt": "test"}))

    task = asyncio.create_task(h.a.chat_turn(111, "slow command", "chat"))
    # Wait until turn begins and placeholder is sent
    await asyncio.sleep(0.12)

    # Spinner message was displayed
    assert len(h.tg.sent) >= 1
    placeholder_id = h.tg.sent[0]["message_id"]

    # Cancel the running turn
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Placeholder was deleted
    assert any(d.get("message_id") == placeholder_id for d in h.tg.deleted)
