"""Read-only MCP server (Streamable HTTP) — the mirror image of
app/ticktick/mcp_client.py, where THIS app is normally the MCP *client*
(calling out to ticktick-mcp to write tasks). Here it is the *server*,
exposing the owner's conversation history + already-extracted tasks
READ-ONLY to an external MCP client (e.g. "Claude Cowork", which has its own
separate write-capable TickTick connector — nothing in this module ever
writes anything back to Mongo or TickTick).

Mounted at /mcp/<MCP_READONLY_SECRET> in app/web/server.py::build_app, same
path-embedded-secret convention TICKTICK_MCP_URL already uses in this repo
(see .env.example). Empty secret -> register_routes() is a no-op (fail-open,
matching QWEN_BASE_URL/TRANSCRIBE_URL/BACKUP_S3_* elsewhere in config.py).

Three tools:
  list_conversations — raw transcripts for the last N CALENDAR days (not
                        "active days" — this repo has no reusable "active
                        days" concept as a function; see get_daily_bundle
                        below for the one place that notion IS built, purely
                        for that tool).
  list_tasks          — tasks created in the last N days (reuses
                        repo.get_tasks_created_between).
  get_daily_bundle     — the main entry point: walks backward from a date
                        over calendar days, skipping empty ones, until
                        `active_days` days WITH at least one message are
                        found (capped at _SCAN_CAP_DAYS lookback so a truly
                        dead chat can't scan forever), then returns
                        conversations + tasks for the whole
                        [earliest active day .. requested date] span in one
                        call. list_conversations/list_tasks stay available
                        as a fallback for a wider/different range.

ASGI bridge: aiohttp has no native ASGI host, and the `mcp` package's
Streamable HTTP transport (mcp.server.streamable_http_manager) is ASGI-only.
Rather than pull in a second web framework just to serve one endpoint, this
module drives the transport's `handle_request(scope, receive, send)`
directly from a small local scope/receive/send bridge (see _asgi_bridge).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiohttp import web
from mcp.server.fastmcp import FastMCP
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from . import repositories as repo
from .config import get_settings
from .db import get_db

logger = logging.getLogger(__name__)

# How far back get_daily_bundle will scan looking for `active_days` days with
# messages before giving up and returning whatever it found (never an error —
# see the docstring on get_daily_bundle).
_SCAN_CAP_DAYS = 14

mcp = FastMCP(
    name="tg-ai-assistant-readonly",
    instructions=(
        "Read-only view into this owner's Telegram task-extraction pipeline: "
        "raw conversation transcripts and the tasks already extracted from "
        "them. There are no write tools here — creating/editing/completing "
        "tasks happens through the owner's own separate TickTick MCP "
        "connector, not this server. Start with get_daily_bundle for a "
        "single-call daily snapshot; fall back to list_conversations / "
        "list_tasks directly for a wider or more specific range."
    ),
    json_response=True,
)


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _local_midnight_utc(day: date, tz: ZoneInfo) -> datetime:
    """Local midnight of `day` (in `tz`), converted to an aware UTC datetime."""
    return datetime(day.year, day.month, day.day, tzinfo=tz).astimezone(timezone.utc)


def _parse_date(value: str | None, tz: ZoneInfo) -> date:
    if not value:
        return datetime.now(tz).date()
    return datetime.strptime(value, "%Y-%m-%d").date()


def _numeric_chat_id(chat_key: str) -> int:
    """"user_123" / "group_-100123" -> 123 / -100123. See handlers_ui.py's
    f"user_{chat.id}" / f"group_{chat.id}" — the id itself already carries
    the group/DM distinction (Telegram group ids are negative, user ids are
    always positive), so round-tripping it back to an int loses nothing."""
    for prefix in ("user_", "group_"):
        if chat_key.startswith(prefix):
            try:
                return int(chat_key[len(prefix):])
            except ValueError:
                return 0
    return 0


def _candidate_chat_keys(chat_id: int) -> list[str]:
    """A bare Telegram chat id could be either internal key — try both. In
    practice only one exists (group ids are negative, user ids positive), so
    this never returns two real matches, it's just cheaper than a lookup."""
    return [f"user_{chat_id}", f"group_{chat_id}"]


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _format_message(m: dict[str, Any]) -> dict[str, Any]:
    sender = m.get("senderName") or ("владелец" if m.get("direction") == "out" else "собеседник")
    return {
        "from": sender,
        "direction": m.get("direction"),
        "text": m.get("text") or "",
        "date": _iso(m.get("date")),
        "message_id": m.get("messageId"),
    }


