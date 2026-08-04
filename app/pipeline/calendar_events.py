"""Calendar-events capture stage — a SEPARATE pipeline stage, downstream of
triage (app/pipeline/triage.py), that turns triaged Telegram signals with an
actual scheduled-meeting shape into events on an ISOLATED secondary Google
Calendar ("AI Captured"), via app/calendar_mcp.py — see that module's
docstring for the real calendar-mcp server constraints this stage is shaped
around.

── 0. Why a separate stage, not a claims-schema extension ─────────────────

app/pipeline/claims.py's CLAIMS_SCHEMA has no `activity_type`/`counterparty`
slots a calendar title needs to be ASSEMBLED (not summarized) from, and
CLAIMS_SYSTEM is a working prompt already producing real TickTick tasks —
extending it risks a task-creation regression for a feature that ships
behind its own OFF-by-default flag. A separate stage means a separate
model call, a separate interval, a separate flag, and a trivial rollback
(delete one scheduler job) if anything goes wrong. The cost is one extra
batched LLM call every CALENDAR_EVENTS_INTERVAL_MINUTES — cheap.

── 1. Idempotency model (three levels, see app/calendar_mcp.py's docstring
for why extendedProperties.private — the usual Google-API idempotency
pattern — is NOT available against the deployed calendar-mcp server) ──────

  1. `calendar_events.event_key` (Mongo unique index, see app/db.py) — a
     CONTENT hash (sha256 of activity_type|counterparty_norm|start_key),
     never a random id. This matters because app/signals.py resets
     `calendar_extracted` (alongside `triage`/`claimed`) whenever a
     chat/day bucket's message set changes (_STALE_ON_CONTENT_CHANGE) —
     the SAME bucket WILL be re-extracted, and a random key would create a
     duplicate event on every re-extraction. A content key means a
     re-extraction upserts the SAME row instead.
     `source_id`/signal identity is deliberately NOT part of the key: the
     same real-world meeting mentioned in two different chats collapses
     onto ONE event (signal_ids accrues both via $addToSet), matching
     app/pipeline/claim_dedup.py's cross-source-dedup spirit.
  2. The `[tg-ai-assistant] key=<event_key>` marker line in the created
     event's description (build_description) + find_event_by_marker — a
     self-healing check so a process crash between a successful Google
     write and the local Mongo status update doesn't create a second
     event next tick.
  3. (Future, not implemented) `extendedProperties.private` once
     calendar-mcp's schema accepts it — see settings.
     calendar_mcp_supports_extended.

── 2. Status lifecycle of a `calendar_events` doc ──────────────────────────

    pending -> created                 (create_pending_events succeeds, or
                                         find_event_by_marker self-heals)
    pending -> pending (attempts += 1)  (create_event failed, retryable)
    pending -> failed                   (attempts hit calendar_events_
                                         max_attempts)
    (written directly at extraction)   skipped  (normalize_times failed, or
                                         confidence below threshold — never
                                         attempted)

`upsert_calendar_event` NEVER lets a re-extraction's content $set touch
`status`/`skip_reason`/`google_event_id`/`google_html_link`/`calendar_id`/
`account`/`attempts`/`last_error` — those are $setOnInsert-only (first
write wins) or explicit updates from the create phase. Without this, a
re-extraction of an already-`created` event (see point 1 above — a
same-day content change WILL trigger one) would silently reset it back to
`pending`, and the create phase would try to create it again.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from ..audit.log import finalize_mutation, record_mutation
from ..calendar_mcp import CalendarMCP, resolve_calendar
from ..db import get_db
from ..llm import claude
from ..repositories import utcnow
from ..signals import upsert_signal
from .triage import signal_id as triage_signal_id

log = logging.getLogger(__name__)

# Human label for each activity_type — see build_event_title. "other" is the
# CALENDAR_EVENTS_SCHEMA fallback bucket and also the default when the model
# (or a hand-built test doc) sends an unrecognised value.
ACTIVITY_LABELS: dict[str, str] = {
    "meeting": "Встреча",
    "call": "Созвон",
    "visit": "Визит",
    "appointment": "Приём",
    "trip": "Поездка",
    "other": "Событие",
}

# Idempotency/restore-anchor marker prefix — see module docstring §1.2 and
# build_description/find_event_by_marker.
MARKER_PREFIX = "[tg-ai-assistant]"

# get_pending_calendar_events' cap per tick — a huge backlog (e.g. after a
# long CALENDAR_EVENTS_ENABLED=false review period) must not blow up one
# tick's worth of calendar-mcp calls.
_CREATE_BATCH_CAP = 50

_EDGE_STRIP_RE = re.compile(r"^[^\w]+|[^\w]+$", re.UNICODE)


# ── build_event_title — ASSEMBLY, not summarization ─────────────────────


def _clean_counterparty_words(counterparty: str) -> list[str]:
    """Split `counterparty` on whitespace and strip quotes/emoji/punctuation
    from each word's edges (`\\w` under re.UNICODE covers Cyrillic letters,
    so an edge-emoji or edge-quote is stripped but the word itself isn't
    mangled)."""
    words: list[str] = []
    for raw in counterparty.strip().split():
        w = _EDGE_STRIP_RE.sub("", raw)
        if w:
            words.append(w)
    return words


def _truncate_at_word_boundary(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip()


def build_event_title(activity_type: str, counterparty: str | None) -> str:
    """ASSEMBLE a 1-3 word title from slots — never reads a source
    quote/summary at all (no such parameter exists on this function — see
    tests/test_calendar_events.py's inspect.signature check, the guarantee
    that this is assembly, not abstractive compression of source text).

    `label: контрагент` (colon, not the Russian preposition "с") — "с
    Иваном" needs the counterparty declined into the instrumental case,
    which this function cannot do correctly for an arbitrary name/role
    string; a wrong declension would be its own small hallucination. The
    colon sidesteps grammar entirely. Falls back to the bare label when
    counterparty is empty/unresolvable. Result capped to 40 chars at a word
    boundary."""
    label = ACTIVITY_LABELS.get(activity_type, "Событие")
    if not counterparty or not counterparty.strip():
        return label
    words = _clean_counterparty_words(counterparty)
    if not words:
        return label
    who = " ".join(words[:2])
    return _truncate_at_word_boundary(f"{label}: {who}", 40)


# ── event_key — content hash, not a random id (see module docstring §1) ──


def event_key(activity_type: str, counterparty: str | None, start_key: str) -> str:
    counterparty_norm = re.sub(r"\s+", " ", (counterparty or "").strip()).lower()
    raw = f"{activity_type}|{counterparty_norm}|{start_key}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _start_key(start: str, all_day: bool, settings: Any) -> str:
    """The event_key's time component: the bare date for an all-day event,
    else "YYYY-MM-DDTHH:MM" in settings.default_timezone — deliberately
    minute-precision (not seconds), so two extractions of the same meeting
    that differ only in seconds-level jitter still collapse onto one key."""
    if all_day:
        return start
    dt = datetime.fromisoformat(start)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(settings.default_timezone))
    dt = dt.astimezone(ZoneInfo(settings.default_timezone))
    return dt.strftime("%Y-%m-%dT%H:%M")


# ── normalize_times — pure, no Mongo/network ────────────────────────────


def _to_local(dt: datetime, tz: ZoneInfo) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz)


def normalize_times(
    ev: dict[str, Any], anchor: datetime, settings: Any
) -> tuple[str, str, bool] | None:
    """Normalize one extracted event's start/end into calendar-ready
    strings, or None when it should be rejected outright (never created,
    see run_calendar_events_tick's "unparseable_start"/"too_far_past"/
    "too_far_future" skip_reason handling for what a caller does with a
    None here).

    Returns (start, end, all_day):
      all_day=True  -> start/end are bare "YYYY-MM-DD" (end = start + 1 day
                        — Google's `end.date` is EXCLUSIVE, so a same-day
                        all-day event needs the day AFTER to render as one
                        day, not zero).
      all_day=False -> start/end are `datetime.isoformat()` (colon-offset
                        RFC3339, e.g. "...+03:00" — NOT `%z`'s "+0300",
                        which pipeline/dedup.py::to_ticktick_due uses for
                        TickTick's own due-date format but which Google
                        Calendar's API rejects as invalid RFC3339; that
                        function's zone-handling STYLE is a fine reference,
                        the function itself is not reused here). A
                        naive/missing end, or end <= start, falls back to
                        start + calendar_event_default_duration_minutes.

    Rejects (returns None): an unparseable `start`; a start older than
    `anchor - calendar_event_max_past_days`; a start beyond `anchor +
    calendar_event_max_future_days`; or a start whose year falls outside
    [anchor.year - 1, anchor.year + 2] (belt-and-braces against a
    hallucinated date the day/offset checks alone wouldn't catch, e.g. a
    wildly wrong year with an otherwise "close" day-of-year)."""
    tz = ZoneInfo(settings.default_timezone)
    raw_start = ev.get("start")
    all_day = bool(ev.get("all_day"))

    if not isinstance(raw_start, str) or not raw_start.strip():
        return None

    if all_day:
        try:
            start_date = datetime.strptime(raw_start.strip(), "%Y-%m-%d").date()
        except ValueError:
            return None
        start_local = datetime(start_date.year, start_date.month, start_date.day, tzinfo=tz)
        start_out = start_date.isoformat()
        end_out = (start_date + timedelta(days=1)).isoformat()
    else:
        try:
            start_local = datetime.fromisoformat(raw_start.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        start_local = _to_local(start_local, tz)

        end_local: datetime | None = None
        raw_end = ev.get("end")
        if isinstance(raw_end, str) and raw_end.strip():
            try:
                end_local = _to_local(
                    datetime.fromisoformat(raw_end.strip().replace("Z", "+00:00")), tz
                )
            except ValueError:
                end_local = None
        if end_local is None or end_local <= start_local:
            end_local = start_local + timedelta(
                minutes=settings.calendar_event_default_duration_minutes
            )
        start_out = start_local.isoformat()
        end_out = end_local.isoformat()

    anchor_local = _to_local(anchor, tz)
    if start_local < anchor_local - timedelta(days=settings.calendar_event_max_past_days):
        return None
    if start_local > anchor_local + timedelta(days=settings.calendar_event_max_future_days):
        return None
    if not (anchor_local.year - 1 <= start_local.year <= anchor_local.year + 2):
        return None

    return start_out, end_out, all_day


def _classify_skip_reason(ev: dict[str, Any], anchor: datetime, settings: Any) -> str:
    """Best-effort reason string for a normalize_times() rejection — used
    only for the stored `skip_reason` field (diagnostics), never re-derives
    a normalized time. Falls back to "unparseable_start" for anything that
    doesn't clearly land in the past/future buckets (including a bad year,
    which is a form of "not really parseable as a sane date")."""
    tz = ZoneInfo(settings.default_timezone)
    raw_start = ev.get("start")
    if not isinstance(raw_start, str) or not raw_start.strip():
        return "unparseable_start"
    try:
        if ev.get("all_day"):
            start_local = datetime.strptime(raw_start.strip(), "%Y-%m-%d").replace(tzinfo=tz)
        else:
            start_local = _to_local(
                datetime.fromisoformat(raw_start.strip().replace("Z", "+00:00")), tz
            )
    except ValueError:
        return "unparseable_start"
    anchor_local = _to_local(anchor, tz)
    if start_local < anchor_local - timedelta(days=settings.calendar_event_max_past_days):
        return "too_far_past"
    if start_local > anchor_local + timedelta(days=settings.calendar_event_max_future_days):
        return "too_far_future"
    return "unparseable_start"


# ── verify_quote / build_description ────────────────────────────────────


def _collapse_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip())


def verify_quote(quote: str | None, summary: str | None) -> bool:
    """True only for an EXACT (whitespace-collapsed) substring match of
    `quote` inside `summary` — a paraphrase (even a close one) fails this,
    by design: description's quote block must never show text the source
    didn't literally contain."""
    if not quote or not summary:
        return False
    return _collapse_ws(quote) in _collapse_ws(summary)


