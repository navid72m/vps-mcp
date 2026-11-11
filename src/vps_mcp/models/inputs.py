"""Constrained input types (FR-030, research R10).

Every value that may end up on a remote command line is validated here first. The
executor still shell-quotes everything (defence in depth).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Any

from pydantic import AfterValidator, Field, TypeAdapter, ValidationError

from vps_mcp.errors import invalid

SERVICE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.@-]{0,127}$")
PACKAGE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]{0,127}$")
PACKAGE_VERSION_RE = re.compile(r"^[A-Za-z0-9.+:~_-]{1,64}$")
CONFIG_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
NETWORK_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")
IMAGE_REF_RE = re.compile(
    # optional registry: needs a dot, a port or "localhost" (else it is a lowercase path part)
    r"^(?:(?:[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+|localhost)(?::[0-9]{1,5})?/"
    r"|[a-zA-Z0-9-]+:[0-9]{1,5}/)?"
    r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"  # first path component
    r"(?:/[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*)*"  # further path components
    r"(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?"  # :tag
    r"(?:@sha256:[a-f0-9]{64})?$"  # @digest
)
PORT_RE = re.compile(
    r"^(?:(?P<ip>\d{1,3}(?:\.\d{1,3}){3}):)?(?P<host>\d{1,5}):(?P<ctr>\d{1,5})(?:/(?:tcp|udp))?$"
)
RELATIVE_TIME_RE = re.compile(r"^(\d{1,6})([smhd])$")
CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")

FORBIDDEN_PATH_PREFIXES = ("/proc", "/sys", "/dev")
# Host paths a container may never bind-mount: mounting these hands the container root on the VPS.
FORBIDDEN_BIND_PREFIXES = (
    "/etc",
    "/root",
    "/boot",
    "/run",
    "/var/run",
    "/proc",
    "/sys",
    "/dev",
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/var/lib/docker",
    "/var/lib/containers",
)
RESERVED_NETWORKS = {"host", "none"}

MAX_LINES = 5000


class ServiceKind(StrEnum):
    OS = "os"
    CONTAINER = "container"


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def check_service_name(v: str) -> str:
    if not SERVICE_NAME_RE.fullmatch(v):
        raise ValueError("service name must match ^[a-zA-Z0-9][a-zA-Z0-9_.@-]{0,127}$")
    return v


def check_package_name(v: str) -> str:
    if not PACKAGE_NAME_RE.fullmatch(v):
        raise ValueError("package name must match ^[a-z0-9][a-z0-9+.-]{0,127}$")
    return v


def check_package_version(v: str) -> str:
    if not PACKAGE_VERSION_RE.fullmatch(v):
        raise ValueError("package version must match ^[A-Za-z0-9.+:~_-]{1,64}$")
    return v


def check_image_ref(v: str) -> str:
    if len(v) > 255 or not IMAGE_REF_RE.fullmatch(v):
        raise ValueError(
            "image must be an OCI reference like registry/path:tag or @sha256:<64 hex>"
        )
    return v


def check_config_key(v: str) -> str:
    if not CONFIG_KEY_RE.fullmatch(v):
        raise ValueError(f"key {v!r} must match ^[A-Za-z_][A-Za-z0-9_]{{0,127}}$")
    return v


def check_network_name(v: str) -> str:
    if v in RESERVED_NETWORKS or v.startswith("container:"):
        raise ValueError(f"network {v!r} is not allowed (bypasses isolation)")
    if not NETWORK_NAME_RE.fullmatch(v):
        raise ValueError("network name must match ^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")
    return v


def check_abs_path(v: str) -> str:
    if not v.startswith("/") or "\x00" in v or CONTROL_CHARS_RE.search(v):
        raise ValueError(f"path {v!r} must be absolute")
    if any(part == ".." for part in v.split("/")):
        raise ValueError(f"path {v!r} must not contain '..'")
    if any(_under(v, p) for p in FORBIDDEN_PATH_PREFIXES):
        raise ValueError(f"path {v!r} must not be under /proc, /sys or /dev")
    if v != "/" and v.endswith("/"):
        v = v.rstrip("/")
    return v


def check_keyword(v: str) -> str:
    if len(v) > 200:
        raise ValueError("keyword must be at most 200 characters")
    if CONTROL_CHARS_RE.search(v):
        raise ValueError("keyword must not contain control characters")
    return v


def check_time_filter(v: str) -> str:
    if RELATIVE_TIME_RE.fullmatch(v):
        return v
    try:
        datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("time must be ISO-8601 or relative like 15m, 2h, 1d") from exc
    return v


def check_port(v: str) -> str:
    m = PORT_RE.fullmatch(v)
    if not m or not (1 <= int(m["host"]) <= 65535 and 1 <= int(m["ctr"]) <= 65535):
        raise ValueError(f"port {v!r} must look like hostPort:containerPort[/tcp|udp]")
    return v


def check_volume(v: str) -> str:
    parts = v.split(":")
    if len(parts) == 3:
        if parts[2] not in ("ro", "rw"):
            raise ValueError(f"volume {v!r}: mode must be ro or rw")
    elif len(parts) != 2:
        raise ValueError(f"volume {v!r} must look like name_or_abs_path:container_path[:ro]")
    src, dst = parts[0], parts[1]
    check_abs_path(dst)
    if src.startswith("/"):
        check_abs_path(src)
        if src == "/" or any(_under(src, p) for p in FORBIDDEN_BIND_PREFIXES):
            raise ValueError(f"volume {v!r}: bind-mounting {src} is not allowed")
    elif not NETWORK_NAME_RE.fullmatch(src):  # same shape as docker volume names
        raise ValueError(f"volume {v!r}: invalid volume name")
    return v


def check_env_value(v: str) -> str:
    if "\n" in v or "\r" in v or "\x00" in v:
        raise ValueError("env values must be single-line")
    if len(v) > 8192:
        raise ValueError("env values must be at most 8192 characters")
    return v


def check_command_arg(v: str) -> str:
    if "\x00" in v or len(v) > 1024:
        raise ValueError("command arguments must be at most 1024 characters, without NUL")
    return v


ServiceName = Annotated[str, AfterValidator(check_service_name)]
PackageName = Annotated[str, AfterValidator(check_package_name)]
PackageVersion = Annotated[str, AfterValidator(check_package_version)]
ImageRef = Annotated[str, AfterValidator(check_image_ref)]
ConfigKey = Annotated[str, AfterValidator(check_config_key)]
NetworkName = Annotated[str, AfterValidator(check_network_name)]
AbsPath = Annotated[str, AfterValidator(check_abs_path)]
Keyword = Annotated[str, AfterValidator(check_keyword)]
TimeFilter = Annotated[str, AfterValidator(check_time_filter)]
LineCount = Annotated[int, Field(ge=1, le=MAX_LINES)]
PortMapping = Annotated[str, AfterValidator(check_port)]
VolumeMapping = Annotated[str, AfterValidator(check_volume)]
EnvValue = Annotated[str, AfterValidator(check_env_value)]
CommandArg = Annotated[str, AfterValidator(check_command_arg)]


def validated(type_: Any, value: Any, field: str) -> Any:
    """Validate ``value`` against ``type_``, raising ``invalid_input`` on failure."""
    try:
        return TypeAdapter(type_).validate_python(value)
    except ValidationError as exc:
        raise invalid(f"{field}: {first_error(exc)}") from None


def first_error(exc: ValidationError) -> str:
    errs = exc.errors()
    if not errs:
        return str(exc)
    e = errs[0]
    loc = ".".join(str(p) for p in e.get("loc", ()) if p != "function-after")
    msg = str(e.get("msg", "invalid value")).removeprefix("Value error, ")
    return f"{loc}: {msg}" if loc else msg


def resolve_time(v: str, now: datetime | None = None) -> datetime:
    """Turn a validated TimeFilter into an absolute UTC datetime."""
    now = now or datetime.now(UTC)
    m = RELATIVE_TIME_RE.fullmatch(v)
    if m:
        amount, unit = int(m[1]), m[2]
        delta = {
            "s": timedelta(seconds=amount),
            "m": timedelta(minutes=amount),
            "h": timedelta(hours=amount),
            "d": timedelta(days=amount),
        }[unit]
        return now - delta
    dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)
