"""Integration tests for personal Telegram user tools, approvals, and slash commands in runtime."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from assistant.cli import cmd_tg_auth
from assistant.telegram_user import TelegramCredentials, TelegramUserClient
from tests.assistant_fakes import harness, say, use
from tests.unit.pa.test_telegram_user import FakeTelethonClient


@pytest.fixture
def fake_client(tmp_path: Path) -> TelegramUserClient:
    creds = TelegramCredentials(
        api_id=12345, api_hash="fakehash", session_path=tmp_path / "sess.session"
    )
    raw = FakeTelethonClient(authorized=True)
    return TelegramUserClient(creds, client=raw)


@pytest.mark.asyncio
async def test_tool_registration(tmp_path: Path) -> None:
    # 1. Disabled
    h_off = await harness(tmp_path, telegram_user={"enabled": False})
    names_off = [s.name for s in h_off.a.agent.builtin_specs]
    assert not any(n.startswith("tg_user__") for n in names_off)

    # 2. Enabled
    h_on = await harness(tmp_path, telegram_user={"enabled": True})
    names_on = [s.name for s in h_on.a.agent.builtin_specs]
    assert "tg_user__inbox" in names_on
    assert "tg_user__search" in names_on
    assert "tg_user__read" in names_on
    assert "tg_user__send" in names_on
    assert "tg_user__save" in names_on


@pytest.mark.asyncio
async def test_runtime_tools_execution(tmp_path: Path, fake_client: TelegramUserClient) -> None:
    h = await harness(tmp_path, telegram_user={"enabled": True})
    h.a.tg_user_client = fake_client

    # 1. tg_user__inbox
    out, is_err = await h.a.builtin(
        "tg_user__inbox",
        {"unread_only": True},
        chat_id=111,
    )
    assert not is_err
    data = json.loads(out)
    assert len(data["content"]["dialogs"]) == 2
    assert data["content"]["dialogs"][0]["title"] == "Alice"

    # 2. tg_user__search
    out, is_err = await h.a.builtin(
        "tg_user__search",
        {"query": "update"},
        chat_id=111,
    )
    assert not is_err
    data = json.loads(out)
    assert len(data["content"]["messages"]) == 2
    assert "[untrusted Telegram message" in data["content"]["messages"][0]["text"]

    # 3. tg_user__read
    out, is_err = await h.a.builtin(
        "tg_user__read",
        {"chat": "@alice"},
        chat_id=111,
    )
    assert not is_err
    data = json.loads(out)
    assert len(data["content"]["messages"]) == 2

    # 4. tg_user__save
    out, is_err = await h.a.builtin(
        "tg_user__save",
        {"text": "Call doctor at 3pm"},
        chat_id=111,
    )
    assert not is_err
    data = json.loads(out)
    assert data["content"]["message_id"] == 888


@pytest.mark.asyncio
async def test_send_approval_flow(tmp_path: Path, fake_client: TelegramUserClient) -> None:
    h = await harness(tmp_path, telegram_user={"enabled": True})
    h.a.tg_user_client = fake_client

    h.provider.add(
        use("tg_user__send", {"recipient": "@alice", "message": "Hi Alice, see you tomorrow."}),
        say("Telegram message sent successfully."),
    )

    task = asyncio.create_task(h.say("Tell Alice on Telegram that I'll see her tomorrow"))
    for _ in range(200):
        if h.tg.sent and h.tg.buttons():
            break
        await asyncio.sleep(0.01)

    assert any("Send Telegram message from personal account" in m["text"] for m in h.tg.sent)
    data = next(d for label, d in h.tg.buttons() if label.startswith("✅"))
    await h.press(data)
    await h.finish()
    await task

    # Verify message was sent
    assert len(fake_client._raw_client.sent) == 1
    assert fake_client._raw_client.sent[0][1] == "Hi Alice, see you tomorrow."
    assert any("Telegram message sent successfully." in m["text"] for m in h.tg.sent)


@pytest.mark.asyncio
async def test_send_approval_rejection(tmp_path: Path, fake_client: TelegramUserClient) -> None:
    h = await harness(tmp_path, telegram_user={"enabled": True})
    h.a.tg_user_client = fake_client

    h.provider.add(
        use("tg_user__send", {"recipient": "@bob", "message": "Important contract details"}),
        say("I cancelled the send request."),
    )

    task = asyncio.create_task(h.say("Send message to Bob"))
    for _ in range(200):
        if h.tg.sent and h.tg.buttons():
            break
        await asyncio.sleep(0.01)

    data = next(d for label, d in h.tg.buttons() if label.startswith("❌"))
    await h.press(data)
    await h.finish()
    await task

    # Verified nothing sent
    assert len(fake_client._raw_client.sent) == 0


@pytest.mark.asyncio
async def test_slash_command_tg(tmp_path: Path, fake_client: TelegramUserClient) -> None:
    # 1. Enabled
    h = await harness(tmp_path, telegram_user={"enabled": True})
    h.a.tg_user_client = fake_client

    # /tg unread
    await h.say("/tg unread")
    await h.finish()
    assert any("📬 **Unread Chats**" in m["text"] for m in h.tg.sent)

    # /tg chats
    await h.say("/tg chats")
    await h.finish()
    assert any("📬 **Recent Chats**" in m["text"] for m in h.tg.sent)

    # /tg search query
    await h.say("/tg search report")
    await h.finish()
    assert any("🔍 Found 2 message(s)" in m["text"] for m in h.tg.sent)

    # /tg unknown
    await h.say("/tg invalid")
    await h.finish()
    assert any("Usage: /tg [unread|chats|search <query>]" in m["text"] for m in h.tg.sent)

    # 2. Disabled
    h_off = await harness(tmp_path, telegram_user={"enabled": False})
    await h_off.say("/tg")
    await h_off.finish()
    assert any("Personal Telegram integration is not enabled" in m["text"] for m in h_off.tg.sent)


def test_cli_tg_auth(tmp_path: Path, monkeypatch: Any) -> None:
    cfg_file = tmp_path / "assistant.toml"
    cfg_file.write_text(
        """
[assistant]
name = "Tester"

[telegram]
allowed_user_ids = [111]

[telegram_user]
enabled = true
session_path = "sess.session"
"""
    )

    # Mock provider_for
    class FakeProv:
        def get(self, name: str) -> str:
            if name == "telegram_api_id":
                return "12345"
            if name == "telegram_api_hash":
                return "hash123"
            return ""

    monkeypatch.setattr("assistant.secrets.provider_for", lambda *a, **kw: FakeProv())

    # Mock telethon.TelegramClient
    class MockCl:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def start(self) -> None:
            pass

        async def get_me(self) -> Any:
            class Me:
                id = 999
                first_name = "Navid"
                last_name = "Mir"
                username = "navid"

            return Me()

        async def disconnect(self) -> None:
            pass

    monkeypatch.setattr("telethon.TelegramClient", MockCl)

    args = argparse.Namespace(config=cfg_file)
    ret = cmd_tg_auth(args)
    assert ret == 0
