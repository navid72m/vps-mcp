"""A simulated VPS implementing the RemoteExecutor protocol.

It models just enough of systemd, apt, docker and coreutils to exercise every use case
without a real machine. Every call is recorded so tests can assert on exact argv.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from vps_mcp.errors import ConnectionLost, ErrorCategory, VpsMcpError
from vps_mcp.remote.executor import CommandResult

NOW = datetime(2026, 11, 11, 12, 0, 0, tzinfo=UTC)

MUTATING = [
    ("systemctl", ("start", "stop", "restart", "enable", "disable", "reload")),
    ("apt-get", ("install", "remove", "purge", "update")),
    ("dnf", ("install", "remove", "downgrade")),
    (
        "docker",
        ("run", "start", "stop", "restart", "update", "rm", "rename", "pull", "rmi", "create"),
    ),
    ("install", None),
    ("tee", None),
    ("rm", None),
]
DOCKER_SUB_MUTATING = {
    ("network", "create"),
    ("network", "connect"),
    ("network", "rm"),
    ("volume", "rm"),
}


def is_mutating(argv: list[str]) -> bool:
    if not argv:
        return False
    head = argv[0]
    if head == "docker" and len(argv) > 2 and (argv[1], argv[2]) in DOCKER_SUB_MUTATING:
        return True
    for cmd, verbs in MUTATING:
        if head == cmd and (verbs is None or (len(argv) > 1 and argv[1] in verbs)):
            return True
    return False


@dataclass
class Call:
    argv: list[str]
    sudo: bool
    stdin: str | None
    env: dict[str, str] | None


@dataclass
class Unit:
    active: str = "active"
    enabled: bool = True
    since: datetime = NOW - timedelta(hours=1)
    package: str | None = None
    fail_on_start: bool = False
    logs: list[tuple[datetime, str]] = field(default_factory=list)


@dataclass
class Container:
    name: str
    spec: dict[str, Any]
    status: str = "running"
    exit_code: int = 0
    started: datetime = NOW - timedelta(minutes=30)
    networks: list[str] = field(default_factory=list)
    logs: list[tuple[datetime, str, str]] = field(default_factory=list)
    health: str | None = None


def ok(stdout: str = "", stderr: str = "") -> CommandResult:
    return CommandResult(0, stdout, stderr)


def err(stderr: str, code: int = 1, stdout: str = "") -> CommandResult:
    return CommandResult(code, stdout, stderr)


class FakeVps:
    def __init__(self) -> None:
        self.calls: list[Call] = []
        self.units: dict[str, Unit] = {}
        self.installed: dict[str, str] = {}
        # repo: package -> {version: [source lines]}
        self.repo: dict[str, dict[str, list[str]]] = {}
        self.origins: dict[str, str] = {
            "http://archive.ubuntu.com/ubuntu noble/main amd64 Packages": "Ubuntu",
            "http://evil.example.com/apt stable/main amd64 Packages": "Evil",
        }
        self.pkg_units: dict[str, str] = {}
        self.files: dict[str, str] = {}
        self.containers: dict[str, Container] = {}
        self.images: set[str] = set()
        self.registry: set[str] = set()  # images that can be pulled
        self.networks: dict[str, dict[str, str]] = {"bridge": {}}
        self.volumes: set[str] = set()
        self.free_mb: dict[str, int] = {"/": 20_000, "/var/lib/docker": 20_000}
        self.crash_images: set[str] = set()  # containers from these images exit with code 1
        self.unhealthy_images: set[str] = set()
        self.drop_connection_on: tuple[str, ...] | None = None
        self.unreachable = False
        self.sudo_denied: set[str] = set()
        self._tmp = 0

    # ------------------------------------------------------------------ helpers

    def add_unit(self, unit: str, **kw: Any) -> Unit:
        u = Unit(**kw)
        self.units[unit if "." in unit else f"{unit}.service"] = u
        return u

    def add_container(self, name: str, image: str, **kw: Any) -> Container:
        spec = {
            "image": image,
            "env": kw.pop("env", {}),
            "ports": kw.pop("ports", []),
            "volumes": kw.pop("volumes", []),
            "restart": kw.pop("restart", "unless-stopped"),
            "command": kw.pop("command", None),
            "health_cmd": None,
        }
        c = Container(name=name, spec=spec, **kw)
        if not c.networks:
            c.networks = ["bridge"]
        self.containers[name] = c
        self.images.add(image)
        for v in spec["volumes"]:
            if not v.startswith("/"):
                self.volumes.add(v.split(":")[0])
        return c

    def mutating_calls(self) -> list[Call]:
        return [c for c in self.calls if is_mutating(c.argv)]

    def commands(self) -> list[str]:
        return [" ".join(c.argv) for c in self.calls]

    # ---------------------------------------------------------------- protocol

    async def run(
        self,
        argv: list[str],
        *,
        sudo: bool = False,
        timeout: float = 30.0,
        stdin: str | None = None,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        if self.unreachable:
            raise VpsMcpError(
                ErrorCategory.CONNECTION_FAILURE, "host unreachable", subtype="unreachable"
            )
        self.calls.append(Call(list(argv), sudo, stdin, env))
        if (
            self.drop_connection_on
            and tuple(argv[: len(self.drop_connection_on)]) == self.drop_connection_on
        ):
            self.drop_connection_on = None
            raise ConnectionLost()
        if sudo and argv[0] in self.sudo_denied:
            raise VpsMcpError(ErrorCategory.PERMISSION_DENIED, f"may not run {argv[0]}")
        handler = getattr(self, f"_h_{argv[0].replace('-', '_')}", None)
        if handler is None:
            return err(f"{argv[0]}: command not found", 127)
        return handler(argv[1:], stdin)  # type: ignore[no-any-return]

    # ----------------------------------------------------------------- systemd

    def _unit_props(self, name: str) -> str:
        u = self.units.get(name)
        if u is None:
            return f"Id={name}\nLoadState=not-found\nActiveState=inactive\nSubState=dead\nUnitFileState=\n"
        ts = u.since.strftime("%a %Y-%m-%d %H:%M:%S UTC")
        result = "exit-code" if u.active == "failed" else "success"
        status = "1" if u.active == "failed" else "0"
        return (
            f"Id={name}\nLoadState=loaded\nActiveState={u.active}\nSubState=running\n"
            f"UnitFileState={'enabled' if u.enabled else 'disabled'}\n"
            f"ActiveEnterTimestamp={ts}\nStateChangeTimestamp={ts}\n"
            f"ExecMainStatus={status}\nResult={result}\n"
            f"MemoryCurrent={10485760 if u.active == 'active' else '[not set]'}\n"
            f"CPUUsageNSec={1_000_000_000 + len(self.calls) * 10_000_000}\n"
        )

    def _h_systemctl(self, args: list[str], stdin: str | None) -> CommandResult:
        verb = args[0]
        if verb == "show":
            units = [a for a in args[1:] if not a.startswith("--")]
            return ok("\n".join(self._unit_props(u) for u in units))
        flags = [a for a in args[1:] if a.startswith("--")]
        name = [a for a in args[1:] if not a.startswith("--")][-1]
        u = self.units.get(name)
        if u is None:
            return err(f"Failed to {verb} {name}: Unit {name} not found.", 5)
        if verb in ("start", "restart"):
            if u.fail_on_start:
                u.active = "failed"
                u.logs.append((NOW, f"{name}: fatal error, exiting"))
                return err(
                    f"Job for {name} failed because the control process exited with error code."
                )
            u.active, u.since = "active", NOW
        elif verb == "reload":
            if u.active != "active":
                return err(f"{name} is not active, cannot reload.")
            if u.fail_on_start:
                u.active = "failed"
        elif verb == "stop":
            u.active = "inactive"
        elif verb in ("enable", "disable"):
            u.enabled = verb == "enable"
            if "--now" in flags:
                u.active = "active" if verb == "enable" else "inactive"
        return ok()

    def _h_journalctl(self, args: list[str], stdin: str | None) -> CommandResult:
        unit = args[args.index("-u") + 1]
        n = int(args[args.index("-n") + 1])
        u = self.units.get(unit)
        lines = (u.logs if u else [])[-n:]
        out = "\n".join(
            json.dumps({"__REALTIME_TIMESTAMP": str(int(ts.timestamp() * 1e6)), "MESSAGE": msg})
            for ts, msg in lines
        )
        return ok(out)

    # --------------------------------------------------------------- packages

    def _h_dpkg_query(self, args: list[str], stdin: str | None) -> CommandResult:
        pkgs = args[3:]
        out = "".join(f"{p}\t{self.installed[p]}\tii \n" for p in pkgs if p in self.installed)
        missing = [p for p in pkgs if p not in self.installed]
        return CommandResult(
            1 if missing else 0,
            out,
            "".join(f"dpkg-query: no packages found matching {p}\n" for p in missing),
        )

    def _h_apt_cache(self, args: list[str], stdin: str | None) -> CommandResult:
        if len(args) == 1:  # global policy
            out = "Package files:\n 100 /var/lib/dpkg/status\n     release a=now\n"
            for src, origin in self.origins.items():
                out += f" 500 {src}\n     release v=24.04,o={origin},a=noble,c=main,b=amd64\n"
            return ok(out)
        pkg = args[1]
        versions = self.repo.get(pkg)
        if not versions:
            return ok(f"N: Unable to locate package {pkg}\n")
        cand = sorted(versions)[-1]
        out = f"{pkg}:\n  Installed: {self.installed.get(pkg, '(none)')}\n  Candidate: {cand}\n  Version table:\n"
        for ver, sources in sorted(versions.items(), reverse=True):
            out += f" {'***' if self.installed.get(pkg) == ver else '   '} {ver} 500\n"
            for s in sources:
                out += f"        500 {s}\n"
        return ok(out)

    def _h_apt_get(self, args: list[str], stdin: str | None) -> CommandResult:
        verb = args[0]
        if verb == "update":
            return ok("Reading package lists...")
        spec = args[-1]
        pkg, _, ver = spec.partition("=")
        if verb == "install":
            versions = self.repo.get(pkg)
            if not versions:
                return err(f"E: Unable to locate package {pkg}", 100)
            ver = ver or sorted(versions)[-1]
            if ver not in versions:
                return err(f"E: Version '{ver}' for '{pkg}' was not found", 100)
            self.installed[pkg] = ver
            unit = self.pkg_units.get(pkg)
            if unit and unit not in self.units:
                self.units[unit] = Unit(active="inactive", enabled=False, package=pkg)
            return ok(f"Setting up {pkg} ({ver}) ...")
        if verb in ("remove", "purge"):
            self.installed.pop(pkg, None)
            unit = self.pkg_units.get(pkg)
            if unit:
                self.units.pop(unit, None)
            return ok()
        return err("E: invalid operation", 100)

    # ----------------------------------------------------------------- files

    def _h_cat(self, args: list[str], stdin: str | None) -> CommandResult:
        path = args[-1]
        if path == "/proc/loadavg":
            return ok("0.52 0.40 0.31 1/123 4567\n")
        if path == "/proc/meminfo":
            return ok(
                "MemTotal:        2048000 kB\nMemFree:          100000 kB\nMemAvailable:    1024000 kB\n"
            )
        if path in self.files:
            return ok(self.files[path])
        return err(f"cat: {path}: No such file or directory")

    def _h_nproc(self, args: list[str], stdin: str | None) -> CommandResult:
        return ok("2\n")

    def _h_df(self, args: list[str], stdin: str | None) -> CommandResult:
        if any(a.startswith("--output") for a in args):
            paths = [a for a in args if a.startswith("/")]
            lines = ["Avail Mounted on"] + [f"{self.free_mb.get(p, 20_000)}M {p}" for p in paths]
            return ok("\n".join(lines).replace("M ", " ") + "\n")
        return ok(
            "Filesystem 1-blocks Used Available Capacity Mounted on\n"
            "/dev/vda1 42000000000 21000000000 21000000000 50% /\n"
        )

    def _h_mktemp(self, args: list[str], stdin: str | None) -> CommandResult:
        self._tmp += 1
        return ok(f"/tmp/vps-mcp.AbCd{self._tmp:04d}\n")

    def _h_tee(self, args: list[str], stdin: str | None) -> CommandResult:
        self.files[args[-1]] = stdin or ""
        return ok(stdin or "")

    def _h_install(self, args: list[str], stdin: str | None) -> CommandResult:
        src, dst = args[-2], args[-1]
        if src not in self.files:
            return err(f"install: cannot stat '{src}'")
        self.files[dst] = self.files[src]
        return ok()

    def _h_rm(self, args: list[str], stdin: str | None) -> CommandResult:
        path = args[-1]
        for p in [p for p in self.files if p == path or p.startswith(path + "/")]:
            del self.files[p]
        return ok()

    def _h_nginx(self, args: list[str], stdin: str | None) -> CommandResult:
        target = args[args.index("-c") + 1] if "-c" in args else "/etc/nginx/nginx.conf"
        if "INVALID" in self.files.get(target, ""):
            return err(f'nginx: [emerg] unknown directive "INVALID" in {target}:1')
        return ok("", "nginx: configuration file test is successful")

    # ---------------------------------------------------------------- docker

    def _inspect_json(self, c: Container) -> dict[str, Any]:
        s = c.spec
        env = [f"{k}={v}" for k, v in s["env"].items()] + ["PATH=/usr/local/bin:/usr/bin"]
        bindings: dict[str, list[dict[str, str]]] = {}
        for p in s["ports"]:
            parts = p.split(":")
            proto = "udp" if p.endswith("/udp") else "tcp"
            ctr = parts[-1].split("/")[0]
            ip = parts[0] if len(parts) == 3 else ""
            bindings[f"{ctr}/{proto}"] = [{"HostIp": ip, "HostPort": parts[-2]}]
        mounts = []
        for v in s["volumes"]:
            src, dst, *mode = v.split(":")
            mounts.append(
                {
                    "Type": "bind" if src.startswith("/") else "volume",
                    "Name": None if src.startswith("/") else src,
                    "Source": src,
                    "Destination": dst,
                    "RW": not mode or mode[0] != "ro",
                }
            )
        health = {"Status": c.health} if c.health else None
        return {
            "Name": "/" + c.name,
            "Image": "sha256:" + "a" * 64,
            "Config": {
                "Image": s["image"],
                "Env": env,
                "Cmd": s.get("command"),
                "Labels": {"io.vps-mcp.managed": "true"},
                "Healthcheck": {
                    "Test": ["CMD-SHELL", s["health_cmd"]],
                    "Interval": 30 * 10**9,
                    "Retries": 3,
                }
                if s.get("health_cmd")
                else None,
            },
            "State": {
                "Status": c.status,
                "Running": c.status == "running",
                "ExitCode": c.exit_code,
                "StartedAt": c.started.isoformat().replace("+00:00", "Z"),
                "FinishedAt": "0001-01-01T00:00:00Z",
                "Health": health,
            },
            "HostConfig": {"RestartPolicy": {"Name": s["restart"]}, "PortBindings": bindings},
            "Mounts": mounts,
            "NetworkSettings": {"Networks": {n: {} for n in c.networks}},
        }

    def _run_container(self, name: str, c: Container) -> None:
        image = c.spec["image"]
        if image in self.crash_images or c.spec["env"].get("CRASH") == "1":
            c.status, c.exit_code = "exited", 1
            c.logs.append((NOW, "stderr", "fatal: crashed on start"))
        else:
            c.status, c.exit_code, c.started = "running", 0, NOW
            c.health = (
                "unhealthy"
                if image in self.unhealthy_images
                else ("healthy" if c.spec.get("health_cmd") else None)
            )

    def _h_docker(self, args: list[str], stdin: str | None) -> CommandResult:
        sub = args[0]
        if sub == "container" and args[1] == "inspect":
            names = args[4:]
            out, errs = [], []
            for n in names:
                c = self.containers.get(n)
                if c:
                    out.append(json.dumps(self._inspect_json(c)))
                else:
                    errs.append(f"Error: No such container: {n}")
            return CommandResult(
                1 if errs else 0, "\n".join(out) + ("\n" if out else ""), "\n".join(errs)
            )
        if sub in ("start", "restart"):
            c = self.containers.get(args[1])
            if not c:
                return err(f"Error response from daemon: No such container: {args[1]}")
            self._run_container(args[1], c)
            return ok(args[1])
        if sub == "stop":
            c = self.containers.get(args[1])
            if not c:
                return err(f"Error response from daemon: No such container: {args[1]}")
            c.status, c.exit_code = "exited", 0
            return ok(args[1])
        if sub == "update":
            c = self.containers[args[-1]]
            c.spec["restart"] = args[args.index("--restart") + 1]
            return ok()
        if sub == "logs":
            name = args[-1]
            c = self.containers.get(name)
            if not c:
                return err(f"Error: No such container: {name}")
            n = int(args[args.index("--tail") + 1])
            entries = c.logs[-n:]
            fmt = lambda e: f"{e[0].isoformat().replace('+00:00', '.123456789Z')} {e[2]}"  # noqa: E731
            return ok(
                "\n".join(fmt(e) for e in entries if e[1] == "stdout"),
                "\n".join(fmt(e) for e in entries if e[1] == "stderr"),
            )
        if sub == "stats":
            rows = [
                json.dumps({"Name": n, "CPUPerc": "1.50%", "MemUsage": "64MiB / 1.9GiB"})
                for n in args[3:]
                if n in self.containers
            ]
            return ok("\n".join(rows))
        if sub == "image" and args[1] == "inspect":
            return ok("[]") if args[2] in self.images else err(f"Error: No such image: {args[2]}")
        if sub == "pull":
            if args[1] not in self.registry:
                return err(f"Error response from daemon: manifest for {args[1]} not found")
            self.images.add(args[1])
            return ok()
        if sub == "rmi":
            self.images.discard(args[1])
            return ok()
        if sub == "rm":
            name = args[-1]
            self.containers.pop(name, None)
            return ok()
        if sub == "rename":
            old, new = args[1], args[2]
            if old not in self.containers:
                return err(f"Error: No such container: {old}")
            c = self.containers.pop(old)
            c.name = new
            self.containers[new] = c
            return ok()
        if sub == "run":
            return self._docker_run(args[1:], stdin)
        if sub == "network":
            return self._docker_network(args[1:])
        if sub == "ps":
            net = args[args.index("--filter") + 1].split("=", 1)[1]
            names = [c.name for c in self.containers.values() if net in c.networks]
            return ok("\n".join(names))
        if sub == "volume" and args[1] == "rm":
            self.volumes.discard(args[2])
            return ok()
        if sub == "info":
            return ok("/var/lib/docker\n")
        return err(f"unknown docker command {args}")

    def _docker_run(self, args: list[str], stdin: str | None) -> CommandResult:
        spec: dict[str, Any] = {
            "env": {},
            "ports": [],
            "volumes": [],
            "restart": "no",
            "command": None,
            "health_cmd": None,
        }
        name, network = "", "bridge"
        i = 0
        while i < len(args):
            a = args[i]
            if a == "-d":
                i += 1
                continue
            if a in (
                "--name",
                "--label",
                "--restart",
                "--env-file",
                "-p",
                "-v",
                "--network",
                "--health-cmd",
                "--health-interval",
                "--health-retries",
            ):
                val = args[i + 1]
                if a == "--name":
                    name = val
                elif a == "--restart":
                    spec["restart"] = val
                elif a == "--env-file":
                    for line in (stdin or "").splitlines():
                        k, _, v = line.partition("=")
                        spec["env"][k] = v
                elif a == "-p":
                    spec["ports"].append(val)
                elif a == "-v":
                    spec["volumes"].append(val)
                elif a == "--network":
                    network = val
                elif a == "--health-cmd":
                    spec["health_cmd"] = val
                i += 2
                continue
            spec["image"] = a
            spec["command"] = args[i + 1 :] or None
            break
        if name in self.containers:
            return err(f'Conflict. The container name "/{name}" is already in use', 125)
        if spec.get("image") not in self.images:
            return err(f"Unable to find image '{spec.get('image')}' locally", 125)
        if network not in self.networks:
            return err(f"network {network} not found", 125)
        for v in spec["volumes"]:
            if not v.startswith("/"):
                self.volumes.add(v.split(":")[0])
        c = Container(name=name, spec=spec, networks=[network])
        self.containers[name] = c
        self._run_container(name, c)
        return ok("f" * 64)

    def _docker_network(self, args: list[str]) -> CommandResult:
        verb = args[0]
        if verb == "inspect":
            net = args[-1]
            if net not in self.networks:
                return err(f"Error: No such network: {net}")
            if "--format" in args:
                return ok(json.dumps(self.networks[net] or None) + "\n")
            return ok("[{}]")
        if verb == "create":
            net = args[-1]
            labels = {}
            if "--label" in args:
                k, _, v = args[args.index("--label") + 1].partition("=")
                labels[k] = v
            self.networks[net] = labels
            return ok("n" * 64)
        if verb == "connect":
            net, name = args[1], args[2]
            if net not in self.networks:
                return err(f"Error: No such network: {net}")
            self.containers[name].networks.append(net)
            return ok()
        if verb == "rm":
            net = args[1]
            if any(net in c.networks for c in self.containers.values()):
                return err(f"error: network {net} has active endpoints")
            self.networks.pop(net, None)
            return ok(net)
        return err("unknown network command")


def make_status_lines(text: str) -> list[str]:
    return [line for line in re.split(r"\n", text) if line]
