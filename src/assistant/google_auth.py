"""Google OAuth 2.0 token management and authorization flow (feature 011)."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx2

from assistant.config import Config

log = logging.getLogger(__name__)

CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events"
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
SCOPES = [CALENDAR_SCOPE, GMAIL_SCOPE]
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
REDIRECT_URI = "http://localhost"


class GoogleAuthError(Exception):
    pass


@dataclass
class GoogleCredentials:
    client_id: str
    client_secret: str
    refresh_token: str

    @classmethod
    def load(cls, cfg: Config, get_secret: Callable[[str], str]) -> GoogleCredentials:
        """Load credentials from JSON file or Keychain secrets."""
        client_id = ""
        client_secret = ""

        # Check credentials_path first (standard Google Cloud Console JSON)
        path = Path(cfg.google.credentials_path).expanduser()
        if path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                info = raw.get("installed") or raw.get("web") or raw
                client_id = str(info.get("client_id", "")).strip()
                client_secret = str(info.get("client_secret", "")).strip()
            except Exception as exc:
                log.warning("failed to parse google credentials file %s: %s", path, exc)

        if not client_id:
            try:
                client_id = get_secret(cfg.google.client_id_secret)
            except Exception:
                client_id = ""
        if not client_secret:
            try:
                client_secret = get_secret(cfg.google.client_secret_secret)
            except Exception:
                client_secret = ""

        try:
            refresh_token = get_secret(cfg.google.token_secret)
        except Exception:
            refresh_token = ""

        if not client_id or not client_secret:
            raise GoogleAuthError(
                "Google client credentials not found. Provide a client JSON file at "
                f"{cfg.google.credentials_path} or store {cfg.google.client_id_secret} "
                f"and {cfg.google.client_secret_secret} in your secrets provider."
            )
        if not refresh_token:
            raise GoogleAuthError(
                "Google refresh token not found. Run 'assistant google-auth' to authenticate."
            )

        return cls(client_id=client_id, client_secret=client_secret, refresh_token=refresh_token)


class GoogleAuth:
    """Manages in-memory access tokens refreshed automatically via Google's token endpoint."""

    def __init__(
        self,
        creds: GoogleCredentials,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.creds = creds
        self.timeout = timeout
        self.transport = transport
        self._access_token: str | None = None
        self._expires_at: float = 0.0

    async def get_access_token(self) -> str:
        """Return a valid access token, refreshing if within 60s of expiration."""
        now = time.monotonic()
        if self._access_token and now < (self._expires_at - 60.0):
            return self._access_token

        kw: dict[str, Any] = {"timeout": httpx2.Timeout(self.timeout, connect=10.0)}
        if self.transport is not None:
            kw["transport"] = self.transport

        async with httpx2.AsyncClient(**kw) as client:
            try:
                resp = await client.post(
                    TOKEN_URL,
                    data={
                        "client_id": self.creds.client_id,
                        "client_secret": self.creds.client_secret,
                        "refresh_token": self.creds.refresh_token,
                        "grant_type": "refresh_token",
                    },
                )
            except Exception as exc:
                raise GoogleAuthError(f"failed to connect to Google OAuth: {exc}") from exc

            if resp.status_code != 200:
                try:
                    err_msg = resp.json().get("error_description") or resp.text
                except Exception:
                    err_msg = resp.text
                raise GoogleAuthError(
                    f"Google token refresh failed (HTTP {resp.status_code}): {err_msg[:200]}"
                )

            data = resp.json()
            token = data.get("access_token")
            if not token:
                raise GoogleAuthError("Google token endpoint returned no access_token")

            expires_in = float(data.get("expires_in", 3600.0))
            self._access_token = str(token)
            self._expires_at = time.monotonic() + expires_in
            return self._access_token

    async def auth_headers(self) -> dict[str, str]:
        """HTTP headers with valid Bearer authorization."""
        token = await self.get_access_token()
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }


def make_auth_url(client_id: str, redirect_uri: str = REDIRECT_URI) -> str:
    """Generate the user authorization URL."""
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
    }
    return f"{AUTH_URL}?{urlencode(params)}"


async def exchange_auth_code(
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str = REDIRECT_URI,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> str:
    """Exchange an authorization code for a long-lived refresh token."""
    kw: dict[str, Any] = {"timeout": httpx2.Timeout(30.0, connect=10.0)}
    if transport is not None:
        kw["transport"] = transport

    async with httpx2.AsyncClient(**kw) as client:
        resp = await client.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code.strip(),
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
        )
        if resp.status_code != 200:
            try:
                err = resp.json().get("error_description") or resp.text
            except Exception:
                err = resp.text
            raise GoogleAuthError(
                f"OAuth code exchange failed (HTTP {resp.status_code}): {err[:200]}"
            )

        data = resp.json()
        refresh_token = data.get("refresh_token")
        if not refresh_token:
            raise GoogleAuthError(
                "Google did not return a refresh_token. If you already authorized previously, "
                "revoke access at https://myaccount.google.com/permissions and retry with prompt=consent."
            )
        return str(refresh_token)
