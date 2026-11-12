"""Unit tests for Google Calendar integration."""

import httpx2
import pytest

from assistant import calendar
from assistant.google_auth import GoogleAuth, GoogleCredentials


@pytest.fixture
def auth() -> GoogleAuth:
    creds = GoogleCredentials("id1", "sec1", "ref1")
    return GoogleAuth(creds)


@pytest.mark.asyncio
async def test_calendar_list_events(auth: GoogleAuth) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert "calendars/primary/events" in str(request.url)
        return httpx2.Response(
            200,
            json={
                "items": [
                    {
                        "id": "ev_1",
                        "summary": "Team Standup",
                        "start": {"dateTime": "2026-11-13T10:00:00Z"},
                        "end": {"dateTime": "2026-11-13T10:30:00Z"},
                        "location": "Google Meet",
                        "attendees": [{"email": "alex@example.com"}],
                        "htmlLink": "https://calendar.google.com/event?eid=1",
                    }
                ]
            },
        )

    # Invalidate token cache by providing MockTransport on auth
    auth.transport = httpx2.MockTransport(
        lambda req: httpx2.Response(200, json={"access_token": "tok"})
    )
    auth._access_token = "tok"
    auth._expires_at = 9999999999.0

    auth.transport = httpx2.MockTransport(handler)
    events = await calendar.list_events(auth, timezone="UTC")
    assert len(events) == 1
    assert events[0]["id"] == "ev_1"
    assert events[0]["summary"] == "Team Standup"
    assert "Google Meet" in events[0]["location"]
    assert "alex@example.com" in events[0]["attendees"]


@pytest.mark.asyncio
async def test_calendar_create_event(auth: GoogleAuth) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.method == "POST"
        return httpx2.Response(
            201,
            json={
                "id": "ev_new",
                "summary": "Planning Session",
                "start": {"dateTime": "2026-11-13T14:00:00Z"},
                "end": {"dateTime": "2026-11-13T15:00:00Z"},
                "htmlLink": "https://calendar.google.com/event?eid=new",
            },
        )

    auth._access_token = "tok"
    auth._expires_at = 9999999999.0
    auth.transport = httpx2.MockTransport(handler)

    res = await calendar.create_event(
        auth,
        summary="Planning Session",
        start="2026-11-13T14:00:00Z",
        end="2026-11-13T15:00:00Z",
        timezone="UTC",
    )
    assert res["id"] == "ev_new"
    assert res["summary"] == "Planning Session"
    assert "html_link" in res


@pytest.mark.asyncio
async def test_calendar_delete_event(auth: GoogleAuth) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.method == "DELETE"
        assert "ev_to_delete" in str(request.url)
        return httpx2.Response(204)

    auth._access_token = "tok"
    auth._expires_at = 9999999999.0
    auth.transport = httpx2.MockTransport(handler)

    res = await calendar.delete_event(auth, "ev_to_delete")
    assert res["status"] == "deleted"
    assert res["event_id"] == "ev_to_delete"


def test_calendar_format_agenda() -> None:
    empty = calendar.format_agenda([], "Today")
    assert "clear" in empty.lower()

    events = [
        {
            "summary": "Design Sync",
            "time_span": "10:00 – 11:00",
            "location": "Room A",
            "html_link": "https://cal.com/1",
            "attendees": ["sara@example.com"],
        }
    ]
    text = calendar.format_agenda(events, "Today")
    assert "Design Sync" in text
    assert "10:00 – 11:00" in text
    assert "Room A" in text
    assert "sara@example.com" in text
