"""Read code and history (US1, contracts/gitlab-tools.md §Read)."""

from __future__ import annotations

import posixpath
from typing import Any

from vps_mcp.errors import ErrorCategory, VpsMcpError
from vps_mcp.gitlab import render
from vps_mcp.gitlab.access import pid
from vps_mcp.gitlab.client import enc
from vps_mcp.gitlab.runtime import GitLabRuntime


def repo_path(path: str | None) -> str | None:
    if path is None or path in ("", "/", "."):
        return None
    norm = posixpath.normpath(path.strip("/"))
    if norm.startswith("..") or "\x00" in norm or len(norm) > 500:
        raise VpsMcpError(ErrorCategory.INVALID_INPUT, "path must be relative to the repository")
    return norm


def _limit(n: int, cap: int) -> int:
    if not 1 <= n <= cap:
        raise VpsMcpError(ErrorCategory.INVALID_INPUT, f"limit must be between 1 and {cap}")
    return n


async def projects(rt: GitLabRuntime) -> dict[str, Any]:
    out = []
    for p in rt.settings.project:
        out.append(
            {
                "alias": p.alias,
                "project": p.path or f"id {p.id}",
                "permissions": p.permissions,
                "deploy_targets": [d.name for d in rt.settings.deploy if d.project == p.alias],
            }
        )
    return {"projects": out}


async def refs(
    rt: GitLabRuntime, project: str, kind: str = "both", search: str | None = None, limit: int = 50
) -> dict[str, Any]:
    decl = rt.project(project)
    _limit(limit, render.LIST_MAX)
    kinds = ("branches", "tags") if kind == "both" else (kind,)
    if any(k not in ("branches", "tags") for k in kinds):
        raise VpsMcpError(ErrorCategory.INVALID_INPUT, "kind must be branches, tags or both")
    items: list[dict[str, Any]] = []
    truncated = False
    for k in kinds:
        got, cut = await rt.client.paged(
            f"/projects/{pid(decl)}/repository/{k}", {"search": search}, limit
        )
        truncated |= cut
        for r in got:
            c = r.get("commit") or {}
            items.append(
                {
                    "name": r.get("name"),
                    "kind": "branch" if k == "branches" else "tag",
                    "sha12": str(c.get("id", ""))[:12],
                    "date": render.day(c.get("committed_date")),
                    "title": str(c.get("title", ""))[:120],
                }
            )
    return {"refs": items[:limit], "truncated": truncated or len(items) > limit}


async def tree(
    rt: GitLabRuntime,
    project: str,
    ref: str | None = None,
    path: str | None = None,
    recursive: bool = False,
    limit: int = render.LIST_MAX,
) -> dict[str, Any]:
    decl = rt.project(project)
    _limit(limit, render.LIST_MAX)
    ref = ref or await rt.default_branch(decl)
    got, truncated = await rt.client.paged(
        f"/projects/{pid(decl)}/repository/tree",
        {"ref": ref, "path": repo_path(path), "recursive": str(recursive).lower()},
        limit,
    )
    return {
        "ref": ref,
        "entries": [{"path": e.get("path"), "type": e.get("type")} for e in got],
        "truncated": truncated,
    }


