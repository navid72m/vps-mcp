"""CI/CD: pipelines, jobs, logs and previewed actions (US4)."""

from __future__ import annotations

from typing import Any

from vps_mcp.errors import ErrorCategory, VpsMcpError
from vps_mcp.gitlab import render
from vps_mcp.gitlab.access import pid, trigger_allowed
from vps_mcp.gitlab.runtime import GitLabRuntime, Write, guarded, refuse

CI_ACTIONS = ("retry_job", "cancel_job", "retry_pipeline", "cancel_pipeline", "trigger_pipeline")


async def pipelines(
    rt: GitLabRuntime,
    project: str,
    ref: str | None = None,
    status: str | None = None,
    limit: int = 10,
    pipeline_id: int | None = None,
) -> dict[str, Any]:
    decl = rt.project(project)
    base = f"/projects/{pid(decl)}/pipelines"
    if pipeline_id is not None:
        p = await rt.client.get_json(f"{base}/{int(pipeline_id)}")
        jobs, cut = await rt.client.paged(f"{base}/{int(pipeline_id)}/jobs", None, 100)
        return {
            "pipeline": render.pipeline_line(p, _failed(jobs)),
            "jobs": [
                f"{j.get('id')} {j.get('name')} {j.get('stage')} {j.get('status')} "
                f"{render.duration(j.get('duration'))}"
                for j in jobs
            ],
            "truncated": cut,
        }
    if not 1 <= limit <= 50:
        raise VpsMcpError(ErrorCategory.INVALID_INPUT, "limit must be between 1 and 50")
    got, truncated = await rt.client.paged(base, {"ref": ref, "status": status}, limit)
    lines = []
    for p in got:
        failed: list[str] = []
        if p.get("status") == "failed":
            jobs, _ = await rt.client.paged(f"{base}/{p.get('id')}/jobs", {"scope[]": "failed"}, 20)
            failed = _failed(jobs)
        lines.append(render.pipeline_line(p, failed))
    return {"pipelines": lines, "truncated": truncated}


def _failed(jobs: list[dict[str, Any]]) -> list[str]:
    return [str(j.get("name")) for j in jobs if j.get("status") == "failed"]


async def job_log(
    rt: GitLabRuntime, project: str, job_id: int, lines: int = render.LOG_LINES_DEFAULT
) -> dict[str, Any]:
    decl = rt.project(project)
    if not 1 <= lines <= render.LOG_LINES_MAX:
        raise VpsMcpError(ErrorCategory.INVALID_INPUT, "lines must be between 1 and 2000")
    base = f"/projects/{pid(decl)}/jobs/{int(job_id)}"
    job = await rt.client.get_json(base)
    data = await rt.client.get_text(f"{base}/trace")
    head = {"job": f"{job.get('id')} {job.get('name')}", "status": job.get("status")}
    if render.is_binary(data[-1024 * 1024 :]):
        return {**head, **render.binary_summary(data)}
    all_lines = data[-4 * 1024 * 1024 :].decode("utf-8", errors="replace").splitlines()
    tail = all_lines[-lines:]
    return {
        **head,
        "lines": len(tail),
        "log": render.external(rt.mask, "\n".join(tail), max_bytes=512 * 1024),
        "truncated": len(all_lines) > lines,
    }


async def action(
    rt: GitLabRuntime,
    project: str,
    act: str,
    confirm: bool,
    session_id: str | None,
    **kw: Any,
) -> dict[str, Any]:
    try:
        return await _action(rt, project, act, confirm, session_id, **kw)
    except VpsMcpError as err:
        if "audit_id" in err.extra:
            raise
        params = {"project": str(project)[:40], "action": str(act)[:20]}
        raise refuse(
            rt, Write("gitlab_ci", str(project)[:40], "gitlab", params, confirm, session_id), err
        ) from None


async def _action(
    rt: GitLabRuntime,
    project: str,
    act: str,
    confirm: bool,
    session_id: str | None,
    *,
    job_id: int | None = None,
    pipeline_id: int | None = None,
    ref: str | None = None,
) -> dict[str, Any]:
    if act not in CI_ACTIONS:
        raise VpsMcpError(
            ErrorCategory.INVALID_INPUT, f"action must be one of {', '.join(CI_ACTIONS)}"
        )
    decl = rt.project(project, "ci")
    base = f"/projects/{pid(decl)}"
    if act.endswith("_job"):
        if job_id is None:
            raise VpsMcpError(ErrorCategory.INVALID_INPUT, "job_id is required")
        path, what = f"{base}/jobs/{int(job_id)}/{act.split('_')[0]}", f"job {job_id}"
        data: dict[str, Any] | None = None
    elif act == "trigger_pipeline":
        if not ref:
            raise VpsMcpError(ErrorCategory.INVALID_INPUT, "ref is required")
        if not trigger_allowed(decl, ref):
            raise VpsMcpError(
                ErrorCategory.INVALID_INPUT,
                f"ref '{ref}' does not match the project's trigger_refs",
            )
        name = ref.split(":", 1)[1] if ref.startswith(("tag:", "branch:")) else ref
        path, what, data = f"{base}/pipeline", f"new pipeline on {name}", {"ref": name}
    else:
        if pipeline_id is None:
            raise VpsMcpError(ErrorCategory.INVALID_INPUT, "pipeline_id is required")
        path = f"{base}/pipelines/{int(pipeline_id)}/{act.split('_')[0]}"
        what, data = f"pipeline {pipeline_id}", None
    params = {
        "project": decl.alias,
        "action": act,
        "job_id": job_id,
        "pipeline_id": pipeline_id,
        "ref": ref,
    }

    async def preview() -> dict[str, Any]:
        return {"request": {"action": act, "project": decl.alias, "target": what}}

    async def apply() -> dict[str, Any]:
        res = await rt.client.post_json(path, data)
        return {"result": {"id": res.get("id"), "status": res.get("status")}}

    return await guarded(
        rt, Write("gitlab_ci", decl.alias, "gitlab", params, confirm, session_id), preview, apply
    )
