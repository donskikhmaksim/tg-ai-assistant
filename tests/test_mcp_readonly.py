"""app/mcp_readonly.py: the read-only MCP server exposed to an external
client (Claude Cowork) — see the module docstring for the client/server role
split. Follows the repo convention (tests/test_audit_log.py,
tests/test_policy_api.py): pure-logic + a tiny in-memory fake Mongo for the
query layer, aiohttp TestClient/TestServer for the one HTTP-level concern
(wrong secret in the path).

Covers:
  - list_conversations: days_back window, grouping by chat, chat_id filter.
  - list_tasks: days_back window, chat_id filter (and its absence).
  - get_daily_bundle: normal case, skipping empty days while scanning
    backward, hitting the scan cap, and the chat_id filter.
  - HTTP layer: a wrong (or missing) secret in the path 404s; the feature is
    off entirely (no route at all) when MCP_READONLY_SECRET is unset.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer

import app.mcp_readonly as mcpro
import app.repositories as repo
from app.web import server as server_mod

TOKEN = "123456:TEST-BOT-TOKEN"


def _run(coro):
    return asyncio.run(coro)


# ─────────────────────────────────────────────────────────────────────────────
# Tiny in-memory fake Mongo (repo convention — see tests/test_audit_log.py,
# tests/test_policy_api.py). Supports just the operators mcp_readonly.py and
# repositories.py actually issue: exact match, $gte/$lt, $in, .sort(field,
# direction), find_one with a projection.
# ─────────────────────────────────────────────────────────────────────────────

def _matches(doc: dict, query: dict) -> bool:
    for k, cond in query.items():
        val = doc.get(k)
        if isinstance(cond, dict):
            for op, target in cond.items():
                if op == "$gte" and not (val is not None and val >= target):
                    return False
                if op == "$lt" and not (val is not None and val < target):
                    return False
                if op == "$in" and val not in target:
                    return False
        elif val != cond:
            return False
    return True


def _project(doc: dict, projection: dict | None) -> dict:
    if not projection:
        return dict(doc)
    return {k: v for k, v in doc.items() if k in projection or k == "_id"}


class FakeCursor:
    def __init__(self, docs: list[dict], projection: dict | None = None):
        self._docs = docs
        self._projection = projection
        self._sort_field = None
        self._sort_dir = 1

    def sort(self, field, direction=1):
        self._sort_field = field
        self._sort_dir = direction
        return self

    def __aiter__(self):
        docs = list(self._docs)
        if self._sort_field:
            docs.sort(key=lambda d: d.get(self._sort_field), reverse=(self._sort_dir == -1))
        self._it = iter(_project(d, self._projection) for d in docs)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


class FakeCollection:
    def __init__(self, docs: list[dict] | None = None):
        self.docs = list(docs or [])

    def find(self, query: dict | None = None, projection: dict | None = None):
        query = query or {}
        return FakeCursor([d for d in self.docs if _matches(d, query)], projection)

    async def find_one(self, query: dict | None = None, projection: dict | None = None):
        query = query or {}
        for d in self.docs:
            if _matches(d, query):
                return _project(d, projection)
        return None


class FakeDB:
    def __init__(self):
        self.raw_messages = FakeCollection()
        self.tasks = FakeCollection()
        self.chat_state = FakeCollection()


def _use_fake_db(monkeypatch, raw_messages=None, tasks=None, chat_state=None):
    db = FakeDB()
    db.raw_messages = FakeCollection(raw_messages or [])
    db.tasks = FakeCollection(tasks or [])
    db.chat_state = FakeCollection(chat_state or [])
    monkeypatch.setattr(mcpro, "get_db", lambda: db)
    monkeypatch.setattr(repo, "get_db", lambda: db)
    return db


def _use_utc_settings(monkeypatch):
    monkeypatch.setattr(mcpro, "get_settings", lambda: SimpleNamespace(default_timezone="UTC"))


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures / builders
# ─────────────────────────────────────────────────────────────────────────────

def _day(days_ago: int, hour: int = 12):
    """UTC datetime at `hour` on the calendar day `days_ago` days before today."""
    d = datetime.now(timezone.utc).date() - timedelta(days=days_ago)
    return datetime(d.year, d.month, d.day, hour, tzinfo=timezone.utc)


def _msg(chat_id_key: str, days_ago: int, text: str, message_id: int, direction="in", sender=None):
    return {
        "chatId": chat_id_key,
        "type": "dm",
        "direction": direction,
        "senderName": sender,
        "text": text,
        "messageId": message_id,
        "date": _day(days_ago),
    }


def _task(chat_id_key: str, days_ago: int, title: str, dedup: str, source_ids=None):
    # hour=0 (local midnight), NOT the _msg default of noon: list_tasks's
    # upper bound is the real "now" (a point in time, not end-of-day), so a
    # fixed noon timestamp would spuriously land "in the future" whenever the
    # suite runs before 12:00 UTC. Midnight is always <= "now" for "today".
    created = _day(days_ago, hour=0)
    return {
        "chatId": chat_id_key,
        "task": title,
        "details": None,
        "status": "open",
        "sourceMessageIds": source_ids or [],
        "dedupHash": dedup,
        "ticktickTaskId": "tt-" + dedup,
        "projectId": "proj-1",
        "createdAt": created,
        "updatedAt": created,
    }


# ─────────────────────────────────────────────────────────────────────────────
# list_conversations
# ─────────────────────────────────────────────────────────────────────────────

def test_list_conversations_respects_days_back_and_groups_by_chat(monkeypatch):
    _use_utc_settings(monkeypatch)
    _use_fake_db(monkeypatch, raw_messages=[
        _msg("user_111", 0, "привет сегодня", 1),
        _msg("user_111", 2, "привет 2 дня назад", 2),
        _msg("user_111", 5, "слишком старое", 3),  # outside days_back=3
        _msg("group_-222", 0, "групповое сегодня", 4),
    ])

    result = _run(mcpro.list_conversations(days_back=3))

    by_chat = {c["chat_id"]: c for c in result}
    assert set(by_chat) == {111, -222}
    assert by_chat[111]["chat_type"] == "dm"
    assert by_chat[-222]["chat_type"] == "group"
    texts = [m["text"] for m in by_chat[111]["messages"]]
    assert texts == ["привет 2 дня назад", "привет сегодня"]  # chronological
    assert [m["text"] for m in by_chat[-222]["messages"]] == ["групповое сегодня"]


def test_list_conversations_chat_id_filter(monkeypatch):
    _use_utc_settings(monkeypatch)
    _use_fake_db(monkeypatch, raw_messages=[
        _msg("user_111", 0, "чат A", 1),
        _msg("group_-222", 0, "чат B", 2),
    ])

    result = _run(mcpro.list_conversations(days_back=3, chat_id=111))

    assert len(result) == 1
    assert result[0]["chat_id"] == 111
    assert result[0]["messages"][0]["text"] == "чат A"


# ─────────────────────────────────────────────────────────────────────────────
# list_tasks
# ─────────────────────────────────────────────────────────────────────────────

def test_list_tasks_respects_days_back(monkeypatch):
    _use_utc_settings(monkeypatch)
    _use_fake_db(
        monkeypatch,
        tasks=[
            _task("user_111", 0, "Свежая задача", "d1", source_ids=[1]),
            _task("user_111", 10, "Старая задача", "d2"),  # outside days_back=3
        ],
        chat_state=[{"chatId": "user_111", "title": "T", "lastMessageAt": _day(0)}],
    )

    result = _run(mcpro.list_tasks(days_back=3))

    assert [t["title"] for t in result] == ["Свежая задача"]
    assert result[0]["chat_id"] == 111
    assert result[0]["source_message_ids"] == [1]
    assert result[0]["ticktick_id"] == "tt-d1"


def test_list_tasks_chat_id_filter(monkeypatch):
    _use_utc_settings(monkeypatch)
    _use_fake_db(monkeypatch, tasks=[
        _task("user_111", 0, "Задача A", "dA"),
        _task("group_-222", 0, "Задача B", "dB"),
    ])

    result = _run(mcpro.list_tasks(days_back=3, chat_id=-222))

    assert [t["title"] for t in result] == ["Задача B"]


def test_list_tasks_without_chat_id_scans_all_known_chats(monkeypatch):
    _use_utc_settings(monkeypatch)
    _use_fake_db(
        monkeypatch,
        tasks=[
            _task("user_111", 0, "Задача A", "dA"),
            _task("group_-222", 0, "Задача B", "dB"),
        ],
        chat_state=[
            {"chatId": "user_111", "title": "A", "lastMessageAt": _day(0)},
            {"chatId": "group_-222", "title": "B", "lastMessageAt": _day(0)},
        ],
    )

    result = _run(mcpro.list_tasks(days_back=3))

    assert {t["title"] for t in result} == {"Задача A", "Задача B"}


# ─────────────────────────────────────────────────────────────────────────────
# get_daily_bundle
# ─────────────────────────────────────────────────────────────────────────────

def test_get_daily_bundle_normal_case(monkeypatch):
    _use_utc_settings(monkeypatch)
    target = datetime.now(timezone.utc).date()
    _use_fake_db(monkeypatch, raw_messages=[
        _msg("user_111", 0, "сегодня", 1),
        _msg("user_111", 1, "вчера", 2),
    ], tasks=[
        _task("user_111", 0, "Задача сегодня", "d1"),
    ], chat_state=[{"chatId": "user_111", "title": "T", "lastMessageAt": _day(0)}])

    bundle = _run(mcpro.get_daily_bundle(date=target.isoformat(), active_days=2))

    assert bundle["requested_date"] == target.isoformat()
    assert bundle["active_days_included"] == sorted([
        target.isoformat(),
        (target - timedelta(days=1)).isoformat(),
    ])
    assert len(bundle["conversations"]) == 1
    texts = [m["text"] for m in bundle["conversations"][0]["messages"]]
    assert texts == ["вчера", "сегодня"]
    assert [t["title"] for t in bundle["tasks_created"]] == ["Задача сегодня"]
    assert "list_conversations" in bundle["note"]


def test_get_daily_bundle_skips_empty_days_scanning_backward(monkeypatch):
    _use_utc_settings(monkeypatch)
    target = datetime.now(timezone.utc).date()
    # target day (0) and day 1 back are EMPTY; days 2 and 3 back have messages.
    _use_fake_db(monkeypatch, raw_messages=[
        _msg("user_111", 2, "позавчера-минус", 1),
        _msg("user_111", 3, "ещё раньше", 2),
    ])

    bundle = _run(mcpro.get_daily_bundle(date=target.isoformat(), active_days=2))

    expected_days = sorted([
        (target - timedelta(days=2)).isoformat(),
        (target - timedelta(days=3)).isoformat(),
    ])
    assert bundle["active_days_included"] == expected_days
    # The range pulled spans earliest-active..target inclusive, so both
    # messages show up even though the days in between were empty.
    texts = [m["text"] for m in bundle["conversations"][0]["messages"]]
    assert texts == ["ещё раньше", "позавчера-минус"]


def test_get_daily_bundle_hits_scan_cap_without_erroring(monkeypatch):
    _use_utc_settings(monkeypatch)
    target = datetime.now(timezone.utc).date()
    # Only a message far outside the 14-day scan cap — no active day is ever found.
    _use_fake_db(monkeypatch, raw_messages=[
        _msg("user_111", 20, "слишком далеко", 1),
    ])

    bundle = _run(mcpro.get_daily_bundle(date=target.isoformat(), active_days=3))

    assert bundle["active_days_included"] == []
    assert bundle["conversations"] == []
    assert bundle["tasks_created"] == []
    assert "requested_date" in bundle  # still a well-formed result, not an error


def test_get_daily_bundle_chat_id_filter(monkeypatch):
    _use_utc_settings(monkeypatch)
    target = datetime.now(timezone.utc).date()
    _use_fake_db(monkeypatch, raw_messages=[
        _msg("user_111", 0, "чат A сегодня", 1),
        _msg("group_-222", 1, "чат B вчера", 2),
    ], tasks=[
        _task("user_111", 0, "Задача A", "dA"),
        _task("group_-222", 1, "Задача B", "dB"),
    ])

    # chat_id=-222 only has activity yesterday, not today — the scan must
    # walk back one day to find it, and must NOT pick up chat A's messages.
    bundle = _run(mcpro.get_daily_bundle(date=target.isoformat(), active_days=1, chat_id=-222))

    assert bundle["active_days_included"] == [(target - timedelta(days=1)).isoformat()]
    assert len(bundle["conversations"]) == 1
    assert bundle["conversations"][0]["chat_id"] == -222
    assert [t["title"] for t in bundle["tasks_created"]] == ["Задача B"]


# ─────────────────────────────────────────────────────────────────────────────
# HTTP layer: secret-in-path gating
# ─────────────────────────────────────────────────────────────────────────────

def _settings(**overrides):
    base = dict(bot_token=TOKEN, policy_pull_token="", mcp_readonly_secret="right-secret-123")
    base.update(overrides)
    return SimpleNamespace(**base)


def _client(monkeypatch, settings=None):
    monkeypatch.setattr(server_mod, "get_settings", lambda: settings or _settings())
    monkeypatch.setattr(mcpro, "get_settings", lambda: settings or _settings())

    async def fake_get_bot_state(key):
        return None

    monkeypatch.setattr(repo, "get_bot_state", fake_get_bot_state)
    app = server_mod.build_app(bot=SimpleNamespace())
    return TestClient(TestServer(app))


def test_wrong_secret_in_path_is_not_found(monkeypatch):
    async def go():
        async with _client(monkeypatch) as client:
            resp = await client.post("/mcp/wrong-secret", json={})
            assert resp.status == 404

    _run(go())


def test_missing_secret_disables_the_route_entirely(monkeypatch):
    async def go():
        async with _client(monkeypatch, settings=_settings(mcp_readonly_secret="")) as client:
            # Even a request that would otherwise be well-formed 404s: the
            # route was never registered when the secret is unset.
            resp = await client.post("/mcp/anything", json={})
            assert resp.status == 404

    _run(go())


def test_correct_secret_path_is_routed_to_the_mcp_server(monkeypatch):
    async def go():
        async with _client(monkeypatch) as client:
            # A bare POST with no MCP initialize handshake is invalid at the
            # protocol layer, but the important thing here is that it reaches
            # the mcp session manager at all (i.e. NOT a 404 from aiohttp
            # routing) — proving the secret gates the mount, not the request.
            resp = await client.post("/mcp/right-secret-123", json={"not": "a valid jsonrpc request"})
            assert resp.status != 404

    _run(go())


def test_mixed_case_headers_reach_a_real_mcp_response_not_400(monkeypatch):
    """Regression test for the ASGI-scope header-casing bug: _asgi_bridge used
    to forward request.raw_headers to the ASGI scope verbatim (whatever case
    the client sent, e.g. "Content-Type"), but the ASGI spec requires the
    host/bridge itself to lower-case header names before handing them to the
    app — Starlette's Headers (built straight from scope["headers"], see
    starlette.datastructures.Headers.__init__'s `elif scope is not None`
    branch) and the mcp package's own transport_security /
    streamable_http._check_content_type / _check_accept_headers all look
    headers up by exact-matching a lower-cased key. With un-lowered headers
    every real HTTP client (which sends "Content-Type", "Accept", not the
    lower-case forms) got a hard-coded 400 "Invalid Content-Type header" —
    the endpoint never produced a real MCP response at all.

    This performs an actual protocol-level `initialize` call through the full
    stack (aiohttp route -> _asgi_bridge -> StreamableHTTPSessionManager),
    with headers cased exactly as a normal HTTP client sends them, and checks
    the STATUS CODE explicitly (previous coverage only asserted `!= 404`,
    which a 400 also satisfies — that's why this regression shipped to prod
    unnoticed).
    """
    async def go():
        async with _client(monkeypatch) as client:
            resp = await client.post(
                "/mcp/right-secret-123",
                headers={
                    # Deliberately mixed/title-case, as sent by aiohttp's own
                    # `json=` kwarg and by virtually every real HTTP client —
                    # NOT already-lower-cased "content-type"/"accept".
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "regression-test-client", "version": "1.0"},
                    },
                },
            )

            # The exact bug: transport_security._validate_content_type looks
            # up request.headers.get("content-type") and, finding nothing
            # (because the stored key was "Content-Type"), always returned
            # 400 "Invalid Content-Type header" here — regardless of secret
            # routing being correct. A bare `!= 404` check does not catch
            # this; the status code itself must be asserted.
            assert resp.status == 200, await resp.text()

            payload = await resp.json()
            assert payload["jsonrpc"] == "2.0"
            assert payload["id"] == 1
            assert "error" not in payload
            result = payload["result"]
            assert result["serverInfo"]["name"] == "tg-ai-assistant-readonly"
            assert "tools" in result["capabilities"]

    _run(go())


def test_mixed_case_headers_tools_list_exposes_all_readonly_tools(monkeypatch):
    """Companion to the initialize regression test above: completes the
    handshake (initialize -> notifications/initialized) and then calls
    tools/list, still with real-client header casing throughout, to confirm
    every read-only tool (including the signals/triage ones — see
    app/signals.py, app/pipeline/triage.py) is actually reachable end-to-end
    and not just that initialize alone happens to succeed."""
    async def go():
        async with _client(monkeypatch) as client:
            common_headers = {
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            }

            init_resp = await client.post(
                "/mcp/right-secret-123",
                headers=common_headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "regression-test-client", "version": "1.0"},
                    },
                },
            )
            assert init_resp.status == 200, await init_resp.text()
            session_id = init_resp.headers.get("mcp-session-id")

            session_headers = dict(common_headers)
            if session_id:
                session_headers["Mcp-Session-Id"] = session_id

            # Required notification before any further request in a
            # stateful session — the JSON-RPC handshake isn't done until
            # this is sent.
            notify_resp = await client.post(
                "/mcp/right-secret-123",
                headers=session_headers,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            assert notify_resp.status == 202, await notify_resp.text()

            tools_resp = await client.post(
                "/mcp/right-secret-123",
                headers=session_headers,
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            )
            assert tools_resp.status == 200, await tools_resp.text()
            payload = await tools_resp.json()
            assert "error" not in payload
            names = {t["name"] for t in payload["result"]["tools"]}
            assert names == {
                "list_conversations", "list_tasks", "get_daily_bundle",
                "get_triage_queue", "submit_triage_feedback",
            }

    _run(go())
