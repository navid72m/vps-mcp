"""SC-009: the test secrets appear nowhere (T035)."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest

from assistant.providers.scripted import ScriptedProvider
from tests.assistant_fakes import TOKEN, harness, say, use

API_KEY = "sk-ant-TESTKEY-0123456789abcdefghij"


async def test_no_secret_anywhere(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    from vps_mcp.remote.redact import register_secret

    register_secret(API_KEY)
    props = {
        "memories": [
            {
                "type": "semantic",
                "subject": "k.k",
                "title": "key",
                "content": f"the api key is {API_KEY}",
            }
        ]
    }
    p = ScriptedProvider(
        [
            use("ops__restart", {"service": f"web {API_KEY}"}),
            say(f"leaking {API_KEY} and {TOKEN}"),
            say(json.dumps(props)),
        ]
    )
    h = await harness(tmp_path, p, schedule=[{"name": "s", "cadence": "every 1h", "prompt": "x"}])
    await h.say("go")
    for _ in range(300):
        if any(t.startswith("🔐") for t in h.tg.texts()):
            break
        await asyncio.sleep(0.01)
    await h.press(h.tg.buttons()[0][1])
    await h.finish()
    await h.say("/done")
    if h.tg.buttons():
        await h.press(h.tg.buttons()[0][1])
    blobs = [
        caplog.text,
        json.dumps(h.tg.sent),
        json.dumps(h.tg.edited),
        json.dumps(h.state.audit_since("")),
    ]
    db = h.a.config.state_dir / "assistant.db"
    blobs.append(db.read_bytes().decode("latin-1"))
    blobs += [p.read_text(errors="ignore") for p in (tmp_path / "mem").rglob("*") if p.is_file()]
    for b in blobs:
        assert TOKEN not in b
    sent = json.dumps(h.tg.sent) + json.dumps(h.tg.edited)
    assert API_KEY not in sent and API_KEY not in caplog.text
