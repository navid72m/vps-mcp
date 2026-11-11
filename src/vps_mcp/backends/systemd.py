"""OS services: systemd + apt/dnf (research R5, R7, R8)."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime
from typing import Any

from vps_mcp.backends import common
from vps_mcp.backends.validators import VALIDATORS
from vps_mcp.config.policy import AppConfig, PackageManager, ServiceDeclaration
from vps_mcp.errors import ErrorCategory, VpsMcpError
from vps_mcp.models.inputs import ServiceKind
from vps_mcp.models.views import LogEntry, Resources, ServiceState, ServiceStatus, ServiceUsage
from vps_mcp.remote.executor import RemoteExecutor

SHOW_PROPS = (
    "Id,LoadState,ActiveState,SubState,UnitFileState,ActiveEnterTimestamp,StateChangeTimestamp,"
    "ExecMainStatus,Result,MemoryCurrent,CPUUsageNSec"
)
_ACTIVE_MAP = {
    "active": ServiceState.RUNNING,
    "inactive": ServiceState.STOPPED,
    "failed": ServiceState.FAILED,
    "activating": ServiceState.TRANSITIONING,
    "deactivating": ServiceState.TRANSITIONING,
    "reloading": ServiceState.TRANSITIONING,
    "maintenance": ServiceState.TRANSITIONING,
}
_UNSET = {"", "[not set]", "n/a", "18446744073709551615"}
APT_ENV = {"DEBIAN_FRONTEND": "noninteractive"}


def parse_show(stdout: str) -> list[dict[str, str]]:
    blocks: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in stdout.splitlines():
        if not line.strip():
            if current:
                blocks.append(current)
                current = {}
            continue
        key, _, value = line.partition("=")
        current[key] = value
    if current:
        blocks.append(current)
    return blocks


def parse_systemd_ts(value: str | None) -> datetime | None:
    if not value or value in _UNSET:
        return None
    if value.startswith("@"):
        try:
            return datetime.fromtimestamp(int(value[1:].split(".")[0]), tz=UTC)
        except ValueError:
            return None
    # "Tue 2026-11-10 10:00:00 UTC" (--timestamp=utc). Without the flag systemd prints the
    # host's local zone, which cannot be resolved reliably; those values are treated as UTC.
    parts = value.split()
    if len(parts) >= 3:
        try:
            return datetime.strptime(f"{parts[1]} {parts[2]}", "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=UTC
            )
        except ValueError:
            return None
    return None


def _int_or_none(value: str | None) -> int | None:
    if value is None or value in _UNSET:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def status_from_props(
    decl: ServiceDeclaration, props: dict[str, str], version: str | None = None
) -> ServiceStatus:
    if props.get("LoadState") == "not-found":
        return ServiceStatus(
            name=decl.name,
            kind=ServiceKind.OS,
            state=ServiceState.NOT_FOUND,
            protected=decl.protected,
        )
    state = _ACTIVE_MAP.get(props.get("ActiveState", ""), ServiceState.TRANSITIONING)
    entered = parse_systemd_ts(props.get("ActiveEnterTimestamp"))
    uptime = None
    if state is ServiceState.RUNNING and entered:
        uptime = max(0, int((common.now_utc() - entered).total_seconds()))
    exit_reason = None
    if state is ServiceState.FAILED:
        exit_reason = (
            f"result={props.get('Result', '?')}, exit_status={props.get('ExecMainStatus', '?')}"
        )
    mem = _int_or_none(props.get("MemoryCurrent"))
    unit_file = props.get("UnitFileState", "")
    return ServiceStatus(
        name=decl.name,
        kind=ServiceKind.OS,
        state=state,
        boot_enabled=unit_file in ("enabled", "enabled-runtime", "alias", "static")
        if unit_file
        else None,
        uptime_s=uptime,
        last_state_change=parse_systemd_ts(props.get("StateChangeTimestamp")),
        last_exit_reason=exit_reason,
        version=version,
        protected=decl.protected,
        resources=Resources(mem_bytes=mem) if mem is not None else None,
    )


def parse_journal(stdout: str) -> list[LogEntry]:
    entries: list[LogEntry] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = rec.get("MESSAGE")
        if isinstance(msg, list):  # non-UTF-8 messages arrive as byte arrays
            msg = bytes(int(b) & 0xFF for b in msg).decode("utf-8", "replace")
        elif msg is None:
            msg = ""
        try:
            ts = datetime.fromtimestamp(int(rec["__REALTIME_TIMESTAMP"]) / 1e6, tz=UTC)
        except (KeyError, ValueError, TypeError):
            continue
        entries.append(LogEntry(timestamp=ts, message=str(msg), stream="journal"))
    return entries


# ---------------------------------------------------------------- apt / dnf origin checks

_POLICY_SRC_RE = re.compile(r"^\s*(-?\d+)\s+(\S.*\S)\s*$")


def parse_apt_origins(global_policy: str) -> dict[str, str]:
    """Map 'URL suite/component arch Packages' -> origin (o=) from `apt-cache policy`."""
    origins: dict[str, str] = {}
    current: str | None = None
    for line in global_policy.splitlines():
        m = _POLICY_SRC_RE.match(line)
        if m and not line.strip().startswith("release"):
            current = m[2]
            continue
        s = line.strip()
        if current and s.startswith("release "):
            for part in s[len("release ") :].split(","):
                k, _, v = part.partition("=")
                if k == "o":
                    origins[current] = v
    return origins


def parse_apt_package_policy(text: str) -> tuple[str | None, dict[str, list[str]]]:
    """Return (candidate, {version: [source lines]}) from `apt-cache policy <pkg>`."""
    candidate = None
    table: dict[str, list[str]] = {}
    in_table = False
    current: str | None = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("Candidate:"):
            c = s.split(":", 1)[1].strip()
            candidate = None if c == "(none)" else c
        elif s.startswith("Version table:"):
            in_table = True
        elif in_table and s:
            parts = s.removeprefix("***").split()
            if len(parts) == 2 and parts[1].lstrip("-").isdigit() and not parts[0].isdigit():
                current = parts[0]
                table.setdefault(current, [])
            elif current is not None:
                m = _POLICY_SRC_RE.match(s)
                if m:
                    table[current].append(m[2])
    return candidate, table


class SystemdBackend:
    kind = ServiceKind.OS

    def __init__(self, executor: RemoteExecutor, config: AppConfig) -> None:
        self.ex = executor
        self.config = config
        self.pm = config.vps.package_manager

    @property
    def _q(self) -> float:
        return float(self.config.policy.timeouts.query_s)

    @property
    def _c(self) -> float:
        return float(self.config.policy.timeouts.control_s)

    @property
    def _d(self) -> float:
        return float(self.config.policy.timeouts.deploy_s)

    # ------------------------------------------------------------------ status

    async def _show(self, units: list[str]) -> list[dict[str, str]]:
        # --timestamp=utc needs systemd >= 248 (Ubuntu 22.04 ships 249; "unix" needs 251).
        argv = ["systemctl", "show", "--timestamp=utc", f"--property={SHOW_PROPS}", *units]
        r = await self.ex.run(argv, timeout=self._q)
        if not r.ok and ("timestamp" in r.stderr or "Invalid value" in r.stderr):
            r = await self.ex.run([a for a in argv if a != "--timestamp=utc"], timeout=self._q)
        blocks = parse_show(r.stdout)
        if len(blocks) != len(units):
            if not r.ok:
                raise common.command_failed("systemctl show", r)
            raise VpsMcpError(ErrorCategory.FAILED, "unexpected `systemctl show` output")
        return blocks

    async def _versions(self, packages: list[str]) -> dict[str, str]:
        if not packages:
            return {}
        if self.pm is PackageManager.APT:
            argv = [
                "dpkg-query",
                "-W",
                "-f",
                "${Package}\t${Version}\t${db:Status-Abbrev}\n",
                *packages,
            ]
        else:
            argv = ["rpm", "-q", "--qf", "%{NAME}\t%{VERSION}-%{RELEASE}\tii\n", *packages]
        r = await self.ex.run(argv, timeout=self._q)
        out: dict[str, str] = {}
        for line in r.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3 and parts[2].strip().startswith("ii") and parts[1]:
                out[parts[0].split(":")[0]] = parts[1]
        return out

    async def list_status(self, decls: list[ServiceDeclaration]) -> list[ServiceStatus]:
        if not decls:
            return []
        blocks, versions = await asyncio.gather(
            self._show([d.unit_name for d in decls]),
            self._versions(sorted({d.package for d in decls if d.package})),
        )
        return [
            status_from_props(d, b, versions.get(d.package or ""))
            for d, b in zip(decls, blocks, strict=True)
        ]

    async def status(self, decl: ServiceDeclaration) -> ServiceStatus:
        return (await self.list_status([decl]))[0]

    # ----------------------------------------------------------------- control

    async def _systemctl(self, verb: str, decl: ServiceDeclaration, *extra: str) -> None:
        r = await self.ex.run(
            ["systemctl", verb, *extra, decl.unit_name], sudo=True, timeout=self._c
        )
        if not r.ok:
            raise common.command_failed(
                f"systemctl {verb} {decl.unit_name}", r, last_known_state=None
            )

    async def _expect_running(self, decl: ServiceDeclaration, what: str) -> ServiceStatus:
        st = await common.settle_running(lambda: self.status(decl), deadline_s=self._c)
        if st.state is not ServiceState.RUNNING:
            raise VpsMcpError(
                ErrorCategory.FAILED,
                f"{what}: '{decl.name}' is {st.state.value} afterwards"
                + (f" ({st.last_exit_reason})" if st.last_exit_reason else ""),
                last_known_state=st.to_dict(),
            )
        return st

    async def start(self, decl: ServiceDeclaration) -> ServiceStatus:
        try:
            await self._systemctl("start", decl)
        except VpsMcpError as err:
            err.last_known_state = (await self.status(decl)).to_dict()
            raise
        return await self._expect_running(decl, "start")

    async def restart(self, decl: ServiceDeclaration) -> ServiceStatus:
        try:
            await self._systemctl("restart", decl)
        except VpsMcpError as err:
            err.last_known_state = (await self.status(decl)).to_dict()
            raise
        return await self._expect_running(decl, "restart")

    async def stop(self, decl: ServiceDeclaration) -> ServiceStatus:
        await self._systemctl("stop", decl)
        st = await common.poll_status(lambda: self.status(decl), deadline_s=self._c)
        if st.state is ServiceState.RUNNING:
            raise VpsMcpError(
                ErrorCategory.FAILED,
                f"'{decl.name}' is still running after stop",
                last_known_state=st.to_dict(),
            )
        return st

    async def set_boot(self, decl: ServiceDeclaration, enabled: bool) -> ServiceStatus:
        await self._systemctl("enable" if enabled else "disable", decl)
        return await self.status(decl)

    async def activate(self, decl: ServiceDeclaration, how: str | None = None) -> None:
        verb = how or decl.activate
        if verb == "reload":
            st = await self.status(decl)
            if st.state is not ServiceState.RUNNING:
                verb = "restart"
        await self._systemctl("reload" if verb == "reload" else "restart", decl)

    # -------------------------------------------------------------------- logs

    async def logs(
        self, decl: ServiceDeclaration, lines: int, since: datetime | None, until: datetime | None
    ) -> list[LogEntry]:
        argv = ["journalctl", "-u", decl.unit_name, "-o", "json", "--no-pager", "-n", str(lines)]
        if since:
            argv += ["--since", since.strftime("%Y-%m-%d %H:%M:%S UTC")]
        if until:
            argv += ["--until", until.strftime("%Y-%m-%d %H:%M:%S UTC")]
        r = await self.ex.run(argv, sudo=True, timeout=self._q)
        if not r.ok and not r.stdout:
            raise common.command_failed("journalctl", r)
        return parse_journal(r.stdout)

    # --------------------------------------------------------------- resources

    async def resource_usage(self, decls: list[ServiceDeclaration]) -> list[ServiceUsage]:
        if not decls:
            return []
        units = [d.unit_name for d in decls]
        first = await self._show(units)
        await asyncio.sleep(common.POLL_INTERVAL)
        second = await self._show(units)
        usage = []
        for d, a, b in zip(decls, first, second, strict=True):
            cpu = None
            n1, n2 = _int_or_none(a.get("CPUUsageNSec")), _int_or_none(b.get("CPUUsageNSec"))
            if n1 is not None and n2 is not None and common.POLL_INTERVAL:
                cpu = round(max(0, n2 - n1) / (common.POLL_INTERVAL * 1e9) * 100, 2)
            usage.append(
                ServiceUsage(
                    name=d.name,
                    kind=ServiceKind.OS,
                    cpu_pct=cpu,
                    mem_bytes=_int_or_none(b.get("MemoryCurrent")),
                )
            )
        return usage

    # ------------------------------------------------------------- packages

    async def package_version(self, package: str) -> str | None:
        return (await self._versions([package])).get(package)

    async def check_origin(self, package: str, version: str | None) -> str:
        """Refuse packages whose source repository is not approved (FR-014). Returns the version."""
        approved = (
            self.config.policy.approved_sources.apt_origins
            if self.pm is PackageManager.APT
            else self.config.policy.approved_sources.dnf_repos
        )
        if self.pm is PackageManager.APT:
            pkg_r, glob_r = await asyncio.gather(
                self.ex.run(["apt-cache", "policy", package], timeout=self._q),
                self.ex.run(["apt-cache", "policy"], timeout=self._q),
            )
            candidate, table = parse_apt_package_policy(pkg_r.stdout)
            target = version or candidate
            if not target or target not in table:
                raise VpsMcpError(
                    ErrorCategory.FAILED,
                    f"package '{package}'"
                    + (f" version {version}" if version else "")
                    + " is not available from any configured repository",
                )
            origins = parse_apt_origins(glob_r.stdout)
            sources = [s for s in table[target] if not s.startswith("/var/lib/dpkg/status")]
            found = [origins.get(s) for s in sources]
            if not sources or any(o not in approved for o in found):
                raise VpsMcpError(
                    ErrorCategory.UNAPPROVED_SOURCE,
                    f"package '{package}' {target} comes from origin(s) "
                    f"{sorted({o or 'unknown' for o in found})}, not in approved apt_origins",
                )
            return target
        r = await self.ex.run(
            ["dnf", "-q", "repoquery", "--qf", "%{repoid}\t%{evr}\n", package], timeout=self._q
        )
        rows = [line.split("\t") for line in r.stdout.splitlines() if "\t" in line]
        if version:
            rows = [
                x
                for x in rows
                if x[1] == version
                or x[1].split(":")[-1] == version
                or x[1].split(":")[-1].startswith(version + "-")
            ]
        if not rows:
            raise VpsMcpError(
                ErrorCategory.FAILED,
                f"package '{package}' is not available from any enabled repository",
            )
        bad = sorted({x[0] for x in rows if x[0] not in approved})
        if bad:
            raise VpsMcpError(
                ErrorCategory.UNAPPROVED_SOURCE,
                f"package '{package}' is offered by unapproved repo(s) {bad}",
            )
        return version or rows[-1][1]

    async def _install(self, package: str, version: str | None, *, downgrade: bool = False) -> None:
        if self.pm is PackageManager.APT:
            spec = f"{package}={version}" if version else package
            argv = ["apt-get", "install", "-y", "-q", "-o", "Dpkg::Options::=--force-confold"]
            if downgrade:
                argv.append("--allow-downgrades")
            r = await self.ex.run([*argv, spec], sudo=True, timeout=self._d, env=APT_ENV)
        else:
            spec = f"{package}-{version}" if version else package
            verb = "install"
            r = await self.ex.run(["dnf", verb, "-y", spec], sudo=True, timeout=self._d)
            if not r.ok and downgrade:
                r = await self.ex.run(["dnf", "downgrade", "-y", spec], sudo=True, timeout=self._d)
        if not r.ok:
            raise common.command_failed(f"installing {spec}", r)

    async def refresh_index(self) -> None:
        if self.pm is PackageManager.APT:
            r = await self.ex.run(
                ["apt-get", "update", "-q"], sudo=True, timeout=self._d, env=APT_ENV
            )
            if not r.ok:
                raise common.command_failed("apt-get update", r)

    async def _uninstall(self, package: str, purge: bool) -> None:
        if self.pm is PackageManager.APT:
            argv = ["apt-get", "purge" if purge else "remove", "-y", "-q", package]
            r = await self.ex.run(argv, sudo=True, timeout=self._d, env=APT_ENV)
        else:
            r = await self.ex.run(["dnf", "remove", "-y", package], sudo=True, timeout=self._d)
        if not r.ok:
            raise common.command_failed(f"removing {package}", r)

    # --------------------------------------------------------------- deploy

    async def deploy(
        self, decl: ServiceDeclaration, version: str | None, *, enable_on_boot: bool, start: bool
    ) -> dict[str, Any]:
        """Install the package and start the unit. Returns cleanup info for failures."""
        assert decl.package
        was_installed = await self.package_version(decl.package) is not None
        state: dict[str, Any] = {"was_installed": was_installed}
        await self._install(decl.package, version)
        st = await self.status(decl)
        if st.state is ServiceState.NOT_FOUND:
            raise VpsMcpError(
                ErrorCategory.FAILED,
                f"package '{decl.package}' installed but unit '{decl.unit_name}' does not exist",
                extra={"_cleanup": state},
            )
        if enable_on_boot:
            await self._systemctl("enable", decl)
        if start:
            await self._systemctl("restart" if st.state is ServiceState.RUNNING else "start", decl)
            st = await self._expect_running(decl, "deploy")
        else:
            st = await self.status(decl)
        state["status"] = st
        return state

    async def cleanup_failed_deploy(self, decl: ServiceDeclaration, was_installed: bool) -> bool:
        if was_installed or not decl.package:
            return True  # never remove a package that existed before this attempt
        try:
            await self.ex.run(["systemctl", "stop", decl.unit_name], sudo=True, timeout=self._c)
            await self._uninstall(decl.package, purge=False)
            return True
        except VpsMcpError:
            return False

    async def update(self, decl: ServiceDeclaration, version: str, *, downgrade: bool) -> None:
        assert decl.package
        await self._install(decl.package, version, downgrade=downgrade)

    # --------------------------------------------------------------- config

    async def read_files(self, decl: ServiceDeclaration) -> dict[str, str]:
        out: dict[str, str] = {}
        for f in decl.config_files:
            r = await self.ex.run(["cat", "--", f.path], sudo=True, timeout=self._q)
            if not r.ok:
                if "No such file" in r.stderr:
                    out[f.path] = ""
                    continue
                raise common.command_failed(f"reading {f.path}", r)
            out[f.path] = r.stdout
        return out

    async def _stage(self, content: str) -> str:
        r = await self.ex.run(["mktemp", "/tmp/vps-mcp.XXXXXXXX"], timeout=self._q)
        if not r.ok:
            raise common.command_failed("mktemp", r)
        tmp = r.stdout.strip()
        if not re.fullmatch(r"/tmp/vps-mcp\.[A-Za-z0-9]{8}", tmp):
            raise VpsMcpError(ErrorCategory.FAILED, "unexpected mktemp output")
        w = await self.ex.run(["tee", "--", tmp], stdin=content, timeout=self._q)
        if not w.ok:
            raise common.command_failed("staging file", w)
        return tmp

    async def _unstage(self, tmp: str) -> None:
        await self.ex.run(["rm", "-f", "--", tmp], timeout=self._q)

    async def write_files(self, decl: ServiceDeclaration, files: dict[str, str]) -> None:
        decls = {f.path: f for f in decl.config_files}
        for path, content in files.items():
            f = decls[path]
            tmp = await self._stage(content)
            try:
                r = await self.ex.run(
                    ["install", "-m", f.mode, "-o", f.owner, "-g", f.group, "--", tmp, path],
                    sudo=True,
                    timeout=self._c,
                )
                if not r.ok:
                    raise common.command_failed(f"writing {path}", r)
            finally:
                await self._unstage(tmp)

    async def validate_in_place(self, decl: ServiceDeclaration) -> tuple[bool, str]:
        if not decl.validate_with:
            return True, ""
        v = VALIDATORS[decl.validate_with]
        r = await self.ex.run(v.in_place(), sudo=True, timeout=self._c)
        return r.ok, common.tail_text(r.stderr + r.stdout, 30)

    async def validate_staged(
        self, decl: ServiceDeclaration, files: dict[str, str]
    ) -> dict[str, Any] | None:
        """Best-effort validation for previews; only when the main config file is edited."""
        if not decl.validate_with:
            return None
        v = VALIDATORS[decl.validate_with]
        if v.main_path not in files:
            return {"ok": None, "output": "validation runs at apply time (main config unchanged)"}
        tmp = await self._stage(files[v.main_path])
        try:
            r = await self.ex.run(v.staged(tmp), sudo=True, timeout=self._c)
        finally:
            await self._unstage(tmp)
        return {"ok": r.ok, "output": common.tail_text(r.stderr + r.stdout, 30)}

    # --------------------------------------------------------------- remove

    async def remove(self, decl: ServiceDeclaration, delete_data: bool) -> dict[str, Any]:
        if not decl.package:
            raise VpsMcpError(
                ErrorCategory.FAILED,
                f"service '{decl.name}' has no package declared; cannot uninstall",
            )
        await self.ex.run(
            ["systemctl", "disable", "--now", decl.unit_name], sudo=True, timeout=self._c
        )
        await self._uninstall(decl.package, purge=delete_data)
        removed_paths = []
        if delete_data:
            for p in decl.data_paths:
                r = await self.ex.run(
                    ["rm", "-rf", "--one-file-system", "--", p], sudo=True, timeout=self._d
                )
                if not r.ok:
                    raise common.command_failed(f"deleting {p}", r)
                removed_paths.append(p)
        return {
            "data_deleted": delete_data,
            "data_paths_removed": removed_paths,
            "data_paths_kept": [] if delete_data else list(decl.data_paths),
        }
