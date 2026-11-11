"""Deploy a GitLab commit to a declared VPS target (US2, research G9-G13).

Layout on the VPS::

    /var/lib/vps-mcp/deploys/<target>/releases/<UTC ts>-<sha12>/   (files + MANIFEST + marker)
    <destination>  ->  symlink to the active release

Every remote command is a fixed argv built from validated paths, run with sudo. The token
never leaves the laptop: the laptop downloads the archive and streams a clean tar over SSH.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
import secrets
from datetime import UTC, datetime
from typing import Any

from vps_mcp.errors import ErrorCategory, VpsMcpError
from vps_mcp.gitlab import archive
from vps_mcp.gitlab.access import pid, split_ref, target_allows
from vps_mcp.gitlab.client import enc
from vps_mcp.gitlab.config import DeployTarget
from vps_mcp.gitlab.read import _file_change
from vps_mcp.gitlab.runtime import GitLabRuntime, Write, guarded, refuse
from vps_mcp.gitlab.store import DeployStore
from vps_mcp.models.views import ServiceState

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
RELEASE_RE = re.compile(r"^(?:\d{8}T\d{6}-[0-9a-f]{12}|pre-vps-mcp-\d{8}T\d{6})$")
NEW_SUFFIX = ".vps-mcp-new"
HEADROOM_BYTES = 100 * 1024 * 1024
DEPLOY_TIMEOUT_S = 900.0


def _err(cat: ErrorCategory, msg: str, **extra: Any) -> VpsMcpError:
    return VpsMcpError(cat, msg, extra=extra or None)


def _target(rt: GitLabRuntime, name: object) -> DeployTarget:
    t = rt.settings.get_target(name) if isinstance(name, str) else None
    if t is None:
        raise _err(ErrorCategory.NOT_PERMITTED, "that is not a declared deploy target")
    rt.project(t.project, "deploy")
    return t


def _under(base: str, path: str) -> bool:
    norm = posixpath.normpath(path)
    return norm.startswith(base.rstrip("/") + "/") and norm == path


# ---------------------------------------------------------------- VPS primitives


async def _sh(rt: GitLabRuntime, argv: list[str], *, timeout: float = 60.0, **kw: Any) -> Any:
    return await rt.app.executor.run(argv, sudo=True, timeout=timeout, **kw)


async def _must(rt: GitLabRuntime, argv: list[str], what: str, **kw: Any) -> str:
    res = await _sh(rt, argv, **kw)
    if not res.ok:
        detail = rt.mask((res.stderr or res.stdout).strip())[:300]
        raise _err(ErrorCategory.FAILED, f"{what} failed on the VPS: {detail}")
    return str(res.stdout)


async def _kind_of(rt: GitLabRuntime, path: str) -> str | None:
    """'l' symlink, 'd' directory, 'f' file, None missing."""
    res = await _sh(rt, ["find", path, "-maxdepth", "0", "-printf", "%y"])
    return res.stdout.strip() or None if res.ok else None


async def _readlink(rt: GitLabRuntime, path: str) -> str | None:
    res = await _sh(rt, ["readlink", path])
    return res.stdout.strip() or None if res.ok else None


async def _marker(rt: GitLabRuntime, release: str) -> dict[str, Any] | None:
    res = await _sh(rt, ["cat", f"{release}/{archive.MARKER}"])
    if not res.ok:
        return None
    try:
        data = json.loads(res.stdout)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


async def _releases(rt: GitLabRuntime, t: DeployTarget) -> list[str]:
    res = await _sh(
        rt,
        [
            "find",
            t.releases_dir,
            "-mindepth",
            "1",
            "-maxdepth",
            "1",
            "-type",
            "d",
            "-printf",
            "%f\\n",
        ],
    )
    if not res.ok:
        return []
    return sorted(n for n in res.stdout.split() if RELEASE_RE.fullmatch(n))


async def _local_changes(rt: GitLabRuntime, release: str) -> list[str]:
    res = await _sh(rt, ["sha256sum", "-c", "--quiet", f"{release}/{archive.MANIFEST}"])
    if res.ok:
        return []
    out = []
    for line in (res.stdout or "").splitlines():
        path, _, status = line.rpartition(": ")
        if status and path.startswith(release + "/"):
            out.append(path[len(release) + 1 :])
    return out[:50] or ["(manifest check failed)"]


async def _free_bytes(rt: GitLabRuntime) -> int:
    out = await _must(rt, ["df", "-P", "-B1", "/var/lib"], "disk check")
    lines = out.strip().splitlines()
    try:
        return int(lines[-1].split()[3])
    except (IndexError, ValueError):
        raise _err(ErrorCategory.FAILED, "could not read free disk space") from None


async def current_state(rt: GitLabRuntime, t: DeployTarget) -> dict[str, Any]:
    kind = await _kind_of(rt, t.destination)
    state: dict[str, Any] = {"exists": kind is not None, "managed": False}
    if kind == "l":
        link = await _readlink(rt, t.destination)
        if link and _under(t.releases_dir, link):
            m = await _marker(rt, link) or {}
            state.update(
                managed=True,
                release=posixpath.basename(link),
                release_dir=link,
                sha=m.get("sha"),
                ref=m.get("ref"),
                deployed_at=m.get("deployed_at"),
            )
        else:
            state["link"] = link
    elif kind == "d":
        state["unmanaged_dir"] = True
    elif kind is not None:
        state["unexpected"] = kind
    return state


# --------------------------------------------------------------------- planning


async def _resolve(rt: GitLabRuntime, t: DeployTarget, ref: str) -> tuple[str, str]:
    """Return ``(ref_label, full_sha)`` or refuse when the ref is not allowed."""
    decl = rt.project(t.project, "deploy")
    base = f"/projects/{pid(decl)}/repository"
    ref = (ref or "").strip()
    if SHA_RE.fullmatch(ref):
        refs = await rt.client.get_json(f"{base}/commits/{ref}/refs", {"type": "all"})
        for r in refs or []:
            if target_allows(t, str(r.get("type")), str(r.get("name"))):
                return f"{r.get('type')}:{r.get('name')}@{ref[:12]}", ref
        raise _err(ErrorCategory.INVALID_INPUT, "that commit is not on any allowed ref")
    kind, name = split_ref(ref)
    if not name or len(name) > 200:
        raise _err(ErrorCategory.INVALID_INPUT, "give a branch, a tag or a full commit SHA")
    for k in (kind,) if kind else ("tag", "branch"):
        coll = "tags" if k == "tag" else "branches"
        try:
            info = await rt.client.get_json(f"{base}/{coll}/{enc(name)}")
        except VpsMcpError as err:
            if err.category is ErrorCategory.NOT_FOUND:
                continue
            raise
        if not target_allows(t, str(k), name):
            raise _err(
                ErrorCategory.INVALID_INPUT,
                f"{k} '{name}' is not allowed for target '{t.name}' (refs: {', '.join(t.refs)})",
            )
        return f"{k}:{name}", str((info.get("commit") or {}).get("id"))
    raise _err(ErrorCategory.NOT_FOUND, f"no branch or tag named '{name}'")


async def _changes(
    rt: GitLabRuntime, t: DeployTarget, current_sha: str | None, sha: str
) -> dict[str, Any]:
    decl = rt.project(t.project, "deploy")
    if current_sha:
        data = await rt.client.get_json(
            f"/projects/{pid(decl)}/repository/compare", {"from": current_sha, "to": sha}
        )
        lines = [
            _file_change(d)
            for d in data.get("diffs") or []
            if archive.selected(str(d.get("new_path") or d.get("old_path")), t.include, t.exclude)
        ]
    else:
        entries, cut = await rt.client.paged(
            f"/projects/{pid(decl)}/repository/tree",
            {"ref": sha, "recursive": "true"},
            5000,
        )
        lines = [
            f"A {e.get('path')}"
            for e in entries
            if e.get("type") == "blob"
            and archive.selected(str(e.get("path")), t.include, t.exclude)
        ]
    counts = {"added": 0, "modified": 0, "removed": 0, "renamed": 0}
    for ln in lines:
        counts[{"A": "added", "M": "modified", "D": "removed", "R": "renamed"}[ln[0]]] += 1
    return {**counts, "files": lines[:50], "truncated": len(lines) > 50}


async def _pipeline(rt: GitLabRuntime, t: DeployTarget, sha: str) -> dict[str, Any] | None:
    decl = rt.project(t.project, "deploy")
    got, _ = await rt.client.paged(f"/projects/{pid(decl)}/pipelines", {"sha": sha}, 1)
    if not got:
        return None
    return {"id": got[0].get("id"), "status": got[0].get("status")}


def _plan_hash(plan: dict[str, Any]) -> str:
    facts = {k: plan.get(k) for k in ("target", "commit", "current", "changes", "services")}
    facts["commit"] = (plan.get("commit") or {}).get("sha")
    return hashlib.sha256(json.dumps(facts, sort_keys=True, default=str).encode()).hexdigest()[:32]


async def build_plan(rt: GitLabRuntime, t: DeployTarget, ref: str) -> dict[str, Any]:
    decl = rt.project(t.project, "deploy")
    label, sha = await _resolve(rt, t, ref)
    c = await rt.client.get_json(f"/projects/{pid(decl)}/repository/commits/{sha}")
    current = await current_state(rt, t)
    if current.get("managed"):
        current["local_changes"] = await _local_changes(rt, current["release_dir"])
    changes = await _changes(rt, t, current.get("sha"), sha)
    pipeline = await _pipeline(rt, t, sha)
    warnings = []
    if current.get("unmanaged_dir"):
        warnings.append(
            f"{t.destination} is an existing directory not managed by vps-mcp: it will be moved "
            f"to {t.releases_dir}/pre-vps-mcp-<time> (kept, not deleted)"
        )
    if current.get("link") or current.get("unexpected"):
        warnings.append(f"{t.destination} exists but is not a vps-mcp release; it will be replaced")
    if current.get("local_changes"):
        warnings.append(
            f"{len(current['local_changes'])} file(s) were changed on the VPS since the last "
            "deploy; those edits stay in the old release and are not carried over"
        )
    if pipeline is None or pipeline["status"] != "success":
        status = pipeline["status"] if pipeline else "no pipeline"
        warnings.append(
            f"latest pipeline for this commit: {status}"
            + (
                " (deploy will be refused: require_pipeline_success)"
                if t.require_pipeline_success
                else ""
            )
        )
    if current.get("sha") == sha:
        warnings.append("this commit is already deployed; confirming redeploys it")
    plan: dict[str, Any] = {
        "target": t.name,
        "project": t.project,
        "destination": t.destination,
        "ref": label,
        "commit": {
            "sha": sha,
            "short": sha[:12],
            "author": c.get("author_name"),
            "date": c.get("committed_date"),
            "title": rt.mask(str(c.get("title", "")))[:200],
        },
        "current": current,
        "changes": changes,
        "pipeline": pipeline,
        "unmanaged_existing_dir": bool(current.get("unmanaged_dir")),
        "services": t.services,
        "disk": {"free_bytes": await _free_bytes(rt)},
        "warnings": warnings,
    }
    plan["plan_id"] = _plan_hash(plan)
    return plan


# ------------------------------------------------------------------- switching


async def _switch(rt: GitLabRuntime, t: DeployTarget, release_dir: str) -> None:
    tmp = t.destination + NEW_SUFFIX
    await _sh(rt, ["rm", "-f", "--", tmp])
    await _must(rt, ["ln", "-s", release_dir, tmp], "creating the release link")
    await _must(rt, ["mv", "-T", tmp, t.destination], "switching the release")


async def _restart_and_check(rt: GitLabRuntime, t: DeployTarget) -> tuple[bool, str, list[str]]:
    app = rt.app
    for name in t.services:
        decl = next(s for s in app.config.service if s.name == name)
        try:
            st = await app.backend(decl.kind).restart(decl)
        except VpsMcpError as err:
            return False, f"{name}: {err.message}", await _logs(rt, decl)
        if st.state is not ServiceState.RUNNING:
            return False, f"{name} is {st.state.value} after restart", await _logs(rt, decl)
    if t.health_url:
        res = await _sh(
            rt, ["curl", "-fsS", "-o", "/dev/null", "--max-time", "10", t.health_url], timeout=20
        )
        if not res.ok:
            return False, f"health check {t.health_url} failed", []
    return True, "", []


async def _logs(rt: GitLabRuntime, decl: Any) -> list[str]:
    try:
        entries = await rt.app.backend(decl.kind).logs(decl, 20, None, None)
        return [rt.mask(str(getattr(e, "message", e))) for e in entries][-20:]
    except Exception:  # noqa: BLE001 - best effort
        return []


async def _prune(rt: GitLabRuntime, t: DeployTarget, active: str) -> list[str]:
    names = await _releases(rt, t)
    candidates = [
        n for n in names if not n.startswith("pre-vps-mcp-") and f"{t.releases_dir}/{n}" != active
    ]
    doomed = candidates[: max(0, len(candidates) - t.keep_releases)]
    removed = []
    for n in doomed:
        path = f"{t.releases_dir}/{n}"
        if _under(t.releases_dir, path) and RELEASE_RE.fullmatch(n):
            await _sh(rt, ["rm", "-rf", "--", path], timeout=300)
            removed.append(n)
    return removed


def _busy(rt: GitLabRuntime, t: DeployTarget, store: DeployStore) -> str | None:
    if store.running(t.name) or rt.lock(t.name).locked():
        return f"another deploy of '{t.name}' is running"
    app = rt.app
    mig = app.config.migration
    if mig is not None and t.services:
        rows = app.db.execute("SELECT job FROM job_runs WHERE status = 'running'").fetchall()
        for row in rows:
            job = mig.get_job(row["job"])
            if job and set(job.services) & set(t.services):
                return f"declared job '{job.name}' is running and uses the target's services"
        if app.db.execute(
            "SELECT 1 FROM migration_rehearsals WHERE status = 'running' LIMIT 1"
        ).fetchone():
            return "a rehearsal is running"
    return None


# -------------------------------------------------------------------- use cases


async def targets(rt: GitLabRuntime) -> dict[str, Any]:
    out = []
    for t in rt.settings.deploy:
        try:
            cur = await current_state(rt, t)
            releases = await _releases(rt, t)
        except VpsMcpError as err:
            cur, releases = {"error": err.message}, []
        out.append(
            {
                "name": t.name,
                "project": t.project,
                "refs": t.refs,
                "destination": t.destination,
                "services": t.services,
                "current": (
                    {k: cur.get(k) for k in ("release", "sha", "ref", "deployed_at")}
                    if cur.get("managed")
                    else ({"unmanaged_dir": True} if cur.get("unmanaged_dir") else None)
                ),
                "releases": releases,
            }
        )
    return {"targets": out}


async def deploy(
    rt: GitLabRuntime,
    target: str,
    ref: str,
    confirm: bool,
    plan_id: str | None,
    session_id: str | None,
) -> dict[str, Any]:
    try:
        t = _target(rt, target)
    except VpsMcpError as err:
        req = Write(
            "deploy", str(target)[:40], "gitlab", {"target": str(target)[:40]}, confirm, session_id
        )
        raise refuse(rt, req, err) from None
    store = DeployStore(rt.app.db)
    params = {"target": t.name, "ref": ref, "plan_id": plan_id}

    async def preview() -> dict[str, Any]:
        plan = await build_plan(rt, t, ref)
        store.add_plan(f"dep_{secrets.token_hex(6)}", t.name, "deploy", plan["plan_id"], plan)
        return {
            "plan": plan,
            "message": "Review the plan, then call deploy again with confirm=true and this "
            "plan_id.",
        }

    async def apply() -> dict[str, Any]:
        if not plan_id:
            raise _err(ErrorCategory.INVALID_INPUT, "plan_id is required with confirm=true")
        row = store.by_plan(t.name, plan_id)
        if row is None:
            fresh = await preview()
            raise _err(
                ErrorCategory.CONFLICT,
                "plan_id is unknown, used or older than 30 minutes; review the fresh plan",
                plan=fresh["plan"],
            )
        why = _busy(rt, t, store)
        if why:
            raise _err(ErrorCategory.CONFLICT, why)
        planned = row["plan"]
        sha = planned["commit"]["sha"]
        async with rt.lock(t.name):
            store.update(row["deploy_id"], status="running")
            try:
                return await _execute(rt, t, store, row["deploy_id"], planned, sha, params)
            except BaseException as exc:
                status = "failed"
                if isinstance(exc, VpsMcpError) and exc.extra.get("rolled_back"):
                    status = "rolled_back"
                store.update(row["deploy_id"], status=status, error=str(exc)[:300])
                raise

    return await guarded(
        rt,
        Write("deploy", t.name, "gitlab", params, confirm, session_id, timeout=DEPLOY_TIMEOUT_S),
        preview,
        apply,
    )


async def _execute(
    rt: GitLabRuntime,
    t: DeployTarget,
    store: DeployStore,
    deploy_id: str,
    planned: dict[str, Any],
    sha: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    fresh = await build_plan(rt, t, sha)
    if fresh["plan_id"] != planned["plan_id"]:
        store.add_plan(f"dep_{secrets.token_hex(6)}", t.name, "deploy", fresh["plan_id"], fresh)
        raise _err(
            ErrorCategory.CONFLICT,
            "the situation changed since the plan (deployed release, local edits or file "
            "changes); review the fresh plan",
            plan=fresh,
        )
    pipeline = fresh.get("pipeline") or {}
    if t.require_pipeline_success and pipeline.get("status") != "success":
        raise _err(
            ErrorCategory.FAILED,
            f"the commit's pipeline is '{pipeline.get('status', 'missing')}' and the target "
            "requires a successful pipeline",
        )
    decl = rt.project(t.project, "deploy")
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    release = f"{t.releases_dir}/{ts}-{sha[:12]}"
    marker = {
        "target": t.name,
        "sha": sha,
        "ref": planned["ref"],
        "deployed_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "deploy_id": deploy_id,
    }
    cap = rt.settings.max_archive_mb * 1024 * 1024
    src = await archive.download(rt.client, pid(decl), sha, cap)
    try:
        clean = archive.sanitize(
            src,
            include=t.include,
            exclude=t.exclude,
            cap_bytes=cap,
            release_dir=release,
            marker=marker,
        )
    finally:
        archive.cleanup(src)
    if not clean.files:
        raise _err(ErrorCategory.INVALID_INPUT, "nothing to deploy after include/exclude")
    free = await _free_bytes(rt)
    if free < clean.total_bytes * 2 + HEADROOM_BYTES:
        raise _err(
            ErrorCategory.INSUFFICIENT_DISK,
            f"not enough free disk on the VPS ({free >> 20} MB free)",
        )
    current = fresh["current"]
    previous = current.get("release_dir")

    # 1. extract the new release next to the old ones (nothing user-visible changes yet)
    await _must(rt, ["mkdir", "-p", "-m", "0755", release], "creating the release directory")
    await _must(
        rt,
        ["tar", "-x", "-f", "-", "-C", release, "--no-same-owner"],
        "extracting the release",
        stdin_bytes=clean.tar_bytes,
        timeout=600,
    )
    await _must(rt, ["chown", "-R", f"{t.owner}:{t.group}", release], "setting ownership")
    await _must(rt, ["chmod", "-R", "u+rwX,go+rX,go-w", release], "setting permissions")

    # 2. make room for the symlink
    if current.get("unmanaged_dir") or current.get("link") or current.get("unexpected"):
        aside = f"{t.releases_dir}/pre-vps-mcp-{ts}"
        await _must(rt, ["mv", "-T", t.destination, aside], "moving the existing directory aside")
        previous = aside if current.get("unmanaged_dir") else None
    elif not current.get("exists"):
        parent = posixpath.dirname(t.destination)
        await _must(rt, ["mkdir", "-p", parent], "creating the destination parent")

    # 3. atomic switch, restart, verify
    await _switch(rt, t, release)
    healthy, reason, logs = await _restart_and_check(rt, t)
    store.update(
        deploy_id,
        commit_sha=sha,
        release_dir=release,
        previous_dir=previous,
        pipeline_status=pipeline.get("status"),
    )
    if not healthy:
        if previous:
            await _switch(rt, t, previous)
        else:
            await _sh(rt, ["rm", "-f", "--", t.destination])
        await _restart_and_check(rt, t)
        raise _err(
            ErrorCategory.FAILED,
            f"deploy rolled back: {reason}",
            rolled_back=True,
            outcome="rolled_back",
            restored=posixpath.basename(previous) if previous else None,
            logs=logs,
        )
    pruned = await _prune(rt, t, release)
    store.update(deploy_id, status="succeeded")
    params["commit"] = sha[:12]
    return {
        "deploy_id": deploy_id,
        "status": "succeeded",
        "commit": {"sha": sha, "ref": planned["ref"], "title": planned["commit"]["title"]},
        "release": posixpath.basename(release),
        "files": len(clean.files),
        "skipped": [f"{p} ({why})" for p, why in clean.skipped][:20],
        "services": t.services,
        "pruned": pruned,
    }


async def rollback(
    rt: GitLabRuntime,
    target: str,
    release: str | None,
    confirm: bool,
    session_id: str | None,
) -> dict[str, Any]:
    req = Write(
        "rollback_deploy",
        str(target)[:40],
        "gitlab",
        {"target": str(target)[:40]},
        confirm,
        session_id,
    )
    try:
        t = _target(rt, target)
        if release is not None and not RELEASE_RE.fullmatch(release):
            raise _err(ErrorCategory.INVALID_INPUT, "release must be a name from deploy_targets")
    except VpsMcpError as err:
        raise refuse(rt, req, err) from None
    store = DeployStore(rt.app.db)
    params = {"target": t.name, "release": release}

    async def choose() -> tuple[dict[str, Any], str]:
        cur = await current_state(rt, t)
        if not cur.get("managed"):
            raise _err(ErrorCategory.CONFLICT, f"'{t.name}' has no active vps-mcp release")
        names = await _releases(rt, t)
        active = cur["release"]
        if release is not None:
            if release not in names or release == active:
                raise _err(ErrorCategory.NOT_FOUND, f"no other release named '{release}'")
            return cur, release
        older = [n for n in names if n < active and n != active]
        if not older:
            raise _err(ErrorCategory.CONFLICT, "there is no previous release to roll back to")
        return cur, older[-1]

    async def preview() -> dict[str, Any]:
        cur, to = await choose()
        return {"from": cur["release"], "to": to, "services": t.services}

    async def apply() -> dict[str, Any]:
        why = _busy(rt, t, store)
        if why:
            raise _err(ErrorCategory.CONFLICT, why)
        async with rt.lock(t.name):
            cur, to = await choose()
            to_dir = f"{t.releases_dir}/{to}"
            await _switch(rt, t, to_dir)
            healthy, reason, logs = await _restart_and_check(rt, t)
            m = await _marker(rt, to_dir) or {}
            deploy_id = f"dep_{secrets.token_hex(6)}"
            store.add_plan(
                deploy_id,
                t.name,
                "rollback",
                "-",
                {"ref": m.get("ref"), "commit": {"sha": m.get("sha")}},
            )
            store.update(
                deploy_id,
                status="succeeded" if healthy else "failed",
                release_dir=to_dir,
                previous_dir=cur["release_dir"],
                error=reason or None,
            )
            if not healthy:
                raise _err(
                    ErrorCategory.FAILED,
                    f"rolled back to {to}, but the services are not healthy: {reason}",
                    logs=logs,
                )
            return {"from": cur["release"], "to": to, "sha": m.get("sha"), "services": t.services}

    return await guarded(
        rt,
        Write("rollback_deploy", t.name, "gitlab", params, confirm, session_id, timeout=600),
        preview,
        apply,
    )
