"""Unified `signals` collection — the tg-ai-assistant port of
omi-task-extractor's app/signals.py (see that repo for the shared
architecture this mirrors: a source-agnostic feed of "things that happened",
built ahead of a cheap batched triage pass — app/pipeline/triage.py — so a
morning review can start from "what needs a decision" instead of a raw
firehose of chat activity).

Schema (Mongo `signals`, unique compound index on (source, source_id) — see
app/db.py::_ensure_indexes — that pair is the idempotency key: a re-ingest
upserts in place, never a duplicate):

    source            "telegram" (the only source this port implements —
                       unlike the omi reference's omi+gmail split, this repo
                       has exactly one conversation source)
    source_id         f"{chatId}:{YYYY-MM-DD}" — see ADAPTATION below
    ts_start          datetime — earliest message in the bucket
    ts_end            datetime — latest message in the bucket
    title             str — chat title (repositories.get_chat_title)
    summary           str | None — a plain "sender: text" transcript excerpt
                       of the bucket (see _SUMMARY_MAX_CHARS below); NOT an
                       LLM summary — no LLM call happens anywhere in this
                       module, same "no LLM call here" contract as the omi
                       reference's adapters
    participants_raw  list[str] — sender display names, in order of first
                       appearance (same convention as
                       app/mcp_readonly.py::_format_message's sender
                       resolution, reused via that function directly)
    ingested_at        datetime — set by upsert_signal at write time
    raw_ref            dict — {"chat_id": ..., "date": "YYYY-MM-DD",
                       "message_ids": [...]}

── ADAPTATION: what counts as "one signal" for Telegram ──────────────────

The omi reference has two adapters, each keyed on a boundary its source
already gives it for free: an OMI conversation has an omi_id, a Gmail thread
has a threadId. Telegram messages carry neither — this repo's data model is
a flat, continuously-growing per-chat message stream (raw_messages), with no
discrete "conversation" concept of its own (the closest thing,
app/pipeline/windows.py's gap-based conversation window, exists to answer a
different question — "what's fresh enough to feed the extractor right now"
— not "group all history into stable, re-visitable units").

Rather than invent a new boundary, this module reuses the one this repo
ALREADY treats as the natural unit of chat activity: the local CALENDAR DAY,
per chat — exactly the granularity app/mcp_readonly.py's get_daily_bundle
already scans over (active-day walking) and app/pipeline/summary.py's
end-of-day group summary already reports on. `source_id` is therefore
f"{chatId}:{YYYY-MM-DD}" (chatId = the internal key as stored on
raw_messages/chat_state, e.g. "user_123"/"group_-100456" — not the
MCP-facing numeric id app/mcp_readonly.py's tools convert to; storage keeps
the same key the rest of this repo's collections use), and the day boundary
itself is computed with the SAME helpers app/mcp_readonly.py's
get_daily_bundle already uses (_tz, _local_midnight_utc) — not a
reimplementation.

── Ingest cursor / re-fetch tradeoff ───────────────────────────────────────

`ingest_telegram_signals(since)` first finds which (chat, day) buckets have
seen a NEW message since `since` (the meta cursor — see
repositories.get/set_signals_last_run_at), then re-reads each touched
bucket's FULL calendar day (not just the delta) to rebuild a complete signal
doc before upserting. This is required for correctness — upsert_signal does
a whole-document $set, so a partial (delta-only) rebuild would silently
regress ts_start/participants/summary back down to just the newest
messages — and it is the SAME accepted tradeoff the omi reference's gmail
adapter documents: "re-running the ingest tick... re-fetches the whole
[bucket] every time — wasteful but harmless, since upsert_signal's (source,
source_id) key makes every re-ingest idempotent."
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from . import mcp_readonly
from . import repositories as repo
from .config import get_settings
from .db import get_db

logger = logging.getLogger(__name__)

# Cap on the "sender: text" transcript excerpt built into `summary` (plain
# string truncation, not an LLM call) — keeps a very chatty day's signal doc
# (and the triage prompt built from it) bounded.
_SUMMARY_MAX_CHARS = 4000


# ── shared upsert ────────────────────────────────────────────────────────


async def upsert_signal(doc: dict[str, Any]) -> None:
    """Upsert one signal by the idempotency key (source, source_id) — see
    app/db.py::_ensure_indexes for the backing unique index.

    Re-ingesting the same source_id overwrites the stored fields (a chat/day
    bucket accrues new messages over the day) and refreshes `ingested_at` —
    it never inserts a duplicate."""
    payload = dict(doc)
    payload["ingested_at"] = repo.utcnow()
    db = get_db()
    await db.signals.update_one(
        {"source": payload["source"], "source_id": payload["source_id"]},
        {"$set": payload},
        upsert=True,
    )


# ── Telegram adapter ────────────────────────────────────────────────────


def _local_day(dt: datetime, tz: ZoneInfo) -> date:
    """Calendar day of `dt` converted into `tz`. A tz-naive `dt` is treated
    as UTC (this repo's Motor client is tz_aware=True, so raw_messages'
    `date` field always comes back aware — this guard is just defensive)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz).date()


