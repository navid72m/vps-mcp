import pytest

from vps_mcp.errors import ErrorCategory, VpsMcpError
from vps_mcp.models.inputs import (
    AbsPath,
    ConfigKey,
    ImageRef,
    Keyword,
    LineCount,
    NetworkName,
    PackageVersion,
    PortMapping,
    ServiceName,
    TimeFilter,
    VolumeMapping,
    validated,
)

INJECTIONS = ["a;b", "a|b", "a&b", "a$b", "a`b`", "a<b", "a>b", "a\nb", "a b", "$(id)"]


def ok(type_, value):
    return validated(type_, value, "f")


def bad(type_, value):
    with pytest.raises(VpsMcpError) as exc:
        validated(type_, value, "f")
    assert exc.value.category is ErrorCategory.INVALID_INPUT


@pytest.mark.parametrize("v", ["nginx", "odoo-db", "api_v2", "getty@tty1", "a.b", "x" * 128])
def test_service_name_ok(v):
    assert ok(ServiceName, v) == v


@pytest.mark.parametrize("v", [*INJECTIONS, "-rf", "", ".hidden", "x" * 129, "../etc"])
def test_service_name_rejected(v):
    bad(ServiceName, v)


@pytest.mark.parametrize("v", ["1.24.0-2ubuntu7", "1:2.3~rc1+dfsg", "7"])
def test_version_ok(v):
    ok(PackageVersion, v)


@pytest.mark.parametrize("v", [*INJECTIONS, "", "1" * 65])
def test_version_rejected(v):
    bad(PackageVersion, v)


@pytest.mark.parametrize(
    "v",
    [
        "redis:7",
        "postgres",
        "ghcr.io/myorg/api:1.0",
        "localhost:5000/x/y:z",
        "odoo@sha256:" + "a" * 64,
        "docker.io/library/odoo:18",
    ],
)
def test_image_ok(v):
    ok(ImageRef, v)


@pytest.mark.parametrize(
    "v", [*INJECTIONS, "", "UPPER/case", "a" * 256, "redis:7 --privileged", "-v/:/host"]
)
def test_image_rejected(v):
    bad(ImageRef, v)


@pytest.mark.parametrize("v", ["odoo-net", "net_1", "a.b"])
def test_network_ok(v):
    ok(NetworkName, v)


@pytest.mark.parametrize("v", ["host", "none", "container:abc", "-x", "a b", "x" * 65, ""])
def test_network_rejected(v):
    bad(NetworkName, v)


@pytest.mark.parametrize("v", ["/etc/nginx/nginx.conf", "/srv/data"])
def test_abs_path_ok(v):
    ok(AbsPath, v)


@pytest.mark.parametrize(
    "v", ["relative", "/srv/../etc/shadow", "/proc/1/environ", "/sys/x", "/dev/sda", "/a\nb"]
)
def test_abs_path_rejected(v):
    bad(AbsPath, v)


def test_config_key():
    ok(ConfigKey, "POSTGRES_PASSWORD")
    bad(ConfigKey, "1BAD")
    bad(ConfigKey, "A-B")


def test_keyword_limits():
    ok(Keyword, "404 not found .*")
    bad(Keyword, "x" * 201)
    bad(Keyword, "a\x1bb")


@pytest.mark.parametrize("v", [0, 5001, -1])
def test_lines_out_of_range(v):
    bad(LineCount, v)


def test_time_filter():
    ok(TimeFilter, "15m")
    ok(TimeFilter, "2026-11-11T10:00:00Z")
    bad(TimeFilter, "yesterday")
    bad(TimeFilter, "15m; rm -rf /")


@pytest.mark.parametrize("v", ["8069:8069", "127.0.0.1:5432:5432", "53:53/udp"])
def test_port_ok(v):
    ok(PortMapping, v)


@pytest.mark.parametrize("v", ["8069", "0:80", "70000:80", "a:b"])
def test_port_rejected(v):
    bad(PortMapping, v)


@pytest.mark.parametrize("v", ["odoo-data:/var/lib/odoo", "/srv/odoo:/mnt/extra:ro"])
def test_volume_ok(v):
    ok(VolumeMapping, v)


@pytest.mark.parametrize(
    "v",
    [
        "/:/host",
        "/etc:/x",
        "/var/run/docker.sock:/var/run/docker.sock",
        "/root/.ssh:/k",
        "data:relative",
        "a:/b:rwx",
        "../x:/y",
    ],
)
def test_volume_rejected(v):
    bad(VolumeMapping, v)
