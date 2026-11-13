"""Google Calendar integration for the assistant (feature 011)."""

from __future__ import annotations

import datetime
import logging
import zoneinfo
from typing import Any
from urllib.parse import urlencode

import httpx2

from assistant.google_auth import GoogleAuth, GoogleAuthError

log = logging.getLogger(__name__)

CALENDAR_BASE = "https://www.googleapis.com/calendar/v3/calendars/primary"


class CalendarError(Exception):
    pass


def _iso_or_default(dt_str: str | None, default: datetime.datetime) -> str:
    if not dt_str or not dt_str.strip():
        return default.isoformat()
    s = dt_str.strip()
    # If user provided YYYY-MM-DD
    if len(s) == 10 and s.count("-") == 2:
        return f"{s}T00:00:00Z"
    return s


def _format_time_span(
    start_raw: dict[str, Any], end_raw: dict[str, Any], tz: zoneinfo.ZoneInfo
) -> str:
    """Format start and end time neatly in operator timezone."""
    st = start_raw.get("dateTime") or start_raw.get("date") or ""
    et = end_raw.get("dateTime") or end_raw.get("date") or ""
    try:
        if "T" in st:
            dt_s = datetime.datetime.fromisoformat(st).astimezone(tz)
            dt_e = datetime.datetime.fromisoformat(et).astimezone(tz) if "T" in et else None
            time_part = f"{dt_s.strftime('%a %d %b %H:%M')}"
            if dt_e and dt_e.date() == dt_s.date():
                time_part += f" – {dt_e.strftime('%H:%M')}"
            elif dt_e:
                time_part += f" – {dt_e.strftime('%a %d %b %H:%M')}"
            return time_part
        return st  # all-day date
    except Exception:
        return f"{st} - {et}"


async def list_events(
    auth: GoogleAuth,
    *,
    time_min: str | None = None,
    time_max: str | None = None,
    query: str = "",
    max_results: int = 10,
    timezone: str = "UTC",
) -> list[dict[str, Any]]:
    """List events within a time window from Google Calendar."""
    tz = zoneinfo.ZoneInfo(timezone)
    now = datetime.datetime.now(tz)
    min_iso = _iso_or_default(time_min, now)
    max_iso = _iso_or_default(time_max, now + datetime.timedelta(days=7))

    params: dict[str, Any] = {
        "timeMin": min_iso,
        "timeMax": max_iso,
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": min(max(max_results, 1), 50),
    }
    if query.strip():
        params["q"] = query.strip()

    try:
        headers = await auth.auth_headers()
    except GoogleAuthError as exc:
        raise CalendarError(str(exc)) from exc

    url = f"{CALENDAR_BASE}/events?{urlencode(params)}"
    kw: dict[str, Any] = {"timeout": httpx2.Timeout(auth.timeout, connect=10.0)}
    if auth.transport is not None:
        kw["transport"] = auth.transport

    async with httpx2.AsyncClient(**kw) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            raise CalendarError(
                f"Google Calendar API error (HTTP {resp.status_code}): {resp.text[:200]}"
            )

        data = resp.json()
        items = data.get("items") or []
        events: list[dict[str, Any]] = []
        for it in items:
            start_raw = it.get("start") or {}
            end_raw = it.get("end") or {}
            attendees = [a.get("email") for a in it.get("attendees", []) if a.get("email")]
            events.append(
                {
                    "id": it.get("id", ""),
                    "summary": it.get("summary", "(No title)"),
                    "start": start_raw.get("dateTime") or start_raw.get("date", ""),
                    "end": end_raw.get("dateTime") or end_raw.get("date", ""),
                    "time_span": _format_time_span(start_raw, end_raw, tz),
                    "location": it.get("location", ""),
                    "description": it.get("description", ""),
                    "attendees": attendees,
                    "html_link": it.get("htmlLink", ""),
                }
            )
        return events