async def file(
    rt: GitLabRuntime,
    project: str,
    path: str,
    ref: str | None = None,
    start_line: int = 1,
    max_lines: int | None = None,
) -> dict[str, Any]:
    decl = rt.project(project)
    rel = repo_path(path)
    if rel is None:
        raise VpsMcpError(ErrorCategory.INVALID_INPUT, "path is required")
    ref = ref or await rt.default_branch(decl)
    data = await rt.client.get_text(
        f"/projects/{pid(decl)}/repository/files/{enc(rel)}/raw", {"ref": ref}
    )
    if render.is_binary(data):
        return {"path": rel, "ref": ref, **render.binary_summary(data)}
    text = data.decode("utf-8")
    lines = text.splitlines()
    total = len(lines)
    if max_lines is not None and not 1 <= max_lines <= render.LOG_LINES_MAX:
        raise VpsMcpError(ErrorCategory.INVALID_INPUT, "max_lines must be between 1 and 2000")
    first = max(1, start_line)
    window = lines[first - 1 : (first - 1 + max_lines) if max_lines else None]
    content, cut = render.cap_text("\n".join(window), render.FILE_MAX_BYTES)
    shown_end = first - 1 + len(content.splitlines())
    return {
        "path": rel,
        "ref": ref,
        "size": len(data),
        "lines": total,
        "range": [first, shown_end],
        "content": render.external(rt.mask, content, max_bytes=render.FILE_MAX_BYTES),
        "truncated": cut or shown_end < total,
    }


async def commits(
    rt: GitLabRuntime,
    project: str,
    ref: str | None = None,
    path: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 20,
    sha: str | None = None,
) -> dict[str, Any]:
    decl = rt.project(project)
    base = f"/projects/{pid(decl)}/repository/commits"
    if sha:
        c = await rt.client.get_json(f"{base}/{enc(sha)}")
        diffs, cut = await rt.client.paged(
            f"{base}/{enc(sha)}/diff", None, render.COMPARE_FILES_MAX
        )
        return {
            "sha": c.get("id"),
            "author": c.get("author_name"),
            "date": c.get("committed_date"),
            "message": render.external(rt.mask, c.get("message")),
            "stats": c.get("stats"),
            "files": [_file_change(d) for d in diffs],
            "truncated": cut,
        }
    _limit(limit, render.COMMITS_MAX)
    got, truncated = await rt.client.paged(
        base,
        {"ref_name": ref, "path": repo_path(path), "since": since, "until": until},
        limit,
    )
    return {"commits": [render.commit_line(c) for c in got], "truncated": truncated}


def _file_change(d: dict[str, Any]) -> str:
    flag = (
        "A"
        if d.get("new_file")
        else "D"
        if d.get("deleted_file")
        else "R"
        if d.get("renamed_file")
        else "M"
    )
    diff = str(d.get("diff") or "")
    plus = sum(1 for ln in diff.splitlines() if ln.startswith("+") and not ln.startswith("+++"))
    minus = sum(1 for ln in diff.splitlines() if ln.startswith("-") and not ln.startswith("---"))
    path = d.get("new_path") or d.get("old_path")
    return f"{flag} {path} +{plus} -{minus}"


async def compare(
    rt: GitLabRuntime, project: str, from_ref: str, to_ref: str, path: str | None = None
) -> dict[str, Any]:
    decl = rt.project(project)
    data = await rt.client.get_json(
        f"/projects/{pid(decl)}/repository/compare",
        {"from": from_ref, "to": to_ref, "straight": "false"},
    )
    diffs: list[dict[str, Any]] = data.get("diffs") or []
    rel = repo_path(path)
    if rel is not None:
        match = next((d for d in diffs if rel in (d.get("new_path"), d.get("old_path"))), None)
        if match is None:
            raise VpsMcpError(ErrorCategory.NOT_FOUND, f"'{rel}' did not change between the refs")
        lines = str(match.get("diff") or "").splitlines()
        cut = len(lines) > render.DIFF_LINES_MAX
        return {
            "path": rel,
            "diff": render.external(rt.mask, "\n".join(lines[: render.DIFF_LINES_MAX])),
            "truncated": cut,
        }
    cs = data.get("commits") or []
    return {
        "commits": [render.commit_line(c) for c in cs[: render.COMMITS_MAX]],
        "files": [_file_change(d) for d in diffs[: render.COMPARE_FILES_MAX]],
        "total_files": len(diffs),
        "truncated": len(cs) > render.COMMITS_MAX or len(diffs) > render.COMPARE_FILES_MAX,
    }
