"""MCP server: a thin adapter from 16 tools to the core use cases (contracts/mcp-tools.md).

Tool inputs are declared with descriptive JSON schemas (FR-001) but validated inside the
use cases, so malformed requests still get a categorized error and an audit record.
"""

from __future__ import annotations

import copy
import functools
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp_types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field

from vps_mcp.config.loader import load_config
from vps_mcp.config.policy import AppConfig
from vps_mcp.core import actions, changes, deploy, queries, removal
from vps_mcp.core.context import AppContext
from vps_mcp.core.reconcile import Reconciler
from vps_mcp.errors import ErrorCategory, VpsMcpError
from vps_mcp.models.inputs import SERVICE_NAME_RE
from vps_mcp.models.views import ContainerSpec
from vps_mcp.remote.connection import VpsConnection
from vps_mcp.remote.executor import RemoteExecutor, SshExecutor
from vps_mcp.remote.redact import install_redaction, scrub_obj
from vps_mcp.store.db import default_db_path, open_db, purge_audit

log = logging.getLogger("vps_mcp")

INSTRUCTIONS = """\
Manages services on one VPS: operating-system services (systemd) and containers.
Services are addressed by name; pass kind ("os" | "container") when a name exists as both.
State-changing tools that deploy, update, configure, roll back or remove return a preview
unless confirm=true — show the preview to the operator and only confirm with their approval.
Protected services need confirm=true to stop/restart/disable and can never be removed."""


# ----------------------------------------------------------------------- schema helpers


def _inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    defs = schema.pop("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(copy.deepcopy(defs[node["$ref"].split("/")[-1]]))
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return dict(walk(schema))


CONTAINER_SPEC_SCHEMA = _inline_refs(ContainerSpec.model_json_schema())
_SPEC_PATCH_SCHEMA = copy.deepcopy(CONTAINER_SPEC_SCHEMA)
_SPEC_PATCH_SCHEMA.pop("required", None)
_SPEC_PATCH_SCHEMA["description"] = (
    "Fields to change. null resets a field to its default; env is merged key by key "
    "(null removes a key); lists such as networks are replaced whole."
)

ServiceParam = Annotated[
    str,
    Field(
        description="Service name as listed by list_services",
        json_schema_extra={"pattern": SERVICE_NAME_RE.pattern, "maxLength": 128},
    ),
]
KindParam = Annotated[
    Literal["os", "container"] | None,
    Field(description="Service kind; required only when the name exists as both kinds"),
]
ConfirmParam = Annotated[bool, Field(description="Set true only after the operator approved")]
ConfirmRequired = Annotated[
    bool, Field(description="false returns a preview and changes nothing; true applies")
]


# ---------------------------------------------------------------------- result helpers


def _session_id(ctx: Context | None) -> str | None:
    try:
        return f"session-{id(ctx.session):x}" if ctx is not None else None
    except Exception:  # noqa: BLE001 - no request context (direct calls in tests)
        return None


def _result(payload: dict[str, Any], *, is_error: bool, secrets: list[str]) -> CallToolResult:
    clean = scrub_obj(payload, secrets)
    return CallToolResult(
        content=[
            TextContent(
                text=json.dumps(clean, separators=(",", ":"), ensure_ascii=False, default=str)
            )
        ],
        structured_content=clean,
        is_error=is_error,
    )


def _tool_wrapper(
    app: AppContext,
) -> Callable[[Callable[..., Awaitable[dict[str, Any]]]], Callable[..., Awaitable[CallToolResult]]]:
    def decorate(
        fn: Callable[..., Awaitable[dict[str, Any]]],
    ) -> Callable[..., Awaitable[CallToolResult]]:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> CallToolResult:
            try:
                payload = await fn(*args, **kwargs)
            except VpsMcpError as err:
                return _result(err.to_envelope(), is_error=True, secrets=app.secret_strings)
            except Exception as exc:  # noqa: BLE001
                log.exception("unhandled error in tool %s", fn.__name__)
                internal = VpsMcpError(
                    ErrorCategory.FAILED, f"internal error: {exc.__class__.__name__}"
                )
                return _result(internal.to_envelope(), is_error=True, secrets=app.secret_strings)
            return _result({"ok": True, **payload}, is_error=False, secrets=app.secret_strings)

        return wrapper

    return decorate


RO = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)


def _rw(destructive: bool, idempotent: bool = False) -> ToolAnnotations:
    return ToolAnnotations(
        read_only_hint=False,
        destructive_hint=destructive,
        idempotent_hint=idempotent,
        open_world_hint=False,
    )


# ------------------------------------------------------------------------------- server