def build_description(
    sig: dict[str, Any], ev: dict[str, Any], key: str, quote_verified: bool
) -> str:
    """Fixed block order, empty blocks omitted — see module docstring §1.2
    for why the last line (the marker) exists at all. Nothing beyond these
    blocks is ever written: no LLM commentary, no invented detail."""
    lines: list[str] = []
    quote = ev.get("evidence_quote")
    if quote_verified and quote:
        lines.append(f"«{quote}»")
    title = sig.get("title") or ""
    raw_ref = sig.get("raw_ref") or {}
    date = raw_ref.get("date") or ""
    lines.append(f"Чат: {title} · {date}")
    source = sig.get("source") or "telegram"
    lines.append(f"Источник: {source} · {triage_signal_id(sig)}")
    if ev.get("needs_clarification"):
        lines.append("⚠️ Уточнить время")
    lines.append(f"{MARKER_PREFIX} key={key}")
    return "\n".join(lines)


# ── get_extractable_signals ─────────────────────────────────────────────


async def get_extractable_signals(since: datetime) -> list[dict[str, Any]]:
    """`signals` docs with ts_start >= `since`, triage.verdict in ("auto",
    "decision"), not yet `calendar_extracted` — same query shape as
    app/pipeline/claims.py::get_claimable_signals, substituting
    `calendar_extracted` for `claimed`."""
    db = get_db()
    cursor = db.signals.find(
        {
            "ts_start": {"$gte": since},
            "triage.verdict": {"$in": ["auto", "decision"]},
            "calendar_extracted": {"$ne": True},
        }
    )
    return [d async for d in cursor]


