"""Unit tests for Gmail integration."""

import base64

import httpx2
import pytest

from assistant import gmail
from assistant.google_auth import GoogleAuth, GoogleCredentials


@pytest.fixture
def auth() -> GoogleAuth:
    creds = GoogleCredentials("id1", "sec1", "ref1")
    a = GoogleAuth(creds)
    a._access_token = "tok"
    a._expires_at = 9999999999.0
    return a


@pytest.mark.asyncio
async def test_gmail_list_and_search(auth: GoogleAuth) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        if "messages?" in url:
            return httpx2.Response(200, json={"messages": [{"id": "m1", "threadId": "t1"}]})
        if "messages/m1?" in url:
            return httpx2.Response(
                200,
                json={
                    "id": "m1",
                    "threadId": "t1",
                    "snippet": "Hello world",
                    "labelIds": ["INBOX", "UNREAD"],
                    "payload": {
                        "headers": [
                            {"name": "From", "value": "Alex <alex@example.com>"},
                            {"name": "Subject", "value": "Project Update"},
                            {"name": "Date", "value": "Thu, 12 Nov 2026"},
                        ]
                    },
                },
            )
        return httpx2.Response(404)

    auth.transport = httpx2.MockTransport(handler)
    msgs = await gmail.list_inbox(auth, unread_only=True, max_results=5)
    assert len(msgs) == 1
    assert msgs[0]["id"] == "m1"
    assert msgs[0]["subject"] == "Project Update"
    assert msgs[0]["sender"] == "Alex <alex@example.com>"
    assert msgs[0]["is_unread"] is True


@pytest.mark.asyncio
async def test_gmail_read_email_plain_and_html(auth: GoogleAuth) -> None:
    body_text = "Here is the proposal: please review."
    b64_body = base64.urlsafe_b64encode(body_text.encode("utf-8")).decode("ascii")

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={
                "id": "m1",
                "threadId": "t1",
                "payload": {
                    "headers": [
                        {"name": "From", "value": "sara@example.com"},
                        {"name": "Subject", "value": "Review Proposal"},
                    ],
                    "parts": [
                        {
                            "mimeType": "text/plain",
                            "body": {"data": b64_body},
                        }
                    ],
                },
            },
        )

    auth.transport = httpx2.MockTransport(handler)
    res = await gmail.read_email(auth, "m1")
    assert res["id"] == "m1"
    assert res["subject"] == "Review Proposal"
    assert "Here is the proposal" in res["body"]
    assert "[untrusted external email from sara@example.com" in res["body"]


@pytest.mark.asyncio
async def test_gmail_create_draft(auth: GoogleAuth) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.method == "POST"
        assert "drafts" in str(request.url)
        return httpx2.Response(
            201,
            json={"id": "d_123"},
        )

    auth.transport = httpx2.MockTransport(handler)
    draft = await gmail.create_draft(
        auth,
        to="alex@example.com",
        subject="Re: Update",
        body="Got it, thanks!",
    )
    assert draft["status"] == "draft_created"
    assert draft["draft_id"] == "d_123"


@pytest.mark.asyncio
async def test_gmail_send_email(auth: GoogleAuth) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.method == "POST"
        assert "messages/send" in str(request.url)
        return httpx2.Response(
            200,
            json={"id": "sent_123", "threadId": "t_123"},
        )

    auth.transport = httpx2.MockTransport(handler)
    sent = await gmail.send_email(
        auth,
        to="alex@example.com",
        subject="Report",
        body="All systems nominal.",
    )
    assert sent["status"] == "sent"
    assert sent["message_id"] == "sent_123"


def test_gmail_format_inbox() -> None:
    empty = gmail.format_inbox([], "Inbox")
    assert "no unread" in empty.lower()

    msgs = [
        {
            "id": "1",
            "sender": "Sara Connor <sara@sky.net>",
            "subject": "The future",
            "snippet": "Listen carefully",
        }
    ]
    formatted = gmail.format_inbox(msgs, "Inbox")
    assert "The future" in formatted
    assert "Sara Connor" in formatted
    assert "Listen carefully" in formatted
