"""Tests for app/calendar_mcp.py — see that module's docstring for the
transport/auth pattern (mirrors omi-task-extractor's app/calendar_lookup.py,
NOT app/ticktick/mcp_client.py — this server needs an optional Bearer
token) and the real calendar-mcp server constraints (no attendees, no
extendedProperties support today, no calendar-create tool) this client is
shaped around.

No real Mongo, no real network — the transport (streamablehttp_client /
ClientSession) is faked at the module boundary, same "fake at the transport
seam" convention app/ticktick/mcp_client.py's own tests (if any existed)
would use.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app import calendar_mcp as cm


def _run(coro):
    return asyncio.run(coro)


class _Block:
    def __init__(self, text: str) -> None:
        self.text = text


class _Result:
    def __init__(self, text: str, is_error: bool = False) -> None:
        self.content = [_Block(text)]
        self.isError = is_error


class _FakeSessionCtx:
    """Fakes the `async with self._session() as session:` boundary — the
    tests below monkeypatch `client._session` to return one of these
    directly, rather than faking streamablehttp_client/ClientSession, for
    the tests concerned with call()'s own JSON/isError handling."""

    def __init__(self, session) -> None:
        self._session_obj = session

    async def __aenter__(self):
        return self._session_obj

    async def __aexit__(self, *exc):
        return False


# ── call() — JSON-text parsing / isError -> CalendarMCPError ───────────────


def test_call_parses_json_text_result(monkeypatch):
    client = cm.CalendarMCP(url="https://x", token="", account="", calendar_id="cal-id")

    class FakeSession:
        async def call_tool(self, name, args):
            assert name == "calendar_events_list"
            return _Result('{"events": [{"id": "e1"}]}')

    monkeypatch.setattr(client, "_session", lambda: _FakeSessionCtx(FakeSession()))

    data = _run(client.call("calendar_events_list", {}))

    assert data == {"events": [{"id": "e1"}]}


def test_call_raises_calendar_mcp_error_on_is_error(monkeypatch):
    client = cm.CalendarMCP(url="https://x", token="", account="", calendar_id="cal-id")

    class FakeSession:
        async def call_tool(self, name, args):
            return _Result("Error: bad calendar id", is_error=True)

    monkeypatch.setattr(client, "_session", lambda: _FakeSessionCtx(FakeSession()))

    with pytest.raises(cm.CalendarMCPError):
        _run(client.call("calendar_events_list", {}))


# ── resolve_calendar() ──────────────────────────────────────────────────


def test_resolve_calendar_none_when_url_unset(monkeypatch):
    monkeypatch.setattr(cm, "get_settings", lambda: SimpleNamespace(calendar_mcp_url=""))

    assert cm.resolve_calendar() is None


def test_resolve_calendar_returns_client_when_url_set(monkeypatch):
    settings = SimpleNamespace(
        calendar_mcp_url="https://calendar.example/mcp/secret",
        calendar_mcp_token="tok",
        calendar_mcp_account="",
        calendar_target_calendar_id="cal-id",
    )
    monkeypatch.setattr(cm, "get_settings", lambda: settings)

    client = cm.resolve_calendar()

    assert isinstance(client, cm.CalendarMCP)
    assert client.url == settings.calendar_mcp_url


# ── _session — Bearer header only when token is set ────────────────────


def test_session_sends_bearer_header_only_when_token_set(monkeypatch):
    captured: dict = {}

    def fake_streamablehttp_client(url, headers=None):
        captured["url"] = url
        captured["headers"] = headers

        class Ctx:
            async def __aenter__(self):
                return (object(), object(), object())

            async def __aexit__(self, *exc):
                return False

        return Ctx()

    class FakeClientSession:
        def __init__(self, read, write):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def initialize(self):
            return None

    monkeypatch.setattr(cm, "streamablehttp_client", fake_streamablehttp_client)
    monkeypatch.setattr(cm, "ClientSession", FakeClientSession)

    with_token = cm.CalendarMCP(url="https://x", token="secret-tok", account="", calendar_id="cal-id")

    async def _open_with_token():
        async with with_token._session():
            pass

    _run(_open_with_token())
    assert captured["headers"] == {"Authorization": "Bearer secret-tok"}

    without_token = cm.CalendarMCP(url="https://x", token="", account="", calendar_id="cal-id")

    async def _open_without_token():
        async with without_token._session():
            pass

    _run(_open_without_token())
    assert captured["headers"] is None


def test_session_raises_without_url():
    client = cm.CalendarMCP(url="", token="", account="", calendar_id="cal-id")

    async def _open():
        async with client._session():
            pass

    with pytest.raises(cm.CalendarMCPError):
        _run(_open())


# ── create_event — account omitted from args when empty ────────────────


def test_create_event_account_omitted_when_empty(monkeypatch):
    captured: dict = {}

    async def fake_call(self, name, args):
        captured["name"] = name
        captured["args"] = args
        return {"id": "evt1", "htmlLink": "https://calendar.google.com/evt1"}

    monkeypatch.setattr(cm.CalendarMCP, "call", fake_call)
    client = cm.CalendarMCP(url="https://x", token="", account="", calendar_id="cal-id")

    result = _run(
        client.create_event(
            summary="s", start="2026-08-04", end="2026-08-05",
            description="d", all_day=True, time_zone="UTC",
        )
    )

    assert "account" not in captured["args"]
    assert result["id"] == "evt1"


def test_create_event_includes_account_when_set(monkeypatch):
    captured: dict = {}

    async def fake_call(self, name, args):
        captured["args"] = args
        return {"id": "evt1"}

    monkeypatch.setattr(cm.CalendarMCP, "call", fake_call)
    client = cm.CalendarMCP(url="https://x", token="", account="donskikh.ms", calendar_id="cal-id")

    _run(
        client.create_event(
            summary="s", start="2026-08-04", end="2026-08-05",
            description="d", all_day=True, time_zone="UTC",
        )
    )

    assert captured["args"]["account"] == "donskikh.ms"
