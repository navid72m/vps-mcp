"""Error categories and the error envelope returned to MCP clients (FR-036)."""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCategory(StrEnum):
    NOT_PERMITTED = "not_permitted"
    NOT_FOUND = "not_found"
    PERMISSION_DENIED = "permission_denied"
    CONNECTION_FAILURE = "connection_failure"
    TIMED_OUT = "timed_out"
    INVALID_INPUT = "invalid_input"
    CONFLICT = "conflict"
    CONFIRMATION_REQUIRED = "confirmation_required"
    READ_ONLY = "read_only"
    PROTECTED = "protected"
    INSUFFICIENT_DISK = "insufficient_disk"
    UNAPPROVED_SOURCE = "unapproved_source"
    FAILED = "failed"


class VpsMcpError(Exception):
    """An expected, categorized failure that is reported to the assistant."""

    def __init__(
        self,
        category: ErrorCategory,
        message: str,
        *,
        subtype: str | None = None,
        last_known_state: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.message = message
        self.subtype = subtype
        self.last_known_state = last_known_state
        self.extra: dict[str, Any] = dict(extra or {})

    def to_envelope(self) -> dict[str, Any]:
        error: dict[str, Any] = {"category": self.category.value, "message": self.message}
        if self.subtype:
            error["subtype"] = self.subtype
        if self.last_known_state is not None:
            error["last_known_state"] = self.last_known_state
        envelope: dict[str, Any] = {"ok": False, "error": error}
        envelope.update(self.extra)
        return envelope

    def __repr__(self) -> str:
        sub = f"/{self.subtype}" if self.subtype else ""
        return f"VpsMcpError({self.category.value}{sub}: {self.message})"


class ConnectionLost(VpsMcpError):
    """The SSH channel closed after a command was sent; its effect is unknown (FR-013)."""

    def __init__(self, message: str = "connection to the VPS was lost mid-command") -> None:
        super().__init__(ErrorCategory.CONNECTION_FAILURE, message, subtype="connection_lost")


def invalid(message: str) -> VpsMcpError:
    return VpsMcpError(ErrorCategory.INVALID_INPUT, message)


def not_permitted(name: str) -> VpsMcpError:
    # Deliberately identical wording whether or not the service exists on the VPS (FR-025).
    return VpsMcpError(
        ErrorCategory.NOT_PERMITTED,
        f"service '{name}' is unknown or not permitted",
    )
