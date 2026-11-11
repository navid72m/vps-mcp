"""Container services: Docker/Podman CLI over SSH (research R6, R6a, R7)."""

from __future__ import annotations

import json
import shlex
from datetime import datetime
from typing import Any

from vps_mcp.backends import common
from vps_mcp.config.policy import AppConfig, ServiceDeclaration
from vps_mcp.errors import ErrorCategory, VpsMcpError
from vps_mcp.models.inputs import CONFIG_KEY_RE, ServiceKind
from vps_mcp.models.views import (
    ContainerSpec,
    Healthcheck,
    LogEntry,
    Resources,
    ServiceState,
    ServiceStatus,
    ServiceUsage,
)
from vps_mcp.remote.executor import RemoteExecutor

MANAGED_LABEL = "io.vps-mcp.managed=true"
SERVICE_LABEL = "io.vps-mcp.service"
OLD_SUFFIX = ".vps-mcp-old"
DEFAULT_NETWORKS = {"bridge", "podman", "default"}


def status_from_inspect(decl: ServiceDeclaration, data: dict[str, Any] | None) -> ServiceStatus:
    if data is None:
        return ServiceStatus(
            name=decl.name,
            kind=ServiceKind.CONTAINER,
            state=ServiceState.NOT_FOUND,
            protected=decl.protected,
        )
    st = data.get("State") or {}
    raw = str(st.get("Status", "")).lower()
    code = int(st.get("ExitCode") or 0)
    if raw == "running":
        state = ServiceState.RUNNING
    elif raw in ("restarting", "removing"):
        state = ServiceState.TRANSITIONING
    elif raw == "dead" or (raw in ("exited", "stopped") and code != 0):
        state = ServiceState.FAILED
    else:  # created, exited 0, paused, configured, stopped 0
        state = ServiceState.STOPPED
    started = common.parse_rfc3339(st.get("StartedAt"))
    finished = common.parse_rfc3339(st.get("FinishedAt"))
    uptime = None
    if state is ServiceState.RUNNING and started:
        uptime = max(0, int((common.now_utc() - started).total_seconds()))
    reason = None
    if state is ServiceState.FAILED:
        reason = f"exit_code={code}" + (f", error={st['Error']}" if st.get("Error") else "")
        if st.get("OOMKilled"):
            reason += ", oom_killed"
    policy = ((data.get("HostConfig") or {}).get("RestartPolicy") or {}).get("Name", "")
    networks = sorted(((data.get("NetworkSettings") or {}).get("Networks") or {}).keys())
    health = (st.get("Health") or {}).get("Status") or None
    last_change = max((t for t in (started, finished) if t), default=None)
    return ServiceStatus(
        name=decl.name,
        kind=ServiceKind.CONTAINER,
        state=state,
        boot_enabled=policy in ("always", "unless-stopped"),
        uptime_s=uptime,
        last_state_change=last_change,
        last_exit_reason=reason,
        version=(data.get("Config") or {}).get("Image"),
        protected=decl.protected,
        networks=networks,
        health=health,
    )


