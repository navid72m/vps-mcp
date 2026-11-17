"""Compact tool surface: consolidated, action-based facade (13 tools).

Reduces the MCP tool surface from 45 granular tools to 13 action-grouped tools,
saving ~70% prompt token overhead while providing clear workflow routing for LLMs.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp_types import ToolAnnotations
from pydantic import Field

from vps_mcp.core import actions, changes, deploy, queries, removal
from vps_mcp.core.context import AppContext
from vps_mcp.errors import ErrorCategory, VpsMcpError

log = logging.getLogger(__name__)

RO = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
RW = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False)

Wrap = Callable[[Callable[..., Awaitable[dict[str, Any]]]], Callable[..., Awaitable[Any]]]
SessionId = Callable[[Any], str | None]


def register_compact_tools(
    mcp: MCPServer,
    app: AppContext,
    wrap: Wrap,
    session_id: SessionId,
) -> None:
    """Register the consolidated 13-tool surface."""

    # =========================================================================
    # 1. Holistic VPS Reconnaissance
    # =========================================================================
    @mcp.tool(
        annotations=RO,
        description=(
            "One-shot comprehensive VPS infrastructure assessment.\n\n"
            "Inspects OS distribution, kernel architecture, uptime, compute load averages (1m/5m/15m), "
            "RAM utilization, storage volume headroom (<15% free triggers warnings), container runtimes "
            "(Docker/Podman), systemd health, managed services breakdown, allowlisted databases, and "
            "active migration rehearsals. Emits an overall health verdict ('healthy', 'degraded', 'critical') "
            "and actionable operational warnings."
        ),
    )
    @wrap
    async def assess_infrastructure(
        include_services: bool = True,
        include_databases: bool = True,
    ) -> dict[str, Any]:
        return await queries.assess_infrastructure(
            app, include_services=include_services, include_databases=include_databases
        )

    # =========================================================================
    # 2. VPS Service Inspection
    # =========================================================================
    @mcp.tool(
        annotations=RO,
        description=(
            "Inspect VPS services: inventory, deep status, logs, configuration, revisions, or audit trail.\n\n"
            "Actions:\n"
            "- 'list': Enumerate all managed OS services and containers with state, uptime, and versions.\n"
            "- 'status': Deep health inspection of a service including exit code and failure diagnostics.\n"
            "- 'logs': Tail recent log lines (filter by 'lines', 'since', 'until', 'keyword').\n"
            "- 'config': Read live configuration files (OS) or container spec on the VPS (secrets masked).\n"
            "- 'revisions': Inspect stored historical snapshot ledger (active, previous, superseded).\n"
            "- 'audit': Query the state-changing operations audit trail for the host or service."
        ),
    )
    @wrap
    async def service_inspect(
        action: Literal["list", "status", "logs", "config", "revisions", "audit"],
        service: Annotated[str | None, Field(description="Target service name")] = None,
        kind: Annotated[Literal["os", "container"] | None, Field(description="Service kind disambiguator")] = None,
        lines: Annotated[int, Field(ge=1, le=5000, description="Log line count (default 200)")] = 200,
        since: Annotated[str | None, Field(description="Log window start (ISO or relative e.g. 15m)")] = None,
        until: Annotated[str | None, Field(description="Log window end (ISO or relative e.g. 5m)")] = None,
        keyword: Annotated[str | None, Field(description="Case-insensitive log keyword filter")] = None,
        limit: Annotated[int, Field(ge=1, le=500, description="Audit log entry limit (default 50)")] = 50,
        outcome: Annotated[str | None, Field(description="Audit outcome filter")] = None,
    ) -> dict[str, Any]:
        if action == "list":
            return await queries.list_services(app, kind=kind)

        if action == "audit":
            return {"records": app.audit.query(limit=limit, service=service, outcome=outcome)}

        if not service:
            raise VpsMcpError(ErrorCategory.INVALID_INPUT, f"service name is required for action='{action}'")

        if action == "status":
            return await queries.get_service_status(app, service=service, kind=kind)
        if action == "logs":
            return await queries.get_service_logs(
                app, service=service, kind=kind, lines=lines, since=since, until=until, keyword=keyword
            )
        if action == "config":
            return await changes.get_service_config(app, service=service, kind=kind)
        if action == "revisions":
            return await changes.list_revisions(app, service=service, kind=kind)

        raise VpsMcpError(ErrorCategory.INVALID_INPUT, f"Unknown action: {action}")

    # =========================================================================
    # 3. VPS Service Runtime Control
    # =========================================================================
    @mcp.tool(
        annotations=RW,
        description=(
            "Control runtime state of a managed service on the VPS.\n\n"
            "Actions:\n"
            "- 'start': Start a stopped service and report verified status.\n"
            "- 'stop': Stop a service (protected services require confirm=true).\n"
            "- 'restart': Restart a service and report verified state (logs included on failure).\n"
            "- 'enable_boot': Enable service to start automatically at system boot.\n"
            "- 'disable_boot': Disable service from starting at boot (protected requires confirm=true)."
        ),
    )
    @wrap
    async def service_control(
        service: Annotated[str, Field(description="Target service name")],
        action: Literal["start", "stop", "restart", "enable_boot", "disable_boot"],
        kind: Annotated[Literal["os", "container"] | None, Field(description="Service kind disambiguator")] = None,
        confirm: Annotated[bool, Field(description="Operator approval flag (required for protected services)")] = False,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        sess = session_id(ctx)
        if action in ("start", "stop", "restart"):
            return await actions.control(app, action, service, kind, confirm, sess)
        if action == "enable_boot":
            return await actions.set_boot_start(app, service, enabled=True, kind=kind, confirm=confirm, session_id=sess)
        if action == "disable_boot":
            return await actions.set_boot_start(app, service, enabled=False, kind=kind, confirm=confirm, session_id=sess)

        raise VpsMcpError(ErrorCategory.INVALID_INPUT, f"Unknown action: {action}")

    # =========================================================================
    # 4. VPS Service Lifecycle & Configuration
    # =========================================================================
    @mcp.tool(
        annotations=DESTRUCTIVE,
        description=(
            "Manage service lifecycle, deployment, declarative configuration, and rollbacks.\n\n"
            "Actions:\n"
            "- 'deploy': Deploy a new service from an approved OS package or declarative container spec.\n"
            "- 'update': Upgrade/downgrade OS package version or container image (auto-rolls back if unhealthy).\n"
            "- 'configure': Replace declared config files or patch container spec (masked diff preview).\n"
            "- 'rollback': Restore the previous revision of configuration/version.\n"
            "- 'remove': Stop, uninstall, and unmanage a service (data retained unless delete_data=true).\n\n"
            "Safety: confirm=false returns a preview/diff without modifying state; confirm=true applies."
        ),
    )
    @wrap
    async def service_manage(
        service: Annotated[str, Field(description="Target service name")],
        action: Literal["deploy", "update", "configure", "rollback", "remove"],
        confirm: Annotated[bool, Field(description="false = preview only; true = apply change")],
        kind: Annotated[Literal["os", "container"] | None, Field(description="Service kind disambiguator")] = None,
        os: Annotated[dict[str, Any] | None, Field(description="OS package specification for deploy")] = None,
        container: Annotated[dict[str, Any] | None, Field(description="Container specification for deploy")] = None,
        version: Annotated[str | None, Field(description="Target OS package version for update")] = None,
        image: Annotated[str | None, Field(description="Target container image for update")] = None,
        files: Annotated[list[dict[str, str]] | None, Field(description="New config files for configure")] = None,
        spec_patch: Annotated[dict[str, Any] | None, Field(description="Container spec patch for configure")] = None,
        delete_data: Annotated[bool, Field(description="Delete persistent data during remove (default false)")] = False,
        start: Annotated[bool, Field(description="Start service after deploy (default true)")] = True,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        sess = session_id(ctx)
        if action == "deploy":
            if not kind:
                raise VpsMcpError(ErrorCategory.INVALID_INPUT, "kind ('os' | 'container') is required for deploy")
            return await deploy.deploy_service(
                app, name=service, kind=kind, confirm=confirm, os=os, container=container, start=start, session_id=sess
            )
        if action == "update":
            return await changes.update_service(
                app, service=service, confirm=confirm, kind=kind, version=version, image=image, session_id=sess
            )
        if action == "configure":
            return await changes.configure_service(
                app, service=service, kind=kind, files=files, spec_patch=spec_patch, confirm=confirm, session_id=sess
            )
        if action == "rollback":
            return await changes.rollback_service(app, service=service, kind=kind, confirm=confirm, session_id=sess)
        if action == "remove":
            return await removal.remove_service(
                app, service=service, kind=kind, delete_data=delete_data, confirm=confirm, session_id=sess
            )

        raise VpsMcpError(ErrorCategory.INVALID_INPUT, f"Unknown action: {action}")

    # =========================================================================
    # 5. Database Introspection & Comparison
    # =========================================================================
    if app.migration is not None:
        from vps_mcp.migration import checks, cleanup, compare, copies, pipeline_logs, rehearsal, understand

        @mcp.tool(
            annotations=RO,
            description=(
                "Introspect PostgreSQL & Odoo enterprise databases and compare schema states.\n\n"
                "Actions:\n"
                "- 'list': Enumerate allowlisted target databases and active disposable rehearsal copies.\n"
                "- 'overview': Database size, largest tables, module counts, and problem indicators.\n"
                "- 'modules': List installed Odoo modules with version, status, and code existence.\n"
                "- 'table': Structure of one table: columns, data types, nullability, defaults, indexes.\n"
                "- 'compare': Diff before vs after databases with risk findings and promotion verdict."
            ),
        )
        @wrap
        async def db_inspect(
            action: Literal["list", "overview", "modules", "table", "compare"],
            db: Annotated[str | None, Field(description="Database name (target or vmcp_ copy)")] = None,
            before: Annotated[str | None, Field(description="Pre-migration database for compare")] = None,
            after: Annotated[str | None, Field(description="Post-migration database for compare")] = None,
            table: Annotated[str | None, Field(description="Table name for action='table'")] = None,
            state: Annotated[str | None, Field(description="Module state filter")] = None,
            major: Annotated[str | None, Field(description="Major version filter (e.g. '17')")] = None,
            code_missing: Annotated[bool | None, Field(description="Filter modules missing code")] = None,
            exact_counts: Annotated[bool, Field(description="Compute exact table row counts in compare")] = True,
        ) -> dict[str, Any]:
            if action == "list":
                try:
                    await copies.sweep(app.migration)
                except Exception:
                    pass
                return await understand.db_list(app.migration)

            if action == "compare":
                if not before or not after:
                    raise VpsMcpError(ErrorCategory.INVALID_INPUT, "before and after databases required for compare")
                return await compare.compare_databases(app.migration, before, after, exact_counts)

            if not db:
                raise VpsMcpError(ErrorCategory.INVALID_INPUT, f"db is required for action='{action}'")

            if action == "overview":
                return await understand.overview(app.migration, db)
            if action == "modules":
                return await understand.modules(app.migration, db, state, major, code_missing)
            if action == "table":
                if not table:
                    raise VpsMcpError(ErrorCategory.INVALID_INPUT, "table is required for action='table'")
                return await understand.table(app.migration, db, table)

            raise VpsMcpError(ErrorCategory.INVALID_INPUT, f"Unknown action: {action}")

        # =========================================================================
        # 6. Migration Rehearsal Management
        # =========================================================================
        @mcp.tool(
            annotations=RW,
            description=(
                "Execute and monitor zero-risk migration rehearsals on disposable copies.\n\n"
                "Actions:\n"
                "- 'start': Rehearse a pipeline on a disposable copy (confirm=false returns plan & disk estimate).\n"
                "- 'status': Poll progress of running or completed rehearsal and view risk report.\n"
                "- 'cancel': Abort a running rehearsal while preserving the copy for inspection.\n"
                "- 'last_run': Summarize latest pipeline run: step progress and frequent Odoo errors.\n"
                "- 'drop_copies': Drop expired or named disposable rehearsal copies to reclaim disk space."
            ),
        )
        @wrap
        async def rehearsal_manage(
            action: Literal["start", "status", "cancel", "last_run", "drop_copies"],
            pipeline: Annotated[str | None, Field(description="Pipeline name from policy")] = None,
            source: Annotated[str | None, Field(description="Source database to clone from")] = None,
            rehearsal_id: Annotated[str | None, Field(description="Rehearsal identifier")] = None,
            copy_ids: Annotated[list[str] | None, Field(description="Specific copy IDs to drop")] = None,
            expired_only: Annotated[bool, Field(description="Drop only expired copies (default false)")] = False,
            method: Annotated[Literal["auto", "template", "dump"], Field(description="Clone method")] = "auto",
            plan_hash: Annotated[str | None, Field(description="Fingerprint from confirm=false preview")] = None,
            confirm: Annotated[bool, Field(description="false = preview/plan only; true = execute")] = False,
            ctx: Context | None = None,
        ) -> dict[str, Any]:
            sess = session_id(ctx)
            if action == "start":
                if not pipeline or not source:
                    raise VpsMcpError(ErrorCategory.INVALID_INPUT, "pipeline and source required for action='start'")
                return await rehearsal.rehearse(
                    app.migration,
                    pipeline=pipeline,
                    source=source,
                    confirm=confirm,
                    method=method,
                    plan_hash=plan_hash,
                    session_id=sess,
                )
            if action == "status":
                return await rehearsal.status(app.migration, rehearsal_id=rehearsal_id)
            if action == "cancel":
                if not rehearsal_id:
                    raise VpsMcpError(ErrorCategory.INVALID_INPUT, "rehearsal_id required for action='cancel'")
                return await rehearsal.cancel(app.migration, rehearsal_id=rehearsal_id, confirm=confirm, session_id=sess)
            if action == "last_run":
                if not pipeline:
                    raise VpsMcpError(ErrorCategory.INVALID_INPUT, "pipeline required for action='last_run'")
                return await pipeline_logs.summarise(app.migration, pipeline)
            if action == "drop_copies":
                return await cleanup.drop_copies(
                    app.migration, confirm=confirm, copy_ids=copy_ids, expired_only=expired_only, session_id=sess
                )

            raise VpsMcpError(ErrorCategory.INVALID_INPUT, f"Unknown action: {action}")

        # =========================================================================
        # 7. Migration Verification & Reports
        # =========================================================================
        @mcp.tool(
            annotations=RW,
            description=(
                "Run database verification checks and retrieve stored risk comparison reports.\n\n"
                "Actions:\n"
                "- 'run_check': Run declared check (e.g. post-upgrade queries) against a target or copy.\n"
                "- 'list_reports': List recent migration risk comparison reports and promotion verdicts.\n"
                "- 'get_report': Fetch paginated findings of a specific report filtered by severity."
            ),
        )
        @wrap
        async def migration_audit(
            action: Literal["run_check", "list_reports", "get_report"],
            check: Annotated[str | None, Field(description="Declared check name from policy")] = None,
            db: Annotated[str | None, Field(description="Target database")] = None,
            report_id: Annotated[str | None, Field(description="Report identifier")] = None,
            severity: Annotated[Literal["blocking", "warning", "info"] | None, Field(description="Severity filter")] = None,
            category: Annotated[str | None, Field(description="Finding category filter")] = None,
            page: Annotated[int, Field(ge=1, description="Page number (50 items/page)")] = 1,
            limit: Annotated[int, Field(ge=1, le=100, description="Report list limit")] = 20,
            confirm: Annotated[bool, Field(description="Approval flag for may_modify checks")] = False,
            ctx: Context | None = None,
        ) -> dict[str, Any]:
            if action == "run_check":
                if not check or not db:
                    raise VpsMcpError(ErrorCategory.INVALID_INPUT, "check and db required for action='run_check'")
                return await checks.run_check(
                    app.migration, check=check, db=db, confirm=confirm, session_id=session_id(ctx)
                )
            if action == "list_reports":
                return await compare.list_reports(app.migration, limit=limit)
            if action == "get_report":
                if not report_id:
                    raise VpsMcpError(ErrorCategory.INVALID_INPUT, "report_id required for action='get_report'")
                return await compare.get_report(
                    app.migration, report_id=report_id, severity=severity, category=category, page=page
                )

            raise VpsMcpError(ErrorCategory.INVALID_INPUT, f"Unknown action: {action}")

        # =========================================================================
        # 8. Declared Background Automation Jobs
        # =========================================================================
        if app.migration.settings.job:
            from vps_mcp.jobs import runs

            @mcp.tool(
                annotations=RW,
                description=(
                    "Manage declared background automation scripts and migration pipelines on the VPS.\n\n"
                    "Actions:\n"
                    "- 'list': Enumerate operator-declared jobs, target databases, and recent runs.\n"
                    "- 'run': Run a job (confirm=false returns fingerprint plan; confirm=true executes).\n"
                    "- 'status': Poll running job progress, step status, and output logs.\n"
                    "- 'cancel': Terminate an in-flight background job safely."
                ),
            )
            @wrap
            async def job_manage(
                action: Literal["list", "run", "status", "cancel"],
                job: Annotated[str | None, Field(description="Job name from policy")] = None,
                run_id: Annotated[str | None, Field(description="Run ID for status/cancel")] = None,
                params: Annotated[dict[str, str] | None, Field(description="Declared script parameters")] = None,
                plan_id: Annotated[str | None, Field(description="Fingerprint plan ID from preview")] = None,
                confirm: Annotated[bool, Field(description="false = plan only; true = start job")] = False,
                ctx: Context | None = None,
            ) -> dict[str, Any]:
                sess = session_id(ctx)
                if action == "list":
                    return runs.list_jobs(app.migration)
                if action == "run":
                    if not job:
                        raise VpsMcpError(ErrorCategory.INVALID_INPUT, "job name required for action='run'")
                    return await runs.start(
                        app.migration, job=job, params=params, plan_id=plan_id, confirm=confirm, session_id=sess
                    )
                if action == "status":
                    return await runs.status(app.migration, job=job, run_id=run_id)
                if action == "cancel":
                    if not run_id:
                        raise VpsMcpError(ErrorCategory.INVALID_INPUT, "run_id required for action='cancel'")
                    return await runs.cancel(app.migration, run_id=run_id, confirm=confirm, session_id=sess)

                raise VpsMcpError(ErrorCategory.INVALID_INPUT, f"Unknown action: {action}")

    # =========================================================================
    # 9-12. GitLab Integration (Repo, Items, CI, Deploy)
    # =========================================================================
    if app.gitlab is not None:
        from vps_mcp.gitlab import ci, deploy as gitlab_deploy_mod, items, read

        @mcp.tool(
            annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True),
            description=(
                "Read-only inspection of GitLab repositories, branches, directory trees, files, and commits.\n\n"
                "Actions:\n"
                "- 'projects': List allowlisted GitLab project aliases and permissions.\n"
                "- 'refs': List branches and tags for a project.\n"
                "- 'tree': List directory files/folders at a ref.\n"
                "- 'read_file': Read file content at a ref with pagination (start_line/max_lines).\n"
                "- 'commits': View recent commit history with author, date, and commit message.\n"
                "- 'compare': Diff two branches/commits with per-file change summaries."
            ),
        )
        @wrap
        async def gitlab_repo(
            action: Literal["projects", "refs", "tree", "read_file", "commits", "compare"],
            project: Annotated[str, Field(description="Project alias from gitlab_projects")] = "",
            ref: Annotated[str | None, Field(description="Branch, tag, or commit SHA")] = None,
            path: Annotated[str | None, Field(description="File or directory path")] = None,
            from_ref: Annotated[str | None, Field(description="Base ref for compare")] = None,
            to_ref: Annotated[str | None, Field(description="Target ref for compare")] = None,
            sha: Annotated[str | None, Field(description="Specific commit SHA to inspect")] = None,
            since: Annotated[str | None, Field(description="ISO date filter start")] = None,
            until: Annotated[str | None, Field(description="ISO date filter end")] = None,
            kind: Annotated[Literal["branches", "tags", "both"], Field(description="Ref kind")] = "both",
            search: Annotated[str | None, Field(description="Search filter")] = None,
            limit: Annotated[int, Field(ge=1, le=200, description="Items limit")] = 20,
            start_line: Annotated[int, Field(ge=1, description="File start line (1-based)")] = 1,
            max_lines: Annotated[int | None, Field(ge=1, le=2000, description="Max lines to read")] = None,
            recursive: Annotated[bool, Field(description="Recursive tree listing")] = False,
        ) -> dict[str, Any]:
            if action == "projects":
                return await read.projects(app.gitlab)

            if not project:
                raise VpsMcpError(ErrorCategory.INVALID_INPUT, f"project alias is required for action='{action}'")

            if action == "refs":
                return await read.refs(app.gitlab, project, kind=kind, search=search, limit=limit)
            if action == "tree":
                return await read.tree(app.gitlab, project, ref=ref, path=path, recursive=recursive, limit=limit)
            if action == "read_file":
                if not path:
                    raise VpsMcpError(ErrorCategory.INVALID_INPUT, "path is required for action='read_file'")
                return await read.file(app.gitlab, project, path=path, ref=ref, start_line=start_line, max_lines=max_lines)
            if action == "commits":
                return await read.commits(
                    app.gitlab, project, ref=ref, path=path, since=since, until=until, limit=limit, sha=sha
                )
            if action == "compare":
                if not from_ref or not to_ref:
                    raise VpsMcpError(ErrorCategory.INVALID_INPUT, "from_ref and to_ref required for action='compare'")
                return await read.compare(app.gitlab, project, from_ref=from_ref, to_ref=to_ref, path=path)

            raise VpsMcpError(ErrorCategory.INVALID_INPUT, f"Unknown action: {action}")

        @mcp.tool(
            annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True),
            description=(
                "Search, read, and create GitLab issues and merge requests.\n\n"
                "Actions:\n"
                "- 'list': Search or list issues or MRs (filter by state, labels, author, keyword).\n"
                "- 'read': Read an issue or MR description and discussion comments.\n"
                "- 'create_issue': Open a new issue with title, description, and labels (confirm=false previews).\n"
                "- 'comment': Post a comment to an issue or MR (confirm=false previews).\n"
                "- 'create_mr': Open a merge request between branches (confirm=false previews).\n"
                "- 'close_issue': Close an issue created by vps-mcp (confirm=false previews)."
            ),
        )
        @wrap
        async def gitlab_items(
            action: Literal["list", "read", "create_issue", "comment", "create_mr", "close_issue"],
            project: Annotated[str, Field(description="Project alias from gitlab_projects")],
            kind: Annotated[Literal["issue", "mr"] | None, Field(description="'issue' or 'mr'")] = None,
            iid: Annotated[int | None, Field(ge=1, description="Issue or MR number")] = None,
            title: Annotated[str | None, Field(max_length=255, description="Issue or MR title")] = None,
            description: Annotated[str | None, Field(max_length=20000, description="Issue or MR description")] = None,
            body: Annotated[str | None, Field(max_length=20000, description="Comment body")] = None,
            labels: Annotated[list[str] | None, Field(max_length=10, description="Labels")] = None,
            source_branch: Annotated[str | None, Field(description="MR source branch")] = None,
            target_branch: Annotated[str | None, Field(description="MR target branch")] = None,
            state: Annotated[Literal["opened", "closed", "merged", "all"], Field(description="State filter")] = "opened",
            search: Annotated[str | None, Field(description="Search term")] = None,
            author: Annotated[str | None, Field(description="Author filter")] = None,
            limit: Annotated[int, Field(ge=1, le=50, description="Items limit")] = 20,
            notes_limit: Annotated[int, Field(ge=1, le=50, description="Discussion notes limit")] = 20,
            confirm: Annotated[bool, Field(description="false = preview only; true = apply")] = False,
            ctx: Context | None = None,
        ) -> dict[str, Any]:
            if action == "list":
                if not kind:
                    raise VpsMcpError(ErrorCategory.INVALID_INPUT, "kind ('issue' | 'mr') required for action='list'")
                return await items.list_items(
                    app.gitlab, project, kind=kind, state=state, labels=labels, search=search, author=author, limit=limit
                )
            if action == "read":
                if not kind or not iid:
                    raise VpsMcpError(ErrorCategory.INVALID_INPUT, "kind and iid required for action='read'")
                return await items.read_item(app.gitlab, project, kind=kind, iid=iid, notes_limit=notes_limit)

            if action in ("create_issue", "comment", "create_mr", "close_issue"):
                return await items.write(
                    app.gitlab,
                    project=project,
                    action=action,
                    kind=kind,
                    iid=iid,
                    title=title,
                    description=description,
                    body=body,
                    labels=labels,
                    source_branch=source_branch,
                    target_branch=target_branch,
                    confirm=confirm,
                    session_id=session_id(ctx),
                )

            raise VpsMcpError(ErrorCategory.INVALID_INPUT, f"Unknown action: {action}")

        @mcp.tool(
            annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True),
            description=(
                "Inspect pipelines, read build logs, and trigger or cancel GitLab CI/CD jobs.\n\n"
                "Actions:\n"
                "- 'pipelines': List pipelines or jobs within a pipeline.\n"
                "- 'job_log': Tail execution log of a CI job (secrets masked).\n"
                "- 'retry_job': Retry a failed CI job (confirm=false previews).\n"
                "- 'cancel_job': Cancel a running CI job (confirm=false previews).\n"
                "- 'retry_pipeline': Retry failed jobs in a pipeline (confirm=false previews).\n"
                "- 'cancel_pipeline': Cancel an active pipeline (confirm=false previews).\n"
                "- 'trigger_pipeline': Trigger a new pipeline on an allowed ref (confirm=false previews)."
            ),
        )
        @wrap
        async def gitlab_ci(
            action: Literal["pipelines", "job_log", "retry_job", "cancel_job", "retry_pipeline", "cancel_pipeline", "trigger_pipeline"],
            project: Annotated[str, Field(description="Project alias from gitlab_projects")],
            confirm: Annotated[bool, Field(description="false = preview only; true = apply")] = False,
            pipeline_id: Annotated[int | None, Field(ge=1, description="Pipeline ID")] = None,
            job_id: Annotated[int | None, Field(ge=1, description="Job ID")] = None,
            ref: Annotated[str | None, Field(description="Git branch/tag ref")] = None,
            status: Annotated[str | None, Field(description="Pipeline status filter")] = None,
            lines: Annotated[int, Field(ge=1, le=2000, description="Log lines count")] = 200,
            limit: Annotated[int, Field(ge=1, le=50, description="Pipelines limit")] = 10,
            ctx: Context | None = None,
        ) -> dict[str, Any]:
            if action == "pipelines":
                return await read.pipelines(app.gitlab, project, ref=ref, status=status, limit=limit, pipeline_id=pipeline_id)
            if action == "job_log":
                if not job_id:
                    raise VpsMcpError(ErrorCategory.INVALID_INPUT, "job_id required for action='job_log'")
                return await read.job_log(app.gitlab, project, job_id, lines=lines)

            return await ci.ci(
                app.gitlab,
                project=project,
                action=action,
                confirm=confirm,
                job_id=job_id,
                pipeline_id=pipeline_id,
                ref=ref,
                session_id=session_id(ctx),
            )

        @mcp.tool(
            annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True),
            description=(
                "Deploy git code versions from GitLab to declared VPS targets with zero-downtime rollback.\n\n"
                "Actions:\n"
                "- 'targets': List declared deploy targets, active release version, and retention history.\n"
                "- 'deploy': Deploy a git ref to a VPS target (confirm=false returns plan; confirm=true applies).\n"
                "- 'rollback': Revert a target to its previous release and restart associated services."
            ),
        )
        @wrap
        async def gitlab_deploy(
            action: Literal["targets", "deploy", "rollback"],
            confirm: Annotated[bool, Field(description="false = preview only; true = apply change")] = False,
            target: Annotated[str, Field(description="Deploy target name from policy")] = "",
            ref: Annotated[str | None, Field(description="e.g. 'v1.5', 'branch:main', or commit SHA")] = None,
            release: Annotated[str | None, Field(description="Named historical release for rollback")] = None,
            plan_id: Annotated[str | None, Field(description="Plan ID from confirm=false preview")] = None,
            ctx: Context | None = None,
        ) -> dict[str, Any]:
            sess = session_id(ctx)
            if action == "targets":
                return await gitlab_deploy_mod.targets(app.gitlab)

            if not target:
                raise VpsMcpError(ErrorCategory.INVALID_INPUT, f"target is required for action='{action}'")

            if action == "deploy":
                if not ref:
                    raise VpsMcpError(ErrorCategory.INVALID_INPUT, "ref is required for action='deploy'")
                return await gitlab_deploy_mod.deploy(
                    app.gitlab, target=target, ref=ref, confirm=confirm, plan_id=plan_id, session_id=sess
                )
            if action == "rollback":
                return await gitlab_deploy_mod.rollback(
                    app.gitlab, target=target, confirm=confirm, release=release, session_id=sess
                )

            raise VpsMcpError(ErrorCategory.INVALID_INPUT, f"Unknown action: {action}")

    # =========================================================================
    # 13. Persistent Cross-Agent Memory
    # =========================================================================
    if app.memory is not None and app.memory.mem is not None:
        mem = app.memory.mem

        @mcp.tool(
            annotations=RW,
            description=(
                "Manage persistent cross-session agent memory, procedures, and operator knowledge.\n\n"
                "Actions:\n"
                "- 'search': Natural language semantic and keyword search across memories.\n"
                "- 'get': Retrieve a memory record by ID or subject key.\n"
                "- 'save': Store a new memory with category, title, text, importance, and tags.\n"
                "- 'update': Modify an existing memory's content, lifecycle, or tags.\n"
                "- 'archive': Soft-delete/retire a stale memory record."
            ),
        )
        @wrap
        async def memory_manage(
            action: Literal["search", "get", "save", "update", "archive"],
            query: Annotated[str, Field(description="Search question or task")] = "",
            id: Annotated[str | None, Field(description="Memory ID")] = None,
            subject: Annotated[str | None, Field(description="Dotted key prefix, e.g. 'postgres.version'")] = None,
            type: Annotated[Literal["episodic", "semantic", "procedural", "decision"] | None, Field(description="Memory type")] = None,
            title: Annotated[str | None, Field(description="Memory title")] = None,
            content: Annotated[str | None, Field(description="Memory text body")] = None,
            scope: Annotated[str | None, Field(description="Scope: 'project:X', 'environment:X', 'user'")] = None,
            importance: Annotated[int, Field(ge=1, le=5, description="Importance (1 to 5)")] = 3,
            tags: Annotated[list[str] | None, Field(description="Tags list")] = None,
            limit: Annotated[int, Field(ge=1, le=30, description="Search limit")] = 8,
            confirm: Annotated[bool, Field(description="Approval flag for archive")] = False,
            reason: Annotated[str | None, Field(description="Reason for update")] = None,
            ctx: Context | None = None,
        ) -> dict[str, Any]:
            if action == "search":
                return mem.search(query, types=[type] if type else None, subject=subject, limit=limit)
            if action == "get":
                return mem.get(id, subject=subject, scope=scope)
            if action == "save":
                if not type or not subject or not title or not content:
                    raise VpsMcpError(ErrorCategory.INVALID_INPUT, "type, subject, title, and content are required for save")
                return mem.save({
                    "type": type,
                    "subject": subject,
                    "title": title,
                    "content": content,
                    "scope": scope,
                    "importance": importance,
                    "tags": tags,
                    "source_ref": session_id(ctx),
                })
            if action == "update":
                if not id:
                    raise VpsMcpError(ErrorCategory.INVALID_INPUT, "id is required for update")
                kw: dict[str, Any] = {}
                if content is not None:
                    kw["content"] = content
                if title is not None:
                    kw["title"] = title
                if reason is not None:
                    kw["reason"] = reason
                if tags is not None:
                    kw["tags"] = tags
                return mem.update(id, "update", **kw)
            if action == "archive":
                if not id:
                    raise VpsMcpError(ErrorCategory.INVALID_INPUT, "id is required for archive")
                return mem.archive(id, confirm)

            raise VpsMcpError(ErrorCategory.INVALID_INPUT, f"Unknown action: {action}")
