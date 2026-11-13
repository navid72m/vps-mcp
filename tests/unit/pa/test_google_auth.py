"""Unit tests for GoogleAuth token management and OAuth helpers."""

import json
from pathlib import Path

import httpx2
import pytest

from assistant.config import Config, GoogleSection, TelegramSection
from assistant.google_auth import (
    GoogleAuth,
    GoogleAuthError,
    GoogleCredentials,
    exchange_auth_code,
    make_auth_url,
)


def _config(creds_path: str = "") -> Config:
    return Config(
        telegram=TelegramSection(allowed_user_ids=[123]),
        google=GoogleSection(
            enabled=True,
            credentials_path=creds_path,
            client_id_secret="google_client_id",
            client_secret_secret="google_client_secret",
            token_secret="google_refresh_token",
        ),
    )


def test_credentials_load_from_secrets() -> None:
    secrets = {
        "google_client_id": "test_id",
        "google_client_secret": "test_secret",
        "google_refresh_token": "test_refresh",
    }
    creds = GoogleCredentials.load(_config(), lambda k: secrets[k])
    assert creds.client_id == "test_id"
    assert creds.client_secret == "test_secret"
    assert creds.refresh_token == "test_refresh"


def test_credentials_load_from_file(tmp_path: Path) -> None:
    f = tmp_path / "google_client.json"
    f.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "file_id",
                    "client_secret": "file_secret",
                }
            }
        )
    )
    secrets = {"google_refresh_token": "file_refresh"}
    creds = GoogleCredentials.load(_config(str(f)), lambda k: secrets[k])
    assert creds.client_id == "file_id"
    assert creds.client_secret == "file_secret"
    assert creds.refresh_token == "file_refresh"


def test_credentials_missing_raises() -> None:
    with pytest.raises(GoogleAuthError, match="client credentials not found"):
        GoogleCredentials.load(_config(), lambda k: "")


@pytest.mark.asyncio
async def test_google_auth_token_refresh() -> None:
    creds = GoogleCredentials("id1", "sec1", "ref1")

    call_count = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal call_count
        call_count += 1
        assert request.url == "https://oauth2.googleapis.com/token"
        content = request.read().decode("utf-8")
        assert "client_id=id1" in content
        assert "grant_type=refresh_token" in content
        return httpx2.Response(
            200,
            json={"access_token": "tok_xyz", "expires_in": 3600},
        )

    transport = httpx2.MockTransport(handler)
    auth = GoogleAuth(creds, transport=transport)

    token1 = await auth.get_access_token()
    assert token1 == "tok_xyz"
    assert call_count == 1

    # Second call uses in-memory token without HTTP request
    token2 = await auth.get_access_token()
    assert token2 == "tok_xyz"
    assert call_count == 1

    headers = await auth.auth_headers()
    assert headers["Authorization"] == "Bearer tok_xyz"


def test_make_auth_url() -> None:
    url = make_auth_url("my_client_id")
    assert "https://accounts.google.com/o/oauth2/v2/auth" in url
    assert "client_id=my_client_id" in url
    assert "calendar.events" in url
    assert "gmail.modify" in url


@pytest.mark.asyncio
async def test_exchange_auth_code() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url == "https://oauth2.googleapis.com/token"
        content = request.read().decode("utf-8")
        assert "code=auth_code_123" in content
        return httpx2.Response(
            200,
            json={"refresh_token": "new_refresh_token_abc"},
        )

    transport = httpx2.MockTransport(handler)
    ref = await exchange_auth_code("id1", "sec1", "auth_code_123", transport=transport)
    assert ref == "new_refresh_token_abc"
