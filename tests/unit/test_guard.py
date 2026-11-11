import asyncio

import pytest

from tests.conftest import make_config
from vps_mcp.core.guard import ActionRequest, run_action
from vps_mcp.errors import ConnectionLost, ErrorCategory, VpsMcpError
from vps_mcp.server import build_context


def req(**kw):
    base = dict(
        action="restart",
        service="api",
        kind=None,
        confirm=False,
        params={"x": 1},
        session_id="s1",
        timeout=5.0,
    )
    base.update(kw)
    return ActionRequest(**base)


async def ok_body(decl):
    return {"service": {"name": decl.name, "state": "running"}}


def audit_rows(app):
    return app.audit.query(limit=500)


async def expect(app, request, category, body=ok_body):
    with pytest.raises(VpsMcpError) as exc:
        await run_action(app, request, body)
    assert exc.value.category is category
    assert len(audit_rows(app)) == 1
    return exc.value


async def test_invalid_input_refused_and_audited(app):
    err = await expect(app, req(service="bad;name"), ErrorCategory.INVALID_INPUT)
    assert err.extra["outcome"] == "refused"
    assert audit_rows(app)[0]["outcome"] == "refused"


async def test_outside_managed_set(app):
    await expect(app, req(service="postgres"), ErrorCategory.NOT_PERMITTED)


async def test_remote_access_unit_not_permitted(app):
    err = await expect(app, req(service="sshd", kind="os"), ErrorCategory.NOT_PERMITTED)
    assert "unknown or not permitted" in err.message


async def test_read_only(vps, db_path, monkeypatch):
    app = build_context(make_config(read_only=True), executor=vps, db_path=db_path)
    await expect(app, req(), ErrorCategory.READ_ONLY)
    assert vps.mutating_calls() == []


async def test_confirmation_required(app):
    r = req(service="nginx", needs_confirm=lambda d: d.protected)
    await expect(app, r, ErrorCategory.CONFIRMATION_REQUIRED)


async def test_preview_instead_of_refusal(app):
    async def preview(decl):
        return {"diff": "x"}

    out = await run_action(app, req(needs_confirm=lambda d: True, preview=preview), ok_body)
    assert out["outcome"] == "preview" and out["diff"] == "x"
    assert audit_rows(app)[0]["outcome"] == "preview"


async def test_protected_remove_refused_even_with_confirm(app):
    r = req(
        action="remove", service="nginx", confirm=True, is_remove=True, needs_confirm=lambda d: True
    )
    await expect(app, r, ErrorCategory.PROTECTED)


async def test_success_runs_under_lock_and_audits_once(app):
    seen = []

    async def body(decl):
        seen.append(app.locks._locks[("container", "api")].locked())
        return {"service": {"state": "running"}}

    out = await run_action(app, req(), body)
    assert out["outcome"] == "success" and seen == [True]
    rows = audit_rows(app)
    assert len(rows) == 1 and rows[0]["resulting_state"] == "running"


async def test_timeout_reports_last_known_state(app):
    async def slow(decl):
        await asyncio.sleep(10)

    err = await expect(app, req(timeout=0.01), ErrorCategory.TIMED_OUT, body=slow)
    assert err.extra["outcome"] == "timed_out"
    assert err.last_known_state["state"] == "running"


async def test_connection_lost_is_unknown(app):
    async def body(decl):
        raise ConnectionLost()

    err = await expect(app, req(), ErrorCategory.CONNECTION_FAILURE, body=body)
    assert err.extra["outcome"] == "unknown"
    assert app.audit.pending_reconciliation("api", "container")


async def test_failure_audited_as_failed(app):
    async def body(decl):
        raise VpsMcpError(ErrorCategory.FAILED, "boom")

    await expect(app, req(), ErrorCategory.FAILED, body=body)
    assert audit_rows(app)[0]["outcome"] == "failed"


async def test_unexpected_exception_still_audited(app):
    async def body(decl):
        raise RuntimeError("bug")

    await expect(app, req(), ErrorCategory.FAILED, body=body)


async def test_concurrent_actions_serialized(app):
    active = 0
    peak = 0

    async def body(decl):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return {}

    await asyncio.gather(*(run_action(app, req(), body) for _ in range(3)))
    assert peak == 1
    assert len(audit_rows(app)) == 3
