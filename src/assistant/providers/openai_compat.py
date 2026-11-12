"""OpenAI-compatible chat completions adapter: LM Studio, Ollama, llama.cpp, Gemini, ... (R2)."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

import httpx2

from assistant.providers import (
    Msg,
    ProviderError,
    Reply,
    TextBlock,
    ToolResult,
    ToolSpec,
    ToolUse,
)

THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)
STOP = {"tool_calls": "tool_use", "stop": "end_turn", "length": "max_tokens"}


def to_api(system: str, messages: list[Msg]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for m in messages:
        text = "\n".join(b.text for b in m.content if isinstance(b, TextBlock) and b.text)
        calls = [b for b in m.content if isinstance(b, ToolUse)]
        results = [b for b in m.content if isinstance(b, ToolResult)]
        if m.role == "assistant":
            msg: dict[str, Any] = {"role": "assistant", "content": text or None}
            if calls:
                msg["tool_calls"] = [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {"name": c.name, "arguments": json.dumps(c.input)},
                    }
                    for c in calls
                ]
            out.append(msg)
            continue
        for r in results:
            content = f"ERROR: {r.content}" if r.is_error else r.content
            out.append({"role": "tool", "tool_call_id": r.tool_use_id, "content": content})
        if text:
            out.append({"role": "user", "content": text})
    return out


def _args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        val = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return val if isinstance(val, dict) else {}


def from_api(data: dict[str, Any]) -> Reply:
    choices = data.get("choices") or []
    if not choices:
        raise ProviderError("model returned no choices")
    msg = choices[0].get("message") or {}
    blocks: list[TextBlock | ToolUse] = []
    text = THINK_RE.sub("", msg.get("content") or "").strip()
    if not text and not msg.get("tool_calls"):
        # some thinking models occasionally put the whole answer in the reasoning channel
        text = str(msg.get("reasoning_content") or "").strip()
    if text:
        blocks.append(TextBlock(text))
    for c in msg.get("tool_calls") or []:
        fn = c.get("function") or {}
        if fn.get("name"):
            cid = str(c.get("id") or f"call_{uuid.uuid4().hex[:12]}")
            blocks.append(ToolUse(cid, str(fn["name"]), _args(fn.get("arguments"))))
    usage = data.get("usage") or {}
    reason = str(choices[0].get("finish_reason") or "stop")
    return Reply(
        blocks,
        int(usage.get("prompt_tokens") or 0),
        int(usage.get("completion_tokens") or 0),
        STOP.get(reason, reason),
    )


class OpenAICompatProvider:
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "",
        *,
        timeout: float = 300.0,
        reasoning_effort: str = "",
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.model = model
        self.reasoning_effort = reasoning_effort
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        kw: dict[str, Any] = {"timeout": httpx2.Timeout(timeout, connect=10.0), "headers": headers}
        if transport is not None:
            kw["transport"] = transport
        self._http = httpx2.AsyncClient(**kw)

    async def complete(
        self, *, system: str, messages: list[Msg], tools: list[ToolSpec], max_tokens: int
    ) -> Reply:
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": to_api(system, messages),
        }
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description[:1024],
                        "parameters": t.input_schema,
                    },
                }
                for t in tools
            ]
        try:
            resp = await self._http.post(self.url, json=body)
            data = resp.json()
        except (httpx2.HTTPError, ValueError) as exc:
            raise ProviderError(f"{exc.__class__.__name__}: model call failed") from None
        if resp.status_code >= 400 or "error" in data:
            err = data.get("error")
            detail = err.get("message", "") if isinstance(err, dict) else str(err or "")
            raise ProviderError(f"HTTP {resp.status_code}: {detail[:200]}")
        return from_api(data)
