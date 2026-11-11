"""Issues and merge requests: list, read, and previewed writes (US3, research G7, G8)."""

from __future__ import annotations

from typing import Any

from vps_mcp.errors import ErrorCategory, VpsMcpError
from vps_mcp.gitlab import render
from vps_mcp.gitlab.access import pid
from vps_mcp.gitlab.client import enc
from vps_mcp.gitlab.runtime import GitLabRuntime, Write, guarded, refuse

FOOTER = "_Posted via vps-mcp_"
KINDS = {"issue": "issues", "mr": "merge_requests"}
STATES = ("opened", "closed", "merged", "all")
ACTIONS = {
    "create_issue": ("write_issues", "gitlab_issue"),
    "comment": (None, "gitlab_comment"),  # permission depends on kind
    "create_mr": ("write_mrs", "gitlab_mr"),
    "close_issue": ("write_issues", "gitlab_close"),
}


def _kind(kind: str) -> str:
    if kind not in KINDS:
        raise VpsMcpError(ErrorCategory.INVALID_INPUT, "kind must be 'issue' or 'mr'")
    return KINDS[kind]


def web_ref(alias: str, kind: str, iid: Any) -> str:
    return f"{alias}{'#' if kind == 'issue' else '!'}{iid}"


async def list_items(
    rt: GitLabRuntime,
    project: str,
    kind: str,
    state: str = "opened",
    labels: str | None = None,
    search: str | None = None,
    author: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    decl = rt.project(project)
    coll = _kind(kind)
    if state not in STATES:
        raise VpsMcpError(ErrorCategory.INVALID_INPUT, f"state must be one of {', '.join(STATES)}")
    if not 1 <= limit <= 50:
        raise VpsMcpError(ErrorCategory.INVALID_INPUT, "limit must be between 1 and 50")
    got, truncated = await rt.client.paged(
        f"/projects/{pid(decl)}/{coll}",
        {
            "state": None if state == "all" else state,
            "labels": labels,
            "search": search,
            "author_username": author,
            "order_by": "updated_at",
        },
        limit,
    )
    return {
        "kind": kind,
        "items": [rt.mask(render.item_line(i)) for i in got],
        "truncated": truncated,
    }


async def read_item(
    rt: GitLabRuntime, project: str, kind: str, iid: int, notes_limit: int = 20
) -> dict[str, Any]:
    decl = rt.project(project)
    coll = _kind(kind)
    if not 1 <= notes_limit <= render.NOTES_MAX:
        raise VpsMcpError(ErrorCategory.INVALID_INPUT, "notes_limit must be between 1 and 50")
    base = f"/projects/{pid(decl)}/{coll}/{int(iid)}"
    it = await rt.client.get_json(base)
    notes, cut = await rt.client.paged(
        f"{base}/notes", {"sort": "asc", "order_by": "created_at"}, notes_limit
    )
    out: dict[str, Any] = {
        "web_ref": web_ref(decl.alias, kind, it.get("iid")),
        "iid": it.get("iid"),
        "title": rt.mask(str(it.get("title", ""))),
        "state": it.get("state"),
        "author": (it.get("author") or {}).get("username"),
        "labels": it.get("labels") or [],
        "created_at": it.get("created_at"),
        "updated_at": it.get("updated_at"),
        "description": render.external(rt.mask, it.get("description")),
        "notes": [
            render.external(
                rt.mask,
                n.get("body"),
                author=(n.get("author") or {}).get("username"),
                created_at=n.get("created_at"),
                max_bytes=render.NOTE_MAX_BYTES,
            )
            for n in notes
            if not n.get("system")
        ],
        "notes_truncated": cut,
    }
    if kind == "mr":
        out.update(
            source=it.get("source_branch"),
            target=it.get("target_branch"),
            merge_status=it.get("detailed_merge_status") or it.get("merge_status"),
            pipeline_status=(it.get("head_pipeline") or it.get("pipeline") or {}).get("status"),
        )
    return out


def _with_footer(text: str) -> str:
    text = (text or "").rstrip()
    return f"{text}\n\n{FOOTER}" if text else FOOTER


async def write(
    rt: GitLabRuntime,
    project: str,
    action: str,
    confirm: bool,
    session_id: str | None,
    **kw: Any,
) -> dict[str, Any]:
    try:
        return await _write(rt, project, action, confirm, session_id, **kw)
    except VpsMcpError as err:
        if "audit_id" in err.extra:
            raise
        audit_action = ACTIONS.get(action, (None, "gitlab_issue"))[1]
        params = {"project": str(project)[:40], "action": str(action)[:20]}
        raise refuse(
            rt, Write(audit_action, str(project)[:40], "gitlab", params, confirm, session_id), err
        ) from None


async def _write(
    rt: GitLabRuntime,
    project: str,
    action: str,
    confirm: bool,
    session_id: str | None,
    *,
    kind: str | None = None,
    iid: int | None = None,
    title: str | None = None,
    description: str | None = None,
    body: str | None = None,
    labels: list[str] | None = None,
    source_branch: str | None = None,
    target_branch: str | None = None,
) -> dict[str, Any]:
    if action not in ACTIONS:
        raise VpsMcpError(
            ErrorCategory.INVALID_INPUT,
            f"action must be one of {', '.join(ACTIONS)} (merging is not available)",
        )
    need, audit_action = ACTIONS[action]
    coll = ""
    if action == "comment":
        coll = _kind(kind or "")
        need = "write_issues" if kind == "issue" else "write_mrs"
    decl = rt.project(project, need or "read")
    base = f"/projects/{pid(decl)}"
    mask = rt.mask

    def req_text(name: str, value: str | None, limit: int = 255) -> str:
        if not value or not value.strip():
            raise VpsMcpError(ErrorCategory.INVALID_INPUT, f"{name} is required")
        if len(value) > limit:
            raise VpsMcpError(ErrorCategory.INVALID_INPUT, f"{name} is too long (max {limit})")
        return mask(value.strip())

    def label_list() -> str | None:
        if not labels:
            return None
        if len(labels) > 10 or any(not isinstance(x, str) or len(x) > 50 for x in labels):
            raise VpsMcpError(ErrorCategory.INVALID_INPUT, "at most 10 labels of 50 chars")
        return ",".join(labels)

    method, path, fields, ref = "POST", "", {}, ""
    if action == "create_issue":
        fields = {
            "title": req_text("title", title),
            "description": _with_footer(mask(description or "")),
            "labels": label_list(),
        }
        path = f"{base}/issues"
    elif action == "comment":
        if iid is None:
            raise VpsMcpError(ErrorCategory.INVALID_INPUT, "iid is required")
        fields = {"body": _with_footer(req_text("body", body, 20000))}
        path = f"{base}/{coll}/{int(iid)}/notes"
        ref = web_ref(decl.alias, str(kind), iid)
    elif action == "create_mr":
        fields = {
            "source_branch": req_text("source_branch", source_branch),
            "target_branch": req_text("target_branch", target_branch),
            "title": req_text("title", title),
            "description": _with_footer(mask(description or "")),
            "labels": label_list(),
        }
        path = f"{base}/merge_requests"
    else:  # close_issue
        if not decl.close_issues:
            raise VpsMcpError(
                ErrorCategory.NOT_PERMITTED,
                f"project '{decl.alias}' does not allow closing issues (close_issues)",
            )
        if iid is None:
            raise VpsMcpError(ErrorCategory.INVALID_INPUT, "iid is required")
        method, path, fields = "PUT", f"{base}/issues/{int(iid)}", {"state_event": "close"}
        ref = web_ref(decl.alias, "issue", iid)

    async def checks() -> dict[str, Any]:
        extra: dict[str, Any] = {}
        if action == "create_mr":
            for b in (fields["source_branch"], fields["target_branch"]):
                await rt.client.get_json(f"{base}/repository/branches/{enc(str(b))}")
        if action == "close_issue":
            it = await rt.client.get_json(path)
            extra["title"] = mask(str(it.get("title", "")))
            if not decl.close_any_issue and FOOTER not in str(it.get("description") or ""):
                raise VpsMcpError(
                    ErrorCategory.NOT_PERMITTED,
                    "only issues created through vps-mcp can be closed (close_any_issue = false)",
                )
        return extra

    shown = {k: v for k, v in fields.items() if v is not None}
    params = {"project": decl.alias, "action": action, "web_ref": ref or None}

    async def preview() -> dict[str, Any]:
        extra = await checks()
        return {"request": {"action": action, "project": decl.alias, "fields": shown, **extra}}

    async def apply() -> dict[str, Any]:
        await checks()
        if method == "PUT":
            res = await rt.client.put_json(path, shown)
        else:
            res = await rt.client.post_json(path, shown)
        if action in ("create_issue", "create_mr"):
            k = "issue" if action == "create_issue" else "mr"
            wr = web_ref(decl.alias, k, res.get("iid"))
            params["web_ref"] = wr
            return {"web_ref": wr, "iid": res.get("iid")}
        return {"web_ref": ref, "note_id": res.get("id")}

    return await guarded(
        rt, Write(audit_action, decl.alias, "gitlab", params, confirm, session_id), preview, apply
    )
