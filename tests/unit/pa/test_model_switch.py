"""Switchable model profiles: /model, the confirm-first switch tool, persistence."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import pytest

from assistant.cli import make_router
from assistant.config import ConfigError, ModelSection, load
from assistant.providers import TextBlock
from assistant.providers.router import ModelRouter
from assistant.providers.scripted import ScriptedProvider
from assistant.secrets import SecretMissing
from assistant.state import State
from tests.assistant_fakes import Harness, harness, say, use
from tests.unit.pa.test_core import GOOD, write

LOCAL = {
    "alias": "qwen",
    "provider": "openai",
    "name": "qwen3.5-9b",
    "base_url": "http://localhost:1234/v1",
}
CLOUD = {
    "provider": "openai",
    "name": "gemini-flash",
    "api_key_secret": "gemini_api_key",
    "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
}


async def two_models(
    tmp: Path, local: ScriptedProvider, cloud: ScriptedProvider, missing_key: bool = False
) -> Harness:
    def factory(m: ModelSection) -> Any:
        if m.alias == "gemini" and missing_key:
            raise SecretMissing("gemini_api_key", "store it with security add-generic-password")
        return local if m.alias == "qwen" else cloud

    from tests.assistant_fakes import config

    cfg = config(tmp, model=LOCAL, models={"gemini": CLOUD})
    router = ModelRouter(cfg.profiles, factory, "qwen")
    return await harness(tmp, cast(Any, router), model=LOCAL, models={"gemini": CLOUD})


async def test_model_command_lists_and_switches(tmp_path: Path) -> None:
    local, cloud = ScriptedProvider([say("from qwen")]), ScriptedProvider([say("from gemini")])
    h = await two_models(tmp_path, local, cloud)
    await h.say("/model")
    listing = h.tg.texts()[-1]
    assert "▶️ qwen: qwen3.5-9b (local, nothing leaves the laptop)" in listing
    assert "gemini: gemini-flash (cloud: generativelanguage.googleapis.com)" in listing
    [(label, data)] = h.tg.buttons()
    assert label == "gemini"
    await h.press(data)
    assert "Now using gemini" in h.tg.edited[-1]["text"]
    assert h.state.kv_get("model") == "gemini"
    await h.say("hello")
    await h.finish()
    assert h.tg.texts()[-1] == "from gemini"
    assert all(r.max_tokens == 1 for r in local.requests)  # only the warm-up
    ctx = cloud.requests[0].messages[-1].content[0]
    assert isinstance(ctx, TextBlock) and "model: gemini" in ctx.text
    await h.say("/model qwen")
    await h.say("/model nope")
    assert "Now using qwen" in h.tg.texts()[-2] and "Unknown model 'nope'" in h.tg.texts()[-1]


async def test_asking_in_words_needs_a_tap(tmp_path: Path) -> None:
    local = ScriptedProvider(
        [say(""), use("assistant__switch_model", {"model": "gemini"}), say("Asked.")]
    )
    cloud = ScriptedProvider([say("from gemini")])
    h = await two_models(tmp_path, local, cloud)
    await asyncio.sleep(0.01)  # let the warm-up take its reply
    await h.say("switch to gemini please")
    await h.finish()
    names = [t.name for t in local.requests[1].tools]
    assert "assistant__switch_model" in names
    ask = next(i for i, t in enumerate(h.tg.texts()) if t.startswith("🔀 Switch the model"))
    switch, keep = h.tg.buttons(ask)
    assert h.a.router is not None and h.a.router.current == "qwen"  # nothing switched yet
    await h.press(keep[1])
    assert h.a.router.current == "qwen" and "Kept" in h.tg.edited[-1]["text"]
    await h.press(switch[1])
    assert h.a.router.current == "gemini"
    await h.press(switch[1].rsplit(":", 1)[0] + ":deadbeef")  # forged button data
    assert h.tg.answered[-1]["text"] == "expired or invalid"


async def test_switch_without_key_keeps_current(tmp_path: Path) -> None:
    h = await two_models(tmp_path, ScriptedProvider(), ScriptedProvider(), missing_key=True)
    await h.say("/model gemini")
    assert "Can't switch to gemini" in h.tg.texts()[-1] and "security" in h.tg.texts()[-1]
    assert h.a.router is not None and h.a.router.current == "qwen"


async def test_single_model_has_no_switch_tool(tmp_path: Path) -> None:
    p = ScriptedProvider([say("hi")])
    h = await harness(tmp_path, p)
    await h.say("hello")
    await h.finish()
    assert all(not t.name.startswith("assistant__") for t in p.requests[0].tools)
    await h.say("/model")
    assert "Only one model" in h.tg.texts()[-1]


def test_router_restores_last_choice(tmp_path: Path) -> None:
    from tests.assistant_fakes import config

    cfg = config(tmp_path, model=LOCAL, models={"gemini": {**CLOUD, "api_key_secret": ""}})
    state = State(tmp_path / "s.db")
    state.kv_set("model", "gemini")
    assert make_router(cfg, state).current == "gemini"
    state.kv_set("model", "removed-from-config")
    assert make_router(cfg, state).current == "qwen"
    assert state.kv_get("model") == "qwen"


def test_profiles_config(tmp_path: Path) -> None:
    text = GOOD + (
        '\n[model]\nalias = "qwen"\nprovider = "openai"\nname = "q"\n'
        'base_url = "http://127.0.0.1:1234/v1"\n'
        '[models.claude]\nname = "claude-sonnet-5"\n'
    )
    cfg = load(write(tmp_path, text))
    assert list(cfg.profiles) == ["qwen", "claude"]
    assert cfg.profiles["qwen"].local and cfg.profiles["claude"].where == "cloud: Anthropic"


@pytest.mark.parametrize(
    "extra",
    [
        '[models.claude]\nalias = "other"\n',
        '[model]\nalias = "claude"\n[models.claude]\nname = "x"\n',
        '[models."Bad Name"]\nname = "x"\n',
        '[[server]]\nname = "assistant"\ncommand = ["x"]\n',
    ],
)
def test_profiles_config_errors(tmp_path: Path, extra: str) -> None:
    with pytest.raises(ConfigError):
        load(write(tmp_path, GOOD + "\n" + extra))


async def test_prompt_prefix_is_stable_for_caching(tmp_path: Path) -> None:
    p = ScriptedProvider([say("one"), say("two")])
    h = await harness(tmp_path, p)
    await h.say("first")
    await h.finish()
    h.clock.advance(3600)
    await h.say("second")
    await h.finish()
    a, b = p.requests
    assert a.system == b.system and [t.name for t in a.tools] == [t.name for t in b.tools]
    ctx_a, ctx_b = a.messages[-1].content[0], b.messages[-1].content[0]
    assert isinstance(ctx_a, TextBlock) and isinstance(ctx_b, TextBlock)
    assert ctx_a.text != ctx_b.text  # the time moved into the message
    # history keeps what the operator wrote, without the context line
    hist = b.messages[0].content[0]
    assert isinstance(hist, TextBlock) and hist.text == "first"


async def test_local_model_is_warmed_up_at_start(tmp_path: Path) -> None:
    local, cloud = ScriptedProvider([say("")]), ScriptedProvider()
    await two_models(tmp_path, local, cloud)
    await asyncio.sleep(0.01)
    assert len(local.requests) == 1 and local.requests[0].max_tokens == 1
    assert local.requests[0].tools  # the tool list is part of what gets cached
