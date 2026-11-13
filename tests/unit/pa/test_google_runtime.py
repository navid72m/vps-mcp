"""Integration tests for Google Calendar and Gmail tools in Assistant runtime."""

import json
from pathlib import Path

import httpx2
import pytest

from tests.assistant_fakes import harness, say, use


@pytest.fixture
def google_transport() -> httpx2.MockTransport:
    def handler(request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        # OAuth token endpoint
        if "oauth2.googleapis.com/token" in url:
            return httpx2.Response(200, json={"access_token": "mock_tok", "expires_in": 3600})
        # Calendar events list
        if "calendars/primary/events" in url and request.method == "GET":
            return httpx2.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "cal_1",
                            "summary": "Doctor appointment",
                            "start": {"dateTime": "2026-11-13T10:00:00Z"},
                            "end": {"dateTime": "2026-11-13T11:00:00Z"},
                            "location": "Clinic",
                        }
                    ]
                },
            )
        # Calendar event create
        if "calendars/primary/events" in url and request.method == "POST":
            return httpx2.Response(
                201,
                json={
                    "id": "cal_created",
                    "summary": "Sprint Retro",
                    "start": {"dateTime": "2026-11-14T15:00:00Z"},
                    "end": {"dateTime": "2026-11-14T16:00:00Z"},
                    "htmlLink": "https://calendar.google.com/event?eid=retro",
                },
            )
        # Calendar event delete
        if "calendars/primary/events/cal_1" in url and request.method == "DELETE":
            return httpx2.Response(204)
        # Gmail inbox / search
        if "messages?" in url and request.method == "GET":
            return httpx2.Response(200, json={"messages": [{"id": "gm_1", "threadId": "th_1"}]})
        # Gmail message detail
        if "messages/gm_1" in url and request.method == "GET":
            return httpx2.Response(
                200,
                json={
                    "id": "gm_1",
                    "threadId": "th_1",
                    "snippet": "Meeting notes attached",
                    "labelIds": ["INBOX", "UNREAD"],
                    "payload": {
                        "headers": [
                            {"name": "From", "value": "Boss <boss@corp.com>"},
                            {"name": "Subject", "value": "Quarterly Targets"},
                            {"name": "Date", "value": "Fri, 13 Nov 2026"},
                        ],
                        "parts": [
                            {
                                "mimeType": "text/plain",
                                "body": {"data": "UGxlYXNlIHJldmlldyB0aGVzZSB0YXJnZXRzLg=="},
                            }
                        ],
                    },
                },
            )
        # Gmail draft create
        if "drafts" in url and request.method == "POST":
            return httpx2.Response(201, json={"id": "draft_99"})
        # Gmail send
        if "messages/send" in url and request.method == "POST":
            return httpx2.Response(200, json={"id": "msg_sent_100"})
        return httpx2.Response(404, text="Not Found")

    return httpx2.MockTransport(handler)


@pytest.mark.asyncio
async def test_calendar_and_email_tools_in_runtime(
    tmp_path: Path, google_transport: httpx2.MockTransport
) -> None:
    # Set up client json credentials
    cred_file = tmp_path / "google_client.json"
    cred_file.write_text(json.dumps({"installed": {"client_id": "cid", "client_secret": "csec"}}))

    h = await harness(
        tmp_path,
        google={
            "enabled": True,
            "credentials_path": str(cred_file),
            "token_secret": "google_refresh_token",
        },
    )
    h.a.google_transport = google_transport
    # Inject fake secret
    h.a.secret = lambda name: "fake_refresh_tok" if name == "google_refresh_token" else ""

    # 1. Test calendar__events tool call
    out, is_err = await h.a.builtin(
        "calendar__events",
        {"query": "Doctor"},
        chat_id=111,
    )
    assert not is_err
    data = json.loads(out)
    assert len(data["content"]["events"]) == 1
    assert data["content"]["events"][0]["summary"] == "Doctor appointment"

    # 2. Test email__inbox tool call
    out, is_err = await h.a.builtin(
        "email__inbox",
        {"unread_only": True},
        chat_id=111,
    )
    assert not is_err
    data = json.loads(out)
    assert len(data["content"]["messages"]) == 1
    assert data["content"]["messages"][0]["subject"] == "Quarterly Targets"

    # 3. Test email__read tool call with untrusted envelope
    out, is_err = await h.a.builtin(
        "email__read",
        {"message_id": "gm_1"},
        chat_id=111,
    )
    assert not is_err
    data = json.loads(out)
    assert "[untrusted external email from Boss <boss@corp.com>" in data["content"]["body"]


@pytest.mark.asyncio
async def test_email_send_requires_approval(
    tmp_path: Path, google_transport: httpx2.MockTransport
) -> None:
    import asyncio

    cred_file = tmp_path / "google_client.json"
    cred_file.write_text(json.dumps({"installed": {"client_id": "cid", "client_secret": "csec"}}))

    h = await harness(
        tmp_path,
        google={"enabled": True, "credentials_path": str(cred_file)},
    )
    h.a.google_transport = google_transport
    h.a.secret = lambda name: "fake_refresh_tok"

    # Script provider: 1. model tries to send email, 2. after approval finishes turn
    h.provider.add(
        use("email__send", {"to": "partner@vendor.com", "subject": "Deal", "body": "We accept."}),
        say("Email has been sent."),
    )

    task = asyncio.create_task(h.say("Send the email to partner@vendor.com"))
    for _ in range(200):
        if h.tg.sent and h.tg.buttons():
            break
        await asyncio.sleep(0.01)

    assert any("Send email via Gmail" in m.get("text", "") for m in h.tg.sent)
    data = next(d for label, d in h.tg.buttons() if label.startswith("✅"))
    await h.press(data)
    await h.finish()
    await task
    assert "Email has been sent" in h.tg.texts()[-1]


@pytest.mark.asyncio
async def test_agenda_and_inbox_slash_commands(
    tmp_path: Path, google_transport: httpx2.MockTransport
) -> None:
    cred_file = tmp_path / "google_client.json"
    cred_file.write_text(json.dumps({"installed": {"client_id": "cid", "client_secret": "csec"}}))

    h = await harness(
        tmp_path,
        google={"enabled": True, "credentials_path": str(cred_file)},
    )
    h.a.google_transport = google_transport
    h.a.secret = lambda name: "fake_refresh_tok"

    # /agenda
    await h.say("/agenda")
    assert any("Doctor appointment" in m.get("text", "") for m in h.tg.sent)

    # /inbox
    await h.say("/inbox")
    assert any("Quarterly Targets" in m.get("text", "") for m in h.tg.sent)
