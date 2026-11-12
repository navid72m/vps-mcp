"""Config, secrets, state, telegram channel (T003-T005, T008)."""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

import pytest

from assistant.config import ConfigError, load
from assistant.secrets import FileSecrets, KeychainSecrets, SecretMissing
from assistant.state import State
from assistant.telegram import TelegramChannel, split_text
from tests.assistant_fakes import OWNER, TOKEN, FakeTelegram


def write(tmp: Path, text: str) -> Path:
    p = tmp / "a.toml"
    p.write_text(text)
    return p


GOOD = """
[assistant]
timezone = "Europe/Berlin"
heartbeat = "daily 21:00"
[telegram]
allowed_user_ids = [111]
[[server]]
name = "vps"
command = ["uv", "run", "vps-mcp"]
policy = { remove_service = "never" }
[[schedule]]
name = "morning"
cadence = "weekdays 08:00"
prompt = "Briefing please."
"""


def test_config_ok(tmp_path: Path) -> None:
    cfg = load(write(tmp_path, GOOD))
    assert cfg.server[0].policy["remove_service"] == "never"
    assert cfg.schedule[0].cadence == "weekdays 08:00"


@pytest.mark.parametrize(
    "bad",
    [
        GOOD.replace("allowed_user_ids = [111]", "allowed_user_ids = []"),
        GOOD.replace('"Europe/Berlin"', '"Mars/Base"'),
        GOOD.replace('"weekdays 08:00"', '"hourly"'),
        GOOD.replace('name = "vps"', 'name = "Bad Name"'),
        GOOD.replace('name = "vps"', 'name = "memory"'),
        GOOD.replace('"never"', '"sometimes"'),
        GOOD + "\n[surprise]\nx = 1\n",
        GOOD.replace('heartbeat = "daily 21:00"', 'heartbeat = "daily 25:00"'),
    ],
)
def test_config_errors(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ConfigError):
        load(write(tmp_path, bad))


def test_keychain_and_file_secrets(tmp_path: Path) -> None:
    calls = []

    def reader(argv: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        ok = argv[-2] == "telegram_bot_token"
        return subprocess.CompletedProcess(
            argv, 0 if ok else 44, "tok-123456789\n" if ok else "", ""
        )

    k = KeychainSecrets("svc", reader)
    assert k.get("telegram_bot_token") == "tok-123456789"
    assert calls[0] == [
        "/usr/bin/security",
        "find-generic-password",
        "-s",
        "svc",
        "-a",
        "telegram_bot_token",
        "-w",
    ]
    with pytest.raises(SecretMissing) as e:
        k.get("anthropic_api_key")
    assert "security add-generic-password -U -s svc -a anthropic_api_key -w" in str(e.value)
    f = tmp_path / "s.toml"
    f.write_text('anthropic_api_key = "sk-test-abcdefghijklmnop"\n')
    os.chmod(f, 0o644)
    with pytest.raises(PermissionError):
        FileSecrets(f).get("anthropic_api_key")
    os.chmod(f, 0o600)
    assert FileSecrets(f).get("anthropic_api_key").startswith("sk-test")
    with pytest.raises(SecretMissing):
        FileSecrets(f).get("telegram_bot_token")


def test_state_db_private_and_bounded(tmp_path: Path) -> None:
    st = State(tmp_path / "st" / "a.db")
    assert oct((tmp_path / "st" / "a.db").stat().st_mode & 0o777) == "0o600"
    c = st.conversation_new(1, "2026-11-12T09:00:00")
    st.add_turn(c["id"], "t", "user", "x" * 20000)
    assert len(st.recent_turns(c["id"], 5)[0]["text"]) == 8192
    for i in range(10):
        st.add_turn(c["id"], "t", "user", f"m{i}")
    dropped = st.drop_old_turns(c["id"], 4)
    assert len(dropped) == 7 and st.turn_count(c["id"]) == 4
    assert st.forget_all() == 1 and st.turn_count(c["id"]) == 0


def test_split_text() -> None:
    parts = split_text("\n\n".join("p" * 1500 for _ in range(6)))
    assert len(parts) > 1 and parts[0].startswith("(1/") and all(len(p) <= 4010 for p in parts)


async def test_channel_access_control_and_offset(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    st = State(tmp_path / "a.db")
    tg = FakeTelegram()
    ch = TelegramChannel(TOKEN, [OWNER], st, transport=tg.transport)
    tg.updates = [
        {
            "update_id": 5,
            "message": {
                "chat": {"id": OWNER, "type": "private"},
                "from": {"id": OWNER},
                "text": "hi",
                "date": 1,
            },
        },
        {
            "update_id": 6,
            "message": {
                "chat": {"id": 999, "type": "private"},
                "from": {"id": 999},
                "text": "let me in",
                "date": 1,
            },
        },
        {
            "update_id": 7,
            "message": {
                "chat": {"id": -1, "type": "group"},
                "from": {"id": OWNER},
                "text": "group",
                "date": 1,
            },
        },
        {
            "update_id": 8,
            "callback_query": {
                "id": "q",
                "from": {"id": 999},
                "data": "x",
                "message": {"message_id": 1, "chat": {"id": 999, "type": "private"}},
            },
        },
    ]
    got = await ch.poll_once()
    assert len(got) == 1 and got[0].text == "hi"  # type: ignore[union-attr]
    assert st.kv_get("tg_offset") == "9"
    denied = [r for r in st.audit_since("") if r["kind"] == "access_denied"]
    assert len(denied) == 3
    await ch.send(OWNER, "hello", [("OK", "cb")])
    assert tg.sent[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "cb"
    assert TOKEN not in caplog.text
    await ch.aclose()


async def test_channel_backoff(tmp_path: Path) -> None:
    st = State(tmp_path / "a.db")
    tg = FakeTelegram(fail=2)
    ch = TelegramChannel(TOKEN, [OWNER], st, transport=tg.transport)
    import asyncio

    import assistant.telegram as tmod

    sleeps: list[float] = []
    real = asyncio.sleep

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)
        await real(0)

    tmod.asyncio.sleep = fake_sleep  # type: ignore[attr-defined]
    try:
        tg.updates = [
            {
                "update_id": 1,
                "message": {
                    "chat": {"id": OWNER, "type": "private"},
                    "from": {"id": OWNER},
                    "text": "x",
                    "date": 1,
                },
            }
        ]
        gen = ch.updates()
        first = await gen.__anext__()
        assert first.text == "x"  # type: ignore[union-attr]
        assert len(sleeps) == 2 and sleeps[1] > sleeps[0] * 1.2
    finally:
        tmod.asyncio.sleep = real  # type: ignore[attr-defined]
    await ch.aclose()
