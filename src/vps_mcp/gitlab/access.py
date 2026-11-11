"""Alias resolution and permission checks, before any network call (research G3, G4)."""

from __future__ import annotations

import fnmatch

from vps_mcp.errors import ErrorCategory, VpsMcpError
from vps_mcp.gitlab.client import project_id
from vps_mcp.gitlab.config import DeployTarget, GitLabSettings, ProjectDecl


def not_allowlisted() -> VpsMcpError:
    return VpsMcpError(
        ErrorCategory.NOT_PERMITTED,
        "that is not an allowlisted GitLab project (see gitlab_projects)",
    )


def resolve(settings: GitLabSettings, alias: object, need: str = "read") -> ProjectDecl:
    decl = settings.get_project(alias) if isinstance(alias, str) else None
    if decl is None:
        raise not_allowlisted()
    if not decl.allows(need):
        raise VpsMcpError(
            ErrorCategory.NOT_PERMITTED,
            f"project '{decl.alias}' does not allow '{need}' (declare it in [[gitlab.project]])",
        )
    return decl


def pid(decl: ProjectDecl) -> str:
    return project_id(decl.id if decl.id is not None else str(decl.path))


def split_ref(ref: str) -> tuple[str | None, str]:
    """``tag:v1`` -> ("tag", "v1"); ``branch:main`` -> ("branch", "main"); else (None, ref)."""
    for kind in ("tag", "branch"):
        if ref.startswith(kind + ":"):
            return kind, ref[len(kind) + 1 :]
    return None, ref


def ref_allowed(patterns: list[str], kind: str, name: str) -> bool:
    for pat in patterns:
        pkind, pglob = split_ref(pat)
        if pkind is not None and pkind != kind:
            continue
        if fnmatch.fnmatchcase(name, pglob):
            return True
    return False


def target_allows(target: DeployTarget, kind: str, name: str) -> bool:
    return ref_allowed(target.refs, kind, name)


def trigger_allowed(decl: ProjectDecl, ref: str) -> bool:
    kind, name = split_ref(ref)
    return any(
        ref_allowed(decl.trigger_refs, k, name) for k in ((kind,) if kind else ("branch", "tag"))
    )