# ── upsert_calendar_event ───────────────────────────────────────────────

# Fields a content upsert must NEVER touch — see module docstring §2 for why
# (protects an already-created/failed row from being reset by a later
# re-extraction of the same signal).
_UPSERT_PROTECTED_FIELDS = {
    "event_key",
    "status",
    "skip_reason",
    "chat_id",
}


async def upsert_calendar_event(doc: dict[str, Any], signal_id: str) -> dict[str, Any]:
    """Upsert one `calendar_events` row by its (content-hash) event_key.

    `doc` carries the CONTENT fields for this extraction (title,
    activity_type, counterparty, start, end, all_day, time_zone,
    description, evidence_quote, quote_verified, confidence,
    needs_clarification) plus the bookkeeping-only `event_key`, `status`,
    `skip_reason` (initial value, first-insert only — see
    _UPSERT_PROTECTED_FIELDS), and optional `chat_id` (folded into the
    `chat_ids` array via $addToSet, alongside `signal_id` into
    `signal_ids` — the same "accrues across sources" convention as
    app/pipeline/claims.py's claim docs).

    Returns the stored document after the upsert."""
    db = get_db()
    now = utcnow()
    key = doc["event_key"]
    chat_id = doc.get("chat_id")
    content = {k: v for k, v in doc.items() if k not in _UPSERT_PROTECTED_FIELDS}
    content["updated_at"] = now

    add_to_set: dict[str, Any] = {"signal_ids": signal_id}
    if chat_id:
        add_to_set["chat_ids"] = chat_id

    update: dict[str, Any] = {
        "$setOnInsert": {
            "event_key": key,
            "created_at": now,
            "status": doc.get("status") or "pending",
            "skip_reason": doc.get("skip_reason"),
            "attempts": 0,
            "google_event_id": None,
            "google_html_link": None,
            "calendar_id": None,
            "account": None,
            "last_error": None,
            "synced_at": None,
        },
        "$set": content,
        "$addToSet": add_to_set,
    }
    await db.calendar_events.update_one({"event_key": key}, update, upsert=True)
    return await db.calendar_events.find_one({"event_key": key})