def _format_task(t: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": t.get("task"),
        "details": t.get("details"),
        "ticktick_id": t.get("ticktickTaskId"),
        "project_id": t.get("projectId"),
        "created_at": _iso(t.get("createdAt")),
        "chat_id": _numeric_chat_id(t.get("chatId") or ""),
        "status": t.get("status"),
        "source_message_ids": t.get("sourceMessageIds") or [],
    }


# ---------------------------------------------------------------------------
# Core queries (plain functions, unit-tested directly — the @mcp.tool()
# wrappers below just expose them; see tests/test_mcp_readonly.py, same
# pure-logic-vs-transport split as tests/test_verify_still_open.py).
# ---------------------------------------------------------------------------

async def _conversations_in_range(
    start_utc: datetime, end_utc: datetime, chat_id: int | None
) -> list[dict[str, Any]]:
    db = get_db()
    query: dict[str, Any] = {"date": {"$gte": start_utc, "$lt": end_utc}}
    if chat_id is not None:
        query["chatId"] = {"$in": _candidate_chat_keys(chat_id)}
    cursor = db.raw_messages.find(query).sort("date", 1)
    by_chat: dict[str, list[dict[str, Any]]] = {}
    async for doc in cursor:
        by_chat.setdefault(doc["chatId"], []).append(doc)

    result: list[dict[str, Any]] = []
    for cid, msgs in by_chat.items():
        title = await repo.get_chat_title(cid)
        result.append({
            "chat_id": _numeric_chat_id(cid),
            "chat_type": "group" if cid.startswith("group_") else "dm",
            "title": title,
            "date_from": _iso(msgs[0]["date"]),
            "date_to": _iso(msgs[-1]["date"]),
            "messages": [_format_message(m) for m in msgs],
        })
    result.sort(key=lambda c: c["date_from"] or "")
    return result


async def _tasks_in_range(
    start_utc: datetime, end_utc: datetime, chat_id: int | None
) -> list[dict[str, Any]]:
    if chat_id is not None:
        candidate_ids = _candidate_chat_keys(chat_id)
    else:
        candidate_ids = [c["chatId"] for c in await repo.list_known_chats()]

    out: list[dict[str, Any]] = []
    for cid in candidate_ids:
        tasks = await repo.get_tasks_created_between(cid, start_utc, end_utc)
        out.extend(_format_task(t) for t in tasks)
    out.sort(key=lambda t: t["created_at"] or "")
    return out


async def list_conversations(days_back: int = 3, chat_id: int | None = None) -> list[dict[str, Any]]:
    """Raw conversation transcripts for the last `days_back` CALENDAR days
    (today counts as one; NOT "active days" — every calendar day in the
    window is scanned, empty ones just contribute nothing to the result;
    there is no reusable "active days" helper elsewhere in this codebase to
    build on for this call).

    Reads straight from `raw_messages` (TTL app.config.Settings.raw_ttl_days,
    default 90 — anything older is already gone), grouped by chat. If
    `chat_id` is given, only that chat is returned (a bare Telegram chat id;
    both internal key shapes, "user_<id>" and "group_<id>", are tried, but
    only one ever actually matches since group ids are negative and user ids
    are positive). Returns one entry per chat covering the WHOLE window
    (not one entry per day) with fields: chat_id, chat_type ("group"/"dm"),
    title, date_from, date_to, messages (chronological, each
    {from, direction, text, date, message_id}).
    """
    settings = get_settings()
    tz = _tz(settings.default_timezone)
    today = datetime.now(tz).date()
    start_utc = _local_midnight_utc(today - timedelta(days=max(days_back, 1) - 1), tz)
    end_utc = _local_midnight_utc(today + timedelta(days=1), tz)
    return await _conversations_in_range(start_utc, end_utc, chat_id)


