"""Errors raised by agent_memory; categories mirror the vps-mcp envelope."""

from __future__ import annotations

from typing import Any

CATEGORIES = ("invalid_input", "not_permitted", "not_found", "conflict", "failed")


class AgentMemoryError(Exception):
    def __init__(self, category: str, message: str, **extra: Any) -> None:
        assert category in CATEGORIES, category
        super().__init__(message)
        self.category = category
        self.message = message
        self.extra = extra

    def to_envelope(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {"category": self.category, "message": self.message},
            **self.extra,
        }


def invalid(msg: str, **extra: Any) -> AgentMemoryError:
    return AgentMemoryError("invalid_input", msg, **extra)


def not_permitted(msg: str, **extra: Any) -> AgentMemoryError:
    return AgentMemoryError("not_permitted", msg, **extra)


def not_found(msg: str, **extra: Any) -> AgentMemoryError:
    return AgentMemoryError("not_found", msg, **extra)
