"""GitLab REST v4 client: auth header, error mapping, caps (research G1, G5)."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import quote

import httpx2

from vps_mcp.errors import ErrorCategory, VpsMcpError
from vps_mcp.gitlab.config import GitLabSettings
from vps_mcp.gitlab.credentials import KeychainToken
from vps_mcp.remote.redact import scrub

PER_PAGE = 100


def project_id(ref: int | str) -> str:
    return str(ref) if isinstance(ref, int) else quote(ref, safe="")


def enc(segment: str) -> str:
    return quote(segment, safe="")


class GitLabClient:
    def __init__(
        self,
        settings: GitLabSettings,
        token: KeychainToken,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.token = token
        self._transport = transport
        self._client: httpx2.AsyncClient | None = None

    def _http(self) -> httpx2.AsyncClient:
        if self._client is None:
            kw: dict[str, Any] = {
                "base_url": f"{self.settings.url}/api/v4",
                "timeout": httpx2.Timeout(self.settings.timeout_s, connect=10.0),
                "follow_redirects": False,
            }
            if self._transport is not None:
                kw["transport"] = self._transport
            elif self.settings.ca_bundle:
                kw["verify"] = self.settings.ca_bundle
            self._client = httpx2.AsyncClient(**kw)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------ errors

    def _raise_for(self, resp: httpx2.Response) -> None:
        code = resp.status_code
        if code < 400:
            return
        detail = _error_detail(resp)
        if code == 401:
            raise VpsMcpError(
                ErrorCategory.PERMISSION_DENIED,
                "GitLab rejected the token (expired or revoked?); replace it in the Keychain "
                f"(security add-generic-password -U -s {self.token.service} "
                f"-a {self.token.account} -w)",
                subtype="gitlab_auth",
            )
        if code == 403:
            raise VpsMcpError(
                ErrorCategory.PERMISSION_DENIED,
                "GitLab refused the request: the token lacks a scope or role for it"
                + (f" ({detail})" if detail else ""),
                subtype="gitlab_scope",
            )
        if code == 404:
            raise VpsMcpError(ErrorCategory.NOT_FOUND, "not found on GitLab")
        if code == 429:
            retry = resp.headers.get("Retry-After", "60")
            raise VpsMcpError(
                ErrorCategory.TIMED_OUT,
                f"GitLab rate limit reached; retry after {retry}s",
                subtype="gitlab_rate_limited",
                extra={"retry_after_s": int(retry) if retry.isdigit() else 60},
            )
        if code in (400, 409, 422):
            raise VpsMcpError(
                ErrorCategory.INVALID_INPUT,
                "GitLab rejected the request" + (f": {detail}" if detail else ""),
            )
        raise VpsMcpError(ErrorCategory.FAILED, f"GitLab error {code}")

    async def _request(self, method: str, path: str, **kw: Any) -> httpx2.Response:
        headers = {"PRIVATE-TOKEN": self.token.get()}
        try:
            resp = await self._http().request(method, path, headers=headers, **kw)
        except httpx2.TimeoutException:
            raise VpsMcpError(ErrorCategory.TIMED_OUT, "GitLab did not answer in time") from None
        except httpx2.HTTPError as exc:
            raise VpsMcpError(
                ErrorCategory.CONNECTION_FAILURE,
                f"cannot reach GitLab ({exc.__class__.__name__})",
                subtype="gitlab_unreachable",
            ) from None
        self._raise_for(resp)
        return resp

    # ---------------------------------------------------------------- requests

    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return (await self._request("GET", path, params=_clean(params))).json()

    async def get_text(self, path: str, params: dict[str, Any] | None = None) -> bytes:
        return (await self._request("GET", path, params=_clean(params))).content

    async def post_json(self, path: str, data: dict[str, Any] | None = None) -> Any:
        return (await self._request("POST", path, json=_clean(data))).json()

    async def put_json(self, path: str, data: dict[str, Any] | None = None) -> Any:
        return (await self._request("PUT", path, json=_clean(data))).json()

    async def paged(
        self, path: str, params: dict[str, Any] | None, cap: int
    ) -> tuple[list[Any], bool]:
        """Collect up to ``cap`` items across pages; returns ``(items, truncated)``."""
        items: list[Any] = []
        page = 1
        while True:
            q = {**(params or {}), "per_page": min(PER_PAGE, cap + 1), "page": page}
            resp = await self._request("GET", path, params=_clean(q))
            batch = resp.json()
            if not isinstance(batch, list):
                return items, False
            items.extend(batch)
            if len(items) > cap:
                return items[:cap], True
            if not resp.headers.get("X-Next-Page") or not batch:
                return items, False
            page += 1

    @contextlib.asynccontextmanager
    async def stream(
        self, path: str, params: dict[str, Any] | None = None
    ) -> AsyncIterator[httpx2.Response]:
        headers = {"PRIVATE-TOKEN": self.token.get()}
        timeout = httpx2.Timeout(300.0, connect=10.0)
        try:
            async with self._http().stream(
                "GET", path, headers=headers, params=_clean(params), timeout=timeout
            ) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    self._raise_for(resp)
                yield resp
        except httpx2.TimeoutException:
            raise VpsMcpError(ErrorCategory.TIMED_OUT, "GitLab did not answer in time") from None
        except httpx2.HTTPError as exc:
            raise VpsMcpError(
                ErrorCategory.CONNECTION_FAILURE,
                f"cannot reach GitLab ({exc.__class__.__name__})",
                subtype="gitlab_unreachable",
            ) from None


def _clean(d: dict[str, Any] | None) -> dict[str, Any] | None:
    if d is None:
        return None
    return {k: v for k, v in d.items() if v is not None}


def _error_detail(resp: httpx2.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return ""
    msg = body.get("message") or body.get("error") if isinstance(body, dict) else None
    if msg is None:
        return ""
    text = msg if isinstance(msg, str) else str(msg)
    return scrub(text, [])[:300]
