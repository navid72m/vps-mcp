"""Follow-ups from the first live run: split pastes, memory guidance, timings, fallback."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any, cast

import pytest

from assistant.config import ConfigError, ModelSection, load
from assistant.providers import ProviderError, TextBlock
from assistant.providers.router import ModelRouter
from assistant.providers.scripted import ScriptedProvider
from tests.assistant_fakes import harness, say
from tests.unit.pa.test_core import GOOD, write


def user_text(req: Any) -> str:
    block = req.messages[-1].content[0]
    assert isinstance(block, TextBlock)
    return block.text


async def test_split_paste_becomes_one_turn(tmp_path: Path) -> None:
    p = ScriptedProvider([say("one answer for the paste"), say("both answered")])
    h = await harness(tmp_path, p)
    h.a.coalesce_s = 0.05
    await h.say("part 1 of my profile")
    await h.say("part 2 of my profile")
    await h.say("part 3 of my profile")
    await h.finish()
    assert len(p.requests) == 1
    text = user_text(p.requests[0])
    assert text.endswith("part 1 of my profile\n\npart 2 of my profile\n\npart 3 of my profile")
    # messages arriving while a turn runs are answered together in the next turn
    await h.say("question a")
    await asyncio.sleep(0)
    await h.say("question b")
    await h.finish()
    assert user_text(p.requests[1]).endswith("question a\n\nquestion b")


async def test_stop_drops_queued_messages(tmp_path: Path) -> None:
    p = ScriptedProvider([say("never")])
    h = await harness(tmp_path, p)
    h.a.coalesce_s = 0.2
    await h.say("do something")
    await h.say("/stop")
    await asyncio.sleep(0.3)
    assert not p.requests and not h.a.pending


async def test_memory_guidance_matches_the_assistant(tmp_path: Path) -> None:
    p = ScriptedProvider([say("ok")])
    h = await harness(tmp_path, p)
    await h.say("hi")
    await h.finish()
    system = p.requests[0].system
    assert "call me__remember" in system and "memory__search" in system
    assert "memory_save" not in system


async def test_model_calls_record_their_duration(tmp_path: Path) -> None:
    p = ScriptedProvider([say("ok")])
    h = await harness(tmp_path, p)
    await h.say("hi")
    await h.finish()
    calls = [r for r in h.state.audit_since("2000-01-01") if r["kind"] == "model_call"]
    assert re.fullmatch(r"end_turn \d+\.\ds tools=\d+", calls[-1]["outcome"])


async def test_fallback_answers_when_the_model_stalls(tmp_path: Path) -> None:
    main = ScriptedProvider([ProviderError("ReadTimeout: model call failed")])
    backup = ScriptedProvider([say("from backup")])
    from tests.assistant_fakes import config

    models = {
        "main": {"provider": "openai", "name": "m", "base_url": "https://x/v1", "fallback": "bk"},
        "bk": {"provider": "openai", "name": "b", "base_url": "https://x/v1"},
    }
    over: dict[str, Any] = {
        "model": {"alias": "main", **models["main"]},
        "models": {"bk": models["bk"]},
    }
    cfg = config(tmp_path, **over)

    def factory(m: ModelSection) -> Any:
        return main if m.alias == "main" else backup

    router = ModelRouter(cfg.profiles, factory, "main")
    h = await harness(tmp_path, cast(Any, router), **over)
    await h.say("hi")
    await h.finish()
    assert h.tg.texts()[-1] == "from backup"
    calls = [r for r in h.state.audit_since("2000-01-01") if r["kind"] == "model_call"]
    assert " via bk " in calls[-1]["outcome"]


async def test_fallback_failure_reports_both(tmp_path: Path) -> None:
    def factory(m: ModelSection) -> Any:
        return ScriptedProvider([ProviderError(f"{m.alias} down")])

    cfg = load(write(tmp_path, GOOD + '\n[model]\nfallback = "b"\n[models.b]\nname = "x"\n'))
    router = ModelRouter(cfg.profiles, factory, "default")
    with pytest.raises(ProviderError, match="default down; fallback b: b down"):
        await router.complete(system="", messages=[], tools=[], max_tokens=1)


@pytest.mark.parametrize("fb", ['"nope"', '"default"'])
def test_fallback_must_be_another_model(tmp_path: Path, fb: str) -> None:
    with pytest.raises(ConfigError):
        load(write(tmp_path, GOOD + f"\n[model]\nfallback = {fb}\n"))