async def list_tasks(days_back: int = 3, chat_id: int | None = None) -> list[dict[str, Any]]:
    """Tasks CREATED in the last `days_back` calendar days (createdAt, local
    midnight `days_back` days ago through now), oldest first. Reuses
    repo.get_tasks_created_between per chat. Fields per task: title, details,
    ticktick_id, project_id, created_at, chat_id, status, source_message_ids
    (the raw_messages ids the task was extracted from — provenance, see
    app/pipeline/batch.py's sourceMessageIds).

    If `chat_id` is omitted, every chat the bot has ever seen is scanned
    (repo.list_known_chats) — fine for a personal single-tenant instance's
    task volume, but not something to lean on for a huge history.
    """
    settings = get_settings()
    tz = _tz(settings.default_timezone)
    today = datetime.now(tz).date()
    start_utc = _local_midnight_utc(today - timedelta(days=max(days_back, 1) - 1), tz)
    end_utc = datetime.now(timezone.utc)
    return await _tasks_in_range(start_utc, end_utc, chat_id)


async def get_daily_bundle(
    date: str | None = None, active_days: int = 3, chat_id: int | None = None
) -> dict[str, Any]:
    """One-call daily snapshot — the main entry point most callers should
    use instead of orchestrating list_conversations/list_tasks themselves.

    `date` is the target day, "YYYY-MM-DD" in this instance's own timezone
    (Settings.default_timezone — see app/config.py); omitted -> today. Walks
    backward from that date one calendar day at a time, SKIPPING days with no
    messages at all (or no messages in `chat_id`, if given), until
    `active_days` days that DO have at least one message are found, or until
    the scan has looked back _SCAN_CAP_DAYS (14) calendar days — whichever
    comes first. Hitting the cap is not an error: the bundle is returned with
    however many active days were actually found (possibly zero).

    Once the active days are known, conversations + tasks are pulled for the
    WHOLE span from the earliest active day found through `date` inclusive
    (not just the active days themselves) via the same range queries
    list_conversations/list_tasks use, so nothing in between is silently
    dropped.

    Returns: requested_date, active_days_included (dates actually used, or
    [] if none had any messages), conversations, tasks_created, and a `note`
    pointing at list_conversations/list_tasks for a different range.
    """
    settings = get_settings()
    tz = _tz(settings.default_timezone)
    target = _parse_date(date, tz)
    candidate_ids = _candidate_chat_keys(chat_id) if chat_id is not None else None

    db = get_db()
    active_dates: list[date] = []
    day = target
    scanned = 0
    while len(active_dates) < active_days and scanned < _SCAN_CAP_DAYS:
        day_start = _local_midnight_utc(day, tz)
        day_end = _local_midnight_utc(day + timedelta(days=1), tz)
        query: dict[str, Any] = {"date": {"$gte": day_start, "$lt": day_end}}
        if candidate_ids is not None:
            query["chatId"] = {"$in": candidate_ids}
        found = await db.raw_messages.find_one(query, {"_id": 1})
        if found is not None:
            active_dates.append(day)
        day -= timedelta(days=1)
        scanned += 1

    note = "for a wider or different range call list_conversations(days_back=N) / list_tasks(days_back=N) directly"

    if not active_dates:
        return {
            "requested_date": target.isoformat(),
            "active_days_included": [],
            "conversations": [],
            "tasks_created": [],
            "note": note,
        }

    earliest = min(active_dates)
    range_start = _local_midnight_utc(earliest, tz)
    range_end = _local_midnight_utc(target + timedelta(days=1), tz)
    conversations = await _conversations_in_range(range_start, range_end, chat_id)
    tasks = await _tasks_in_range(range_start, range_end, chat_id)

    return {
        "requested_date": target.isoformat(),
        "active_days_included": sorted(d.isoformat() for d in active_dates),
        "conversations": conversations,
        "tasks_created": tasks,
        "note": note,
    }


# ---------------------------------------------------------------------------
# MCP tool registration — thin wrappers so @mcp.tool()'s introspection sees
# clean signatures/docstrings; mcp.server.fastmcp.tool() registers and
# returns `fn` unchanged, so these stay directly callable (and unit-testable)
# without going through the protocol layer.
# ---------------------------------------------------------------------------

mcp.tool(name="list_conversations")(list_conversations)
mcp.tool(name="list_tasks")(list_tasks)
mcp.tool(name="get_daily_bundle")(get_daily_bundle)


# ---------------------------------------------------------------------------
# aiohttp <-> ASGI bridge + mounting
# ---------------------------------------------------------------------------