async def get_pending_calendar_events(settings: Any) -> list[dict[str, Any]]:
    """`calendar_events` docs still needing a real Google write: status
    "pending" with attempts below the retry cap, oldest first, capped at
    _CREATE_BATCH_CAP per tick."""
    db = get_db()
    cursor = db.calendar_events.find(
        {"status": "pending", "attempts": {"$lt": settings.calendar_events_max_attempts}}
    ).sort("created_at", 1)
    docs = [d async for d in cursor]
    return docs[:_CREATE_BATCH_CAP]


def _marker_search_window(doc: dict[str, Any]) -> tuple[str, str]:
    """±1 day (UTC, Z-suffixed) around the event's own start — the window
    find_event_by_marker searches for the self-healing marker lookup."""
    raw_start = doc.get("start") or ""
    try:
        if doc.get("all_day"):
            dt = datetime.strptime(raw_start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        else:
            dt = datetime.fromisoformat(raw_start)
    except ValueError:
        dt = utcnow()
    dt = dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return (dt - timedelta(days=1)).strftime(fmt), (dt + timedelta(days=1)).strftime(fmt)


async def create_pending_events(client: CalendarMCP, settings: Any) -> int:
    """Phase 2: actually write pending `calendar_events` rows to Google via
    `client`. For each pending doc: try the self-healing marker lookup
    first (find_event_by_marker — a doc could be `pending` locally but
    already exist on Google if a previous tick crashed between the Google
    write and the local status update); on a miss, create it for real
    behind the two-phase audit log (fail-open: an audit-logging failure
    must never prevent the real creation, see the try/except around
    record_mutation/finalize_mutation below).

    Returns the number of rows that ended this call as "created" (whether
    via a fresh create_event or a self-healed marker match)."""
    docs = await get_pending_calendar_events(settings)
    if not docs:
        return 0

    db = get_db()
    created = 0
    for doc in docs:
        key = doc["event_key"]

        found: dict[str, Any] | None = None
        try:
            time_min, time_max = _marker_search_window(doc)
            found = await client.find_event_by_marker(
                marker=f"key={key}", time_min=time_min, time_max=time_max
            )
        except Exception:  # noqa: BLE001 — self-heal check is best-effort
            log.warning("create_pending_events: marker lookup failed for %s", key, exc_info=True)

        if found is not None:
            await db.calendar_events.update_one(
                {"event_key": key},
                {
                    "$set": {
                        "status": "created",
                        "google_event_id": found.get("id"),
                        "google_html_link": found.get("htmlLink"),
                        "calendar_id": settings.calendar_target_calendar_id,
                        "account": settings.calendar_mcp_account or None,
                        "last_error": None,
                        "synced_at": utcnow(),
                    }
                },
            )
            created += 1
            continue

        rid = None
        try:
            rid = await record_mutation(
                server="calendar-mcp",
                tool="calendar_event_create",
                target={
                    "id": key,
                    "type": "calendar_event",
                    "calendar_id": settings.calendar_target_calendar_id,
                },
                before=None,
                after={"summary": doc.get("title"), "start": doc.get("start"), "end": doc.get("end")},
                op="create",
                capture_plane="in_band",
                actor={"kind": "automation", "source": "calendar_events"},
                restore={"calendar_id": settings.calendar_target_calendar_id, "event_key": key},
            )
        except Exception:  # noqa: BLE001 — audit logging must never block a real mutation
            log.warning("create_pending_events: record_mutation failed for %s", key, exc_info=True)

        try:
            result = await client.create_event(
                summary=doc.get("title") or "",
                start=doc.get("start"),
                end=doc.get("end"),
                description=doc.get("description") or "",
                all_day=bool(doc.get("all_day")),
                time_zone=doc.get("time_zone") or settings.default_timezone,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("create_pending_events: create_event failed for %s", key, exc_info=True)
            attempts = int(doc.get("attempts") or 0) + 1
            new_status = "failed" if attempts >= settings.calendar_events_max_attempts else "pending"
            await db.calendar_events.update_one(
                {"event_key": key},
                {"$inc": {"attempts": 1}, "$set": {"last_error": str(exc)[:500], "status": new_status}},
            )
            try:
                await finalize_mutation(
                    rid,
                    after=None,
                    result={"status": "error", "record_id": None, "error": str(exc)[:500], "verified": False},
                )
            except Exception:  # noqa: BLE001
                log.warning("create_pending_events: finalize_mutation (error path) failed for %s", key, exc_info=True)
            continue

        google_id = result.get("id") if isinstance(result, dict) else None
        await db.calendar_events.update_one(
            {"event_key": key},
            {
                "$set": {
                    "status": "created",
                    "google_event_id": google_id,
                    "google_html_link": result.get("htmlLink") if isinstance(result, dict) else None,
                    "calendar_id": settings.calendar_target_calendar_id,
                    "account": settings.calendar_mcp_account or None,
                    "last_error": None,
                    "synced_at": utcnow(),
                }
            },
        )
        try:
            await finalize_mutation(
                rid,
                after={"google_event_id": google_id},
                result={"status": "ok", "record_id": str(rid) if rid else None, "error": None, "verified": False},
            )
        except Exception:  # noqa: BLE001
            log.warning("create_pending_events: finalize_mutation (ok path) failed for %s", key, exc_info=True)
        created += 1
    return created


# ── run_calendar_events_tick ─────────────────────────────────────────────

_logged_creation_disabled = False


def _format_anchor(ts_start: Any, tz: ZoneInfo) -> str:
    """"2026-08-03 (Monday) 09:12 America/Los_Angeles" — the per-signal
    anchor line CALENDAR_EVENTS_SYSTEM requires every relative time
    expression to be resolved against (see app/llm/claude.py::
    _calendar_signal_line). Empty when ts_start isn't a real datetime
    (defensive; signals.py always stores one)."""
    if not isinstance(ts_start, datetime):
        return ""
    local = _to_local(ts_start, tz)
    return local.strftime("%Y-%m-%d (%A) %H:%M ") + str(tz)


async def _extract_and_store(pending_signals: list[dict[str, Any]], settings: Any) -> int:
    tz = ZoneInfo(settings.default_timezone)
    items = []
    for doc in pending_signals:
        ts_start = doc.get("ts_start")
        items.append(
            {
                "title": doc.get("title") or "",
                "summary": doc.get("summary") or "",
                "participants_raw": doc.get("participants_raw") or [],
                "source": doc.get("source"),
                "ts_start": ts_start.isoformat() if isinstance(ts_start, datetime) else ts_start,
                "anchor_local": _format_anchor(ts_start, tz),
            }
        )
    results = await claude.extract_calendar_events_batch(items, settings.default_timezone)
    if not results:
        return 0

    by_index = {
        r.get("index"): r
        for r in results
        if isinstance(r, dict) and isinstance(r.get("index"), int)
    }

    extracted = 0
    for i, sig in enumerate(pending_signals):
        r = by_index.get(i)
        if r is None:
            log.info(
                "run_calendar_events_tick: %s not extracted this tick, will retry",
                triage_signal_id(sig),
            )
            continue
        events = r.get("events") if isinstance(r.get("events"), list) else []
        anchor_dt = sig.get("ts_start")
        if not isinstance(anchor_dt, datetime):
            anchor_dt = utcnow()
        sig_id_str = triage_signal_id(sig)
        chat_id = (sig.get("raw_ref") or {}).get("chat_id")

        for ev in events[: settings.calendar_events_max_per_signal]:
            if not isinstance(ev, dict):
                continue
            try:
                confidence_f = float(ev.get("confidence"))
            except (TypeError, ValueError):
                confidence_f = 0.0
            activity_type = ev.get("activity_type")
            if activity_type not in ACTIVITY_LABELS:
                activity_type = "other"
            counterparty = ev.get("counterparty") if isinstance(ev.get("counterparty"), str) else None

            normalized = normalize_times(ev, anchor_dt, settings)
            if normalized is None:
                skip_reason = _classify_skip_reason(ev, anchor_dt, settings)
                raw_start = ev.get("start") if isinstance(ev.get("start"), str) else ""
                start_key = raw_start.strip() or "unknown"
                key = event_key(activity_type, counterparty, start_key)
                await upsert_calendar_event(
                    {
                        "event_key": key,
                        "status": "skipped",
                        "skip_reason": skip_reason,
                        "title": build_event_title(activity_type, counterparty),
                        "activity_type": activity_type,
                        "counterparty": counterparty,
                        "start": raw_start or None,
                        "end": ev.get("end") if isinstance(ev.get("end"), str) else None,
                        "all_day": bool(ev.get("all_day")),
                        "time_zone": settings.default_timezone,
                        "description": build_description(sig, ev, key, False),
                        "evidence_quote": None,
                        "quote_verified": False,
                        "confidence": confidence_f,
                        "needs_clarification": bool(ev.get("needs_clarification")),
                        "chat_id": chat_id,
                    },
                    sig_id_str,
                )
                continue

            start, end, all_day = normalized
            start_key = _start_key(start, all_day, settings)
            key = event_key(activity_type, counterparty, start_key)

            if confidence_f < settings.calendar_events_min_confidence:
                await upsert_calendar_event(
                    {
                        "event_key": key,
                        "status": "skipped",
                        "skip_reason": "low_confidence",
                        "title": build_event_title(activity_type, counterparty),
                        "activity_type": activity_type,
                        "counterparty": counterparty,
                        "start": start,
                        "end": end,
                        "all_day": all_day,
                        "time_zone": settings.default_timezone,
                        "description": build_description(sig, ev, key, False),
                        "evidence_quote": None,
                        "quote_verified": False,
                        "confidence": confidence_f,
                        "needs_clarification": bool(ev.get("needs_clarification")),
                        "chat_id": chat_id,
                    },
                    sig_id_str,
                )
                continue

            quote = ev.get("evidence_quote") if isinstance(ev.get("evidence_quote"), str) else None
            q_ok = verify_quote(quote, sig.get("summary"))
            description = build_description(sig, ev, key, q_ok)
            await upsert_calendar_event(
                {
                    "event_key": key,
                    "status": "pending",
                    "skip_reason": None,
                    "title": build_event_title(activity_type, counterparty),
                    "activity_type": activity_type,
                    "counterparty": counterparty,
                    "start": start,
                    "end": end,
                    "all_day": all_day,
                    "time_zone": settings.default_timezone,
                    "description": description,
                    "evidence_quote": quote if q_ok else None,
                    "quote_verified": q_ok,
                    "confidence": confidence_f,
                    "needs_clarification": bool(ev.get("needs_clarification")),
                    "chat_id": chat_id,
                },
                sig_id_str,
            )

        await upsert_signal(
            {"source": sig["source"], "source_id": sig["source_id"], "calendar_extracted": True}
        )
        extracted += 1
    return extracted


async def run_calendar_events_tick(settings: Any) -> tuple[int, int]:
    """One calendar-events tick: extraction + Mongo bookkeeping ALWAYS run;
    the real Google write (phase 2) only runs when calendar_events_enabled
    AND calendar_mcp_url AND calendar_target_calendar_id are all set — see
    module docstring §2. Returns (extracted, created): `extracted` is the
    number of SIGNALS processed this tick (marked calendar_extracted,
    regardless of how many events — zero or more — they yielded);
    `created` is the number of `calendar_events` rows that ended this tick
    as "created" on Google.

    Called by calendar_events_job in app/main.py, itself wrapped in
    try/except there per this repo's "a failing tick must never kill the
    scheduler" convention — this function does not need its own top-level
    try/except."""
    global _logged_creation_disabled
    since = utcnow() - timedelta(days=settings.calendar_events_lookback_days)
    pending_signals = await get_extractable_signals(since)

    extracted = 0
    if pending_signals:
        extracted = await _extract_and_store(pending_signals, settings)

    creation_allowed = bool(
        settings.calendar_events_enabled
        and settings.calendar_mcp_url
        and settings.calendar_target_calendar_id
    )
    if not creation_allowed:
        if not _logged_creation_disabled:
            log.info(
                "Calendar events: creation disabled (CALENDAR_EVENTS_ENABLED=%s, "
                "CALENDAR_MCP_URL set=%s, CALENDAR_TARGET_CALENDAR_ID set=%s) — "
                "extraction still runs, rows stay pending. Review via "
                "get_calendar_events_queue before enabling.",
                settings.calendar_events_enabled,
                bool(settings.calendar_mcp_url),
                bool(settings.calendar_target_calendar_id),
            )
            _logged_creation_disabled = True
        return extracted, 0
    _logged_creation_disabled = False

    client = resolve_calendar()
    if client is None:
        return extracted, 0
    created = await create_pending_events(client, settings)
    return extracted, created
