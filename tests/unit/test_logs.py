"""US3: log parsing, filtering, truncation (T041)."""

import json
from datetime import UTC, datetime, timedelta

from tests.conftest import call
from vps_mcp.backends.container import parse_docker_logs
from vps_mcp.backends.systemd import parse_journal

T0 = datetime(2026, 11, 11, 10, 0, tzinfo=UTC)


def test_parse_journal_handles_byte_arrays():
    lines = [
        json.dumps({"__REALTIME_TIMESTAMP": str(int(T0.timestamp() * 1e6)), "MESSAGE": "hello"}),
        json.dumps(
            {
                "__REALTIME_TIMESTAMP": str(int(T0.timestamp() * 1e6) + 1),
                "MESSAGE": list(b"bytes \xff"),
            }
        ),
        "not json",
    ]
    entries = parse_journal("\n".join(lines))
    assert [e.message for e in entries] == ["hello", "bytes �"]
    assert entries[0].timestamp == T0 and entries[0].stream == "journal"


def test_parse_docker_logs_merges_streams_in_order():
    out = "2026-11-11T10:00:02.000000001Z second\n"
    err = "2026-11-11T10:00:01.123456789Z first\n"
    entries = parse_docker_logs(out, err)
    assert [(e.stream, e.message) for e in entries] == [("stderr", "first"), ("stdout", "second")]


def add_logs(vps, n):
    u = vps.units["worker.service"]
    u.logs = [
        (T0 + timedelta(seconds=i), f"line {i}" + (" GET /x 404" if i % 10 == 0 else ""))
        for i in range(n)
    ]


async def test_default_cap_and_truncation(client, vps):
    add_logs(vps, 500)
    data = await call(client, "get_service_logs", service="worker")
    assert data["returned"] == 200 and data["truncated"] is True
    assert data["entries"][-1]["message"].startswith("line 499")  # newest last
    stamps = [e["timestamp"] for e in data["entries"]]
    assert stamps == sorted(stamps)


async def test_line_limit_not_truncated(client, vps):
    add_logs(vps, 30)
    data = await call(client, "get_service_logs", service="worker", lines=100)
    assert data["returned"] == 30 and data["truncated"] is False


async def test_keyword_is_literal_and_case_insensitive(client, vps):
    add_logs(vps, 100)
    data = await call(client, "get_service_logs", service="worker", keyword="get /X 404", lines=5)
    assert data["returned"] == 5 and all("404" in e["message"] for e in data["entries"])
    data = await call(client, "get_service_logs", service="worker", keyword=".*")
    assert data["returned"] == 0


async def test_container_logs_and_time_filters(client, vps):
    vps.containers["api"].logs = [(T0, "stdout", "hello"), (T0, "stderr", "oops")]
    data = await call(client, "get_service_logs", service="api", since="15m")
    assert {e["stream"] for e in data["entries"]} == {"stdout", "stderr"}
    since = [c.argv for c in vps.calls if c.argv[:2] == ["docker", "logs"]][-1]
    assert "--since" in since


async def test_invalid_log_filters(client):
    for kwargs in ({"lines": 0}, {"lines": 5001}, {"keyword": "x" * 201}, {"since": "yesterday"}):
        data = await call(client, "get_service_logs", service="worker", **kwargs)
        assert data["error"]["category"] == "invalid_input", kwargs
