"""Views returned by tools (data-model.md §2)."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from vps_mcp.models.inputs import (
    CommandArg,
    ConfigKey,
    EnvValue,
    ImageRef,
    NetworkName,
    PortMapping,
    ServiceKind,
    VolumeMapping,
)


class ServiceState(StrEnum):
    RUNNING = "running"
    STOPPED = "stopped"
    FAILED = "failed"
    TRANSITIONING = "transitioning"
    NOT_FOUND = "not_found"


class ActionOutcome(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    REFUSED = "refused"
    TIMED_OUT = "timed_out"
    UNKNOWN = "unknown"
    ROLLED_BACK = "rolled_back"
    PREVIEW = "preview"


class Resources(BaseModel):
    cpu_pct: float | None = None
    mem_bytes: int | None = None


class ServiceStatus(BaseModel):
    name: str
    kind: ServiceKind
    state: ServiceState
    boot_enabled: bool | None = None
    uptime_s: int | None = None
    last_state_change: datetime | None = None
    last_exit_reason: str | None = None
    version: str | None = None
    protected: bool = False
    resources: Resources | None = None
    pending_reconciliation: bool = False
    networks: list[str] = Field(default_factory=list)
    health: str | None = None  # container healthcheck status, when one is defined

    def to_dict(self) -> dict[str, Any]:
        """Compact form for tool output: omit nulls and default flags, shorten digests."""
        d: dict[str, Any] = self.model_dump(mode="json", exclude_none=True)
        for key, default in (
            ("pending_reconciliation", False),
            ("protected", False),
            ("networks", []),
        ):
            if d.get(key) == default:
                d.pop(key, None)
        version = d.get("version")
        if isinstance(version, str) and "@sha256:" in version:
            image, digest = version.split("@sha256:", 1)
            d["version"] = f"{image}@sha256:{digest[:12]}"
        return d


class LogEntry(BaseModel):
    timestamp: datetime
    message: str
    stream: Literal["stdout", "stderr", "journal"]

    def render(self) -> str:
        """One compact line: UTC timestamp, stderr marker when relevant, message."""
        ts = self.timestamp.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
        marker = " [stderr]" if self.stream == "stderr" else ""
        return f"{ts}{marker} {self.message}"


class LogResult(BaseModel):
    lines: list[str]  # "YYYY-MM-DD HH:MM:SS [stderr] message", UTC, oldest first
    returned: int
    truncated: bool


class Volume(BaseModel):
    mount: str
    total_bytes: int
    used_bytes: int


class ServiceUsage(BaseModel):
    name: str
    kind: ServiceKind
    cpu_pct: float | None = None
    mem_bytes: int | None = None


class ResourceSnapshot(BaseModel):
    taken_at: datetime
    load_avg: list[float]
    cpu_count: int | None
    mem_total_bytes: int
    mem_used_bytes: int
    volumes: list[Volume]
    services: list[ServiceUsage] = Field(default_factory=list)


class Healthcheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    test: list[CommandArg] = Field(min_length=1)
    interval_s: int = Field(default=30, ge=1, le=3600)
    retries: int = Field(default=3, ge=1, le=100)


class ContainerSpec(BaseModel):
    """Declarative container configuration (data-model.md §2 ContainerSpec)."""

    model_config = ConfigDict(extra="forbid")
    image: ImageRef
    env: dict[ConfigKey, EnvValue] = Field(default_factory=dict)
    ports: list[PortMapping] = Field(default_factory=list)
    volumes: list[VolumeMapping] = Field(default_factory=list)
    networks: list[NetworkName] = Field(default_factory=list)
    command: list[CommandArg] | None = None
    restart_on_boot: bool = True
    healthcheck: Healthcheck | None = None

    def named_volumes(self) -> list[str]:
        return [v.split(":")[0] for v in self.volumes if not v.startswith("/")]
