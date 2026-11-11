"""The ServiceBackend protocol shared by the systemd and container backends."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from vps_mcp.config.policy import ServiceDeclaration
from vps_mcp.models.views import LogEntry, ServiceStatus, ServiceUsage


class ServiceBackend(Protocol):
    async def status(self, decl: ServiceDeclaration) -> ServiceStatus: ...

    async def list_status(self, decls: list[ServiceDeclaration]) -> list[ServiceStatus]: ...

    async def start(self, decl: ServiceDeclaration) -> ServiceStatus: ...

    async def stop(self, decl: ServiceDeclaration) -> ServiceStatus: ...

    async def restart(self, decl: ServiceDeclaration) -> ServiceStatus: ...

    async def set_boot(self, decl: ServiceDeclaration, enabled: bool) -> ServiceStatus: ...

    async def logs(
        self, decl: ServiceDeclaration, lines: int, since: datetime | None, until: datetime | None
    ) -> list[LogEntry]: ...

    async def resource_usage(self, decls: list[ServiceDeclaration]) -> list[ServiceUsage]: ...

    async def remove(self, decl: ServiceDeclaration, delete_data: bool) -> dict[str, Any]: ...