async def create_event(
    auth: GoogleAuth,
    *,
    summary: str,
    start: str,
    end: str,
    description: str = "",
    location: str = "",
    attendees: list[str] | None = None,
    timezone: str = "UTC",
) -> dict[str, Any]:
    """Create a new event in Google Calendar."""
    if not summary.strip():
        raise CalendarError("event summary cannot be empty")
    if not start.strip() or not end.strip():
        raise CalendarError("event start and end times are required")

    try:
        headers = await auth.auth_headers()
    except GoogleAuthError as exc:
        raise CalendarError(str(exc)) from exc

    headers["Content-Type"] = "application/json"
    body: dict[str, Any] = {
        "summary": summary.strip(),
        "start": {"dateTime": start, "timeZone": timezone} if "T" in start else {"date": start},
        "end": {"dateTime": end, "timeZone": timezone} if "T" in end else {"date": end},
    }
    if description.strip():
        body["description"] = description.strip()
    if location.strip():
        body["location"] = location.strip()
    if attendees:
        body["attendees"] = [{"email": a.strip()} for a in attendees if a.strip()]

    url = f"{CALENDAR_BASE}/events"
    kw: dict[str, Any] = {"timeout": httpx2.Timeout(auth.timeout, connect=10.0)}
    if auth.transport is not None:
        kw["transport"] = auth.transport

    async with httpx2.AsyncClient(**kw) as client:
        resp = await client.post(url, headers=headers, json=body)
        if resp.status_code not in (200, 201):
            raise CalendarError(
                f"Failed to create Google Calendar event (HTTP {resp.status_code}): {resp.text[:200]}"
            )

        it = resp.json()
        tz = zoneinfo.ZoneInfo(timezone)
        start_raw = it.get("start") or {}
        end_raw = it.get("end") or {}
        return {
            "id": it.get("id", ""),
            "summary": it.get("summary", summary),
            "start": start_raw.get("dateTime") or start_raw.get("date", start),
            "end": end_raw.get("dateTime") or end_raw.get("date", end),
            "time_span": _format_time_span(start_raw, end_raw, tz),
            "html_link": it.get("htmlLink", ""),
        }


async def delete_event(auth: GoogleAuth, event_id: str) -> dict[str, Any]:
    """Delete an event from Google Calendar."""
    if not event_id.strip():
        raise CalendarError("event_id cannot be empty")

    try:
        headers = await auth.auth_headers()
    except GoogleAuthError as exc:
        raise CalendarError(str(exc)) from exc

    url = f"{CALENDAR_BASE}/events/{event_id.strip()}"
    kw: dict[str, Any] = {"timeout": httpx2.Timeout(auth.timeout, connect=10.0)}
    if auth.transport is not None:
        kw["transport"] = auth.transport

    async with httpx2.AsyncClient(**kw) as client:
        resp = await client.delete(url, headers=headers)
        if resp.status_code not in (200, 204):
            raise CalendarError(
                f"Failed to delete event {event_id} (HTTP {resp.status_code}): {resp.text[:200]}"
            )
        return {"status": "deleted", "event_id": event_id}


def format_agenda(events: list[dict[str, Any]], heading: str = "Upcoming Agenda") -> str:
    """Format calendar events neatly for chat /slash command."""
    if not events:
        return f"📅 **{heading}**\nYour schedule is completely clear."

    lines = [f"📅 **{heading}** ({len(events)} event{'s' if len(events) != 1 else ''}):"]
    for ev in events:
        summary = ev.get("summary", "(No title)")
        time_span = ev.get("time_span", "")
        loc = f" 📍 {ev['location']}" if ev.get("location") else ""
        link = f" [link]({ev['html_link']})" if ev.get("html_link") else ""
        attendees = f" (with {', '.join(ev['attendees'])})" if ev.get("attendees") else ""
        lines.append(f"• **{time_span}**: {summary}{loc}{attendees}{link}")
    return "\n".join(lines)
