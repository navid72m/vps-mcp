"""Everything a use case needs, wired once per server process."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from vps_mcp.config.policy import AppConfig
from vps_mcp.core.locks import ServiceLocks
from vps_mcp.core.managed_set import ManagedSet
from vps_mcp.models.inputs import ServiceKind
from vps_mcp.remote.connection import VpsConnection
from vps_mcp.remote.executor import RemoteExecutor
from vps_mcp.store.audit import AuditStore
from vps_mcp.store.revisions import RevisionStore
from vps_mcp.store.services import ServicesStore


@dataclass
class AppContext:
    config: AppConfig
    executor: RemoteExecutor
    db: sqlite3.Connection
    audit: AuditStore
    services: ServicesStore
    revisions: RevisionStore
    managed: ManagedSet
    locks: ServiceLocks
    backends: dict[ServiceKind, Any] = field(default_factory=dict)
    connection: VpsConnection | None = None
    migration: Any = None  # vps_mcp.migration.runtime.MigrationRuntime when [migration] is set
    memory: Any = None  # vps_mcp.memory.service.MemoryService when memory is enabled
    gitlab: Any = None  # vps_mcp.gitlab.runtime.GitLabRuntime when [gitlab] is set

    @property
    def policy(self):  # type: ignore[no-untyped-def]
        return self.config.policy

    @property
    def secret_strings(self) -> list[str]:
        return self.config.secret_strings

    def backend(self, kind: ServiceKind | str) -> Any:
        k = ServiceKind(kind)
        if k not in self.backends:
            from vps_mcp.errors import ErrorCategory, VpsMcpError

            raise VpsMcpError(
                ErrorCategory.NOT_PERMITTED, f"no backend configured for kind '{k.value}'"
            )
        return self.backends[k]

    @classmethod
    def build(
        cls,
        config: AppConfig,
        executor: RemoteExecutor,
        db: sqlite3.Connection,
        connection: VpsConnection | None = None,
    ) -> AppContext:
        from vps_mcp.backends.container import ContainerBackend
        from vps_mcp.backends.systemd import SystemdBackend
        from vps_mcp.config.policy import ContainerRuntime

        services = ServicesStore(db)
        ctx = cls(
            config=config,
            executor=executor,
            db=db,
            audit=AuditStore(
                db, secret_globs=config.policy.secret_keys, secret_strings=config.secret_strings
            ),
            services=services,
            revisions=RevisionStore(db, keep=config.policy.revisions_kept),
            managed=ManagedSet(config, services),
            locks=ServiceLocks(),
            connection=connection,
        )
        ctx.backends[ServiceKind.OS] = SystemdBackend(executor, config)
        if config.vps.container_runtime is not ContainerRuntime.NONE:
            ctx.backends[ServiceKind.CONTAINER] = ContainerBackend(executor, config)
        return ctx
