"""Secret providers: macOS Keychain or a private file (research R14)."""

from __future__ import annotations

import stat
import subprocess
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from vps_mcp.remote.redact import register_secret

SECURITY = "/usr/bin/security"
NAMES = ("telegram_bot_token", "anthropic_api_key")  # defaults; see required()


class SecretMissing(Exception):
    def __init__(self, name: str, hint: str) -> None:
        super().__init__(f"secret '{name}' is missing; {hint}")
        self.name = name
        self.hint = hint


class SecretProvider(Protocol):
    def get(self, name: str) -> str: ...
    def hint(self, name: str) -> str: ...


class KeychainSecrets:
    def __init__(
        self,
        service: str,
        reader: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        self.service = service
        self._reader = reader or _run
        self._cache: dict[str, str] = {}

    def hint(self, name: str) -> str:
        return (
            f"store it (you will be prompted, nothing is echoed): "
            f"security add-generic-password -U -s {self.service} -a {name} -w"
        )

    def get(self, name: str) -> str:
        if name in self._cache:
            return self._cache[name]
        try:
            proc = self._reader(
                [SECURITY, "find-generic-password", "-s", self.service, "-a", name, "-w"]
            )
        except (OSError, subprocess.SubprocessError):
            raise SecretMissing(name, self.hint(name)) from None
        value = (proc.stdout or "").strip()
        if proc.returncode != 0 or not value:
            raise SecretMissing(name, self.hint(name))
        register_secret(value)
        self._cache[name] = value
        return value


class FileSecrets:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self._data: dict[str, str] | None = None

    def hint(self, name: str) -> str:
        return f'add `{name} = "..."` to {self.path} (mode 0600)'

    def _load(self) -> dict[str, str]:
        if self._data is None:
            try:
                st = self.path.stat()
            except FileNotFoundError:
                self._data = {}
                return self._data
            if stat.S_IMODE(st.st_mode) & 0o077:
                raise PermissionError(f"{self.path} must not be readable by others (chmod 600)")
            raw = tomllib.loads(self.path.read_text())
            self._data = {k: str(v) for k, v in raw.items()}
            for v in self._data.values():
                register_secret(v)
        return self._data

    def get(self, name: str) -> str:
        value = self._load().get(name, "").strip()
        if not value:
            raise SecretMissing(name, self.hint(name))
        return value


def provider_for(kind: str, service: str, file: str | None) -> SecretProvider:
    if kind == "file":
        return FileSecrets(file or "~/.config/assistant/secrets.toml")
    return KeychainSecrets(service)


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, timeout=10, check=False)  # noqa: S603
