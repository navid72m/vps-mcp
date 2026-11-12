"""Background service definitions: launchd user agent (macOS) or systemd user unit (R12)."""

from __future__ import annotations

import os
import plistlib
import subprocess
from collections.abc import Callable
from pathlib import Path

LABEL = "io.github.vps-mcp.assistant"
Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False)  # noqa: S603


def program(uv: str, repo: Path, config: Path | None) -> list[str]:
    args = [uv, "run", "--directory", str(repo), "assistant", "run"]
    if config:
        args += ["--config", str(config)]
    return args


def render_launchd(uv: str, repo: Path, config: Path | None, state_dir: Path) -> bytes:
    logs = state_dir / "logs"
    return plistlib.dumps(
        {
            "Label": LABEL,
            "ProgramArguments": program(uv, repo, config),
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False, "NetworkState": True},
            "ThrottleInterval": 10,
            "ProcessType": "Background",
            "StandardOutPath": str(logs / "assistant.out.log"),
            "StandardErrorPath": str(logs / "assistant.err.log"),
            "EnvironmentVariables": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"},
        }
    )


def render_systemd(uv: str, repo: Path, config: Path | None) -> str:
    cmd = " ".join(program(uv, repo, config))
    return (
        "[Unit]\nDescription=Personal assistant (vps-mcp)\nAfter=network-online.target\n"
        "Wants=network-online.target\n\n[Service]\n"
        f"ExecStart={cmd}\nRestart=on-failure\nRestartSec=10\n\n"
        "[Install]\nWantedBy=default.target\n"
    )


def launchd_path(agents_dir: Path | None = None) -> Path:
    return (agents_dir or Path.home() / "Library" / "LaunchAgents") / f"{LABEL}.plist"


def install(
    uv: str,
    repo: Path,
    config: Path | None,
    state_dir: Path,
    *,
    systemd: bool = False,
    agents_dir: Path | None = None,
    runner: Runner = _run,
) -> str:
    (state_dir / "logs").mkdir(mode=0o700, parents=True, exist_ok=True)
    if systemd:
        unit_dir = agents_dir or Path.home() / ".config" / "systemd" / "user"
        unit_dir.mkdir(parents=True, exist_ok=True)
        unit = unit_dir / "assistant.service"
        unit.write_text(render_systemd(uv, repo, config))
        runner(["systemctl", "--user", "daemon-reload"])
        runner(["systemctl", "--user", "enable", "--now", "assistant.service"])
        return str(unit)
    path = launchd_path(agents_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(render_launchd(uv, repo, config, state_dir))
    domain = f"gui/{os.getuid()}"
    runner(["launchctl", "bootout", domain, str(path)])
    res = runner(["launchctl", "bootstrap", domain, str(path)])
    if res.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap failed: {res.stderr.strip()[:300]}")
    return str(path)


def uninstall(
    *, systemd: bool = False, agents_dir: Path | None = None, runner: Runner = _run
) -> str:
    if systemd:
        runner(["systemctl", "--user", "disable", "--now", "assistant.service"])
        unit = (agents_dir or Path.home() / ".config" / "systemd" / "user") / "assistant.service"
        unit.unlink(missing_ok=True)
        return str(unit)
    path = launchd_path(agents_dir)
    runner(["launchctl", "bootout", f"gui/{os.getuid()}", str(path)])
    path.unlink(missing_ok=True)
    return str(path)


def status(*, runner: Runner = _run) -> str:
    res = runner(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"])
    if res.returncode != 0:
        return "not installed or not running"
    state = next(
        (ln.strip() for ln in res.stdout.splitlines() if ln.strip().startswith("state")),
        "state = unknown",
    )
    pid = next((ln.strip() for ln in res.stdout.splitlines() if ln.strip().startswith("pid")), "")
    return f"{state} {pid}".strip()