def _bucket_source_id(chat_id: str, day: date) -> str:
    return f"{chat_id}:{day.isoformat()}"


def telegram_bucket_to_signal(
    chat_id: str, day: date, messages: list[dict[str, Any]], title: str
) -> dict[str, Any]:
    """Pure conversion: one chat's full local-day message bucket -> a
    signals-collection doc (minus ingested_at, stamped by upsert_signal).
    `messages` need not be pre-sorted; this sorts by date itself. Split out
    from ingest_telegram_signals so it's directly unit-testable without
    touching Mongo."""
    ordered = sorted(messages, key=lambda m: m["date"])
    ts_start = ordered[0]["date"]
    ts_end = ordered[-1]["date"]

    participants: list[str] = []
    message_ids: list[int] = []
    lines: list[str] = []
    for m in ordered:
        # Reuses app/mcp_readonly.py's own sender-name resolution rather
        # than re-deriving it — same "владелец"/"собеседник" fallback.
        sender = mcp_readonly._format_message(m)["from"]
        if sender not in participants:
            participants.append(sender)
        mid = m.get("messageId")
        if mid is not None:
            message_ids.append(mid)
        text = (m.get("text") or "").strip()
        if text:
            lines.append(f"{sender}: {text}")

    preview = "\n".join(lines)
    summary: str | None
    if not preview:
        summary = None
    elif len(preview) > _SUMMARY_MAX_CHARS:
        summary = preview[:_SUMMARY_MAX_CHARS] + " …[truncated]"
    else:
        summary = preview

    return {
        "source": "telegram",
        "source_id": _bucket_source_id(chat_id, day),
        "ts_start": ts_start,
        "ts_end": ts_end,
        "title": title,
        "summary": summary,
        "participants_raw": participants,
        "raw_ref": {"chat_id": chat_id, "date": day.isoformat(), "message_ids": message_ids},
    }


async def ingest_telegram_signals(since: datetime) -> int:
    """Fold raw_messages activity into `signals`, one doc per (chat, local
    calendar day) bucket touched since `since` (see module docstring's
    ADAPTATION/tradeoff notes for why a bucket, once touched, is rebuilt from
    its FULL day rather than just the delta).

    Returns the number of buckets successfully upserted (a bucket whose
    title lookup or conversion fails is skipped and logged, not counted, and
    never raises — a single bad chat must not sink the whole tick)."""
    db = get_db()
    settings = get_settings()
    tz = mcp_readonly._tz(settings.default_timezone)

    touched_cursor = db.raw_messages.find({"date": {"$gte": since}}, {"chatId": 1, "date": 1})
    buckets: set[tuple[str, date]] = set()
    async for doc in touched_cursor:
        chat_id = doc.get("chatId")
        msg_date = doc.get("date")
        if not chat_id or msg_date is None:
            continue
        buckets.add((chat_id, _local_day(msg_date, tz)))

    count = 0
    for chat_id, day in buckets:
        day_start = mcp_readonly._local_midnight_utc(day, tz)
        day_end = mcp_readonly._local_midnight_utc(day + timedelta(days=1), tz)
        day_cursor = db.raw_messages.find(
            {"chatId": chat_id, "date": {"$gte": day_start, "$lt": day_end}}
        ).sort("date", 1)
        messages = [m async for m in day_cursor]
        if not messages:
            continue
        try:
            title = await repo.get_chat_title(chat_id)
            signal_doc = telegram_bucket_to_signal(chat_id, day, messages, title)
        except Exception:  # noqa: BLE001
            logger.exception(
                "ingest_telegram_signals: failed to build signal for %s/%s", chat_id, day
            )
            continue
        await upsert_signal(signal_doc)
        count += 1
    return count