def spec_from_inspect(data: dict[str, Any]) -> ContainerSpec:
    cfg = data.get("Config") or {}
    host = data.get("HostConfig") or {}
    env: dict[str, str] = {}
    for item in cfg.get("Env") or []:
        k, _, v = str(item).partition("=")
        if CONFIG_KEY_RE.fullmatch(k) and "\n" not in v:
            env[k] = v
    ports = []
    for key, bindings in (host.get("PortBindings") or {}).items():
        ctr, _, proto = key.partition("/")
        for b in bindings or []:
            hp = b.get("HostPort")
            if not hp:
                continue
            ip = b.get("HostIp") or ""
            wildcard = ip in ("", "0.0.0.0", "::")  # noqa: S104 - parsing, not binding
            base = f"{hp}:{ctr}" if wildcard else f"{ip}:{hp}:{ctr}"
            ports.append(base + ("/udp" if proto == "udp" else ""))
    volumes = []
    for m in data.get("Mounts") or []:
        src = m.get("Name") if m.get("Type") == "volume" else m.get("Source")
        if not src or not m.get("Destination"):
            continue
        volumes.append(f"{src}:{m['Destination']}" + ("" if m.get("RW", True) else ":ro"))
    networks = [
        n
        for n in ((data.get("NetworkSettings") or {}).get("Networks") or {})
        if n not in DEFAULT_NETWORKS
    ]
    hc = cfg.get("Healthcheck") or {}
    healthcheck = None
    if hc.get("Test") and hc["Test"] != ["NONE"]:
        healthcheck = Healthcheck(
            test=hc["Test"],
            interval_s=max(1, int(hc.get("Interval") or 30e9) // 10**9),
            retries=int(hc.get("Retries") or 3),
        )
    policy = (host.get("RestartPolicy") or {}).get("Name", "")
    return ContainerSpec.model_validate(
        {
            "image": cfg.get("Image"),
            "env": env,
            "ports": ports,
            "volumes": volumes,
            "networks": sorted(networks),
            "command": cfg.get("Cmd") or None,
            "restart_on_boot": policy in ("always", "unless-stopped"),
            "healthcheck": healthcheck,
        }
    )


def env_file(env: dict[str, str]) -> str:
    return "".join(f"{k}={v}\n" for k, v in env.items())


def health_args(hc: Healthcheck | None) -> list[str]:
    if hc is None:
        return []
    test = list(hc.test)
    if test[0] == "NONE":
        return ["--no-healthcheck"]
    if test[0] == "CMD-SHELL":
        cmd = " ".join(test[1:])
    elif test[0] == "CMD":
        cmd = shlex.join(test[1:])
    else:
        cmd = shlex.join(test)
    return [
        "--health-cmd",
        cmd,
        "--health-interval",
        f"{hc.interval_s}s",
        "--health-retries",
        str(hc.retries),
    ]


def run_argv(runtime: str, name: str, spec: ContainerSpec) -> list[str]:
    argv = [
        runtime,
        "run",
        "-d",
        "--name",
        name,
        "--label",
        MANAGED_LABEL,
        "--label",
        f"{SERVICE_LABEL}={name}",
        "--restart",
        "unless-stopped" if spec.restart_on_boot else "no",
    ]
    if spec.env:
        argv += ["--env-file", "/dev/stdin"]  # secrets never appear on the command line
    for p in spec.ports:
        argv += ["-p", p]
    for v in spec.volumes:
        argv += ["-v", v]
    if spec.networks:
        argv += ["--network", spec.networks[0]]
    argv += health_args(spec.healthcheck)
    argv.append(spec.image)
    if spec.command:
        argv += list(spec.command)
    return argv


def parse_docker_logs(stdout: str, stderr: str) -> list[LogEntry]:
    entries: list[LogEntry] = []
    for stream, text in (("stdout", stdout), ("stderr", stderr)):
        for line in text.splitlines():
            ts_text, _, msg = line.partition(" ")
            ts = common.parse_rfc3339(ts_text)
            if ts is None:
                continue
            entries.append(LogEntry(timestamp=ts, message=msg, stream=stream))
    entries.sort(key=lambda e: e.timestamp)
    return entries


def _first(d: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if k in d:
            return d[k]
    return None


class ContainerBackend:
    kind = ServiceKind.CONTAINER

    def __init__(self, executor: RemoteExecutor, config: AppConfig) -> None:
        self.ex = executor
        self.config = config
        self.rt = config.vps.container_runtime.value

    @property
    def _q(self) -> float:
        return float(self.config.policy.timeouts.query_s)

    @property
    def _c(self) -> float:
        return float(self.config.policy.timeouts.control_s)

    @property
    def _d(self) -> float:
        return float(self.config.policy.timeouts.deploy_s)

    async def _run(
        self,
        *args: str,
        timeout: float | None = None,
        stdin: str | None = None,
        check: str | None = None,
    ) -> Any:
        r = await self.ex.run([self.rt, *args], sudo=True, timeout=timeout or self._c, stdin=stdin)
        if check and not r.ok:
            raise common.command_failed(check, r)
        return r

    # ------------------------------------------------------------------ status

    async def inspect_many(self, names: list[str]) -> dict[str, dict[str, Any]]:
        if not names:
            return {}
        r = await self._run(
            "container", "inspect", "--format", "{{json .}}", *names, timeout=self._q
        )
        found: dict[str, dict[str, Any]] = {}
        for line in r.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            found[str(data.get("Name", "")).lstrip("/")] = data
        if not r.ok and not found and "no such" not in r.stderr.lower():
            raise common.command_failed(f"{self.rt} inspect", r)
        return found

    async def inspect(self, name: str) -> dict[str, Any] | None:
        return (await self.inspect_many([name])).get(name)

    async def list_status(self, decls: list[ServiceDeclaration]) -> list[ServiceStatus]:
        found = await self.inspect_many([d.name for d in decls])
        return [status_from_inspect(d, found.get(d.name)) for d in decls]

    async def status(self, decl: ServiceDeclaration) -> ServiceStatus:
        return status_from_inspect(decl, await self.inspect(decl.name))

    # ----------------------------------------------------------------- control

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
        await self._run("start", decl.name, check=f"{self.rt} start {decl.name}")
        return await self._expect_running(decl, "start")

    async def restart(self, decl: ServiceDeclaration) -> ServiceStatus:
        await self._run("restart", decl.name, check=f"{self.rt} restart {decl.name}")
        return await self._expect_running(decl, "restart")

    async def stop(self, decl: ServiceDeclaration) -> ServiceStatus:
        await self._run("stop", decl.name, check=f"{self.rt} stop {decl.name}")
        st = await common.poll_status(lambda: self.status(decl), deadline_s=self._c)
        if st.state is ServiceState.RUNNING:
            raise VpsMcpError(
                ErrorCategory.FAILED,
                f"'{decl.name}' is still running after stop",
                last_known_state=st.to_dict(),
            )
        return st

    async def set_boot(self, decl: ServiceDeclaration, enabled: bool) -> ServiceStatus:
        await self._run(
            "update",
            "--restart",
            "unless-stopped" if enabled else "no",
            decl.name,
            check=f"{self.rt} update {decl.name}",
        )
        return await self.status(decl)

    # -------------------------------------------------------------------- logs

    async def logs(
        self, decl: ServiceDeclaration, lines: int, since: datetime | None, until: datetime | None
    ) -> list[LogEntry]:
        args = ["logs", "--timestamps", "--tail", str(lines)]
        if since:
            args += ["--since", since.strftime("%Y-%m-%dT%H:%M:%SZ")]
        if until:
            args += ["--until", until.strftime("%Y-%m-%dT%H:%M:%SZ")]
        r = await self._run(*args, decl.name, timeout=self._q)
        if not r.ok and not r.stdout and not common.parse_rfc3339(r.stderr.split(" ", 1)[0]):
            raise common.command_failed(f"{self.rt} logs", r)
        return parse_docker_logs(r.stdout, r.stderr)

    # --------------------------------------------------------------- resources

    async def resource_usage(self, decls: list[ServiceDeclaration]) -> list[ServiceUsage]:
        running = [s.name for s in await self.list_status(decls) if s.state is ServiceState.RUNNING]
        usage = {d.name: ServiceUsage(name=d.name, kind=ServiceKind.CONTAINER) for d in decls}
        if running:
            r = await self._run(
                "stats", "--no-stream", "--format", "{{json .}}", *running, timeout=self._q
            )
            for line in r.stdout.splitlines():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                name = str(_first(row, "Name", "name") or "").lstrip("/")
                if name not in usage:
                    continue
                cpu = str(_first(row, "CPUPerc", "CPU", "cpu_percent") or "").rstrip("%").strip()
                mem = str(_first(row, "MemUsage", "mem_usage") or "").split("/")[0]
                try:
                    usage[name].cpu_pct = float(cpu) if cpu else None
                except ValueError:
                    pass
                usage[name].mem_bytes = common.parse_size(mem) if mem else None
        return list(usage.values())

    async def apply_resources(
        self, statuses: list[ServiceStatus], decls: list[ServiceDeclaration]
    ) -> None:
        by_name = {u.name: u for u in await self.resource_usage(decls)}
        for st in statuses:
            u = by_name.get(st.name)
            if u and (u.cpu_pct is not None or u.mem_bytes is not None):
                st.resources = Resources(cpu_pct=u.cpu_pct, mem_bytes=u.mem_bytes)

    # ---------------------------------------------------------------- networks

    async def network_exists(self, net: str) -> bool:
        return bool((await self._run("network", "inspect", net, timeout=self._q)).ok)

    async def missing_networks(self, nets: list[str]) -> list[str]:
        return [n for n in nets if not await self.network_exists(n)]

    async def ensure_networks(self, nets: list[str]) -> list[str]:
        created = []
        for net in await self.missing_networks(nets):
            await self._run(
                "network",
                "create",
                "--driver",
                "bridge",
                "--label",
                MANAGED_LABEL,
                net,
                check=f"creating network {net}",
            )
            created.append(net)
        return created

    async def connect_networks(self, name: str, nets: list[str]) -> None:
        for net in nets:
            await self._run("network", "connect", net, name, check=f"connecting {name} to {net}")

    async def gc_managed_networks(self, names: list[str]) -> list[str]:
        """Remove networks labelled as managed by vps-mcp that have no containers left."""
        removed = []
        for net in sorted(set(names) - DEFAULT_NETWORKS):
            r = await self._run(
                "network", "inspect", "--format", "{{json .Labels}}", net, timeout=self._q
            )
            if not r.ok:
                continue
            try:
                labels = json.loads(r.stdout.strip() or "{}") or {}
            except json.JSONDecodeError:
                continue
            if labels.get("io.vps-mcp.managed") != "true":
                continue  # operator-owned network: joined, never deleted
            users = await self._run(
                "ps", "-a", "--filter", f"network={net}", "--format", "{{.Names}}", timeout=self._q
            )
            if users.ok and not users.stdout.strip():
                rm = await self._run("network", "rm", net)
                if rm.ok:
                    removed.append(net)
        return removed

    # ------------------------------------------------------------------ deploy

    async def image_present(self, image: str) -> bool:
        return bool((await self._run("image", "inspect", image, timeout=self._q)).ok)

    async def pull(self, image: str) -> None:
        await self._run("pull", image, timeout=self._d, check=f"pulling {image}")

    async def _create(self, name: str, spec: ContainerSpec) -> None:
        stdin = env_file(spec.env) if spec.env else None
        r = await self.ex.run(
            run_argv(self.rt, name, spec), sudo=True, timeout=self._d, stdin=stdin
        )
        if not r.ok:
            raise common.command_failed(f"starting container {name}", r)
        await self.connect_networks(name, list(spec.networks[1:]))

    async def deploy(
        self, decl: ServiceDeclaration, spec: ContainerSpec, *, start: bool
    ) -> dict[str, Any]:
        state: dict[str, Any] = {"pulled": False, "created_networks": [], "container": False}
        try:
            if not await self.image_present(spec.image):
                await self.pull(spec.image)
                state["pulled"] = True
            state["created_networks"] = await self.ensure_networks(list(spec.networks))
            state["container"] = True
            await self._create(decl.name, spec)
            if start:
                state["status"] = await self._expect_running(decl, "deploy")
            else:
                await self._run("stop", decl.name)
                state["status"] = await self.status(decl)
        except VpsMcpError as err:
            err.extra["_cleanup"] = state
            raise
        return state

    async def cleanup_failed_deploy(
        self, decl: ServiceDeclaration, spec: ContainerSpec, state: dict[str, Any]
    ) -> bool:
        ok = True
        if state.get("container"):
            ok &= bool((await self._run("rm", "-f", decl.name)).ok)
        if state.get("pulled"):
            ok &= bool((await self._run("rmi", spec.image)).ok)
        for net in state.get("created_networks", []):
            ok &= bool((await self._run("network", "rm", net)).ok)
        return ok

    # ----------------------------------------------------------------- config

    async def current_spec(self, decl: ServiceDeclaration) -> ContainerSpec | None:
        data = await self.inspect(decl.name)
        return spec_from_inspect(data) if data else None

    async def recreate(
        self, decl: ServiceDeclaration, old: ContainerSpec, new: ContainerSpec
    ) -> ServiceStatus:
        """Replace the container with one built from ``new``; restore ``old`` on failure."""
        name, old_name = decl.name, decl.name + OLD_SUFFIX
        was_running = (await self.status(decl)).state is ServiceState.RUNNING
        if new.image != old.image and not await self.image_present(new.image):
            await self.pull(new.image)
        await self._run("rm", "-f", old_name)
        await self._run("stop", name, check=f"stopping {name}")
        await self._run("rename", name, old_name, check=f"renaming {name}")
        created: list[str] = []
        try:
            created = await self.ensure_networks(list(new.networks))
            await self._create(name, new)
            st = await self._expect_running(decl, "recreate")
        except VpsMcpError as err:
            await self._run("rm", "-f", name)
            await self._run("rename", old_name, name)
            if was_running:
                await self._run("start", name)
            if created:
                await self.gc_managed_networks(created)
            err.message = f"{err.message}; the previous container was restored"
            raise
        await self._run("rm", "-f", old_name)
        dropped = sorted(set(old.networks) - set(new.networks))
        if dropped:
            await self.gc_managed_networks(dropped)
        return st

    # ----------------------------------------------------------------- remove

    async def remove(self, decl: ServiceDeclaration, delete_data: bool) -> dict[str, Any]:
        data = await self.inspect(decl.name)
        networks: list[str] = []
        volumes: list[str] = []
        if data:
            networks = list(((data.get("NetworkSettings") or {}).get("Networks") or {}).keys())
            volumes = [
                m["Name"]
                for m in data.get("Mounts") or []
                if m.get("Type") == "volume" and m.get("Name")
            ]
            await self._run("rm", "-f", decl.name, check=f"removing container {decl.name}")
        removed_volumes = []
        if delete_data:
            for v in volumes:
                r = await self._run("volume", "rm", v)
                if r.ok:
                    removed_volumes.append(v)
        return {
            "data_deleted": delete_data,
            "volumes_removed": removed_volumes,
            "volumes_kept": [v for v in volumes if v not in removed_volumes],
            "networks_removed": await self.gc_managed_networks(networks),
        }

    # ------------------------------------------------------------------- disk

    async def data_root(self) -> str | None:
        fmt = "{{.DockerRootDir}}" if self.rt == "docker" else "{{.Store.GraphRoot}}"
        r = await self._run("info", "--format", fmt, timeout=self._q)
        root = r.stdout.strip()
        return root if r.ok and root.startswith("/") else None
