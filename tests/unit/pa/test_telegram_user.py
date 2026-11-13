"""Unit tests for Telegram user integration (feature 012)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from assistant.telegram_user import (
    TelegramCredentials,
    TelegramUserAuthError,
    TelegramUserClient,
    format_inbox,
    format_messages,
    wrap_untrusted,
)
from tests.assistant_fakes import config


class FakeSender:
    def __init__(self, first_name: str, id: int = 123) -> None:
        self.first_name = first_name
        self.id = id


class FakeChat:
    def __init__(self, title: str, id: int = 456) -> None:
        self.title = title
        self.id = id


class FakeMessage:
    def __init__(
        self,
        id: int,
        chat_id: int,
        sender: Any,
        text: str,
        out: bool = False,
        photo: bool = False,
        doc: bool = False,
        voice: bool = False,
    ) -> None:
        self.id = id
        self.chat_id = chat_id
        self.chat = FakeChat("Test Chat", chat_id)
        self.sender = sender
        self.sender_id = sender.id if sender else None
        self.message = text
        self.date = datetime(2026, 11, 13, 10, 0, 0, tzinfo=UTC)
        self.out = out
        self.photo = photo
        self.document = doc
        self.voice = voice


class FakeDialog:
    def __init__(
        self, id: int, name: str, unread_count: int, last_msg: str, is_user: bool = True
    ) -> None:
        self.id = id
        self.name = name
        self.unread_count = unread_count
        self.is_user = is_user
        self.is_group = False
        self.is_channel = False
        self.message = FakeMessage(id=1, chat_id=id, sender=None, text=last_msg)


class FakeTelethonClient:
    def __init__(self, authorized: bool = True) -> None:
        self._authorized = authorized
        self.connected = False
        self.disconnected = False
        self.sent: list[tuple[Any, str]] = []

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnected = True

    async def is_user_authorized(self) -> bool:
        return self._authorized

    async def iter_dialogs(self, limit: int = 20) -> Any:
        dialogs = [
            FakeDialog(id=101, name="Alice", unread_count=2, last_msg="Hey there!"),
            FakeDialog(id=102, name="Bob", unread_count=0, last_msg="See you"),
            FakeDialog(id=103, name="News Channel", unread_count=5, last_msg="Breaking news"),
        ]
        for d in dialogs[:limit]:
            yield d

    async def get_entity(self, target: Any) -> Any:
        if str(target).lower() in ("me", "saved", "self"):
            return FakeSender("Saved Messages", id=777)
        if target in (101, "101", "@alice", "Alice"):
            return FakeChat("Alice", id=101)
        if target in (999, "unknown"):
            raise ValueError(f"Cannot find any entity corresponding to {target}")
        return FakeChat(str(target), id=555)

    async def iter_messages(self, entity: Any = None, search: str = "", limit: int = 10) -> Any:
        msgs = [
            FakeMessage(
                id=1,
                chat_id=101,
                sender=FakeSender("Alice"),
                text=f"Match 1 for {search}" if search else "First message",
            ),
            FakeMessage(
                id=2,
                chat_id=101,
                sender=FakeSender("Me"),
                text="Photo attached",
                out=True,
                photo=True,
            ),
        ]
        for m in msgs[:limit]:
            yield m

    async def send_message(self, entity: Any, text: str) -> FakeMessage:
        self.sent.append((entity, text))
        return FakeMessage(id=888, chat_id=101, sender=FakeSender("Me"), text=text, out=True)


def test_credentials_load(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    cfg.telegram_user.session_path = str(tmp_path / "sess.session")

    def secrets_ok(key: str) -> str:
        if key == "telegram_api_id":
            return "123456"
        if key == "telegram_api_hash":
            return "abcdef123456"
        return ""

    creds = TelegramCredentials.load(cfg, secrets_ok)
    assert creds.api_id == 123456
    assert creds.api_hash == "abcdef123456"
    assert creds.session_path == (tmp_path / "sess.session").resolve()

    # Missing secrets
    with pytest.raises(TelegramUserAuthError, match="credentials missing"):
        TelegramCredentials.load(cfg, lambda k: "")

    # Invalid integer
    with pytest.raises(TelegramUserAuthError, match="must be an integer"):
        TelegramCredentials.load(
            cfg,
            lambda k: "not_a_number" if k == "telegram_api_id" else "hash",
        )


def test_wrap_untrusted() -> None:
    wrapped = wrap_untrusted("Hello <script>alert(1)</script>", "Alice", "Dev Group")
    assert '[untrusted Telegram message from "Alice" in chat "Dev Group"' in wrapped
    assert "&lt;script&gt;" in wrapped
    assert "<message_content>" in wrapped


def test_format_inbox() -> None:
    dialogs = [
        {
            "id": 101,
            "title": "Alice",
            "unread_count": 3,
            "last_date": "2026-11-13T10:00:00Z",
            "last_message": "Can you check this?",
        }
    ]
    formatted = format_inbox(dialogs, "My Inbox")
    assert "📬 **My Inbox** (1):" in formatted
    assert "• **Alice** (3 unread) [ID: `101`] · 2026-11-13T10:00" in formatted
    assert "_Can you check this?_" in formatted

    empty = format_inbox([])
    assert "No unread chats" in empty


def test_format_messages() -> None:
    msgs = [
        {
            "sender_name": "Alice",
            "date": "2026-11-13T10:00:00Z",
            "text": "Hi!",
            "media_type": "",
        },
        {
            "sender_name": "Bob",
            "date": "2026-11-13T10:01:00Z",
            "text": "Photo here",
            "media_type": "photo",
        },
    ]
    formatted = format_messages(msgs, "Project X")
    assert "💬 Messages in **Project X** (2):" in formatted
    assert "**Bob** (2026-11-13T10:01) [photo]:" in formatted


@pytest.mark.asyncio
async def test_telegram_user_client_flow(tmp_path: Path) -> None:
    creds = TelegramCredentials(api_id=123, api_hash="hash", session_path=tmp_path / "sess.session")
    raw = FakeTelethonClient(authorized=True)
    client = TelegramUserClient(creds, client=raw)

    # 1. list_inbox with unread filter
    unread = await client.list_inbox(unread_only=True)
    assert len(unread) == 2
    assert unread[0]["title"] == "Alice"
    assert unread[1]["title"] == "News Channel"

    # list_inbox with query filter
    matched_query = await client.list_inbox(query="News")
    assert len(matched_query) == 1
    assert matched_query[0]["title"] == "News Channel"

    # list_inbox without unread filter
    all_dialogs = await client.list_inbox(unread_only=False)
    assert len(all_dialogs) == 3

    # 2. search_messages
    res = await client.search_messages("Alice")
    assert len(res["chats"]) == 1
    assert res["chats"][0]["title"] == "Alice"
    assert len(res["messages"]) == 2
    assert "[untrusted Telegram message" in res["messages"][0]["text"]
    assert res["messages"][0]["sender_name"] == "Alice"

    # 3. read_history
    history = await client.read_history("@alice", limit=5)
    assert len(history) == 2
    assert history[1]["media_type"] == "photo"

    # 4. send_message
    res = await client.send_message("@alice", "Testing 123")
    assert res["message_id"] == 888
    assert res["text"] == "Testing 123"
    assert len(raw.sent) == 1

    # 5. save_to_saved
    res_saved = await client.save_to_saved("Remember to buy milk")
    assert res_saved["recipient"] == "Saved Messages"
    assert len(raw.sent) == 2

    # 6. aclose
    await client.aclose()
    assert raw.disconnected is True
