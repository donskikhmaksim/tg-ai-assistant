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

Five tools:
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
  get_triage_queue     — the triaged `signals` (see app/pipeline/triage.py,
                        app/signals.py — a port of omi-task-extractor's
                        signals+triage layer): score/category/verdict per
                        chat/day activity bucket, optionally filtered to one
                        verdict ("drop"/"auto"/"decision").
  get_claims_queue     — one layer further down the pipeline than
                        get_triage_queue: ready-made task CARDS (see
                        app/pipeline/claims.py, app/pipeline/claim_dedup.py
                        — a port of omi-task-extractor's claims layer), one
                        per triaged (+ cross-source-deduped, once a second
                        signal source exists) signal or signal group.
  submit_triage_feedback — THE ONE WRITE TOOL in this otherwise strictly
                        read-only server. Deliberate, narrow exception: it
                        does not touch TickTick or any other external
                        system — it only appends a row to the internal
                        `triage_feedback` collection
                        (app/pipeline/triage.py::record_triage_feedback),
                        the seed for a future rubric-calibration loop. That
                        is purely internal bookkeeping about how well THIS
                        service's own triage call performed, not an
                        external side effect, so it does not violate "no
                        write tools here — creating/editing/completing
                        tasks happens through the owner's own separate
                        TickTick MCP connector" for anything that matters
                        externally.

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
        "raw conversation transcripts, the tasks already extracted from "
        "them, and a triaged `signals` queue. Creating/editing/completing "
        "tasks happens through the owner's own separate TickTick MCP "
        "connector, not this server — the ONE exception is "
        "submit_triage_feedback, which only records this server's own "
        "internal triage bookkeeping, never an external system. Start with "
        "get_daily_bundle for a single-call daily snapshot; fall back to "
        "list_conversations / list_tasks directly for a wider or more "
        "specific range; use get_triage_queue for the drop/auto/decision "
        "triage review, or get_claims_queue for ready-made task cards one "
        "layer further down the pipeline."
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


def _signal_triage_item(doc: dict[str, Any]) -> dict[str, Any]:
    """One get_triage_queue output item from a raw `signals` doc — see
    get_triage_queue's docstring for field meanings. Every field under
    `triage.*` is None when the signal hasn't been scored yet (shouldn't
    normally happen since the Mongo filter requires `triage` to exist, but
    kept defensive rather than assuming)."""
    triage = doc.get("triage") or {}
    return {
        "signal_id": f"{doc.get('source')}:{doc.get('source_id')}",
        "source": doc.get("source"),
        "source_id": doc.get("source_id"),
        "ts_start": _iso(doc.get("ts_start")),
        "title": doc.get("title"),
        "summary": doc.get("summary"),
        "participants_raw": doc.get("participants_raw") or [],
        "score": triage.get("score"),
        "category": triage.get("category"),
        "verdict": triage.get("verdict"),
        "reason": triage.get("reason"),
        "rubric_version": triage.get("rubric_version"),
        "scored_at": _iso(triage.get("scored_at")),
    }


async def _triage_queue_items(since: datetime, verdict: str | None) -> list[dict[str, Any]]:
    """get_triage_queue's full body, factored out for the same reason
    _conversations_in_range/_tasks_in_range are: keeps the Mongo query +
    shaping testable/reusable independent of the MCP tool wiring."""
    db = get_db()
    filt: dict[str, Any] = {"ts_start": {"$gte": since}, "triage": {"$exists": True}}
    if verdict:
        filt["triage.verdict"] = verdict
    cursor = db.signals.find(filt).sort("ts_start", -1)
    return [_signal_triage_item(d) async for d in cursor]


def _claim_queue_item(doc: dict[str, Any]) -> dict[str, Any]:
    """One get_claims_queue output item from a raw `claims` doc — see
    get_claims_queue's docstring for field meanings."""
    return {
        "claim_id": doc.get("claim_id"),
        "signal_ids": doc.get("signal_ids") or [],
        "title": doc.get("title"),
        "subtasks": doc.get("subtasks") or [],
        "description": doc.get("description"),
        "due_date": doc.get("due_date"),
        "needs_due_clarification": doc.get("needs_due_clarification"),
        "project": doc.get("project"),
        "with_whom": doc.get("with_whom"),
        "needs_clarification": doc.get("needs_clarification"),
        "verdict": doc.get("verdict"),
        "created_at": _iso(doc.get("created_at")),
    }


async def _claims_queue_items(since: datetime, verdict: str | None) -> list[dict[str, Any]]:
    """get_claims_queue's full body, factored out for the same reason
    _triage_queue_items is: keeps the Mongo query + shaping testable/
    reusable independent of the MCP tool wiring."""
    db = get_db()
    filt: dict[str, Any] = {"created_at": {"$gte": since}}
    if verdict:
        filt["verdict"] = verdict
    cursor = db.claims.find(filt).sort("created_at", -1)
    return [_claim_queue_item(d) async for d in cursor]


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


async def get_triage_queue(
    verdict: str | None = None, days_back: int = 3
) -> list[dict[str, Any]]:
    """Triaged `signals` (see app/pipeline/triage.py, app/signals.py) from
    the last `days_back` calendar days — the queue an external morning
    triage review reads from.

    `verdict` — optional filter to exactly one of "drop" / "auto" /
    "decision" (see app/pipeline/triage.py module docstring for what each
    means, driven by TRIAGE_LOW/TRIAGE_HIGH in app/config.py). None
    (default) returns all three.

    Only signals that HAVE been triaged already are returned — a signal
    still waiting on its next triage tick simply isn't in the result yet;
    there is no "pending" verdict to filter on.

    Each returned item:
      signal_id         — f"{source}:{source_id}", the same idempotency key
                          signals.upsert_signal keys on — pass this straight
                          to submit_triage_feedback.
      source             — "telegram" (the only source this repo ingests).
      source_id          — f"{internal chatId}:{YYYY-MM-DD}" — see
                          app/signals.py module docstring for why a chat/day
                          bucket is this repo's signal unit.
      ts_start            — ISO 8601, earliest message in the bucket.
      title               — chat title.
      summary             — a plain "sender: text" transcript excerpt of the
                          bucket (not an LLM summary — see app/signals.py),
                          or null.
      participants_raw    — sender display names.
      score               — 0.0-1.0 triage score.
      category            — "commitment" | "request" | "deadline" | "info" |
                          "noise".
      verdict             — "drop" | "auto" | "decision".
      reason              — one-line explanation from the triage call.
      rubric_version      — the rubric version this signal was scored under
                          (see app/pipeline/triage.py RUBRIC_VERSION).
      scored_at           — ISO 8601, when this triage verdict was written.

    Sorted newest-first by ts_start.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days_back)
    return await _triage_queue_items(since, verdict)


async def get_claims_queue(
    verdict: str | None = None, days_back: int = 3
) -> list[dict[str, Any]]:
    """Ready-made claim CARDS (see app/pipeline/claims.py) from the last
    `days_back` calendar days — one layer further down the pipeline than
    get_triage_queue: dedup (app/pipeline/claim_dedup.py) + an LLM-authored
    card per triaged signal or cross-source-matched signal group. This is
    what an external morning review ultimately acts on.

    `verdict` — optional filter to exactly one of "auto" / "decision"
    (never "drop" — dropped signals never reach the claims layer at all,
    see app/pipeline/claims.py's module docstring). None (default) returns
    both.

    Each returned item:
      claim_id                — stable id for this card.
      signal_ids               — f"{source}:{source_id}" for every
                                 signal this card is evidence for (one, or
                                 several when cross-source corroborated —
                                 see app/pipeline/claim_dedup.py; today this
                                 repo has only one signal source, so every
                                 card carries exactly one signal_id until a
                                 second source is added).
      title                     — imperative-infinitive title, 4-9 words.
      subtasks                  — [] unless the card genuinely bundles 2+
                                 distinct actions.
      description                — fixed-order card body (source quote,
                                 context, participants, open questions).
      due_date                   — absolute date/time, or null when the
                                 source named none — never invented.
      needs_due_clarification    — true if a deadline was hinted but
                                 ambiguous.
      project                    — a TickTick project name, or "unsorted"
                                 when nothing fit.
      with_whom                  — raw "with whom" string as it was said,
                                 or null.
      needs_clarification        — true if something about this card is
                                 genuinely ambiguous.
      verdict                    — "auto" | "decision", inherited from the
                                 member signal(s)' triage verdict.
      created_at                  — ISO 8601, when this card was generated.

    Sorted newest-first by created_at.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days_back)
    return await _claims_queue_items(since, verdict)


async def submit_triage_feedback(
    signal_id: str, verdict: str, note: str | None = None
) -> dict[str, Any]:
    """Record a human verdict on one triaged signal from the
    get_triage_queue result — THE ONLY WRITE tool on this server, see the
    module docstring for why this narrow exception doesn't violate the
    read-only-towards-external-systems contract.

    `signal_id` — the `signal_id` field from a get_triage_queue item
    (f"{source}:{source_id}").
    `verdict` — one of "approved" (the triage call got it right),
    "rejected" (this shouldn't have surfaced / was mis-scored), or
    "pulled_from_dropped" (this was verdict="drop" but a human found it
    anyway and it turned out to matter). Anything else is rejected with
    {"ok": false, ...} rather than silently accepted.
    `note` — optional free-text context for a future rubric-calibration
    pass.

    Returns {"ok": true} on success, or {"ok": false, "error": <str>} on any
    failure (bad verdict, Mongo error) — never raises, so a bad argument
    from the calling client doesn't tear down the MCP session.
    """
    # Local import: app/signals.py imports FROM this module
    # (mcp_readonly._tz/_local_midnight_utc/_format_message), and
    # app/pipeline/triage.py imports FROM app/signals.py (upsert_signal) — a
    # module-level import here would close that into an import cycle
    # (mcp_readonly -> triage -> signals -> mcp_readonly). Importing inside
    # the tool call sidesteps it.
    from .pipeline.triage import record_triage_feedback

    try:
        await record_triage_feedback(signal_id, verdict, note)
    except Exception as exc:  # noqa: BLE001 — bad input/Mongo error -> {"ok": false}, never raise
        return {"ok": False, "error": str(exc)}
    return {"ok": True}


# ---------------------------------------------------------------------------
# MCP tool registration — thin wrappers so @mcp.tool()'s introspection sees
# clean signatures/docstrings; mcp.server.fastmcp.tool() registers and
# returns `fn` unchanged, so these stay directly callable (and unit-testable)
# without going through the protocol layer.
# ---------------------------------------------------------------------------

mcp.tool(name="list_conversations")(list_conversations)
mcp.tool(name="list_tasks")(list_tasks)
mcp.tool(name="get_daily_bundle")(get_daily_bundle)
mcp.tool(name="get_triage_queue")(get_triage_queue)
mcp.tool(name="get_claims_queue")(get_claims_queue)
mcp.tool(name="submit_triage_feedback")(submit_triage_feedback)


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
        # ASGI spec requires the host/bridge to lower-case header names before
        # they reach the app (Starlette's Headers/mcp's transport_security
        # trust this and look up e.g. "content-type" verbatim) — raw_headers
        # come off the wire in whatever case the client sent (browsers/most
        # HTTP clients send "Content-Type"), so lower-case them here rather
        # than passing them through unchanged.
        "headers": [(k.lower(), v) for k, v in request.raw_headers],
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
    logger.info(
        "Read-only MCP server mounted (tools: list_conversations, list_tasks, "
        "get_daily_bundle, get_triage_queue, get_claims_queue, submit_triage_feedback)"
    )
