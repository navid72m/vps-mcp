"""The GitLab token, read from the macOS Keychain at first use (research G2).

The token is never written anywhere by vps-mcp. As soon as it is loaded it is registered with
the process-wide redaction registry, so tool output, audit records and logs scrub it.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable

from vps_mcp.errors import ErrorCategory, VpsMcpError
from vps_mcp.remote.redact import register_secret

SECURITY = "/usr/bin/security"
TIMEOUT_S = 10


def setup_hint(service: str, account: str) -> str:
    return (
        "store your GitLab access token in the macOS Keychain (it prompts for the token, so it "
        f"never appears on a command line): security add-generic-password -U -s {service} "
        f"-a {account} -w"
    )


class TokenMissing(VpsMcpError):
    def __init__(self, service: str, account: str, why: str) -> None:
        super().__init__(
            ErrorCategory.PERMISSION_DENIED,
            f"GitLab token unavailable ({why}); {setup_hint(service, account)}",
            subtype="gitlab_token_missing",
        )


class KeychainToken:
    def __init__(
        self,
        service: str,
        account: str,
        *,
        reader: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
        platform: str | None = None,
    ) -> None:
        self.service = service
        self.account = account
        self._reader = reader or _run
        self._platform = platform or sys.platform
        self._token: str | None = None

    def argv(self) -> list[str]:
        return [SECURITY, "find-generic-password", "-s", self.service, "-a", self.account, "-w"]

    def get(self) -> str:
        if self._token is not None:
            return self._token
        if self._platform != "darwin":
            raise TokenMissing(self.service, self.account, "the Keychain is only on macOS")
        try:
            proc = self._reader(self.argv())
        except (OSError, subprocess.SubprocessError):
            raise TokenMissing(self.service, self.account, "Keychain lookup failed") from None
        token = (proc.stdout or "").strip()
        if proc.returncode != 0 or not token:
            raise TokenMissing(self.service, self.account, "no Keychain entry found")
        register_secret(token)
        self._token = token
        return token

    def available(self) -> bool:
        try:
            self.get()
        except TokenMissing:
            return False
        return True


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        argv, capture_output=True, text=True, timeout=TIMEOUT_S, check=False
    )
