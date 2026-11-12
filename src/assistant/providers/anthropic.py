"""Anthropic Claude adapter (research R2)."""

from __future__ import annotations

from typing import Any

from assistant.providers import (
    ImageBlock,
    Msg,
    ProviderError,
    Reply,
    TextBlock,
    ToolResult,
    ToolSpec,
    ToolUse,
)


def to_api(messages: list[Msg]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        blocks: list[dict[str, Any]] = []
        for b in m.content:
            if isinstance(b, TextBlock):
                if b.text:
                    blocks.append({"type": "text", "text": b.text})
            elif isinstance(b, ImageBlock):
                blocks.append(
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": b.media_type, "data": b.data},
                    }
                )
            elif isinstance(b, ToolUse):
                blocks.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
            elif isinstance(b, ToolResult):
                blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": b.tool_use_id,
                        "content": b.content,
                        "is_error": b.is_error,
                    }
                )
        out.append({"role": m.role, "content": blocks})
    return out


def from_api(resp: Any) -> Reply:
    blocks: list[TextBlock | ToolUse] = []
    for b in getattr(resp, "content", []) or []:
        kind = getattr(b, "type", None)
        if kind == "text":
            blocks.append(TextBlock(str(b.text)))
        elif kind == "tool_use":
            blocks.append(ToolUse(str(b.id), str(b.name), dict(b.input or {})))
    usage = getattr(resp, "usage", None)
    return Reply(
        blocks,
        int(getattr(usage, "input_tokens", 0) or 0),
        int(getattr(usage, "output_tokens", 0) or 0),
        str(getattr(resp, "stop_reason", "end_turn") or "end_turn"),
    )


class AnthropicProvider:
    def __init__(self, api_key: str, model: str, client: Any = None) -> None:
        if client is None:
            import anthropic

            client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=2, timeout=90.0)
        self.client = client
        self.model = model

    async def complete(
        self, *, system: str, messages: list[Msg], tools: list[ToolSpec], max_tokens: int
    ) -> Reply:
        kw: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": to_api(messages),
        }
        if tools:
            kw["tools"] = [
                {
                    "name": t.name,
                    "description": t.description[:1024],
                    "input_schema": t.input_schema,
                }
                for t in tools
            ]
        try:
            resp = await self.client.messages.create(**kw)
        except Exception as exc:  # noqa: BLE001 - normalise provider errors
            raise ProviderError(f"{exc.__class__.__name__}: model call failed") from None
        return from_api(resp)