async def _asgi_bridge(asgi_handler: Any, request: web.Request) -> web.StreamResponse:
    """Drive an ASGI-only callable (here, StreamableHTTPSessionManager's
    handle_request) from an aiohttp handler. aiohttp has no native ASGI host;
    this local scope/receive/send bridge is small enough that pulling in a
    second web framework (Starlette/uvicorn) just to serve one endpoint isn't
    worth it. Streams the response body both ways so the SSE case (a
    standalone GET notification stream, or an SSE tool-call reply) works too,
    not just single-shot JSON.
    """
    body = await request.read()
    body_sent = False

    async def receive() -> dict[str, Any]:
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        # No further body chunks are coming. Poll for the client hanging up
        # so a long-lived GET (SSE) stream can be cancelled from the mcp
        # side — a bare "wait forever" would never notice a real disconnect.
        while not (request.transport is None or request.transport.is_closing()):
            await asyncio.sleep(1)
        return {"type": "http.disconnect"}

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": request.method,
        "scheme": request.scheme,
        "path": request.path,
        "raw_path": request.raw_path.encode(),
        "query_string": request.query_string.encode(),
        "root_path": "",
        "headers": list(request.raw_headers),
        "client": (request.remote or "", 0),
        "server": (request.url.host or "", request.url.port or 0),
        "state": {},
    }

    response: web.StreamResponse | None = None
    start_info: dict[str, Any] = {}

    async def send(message: dict[str, Any]) -> None:
        nonlocal response
        if message["type"] == "http.response.start":
            start_info["status"] = message["status"]
            start_info["headers"] = message.get("headers", [])
        elif message["type"] == "http.response.body":
            if response is None:
                response = web.StreamResponse(status=start_info.get("status", 200))
                for k, v in start_info.get("headers", []):
                    if k.lower() == b"content-length":
                        continue  # StreamResponse manages its own framing
                    response.headers[k.decode("latin-1")] = v.decode("latin-1")
                await response.prepare(request)
            chunk = message.get("body", b"")
            if chunk:
                await response.write(chunk)

    await asgi_handler(scope, receive, send)

    if response is None:
        response = web.StreamResponse(status=start_info.get("status", 204))
        for k, v in start_info.get("headers", []):
            response.headers[k.decode("latin-1")] = v.decode("latin-1")
        await response.prepare(request)
    await response.write_eof()
    return response


def register_routes(app: web.Application) -> None:
    """Mount the read-only MCP endpoint at /mcp/<MCP_READONLY_SECRET>.

    Fail-open, same pattern as QWEN_BASE_URL/TRANSCRIBE_URL/BACKUP_S3_* in
    config.py: an empty secret means the feature is off and nothing is
    mounted — no route, no session manager, no background task.

    Lifecycle: the StreamableHTTPSessionManager needs its .run() context
    active for the process lifetime. Wired through aiohttp's own
    on_startup/on_cleanup signals, which app/web/server.py's start_web()
    already drives via AppRunner.setup()/.cleanup() — no changes needed in
    app/main.py.
    """
    secret = (get_settings().mcp_readonly_secret or "").strip()
    if not secret:
        logger.info("MCP_READONLY_SECRET is not set — read-only MCP server is OFF.")
        return

    session_manager = StreamableHTTPSessionManager(
        app=mcp._mcp_server,  # noqa: SLF001 — the low-level Server; FastMCP itself only exposes it via a Starlette app we don't need
        json_response=True,
        stateless=False,
    )

    async def handle_mcp(request: web.Request) -> web.StreamResponse:
        return await _asgi_bridge(session_manager.handle_request, request)

    app.add_routes([web.route("*", f"/mcp/{secret}", handle_mcp)])

    async def _start(_app: web.Application) -> None:
        cm = session_manager.run()
        await cm.__aenter__()
        _app["mcp_readonly_session_manager_cm"] = cm

    async def _stop(_app: web.Application) -> None:
        cm = _app.get("mcp_readonly_session_manager_cm")
        if cm is not None:
            await cm.__aexit__(None, None, None)

    app.on_startup.append(_start)
    app.on_cleanup.append(_stop)
    # Deliberately not logging the path — it contains the secret.
    logger.info("Read-only MCP server mounted (tools: list_conversations, list_tasks, get_daily_bundle)")
