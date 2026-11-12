"""OpenAI-compatible provider (LM Studio, Ollama, Gemini, ...) and its config."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx2
import pytest

from assistant.cli import _names, make_provider
from assistant.config import ConfigError, load
from assistant.providers import Msg, ProviderError, TextBlock, ToolResult, ToolSpec, ToolUse
from assistant.providers.openai_compat import OpenAICompatProvider, from_api, to_api
from tests.unit.pa.test_core import GOOD, write

LOCAL = (
    GOOD + '\n[model]\nprovider = "openai"\nname = "qwen"\nbase_url = "http://localhost:1234/v1"\n'
)


def test_to_api_maps_tool_turns() -> None:
    msgs = [
        Msg("user", [TextBlock("hi")]),
        Msg("assistant", [TextBlock("checking"), ToolUse("t1", "ops__status", {"a": 1})]),
        Msg("user", [ToolResult("t1", "{}"), ToolResult("t2", "boom", is_error=True)]),
    ]
    api = to_api("sys", msgs)
    assert api[0] == {"role": "system", "content": "sys"}
    assert api[2]["tool_calls"][0]["function"] == {"name": "ops__status", "arguments": '{"a": 1}'}
    assert api[3] == {"role": "tool", "tool_call_id": "t1", "content": "{}"}
    assert api[4]["content"] == "ERROR: boom" and len(api) == 5


def test_from_api_strips_thinking_and_survives_bad_arguments() -> None:
    r = from_api(
        {
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": "<think>secret plan</think>\nOn it.",
                        "tool_calls": [
                            {"id": "c1", "function": {"name": "a", "arguments": '{"x": 2}'}},
                            {"function": {"name": "b", "arguments": "not json"}},
                        ],
                    },
                }
            ],
            "usage": {"prompt_tokens": 11, "completion_tokens": 3},
        }
    )
    assert r.text == "On it." and r.stop_reason == "tool_use"
    assert r.tool_uses[0].input == {"x": 2} and r.tool_uses[1].input == {}
    assert r.tool_uses[1].id.startswith("call_") and (r.usage_in, r.usage_out) == (11, 3)


async def test_complete_posts_chat_completions() -> None:
    seen: dict[str, Any] = {}

    def handler(req: httpx2.Request) -> httpx2.Response:
        seen["url"] = str(req.url)
        seen["auth"] = req.headers.get("authorization")
        seen["body"] = json.loads(req.content)
        msg = {"content": "hello", "role": "assistant"}
        return httpx2.Response(200, json={"choices": [{"message": msg, "finish_reason": "stop"}]})

    prov = OpenAICompatProvider(
        "http://localhost:1234/v1/",
        "qwen",
        reasoning_effort="none",
        transport=httpx2.MockTransport(handler),
    )
    r = await prov.complete(
        system="s",
        messages=[Msg("user", [TextBlock("hi")])],
        tools=[ToolSpec("n", "d", {"type": "object"})],
        max_tokens=10,
    )
    assert r.text == "hello" and r.stop_reason == "end_turn"
    assert seen["url"] == "http://localhost:1234/v1/chat/completions" and seen["auth"] is None
    assert seen["body"]["tools"][0]["function"]["name"] == "n"
    assert seen["body"]["model"] == "qwen" and seen["body"]["reasoning_effort"] == "none"


def test_answer_only_in_reasoning_is_used() -> None:
    msg = {"content": "", "reasoning_content": "Server is healthy."}
    assert from_api({"choices": [{"message": msg}]}).text == "Server is healthy."
    calls = [{"id": "c", "function": {"name": "a", "arguments": "{}"}}]
    msg = {"content": "", "reasoning_content": "thinking...", "tool_calls": calls}
    assert from_api({"choices": [{"message": msg}]}).text == ""


@pytest.mark.parametrize(
    "response",
    [
        httpx2.Response(400, json={"error": {"message": "model not loaded"}}),
        httpx2.Response(500, text="<html>oops"),
        httpx2.Response(200, json={"choices": []}),
    ],
)
async def test_complete_errors_become_provider_errors(response: httpx2.Response) -> None:
    prov = OpenAICompatProvider(
        "http://x/v1", "m", "sk-secret-key", transport=httpx2.MockTransport(lambda _: response)
    )
    with pytest.raises(ProviderError) as e:
        await prov.complete(system="s", messages=[], tools=[], max_tokens=10)
    assert "sk-secret-key" not in str(e.value)


async def test_unreachable_server_is_a_provider_error() -> None:
    def handler(req: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=req)

    prov = OpenAICompatProvider("http://x/v1", "m", transport=httpx2.MockTransport(handler))
    with pytest.raises(ProviderError):
        await prov.complete(system="s", messages=[], tools=[], max_tokens=10)


def test_config_local_model_needs_no_api_key(tmp_path: Path) -> None:
    cfg = load(write(tmp_path, LOCAL))
    assert _names(cfg) == ["telegram_bot_token"]
    assert isinstance(make_provider(cfg.model, lambda n: ""), OpenAICompatProvider)
    cfg = load(write(tmp_path, GOOD))
    assert _names(cfg) == ["telegram_bot_token", "anthropic_api_key"]
    keyed = LOCAL + 'api_key_secret = "gemini_api_key"\n'
    assert _names(load(write(tmp_path, keyed))) == ["telegram_bot_token", "gemini_api_key"]


@pytest.mark.parametrize(
    "bad",
    [
        LOCAL.replace('base_url = "http://localhost:1234/v1"\n', ""),
        LOCAL.replace('"http://localhost:1234/v1"', '"localhost:1234"'),
        LOCAL.replace('"openai"', '"anthropic"') + 'api_key_secret = ""\n',
        LOCAL + 'api_key_secret = "Bad Name"\n',
    ],
)
def test_config_model_errors(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ConfigError):
        load(write(tmp_path, bad))