def create_server(app: AppContext) -> MCPServer:
    mcp = MCPServer(name="vps-mcp", instructions=INSTRUCTIONS, version="0.1.0")
    wrap = _tool_wrapper(app)

    # ------------------------------------------------------------- read-only

    @mcp.tool(
        annotations=RO,
        description=(
            "List every managed service (OS services and containers) with its kind, state, uptime, "
            "boot setting, version and networks."
        ),
    )
    @wrap
    async def list_services(kind: KindParam = None) -> dict[str, Any]:
        return await queries.list_services(app, kind)

    @mcp.tool(
        annotations=RO,
        description=(
            "Show detailed status of one managed service, including the exit reason if it failed."
        ),
    )
    @wrap
    async def get_service_status(service: ServiceParam, kind: KindParam = None) -> dict[str, Any]:
        return await queries.get_service_status(app, service, kind)

    @mcp.tool(
        annotations=RO,
        description=(
            "Read recent log lines of a managed service, newest last. Optional filters: line "
            "count (default 200, max 5000), time window (ISO-8601 or relative like 15m/2h/1d) and "
            "a literal, case-insensitive keyword."
        ),
    )
    @wrap
    async def get_service_logs(
        service: ServiceParam,
        kind: KindParam = None,
        lines: Annotated[
            int | None, Field(json_schema_extra={"minimum": 1, "maximum": 5000})
        ] = None,
        since: Annotated[str | None, Field(description="e.g. 2026-11-11T10:00:00Z or 15m")] = None,
        until: Annotated[str | None, Field(description="e.g. 2026-11-11T11:00:00Z or 5m")] = None,
        keyword: Annotated[str | None, Field(json_schema_extra={"maxLength": 200})] = None,
    ) -> dict[str, Any]:
        return await queries.get_service_logs(app, service, kind, lines, since, until, keyword)

    @mcp.tool(
        annotations=RO,
        description=(
            "Show a service's configuration: declared config files (OS) or the container spec. "
            "Secret values are masked."
        ),
    )
    @wrap
    async def get_service_config(service: ServiceParam, kind: KindParam = None) -> dict[str, Any]:
        return await changes.get_service_config(app, service, kind)

    @mcp.tool(
        annotations=RO,
        description=(
            "Show server load, memory and disk usage, plus CPU/memory of each managed service."
        ),
    )
    @wrap
    async def get_resource_usage(include_services: bool = True) -> dict[str, Any]:
        return await queries.get_resource_usage(app, include_services)

    @mcp.tool(
        annotations=RO,
        description=("Show the audit trail of state-changing actions, newest first."),
    )
    @wrap
    async def get_audit_log(
        limit: Annotated[int, Field(json_schema_extra={"minimum": 1, "maximum": 500})] = 50,
        service: Annotated[str | None, Field(description="Filter by service name")] = None,
        action: Literal[
            "start",
            "stop",
            "restart",
            "enable",
            "disable",
            "deploy",
            "update",
            "configure",
            "rollback",
            "remove",
        ]
        | None = None,
        outcome: Literal[
            "success", "failed", "refused", "timed_out", "unknown", "rolled_back", "preview"
        ]
        | None = None,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 500:
            raise VpsMcpError(ErrorCategory.INVALID_INPUT, "limit must be 1..500")
        return {
            "records": app.audit.query(limit=limit, service=service, action=action, outcome=outcome)
        }

    @mcp.tool(
        annotations=RO, description="List stored configuration/version revisions of a service."
    )
    @wrap
    async def list_revisions(service: ServiceParam, kind: KindParam = None) -> dict[str, Any]:
        return await changes.list_revisions(app, service, kind)

    # ---------------------------------------------------------- control

    @mcp.tool(
        annotations=_rw(False, idempotent=True),
        description=("Start a managed service and report its verified state."),
    )
    @wrap
    async def start_service(
        service: ServiceParam,
        kind: KindParam = None,
        confirm: ConfirmParam = False,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        return await actions.control(app, "start", service, kind, confirm, _session_id(ctx))

    @mcp.tool(
        annotations=_rw(True, idempotent=True),
        description=(
            "Stop a managed service. Protected services require confirm=true. The VPS remote-access "
            "service can never be stopped."
        ),
    )
    @wrap
    async def stop_service(
        service: ServiceParam,
        kind: KindParam = None,
        confirm: ConfirmParam = False,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        return await actions.control(app, "stop", service, kind, confirm, _session_id(ctx))

    @mcp.tool(
        annotations=_rw(True),
        description=(
            "Restart a managed service and report its verified state. Protected services require "
            "confirm=true. On failure, recent log lines are included."
        ),
    )
    @wrap
    async def restart_service(
        service: ServiceParam,
        kind: KindParam = None,
        confirm: ConfirmParam = False,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        return await actions.control(app, "restart", service, kind, confirm, _session_id(ctx))

    @mcp.tool(
        annotations=_rw(False, idempotent=True),
        description=(
            "Enable or disable starting a service at boot. Disabling a protected service requires "
            "confirm=true."
        ),
    )
    @wrap
    async def set_boot_start(
        service: ServiceParam,
        enabled: bool,
        kind: KindParam = None,
        confirm: ConfirmParam = False,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        return await actions.set_boot_start(app, service, kind, enabled, confirm, _session_id(ctx))

    # ------------------------------------------------------- lifecycle

    @mcp.tool(
        annotations=_rw(False),
        description=(
            "Deploy a new service from an approved source: an OS package (kind='os', with 'os') or a "
            "container (kind='container', with 'container'). Containers on the same 'networks' "
            "reach each other by service name. confirm=false returns a preview."
        ),
    )
    @wrap
    async def deploy_service(
        name: ServiceParam,
        kind: Literal["os", "container"],
        confirm: ConfirmRequired,
        os: Annotated[
            dict[str, Any] | None,
            Field(
                description="OS package to install",
                json_schema_extra={
                    "properties": {
                        "package": {"type": "string"},
                        "version": {"type": "string"},
                        "unit": {"type": "string"},
                        "enable_on_boot": {"type": "boolean", "default": True},
                    },
                    "required": ["package"],
                },
            ),
        ] = None,
        container: Annotated[
            dict[str, Any] | None,
            Field(description="Container spec", json_schema_extra=CONTAINER_SPEC_SCHEMA),
        ] = None,
        start: bool = True,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        return await deploy.deploy_service(
            app,
            name=name,
            kind=kind,
            os=os,
            container=container,
            start=start,
            confirm=confirm,
            session_id=_session_id(ctx),
        )

    @mcp.tool(
        annotations=_rw(True),
        description=(
            "Update a service to another version: 'version' for OS packages, 'image' for "
            "containers. Rolls back automatically if the service is unhealthy afterwards. "
            "confirm=false returns a preview."
        ),
    )
    @wrap
    async def update_service(
        service: ServiceParam,
        confirm: ConfirmRequired,
        kind: KindParam = None,
        version: str | None = None,
        image: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        return await changes.update_service(
            app,
            service=service,
            kind=kind,
            version=version,
            image=image,
            confirm=confirm,
            session_id=_session_id(ctx),
        )

    @mcp.tool(
        annotations=_rw(True),
        description=(
            "Change a service's configuration: replace declared config files (OS, 'files') or "
            "patch the container spec ('spec_patch'). confirm=false returns a masked diff. Rolls "
            "back automatically if the service is unhealthy afterwards."
        ),
    )
    @wrap
    async def configure_service(
        service: ServiceParam,
        confirm: ConfirmRequired,
        kind: KindParam = None,
        files: Annotated[
            list[dict[str, Any]] | None,
            Field(
                description="Full new contents of declared config files",
                json_schema_extra={
                    "items": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                        "required": ["path", "content"],
                    }
                },
            ),
        ] = None,
        spec_patch: Annotated[
            dict[str, Any] | None, Field(json_schema_extra=_SPEC_PATCH_SCHEMA)
        ] = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        return await changes.configure_service(
            app,
            service=service,
            kind=kind,
            files=files,
            spec_patch=spec_patch,
            confirm=confirm,
            session_id=_session_id(ctx),
        )

    @mcp.tool(
        annotations=_rw(True),
        description=(
            "Restore the previous revision (configuration/version) of a service. confirm=false "
            "returns a preview."
        ),
    )
    @wrap
    async def rollback_service(
        service: ServiceParam,
        confirm: ConfirmRequired,
        kind: KindParam = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        return await changes.rollback_service(
            app, service=service, kind=kind, confirm=confirm, session_id=_session_id(ctx)
        )

    @mcp.tool(
        annotations=_rw(True),
        description=(
            "Remove a service: stop, uninstall and drop it from the managed set. Data is kept unless "
            "delete_data=true. Protected services cannot be removed. confirm=false returns a "
            "preview of what would be removed."
        ),
    )
    @wrap
    async def remove_service(
        service: ServiceParam,
        confirm: ConfirmRequired,
        kind: KindParam = None,
        delete_data: bool = False,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        return await removal.remove_service(
            app,
            service=service,
            kind=kind,
            delete_data=delete_data,
            confirm=confirm,
            session_id=_session_id(ctx),
        )

    if app.migration is not None:
        from vps_mcp.migration import tools as migration_tools

        migration_tools.register(mcp, app.migration, wrap, _session_id)
    return mcp


# ------------------------------------------------------------------------- bootstrapping


def build_context(
    config: AppConfig, *, executor: RemoteExecutor | None = None, db_path: str | Path | None = None
) -> AppContext:
    connection = None
    if executor is None:
        # Leave headroom inside the query budget so an unreachable host is reported as
        # connection_failure (FR-004) rather than a generic query timeout.
        query_s = float(config.policy.timeouts.query_s)
        connection = VpsConnection(config.vps, connect_timeout=max(1.0, query_s * 0.6))
        executor = SshExecutor(connection)
    db = open_db(db_path or default_db_path(config.vps.name))
    purge_audit(db, config.policy.audit_retention_days)
    app = AppContext.build(config, executor, db, connection=connection)
    Reconciler(app).register()
    if config.migration is not None:
        from vps_mcp.migration.db import MigrationDb
        from vps_mcp.migration.runtime import MigrationRuntime
        from vps_mcp.migration.store import MigrationStore

        app.migration = MigrationRuntime(
            app=app,
            settings=config.migration,
            db=MigrationDb(executor, config.migration),
            store=MigrationStore(db),
        )
    return app


def build_server(config_path: str | Path) -> MCPServer:
    config = load_config(config_path)
    for name in ("vps_mcp", "asyncssh", "mcp"):
        install_redaction(logging.getLogger(name), config.secret_strings)
    return create_server(build_context(config))
