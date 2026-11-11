"""`vps-mcp gitlab-check`: token user, scopes, expiry, project reachability (FR-005)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from vps_mcp.errors import VpsMcpError
from vps_mcp.gitlab.access import pid
from vps_mcp.gitlab.runtime import GitLabRuntime

EXPIRY_WARN_DAYS = 14
WRITE_PERMISSIONS = ("write_issues", "write_mrs", "ci")


async def report(rt: GitLabRuntime, *, with_vps: bool = True) -> tuple[list[str], bool]:
    out = [f"instance: {rt.settings.url}"]
    ok = True
    try:
        rt.token.get()
    except VpsMcpError as err:
        return [*out, f"ERROR {err.message}"], False
    scopes: list[str] = []
    try:
        info: dict[str, Any] = await rt.client.get_json("/personal_access_tokens/self")
        scopes = list(info.get("scopes") or [])
        out.append(f"token: {info.get('name', '?')}, scopes: {', '.join(scopes) or 'none'}")
        exp = info.get("expires_at")
        if exp:
            days = (date.fromisoformat(str(exp)[:10]) - datetime.now(UTC).date()).days
            line = f"expires: {exp} ({days} days)"
            out.append(("WARNING " if days < EXPIRY_WARN_DAYS else "") + line)
        else:
            out.append("expires: never")
    except VpsMcpError as err:
        if err.subtype == "gitlab_auth":
            return [*out, f"ERROR {err.message}"], False
        out.append("token: details unavailable (older GitLab or limited token)")
    try:
        user = await rt.client.get_json("/user")
        out.append(f"user: {user.get('username')}")
    except VpsMcpError as err:
        out.append(f"ERROR user lookup: {err.message}")
        ok = False
    for p in rt.settings.project:
        try:
            proj = await rt.client.get_json(f"/projects/{pid(p)}")
            line = f"project {p.alias}: ok (id {proj.get('id')})"
            if p.path is not None:
                line += f" - consider pinning id = {proj.get('id')} in the config"
            out.append(line)
        except VpsMcpError as err:
            out.append(f"ERROR project {p.alias}: {err.message}")
            ok = False
            continue
        if scopes and "api" not in scopes and any(p.allows(x) for x in WRITE_PERMISSIONS):
            out.append(
                f"WARNING project {p.alias}: declares write/CI permissions but the token lacks "
                "the 'api' scope"
            )
    if with_vps and rt.settings.deploy:
        from vps_mcp.gitlab.deploy import current_state

        for t in rt.settings.deploy:
            try:
                cur = await current_state(rt, t)
            except VpsMcpError as err:
                out.append(f"deploy {t.name}: VPS not checked ({err.message})")
                continue
            if cur.get("managed"):
                out.append(f"deploy {t.name}: {t.destination} -> {cur.get('release')}")
            elif cur.get("unmanaged_dir"):
                out.append(f"deploy {t.name}: {t.destination} exists, not managed yet")
            else:
                out.append(f"deploy {t.name}: not deployed yet")
    return out, ok
