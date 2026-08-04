"""MCP client to the Railway-hosted `calendar-mcp` server (Google Calendar).

Feeds a SEPARATE, isolated "AI Captured" calendar — never the owner's
primary calendar — with events extracted from Telegram chat activity (see
app/pipeline/calendar_events.py for the extraction stage this client backs).

Transport/auth pattern is copied from omi-task-extractor's
app/calendar_lookup.py (streamable-HTTP + optional Bearer token), NOT from
app/ticktick/mcp_client.py — that client's server needs no auth header at
all, while calendar-mcp is gated by a Bearer token selecting the Google
account server-side. Session lifecycle (fresh session per call) mirrors
app/ticktick/mcp_client.py::TickTickMCP.

Two known real-server limitations (confirmed 2026-08 against
/Users/maksim/Code/calendar-mcp's actual source — see
app/pipeline/calendar_events.py's module docstring §0 for the full
writeup) shape this client:

  - `calendar_event_create`'s JSON schema is `additionalProperties: false`
    and does NOT accept `extendedProperties`/`visibility`/`transparency` —
    sending them fails the WHOLE call, not just those fields. So
    idempotency here can't lean on extendedProperties.private (the usual
    Google-API pattern) — see find_event_by_marker for the substitute, and
    `extra`/CalendarMCP.__init__'s `supports_extended` for the future
    extension point once the server is upgraded.
  - There is no calendar-CREATE tool — the "AI Captured" calendar itself is
    made by hand in the Google UI (see the ТЗ's owner manual-steps section);
    this client only ever writes INTO an existing calendar id.
"""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from .config import get_settings

logger = logging.getLogger(__name__)

# Calendar ids that must never receive a write from this client, regardless
# of what CALENDAR_TARGET_CALENDAR_ID is set to — "primary" is Google's own
# alias for the owner's main calendar, and an empty string means "not
# configured", never "use the default". This is a code-level guard, not just
# a config convention: even a future caller passing a bad calendar_id in by
# mistake cannot write into the owner's real calendar through this class.
_FORBIDDEN_CALENDAR_IDS = {"", "primary"}


class CalendarMCPError(RuntimeError):
    """Raised on a tool-level error response from calendar-mcp (isError), or
    a client-side refusal (e.g. the primary-calendar guard)."""


class CalendarMCP:
    """Thin async wrapper, one session per call — same posture as
    app/ticktick/mcp_client.py::TickTickMCP (low call volume; a fresh
    session avoids holding an SSE stream open across the ~20-minute idle
    between calendar_events_job ticks)."""

    def __init__(
        self,
        url: str | None = None,
        token: str | None = None,
        account: str | None = None,
        calendar_id: str | None = None,
    ) -> None:
        settings = get_settings()
        self.url = url if url is not None else settings.calendar_mcp_url
        self.token = token if token is not None else settings.calendar_mcp_token
        self.account = account if account is not None else settings.calendar_mcp_account
        self.calendar_id = calendar_id if calendar_id is not None else settings.calendar_target_calendar_id

    @asynccontextmanager
    async def _session(self):
        if not self.url:
            raise CalendarMCPError("CALENDAR_MCP_URL is not configured")
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else None
        async with streamablehttp_client(self.url, headers=headers) as streams:
            read, write = streams[0], streams[1]
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session

    async def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Call a calendar-mcp tool and parse its JSON-text result.

        The server returns JSON encoded as a text content block (see
        util.ts::ok on the server side); an error response sets `isError`
        and carries a plain-text `Error: ...` message — raised here as
        CalendarMCPError rather than returned, so callers can rely on a
        normal return meaning success."""
        async with self._session() as session:
            result = await session.call_tool(name, args)
            text = "".join(getattr(b, "text", "") for b in result.content)
            if getattr(result, "isError", False):
                raise CalendarMCPError(text[:500])
            return json.loads(text)

    async def list_calendars(self) -> list[dict[str, Any]]:
        data = await self.call("calendar_list", {})
        calendars = data.get("calendars")
        return calendars if isinstance(calendars, list) else []

    async def create_event(
        self,
        *,
        summary: str,
        start: str,
        end: str,
        description: str,
        all_day: bool,
        time_zone: str,
    ) -> dict[str, Any]:
        """Create one event on the configured calendar. NO `attendees`
        parameter exists on this method at all — a code-level guarantee
        this client can never invite anyone (the ТЗ's hard requirement),
        not merely a convention that could be bypassed by passing one in.

        Refuses (CalendarMCPError, no network call made) when calendar_id is
        empty or resolves to "primary" — this client must only ever write
        into the isolated "AI Captured" calendar, never the owner's real
        one. `sendUpdates` is always explicitly "none" — defence in depth
        on top of the missing `attendees` param, since the server's own
        default for that field is "all" (would email invites to nobody
        today, but never rely on the server's default silently staying
        safe)."""
        calendar_id = (self.calendar_id or "").strip()
        if calendar_id.lower() in _FORBIDDEN_CALENDAR_IDS:
            raise CalendarMCPError("refusing to write into the primary calendar")
        args: dict[str, Any] = {
            "calendarId": calendar_id,
            "summary": summary,
            "start": start,
            "end": end,
            "description": description,
            "timeZone": time_zone,
            "sendUpdates": "none",
        }
        if all_day:
            args["allDay"] = True
        if self.account:
            args["account"] = self.account
        return await self.call("calendar_event_create", args)

    async def find_event_by_marker(
        self, *, marker: str, time_min: str, time_max: str
    ) -> dict[str, Any] | None:
        """Best-effort self-healing lookup: search the target calendar for an
        event whose description contains `marker` (the "[tg-ai-assistant]
        key=<event_key>" line — see app/pipeline/calendar_events.py's
        build_description) within [time_min, time_max]. Returns the first
        match, or None if not found / on any error (this is a convenience
        idempotency check, not a required step — callers fall through to a
        normal create_event on None)."""
        calendar_id = (self.calendar_id or "").strip()
        if calendar_id.lower() in _FORBIDDEN_CALENDAR_IDS:
            return None
        args: dict[str, Any] = {
            "calendarId": calendar_id,
            "timeMin": time_min,
            "timeMax": time_max,
            "query": marker,
            "maxResults": 10,
        }
        if self.account:
            args["account"] = self.account
        try:
            data = await self.call("calendar_events_list", args)
        except CalendarMCPError:
            logger.warning("find_event_by_marker: calendar_events_list failed", exc_info=True)
            return None
        events = data.get("events")
        if not isinstance(events, list):
            return None
        for ev in events:
            if isinstance(ev, dict) and marker in (ev.get("description") or ""):
                return ev
        return None


def resolve_calendar() -> CalendarMCP | None:
    """The calendar-mcp client for this (single-tenant) instance, or None
    when CALENDAR_MCP_URL is unset. Unlike ticktick/mcp_client.py::
    resolve_ticktick, there is no runtime `/connect` override here — no Mini
    App UI exists for the calendar connector, so the only source of truth is
    the env var. A fresh instance per call (no module-level cache, unlike
    TickTickMCP's get_ticktick()) — construction is free (no network in
    __init__) and this always reflects the current settings, which matters
    for tests that monkeypatch get_settings()."""
    settings = get_settings()
    if not settings.calendar_mcp_url:
        return None
    return CalendarMCP()
