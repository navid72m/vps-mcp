"""Model provider protocol and message types (research R2)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class TextBlock:
    text: str


@dataclass
class ImageBlock:
    """An image the operator sent, for this turn only (feature 010)."""

    media_type: str  # image/jpeg, image/png, image/webp, image/gif
    data: str  # base64


@dataclass
class ToolUse:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class ToolResult:
    tool_use_id: str
    content: str
    is_error: bool = False


@dataclass
class Msg:
    role: str  # "user" | "assistant"
    content: list[TextBlock | ImageBlock | ToolUse | ToolResult]


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class Reply:
    blocks: list[TextBlock | ToolUse]
    usage_in: int = 0
    usage_out: int = 0
    stop_reason: str = "end_turn"
    served_by: str = ""  # set when a fallback model answered instead of the current one

    @property
    def text(self) -> str:
        return "\n".join(b.text for b in self.blocks if isinstance(b, TextBlock)).strip()

    @property
    def tool_uses(self) -> list[ToolUse]:
        return [b for b in self.blocks if isinstance(b, ToolUse)]


class ProviderError(Exception):
    pass


class Provider(Protocol):
    async def complete(
        self, *, system: str, messages: list[Msg], tools: list[ToolSpec], max_tokens: int
    ) -> Reply: ...


@dataclass
class Request:
    system: str
    messages: list[Msg]
    tools: list[ToolSpec]
    max_tokens: int
    extra: dict[str, Any] = field(default_factory=dict)
