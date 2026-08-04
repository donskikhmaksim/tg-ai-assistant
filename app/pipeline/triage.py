"""Triage stage over the unified `signals` collection (see app/signals.py) —
the tg-ai-assistant port of omi-task-extractor's app/pipeline/triage.py (see
that repo for the shared architecture this mirrors 1:1; only the storage
plumbing differs, since this repo has no Repo class — every function here
reaches Mongo the same way app/mcp_readonly.py already does, via
app.db.get_db()).

Where signals.py's job is "get Telegram chat/day activity into one shared
shape", this module's job is "decide which of those signals actually deserve
a human's attention": a cheap batched triage pass classifies each signal's
urgency/actionability FIRST, so an external review (via the read-only MCP's
get_triage_queue tool — see app/mcp_readonly.py) can start from "what needs a
decision today" instead of a raw firehose of chat activity.

Flow, one tick (a future triage_job in app/main.py, every
TRIAGE_INTERVAL_MINUTES):

    get_pending_triage_signals   -> signals with no `triage` field yet (or a
                                     stale rubric_version) in the last
                                     TRIAGE_LOOKBACK_DAYS days
    score_signals                -> ONE batched Claude call (see
                                     app/llm/claude.py::score_triage_batch)
                                     scores the whole batch at once — never
                                     one call per signal
    run_triage_tick              -> turns each score into a verdict via the
                                     TRIAGE_LOW/TRIAGE_HIGH thresholds
                                     (app/config.py) and writes it back onto
                                     the signal doc via signals.upsert_signal

Verdict thresholds (app/config.py Settings.triage_low/triage_high, defaults
0.3/0.7):
    score <  TRIAGE_LOW              -> "drop"      (not worth a task)
    TRIAGE_LOW <= score <= TRIAGE_HIGH -> "auto"     (candidate for an
                                                        auto-lane/bottom feed)
    score >  TRIAGE_HIGH             -> "decision"   (candidate for "requires
                                                        a decision")

`triage_feedback` collection (this module owns it directly — same
convention as app/signals.py owning `signals` directly): a write-only audit
trail of human verdicts on triaged signals (approved/rejected/pulled back
from dropped), the seed for a FUTURE learning loop that recalibrates the
rubric from real feedback. Only the write path (record_triage_feedback)
exists here — the learning loop itself is a separate, later piece of work
(same scope boundary as the omi reference).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from ..db import get_db
from ..llm import claude
from ..repositories import utcnow
from ..signals import upsert_signal

log = logging.getLogger(__name__)

# Bump this when the rubric text/schema changes meaningfully enough that
# already-scored signals should be re-triaged. get_pending_triage_signals
# treats a signal whose stored triage.rubric_version != RUBRIC_VERSION the
# same as one with no `triage` field at all.
RUBRIC_VERSION = "v0"

# Score bonus for a signal independently corroborated by more than one
# source. NOT WIRED UP in this repo — there is only one source (telegram)
# and no cross-source dedup layer here (the omi reference's boost is fed by
# its claims-layer cross-source matcher, which this port deliberately does
# not carry over — see app/signals.py module docstring's scope note). Kept
# as a documented no-op hook, same forward-compat shape as the reference, in
# case a second signal source is ever added to this repo.
_MULTI_SOURCE_BOOST = 0.15


def signal_id(doc: dict[str, Any]) -> str:
    """The idempotency key signals.upsert_signal keys on (source,
    source_id), joined into one string — used both as the external id
    handed to score_signals' callers/get_triage_queue, and as the argument
    submit_triage_feedback/record_triage_feedback take back."""
    return f"{doc.get('source')}:{doc.get('source_id')}"


def verdict_for_score(score: float, low: float, high: float) -> str:
    """Classify a (post-boost) triage score into a verdict — see module
    docstring for the three bands. `low`/`high` are passed explicitly
    (rather than read from get_settings() here) so this stays a pure,
    trivially-testable function."""
    if score < low:
        return "drop"
    if score > high:
        return "decision"
    return "auto"


def _has_multi_source_match(doc: dict[str, Any]) -> bool:
    """Always False in this repo today — see _MULTI_SOURCE_BOOST's
    docstring. Kept as its own function (rather than inlined) so a test can
    monkeypatch it independent of the real field, and so the read stays in
    exactly one place if a second source is ever added."""
    ids = doc.get("matched_signal_ids")
    return isinstance(ids, list) and len(ids) > 0


def _clamp01(score: float) -> float:
    return max(0.0, min(1.0, score))


async def score_signals(signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Score a batch of `signals` docs with ONE Claude call (see
    app.llm.claude.score_triage_batch — the whole point of batching here,
    scoring one signal at a time would be one LLM call per chat/day bucket,
    far too costly for a tick that may cover a whole day's worth of chat
    activity).

    Returns one dict per INPUT signal, in the SAME order:
        {signal_id, score, category, reason}
    `signal_id` = f"{source}:{source_id}" (see signal_id()) — the same
    idempotency key signals.upsert_signal keys on, so callers can match a
    result back to its signal without re-reading Mongo. The multi-source
    boost (_MULTI_SOURCE_BOOST, never firing in this repo — see its
    docstring) is already applied to `score` here, clamped to [0.0, 1.0].

    Fail-safe: if the batch Claude call itself fails, score_triage_batch
    returns [] and every signal here comes back with score=None,
    category=None — NOT a verdict, NOT written anywhere. Callers (see
    run_triage_tick) must leave those signals without a `triage` field so
    the next tick retries them, rather than inventing a score or verdict.
    """
    if not signals:
        return []
    items = [
        {
            "title": doc.get("title") or "",
            "summary": doc.get("summary") or "",
            "participants_raw": doc.get("participants_raw") or [],
            "source": doc.get("source"),
            "ts_start": doc.get("ts_start").isoformat()
            if isinstance(doc.get("ts_start"), datetime)
            else doc.get("ts_start"),
        }
        for doc in signals
    ]
    verdicts = await claude.score_triage_batch(items)
    by_index = {
        v.get("index"): v
        for v in verdicts
        if isinstance(v, dict) and isinstance(v.get("index"), int)
    }

    out: list[dict[str, Any]] = []
    for i, doc in enumerate(signals):
        v = by_index.get(i)
        if v is None:
            out.append(
                {
                    "signal_id": signal_id(doc),
                    "score": None,
                    "category": None,
                    "reason": "scoring unavailable (batch call failed or returned no "
                    "verdict for this signal)",
                }
            )
            continue
        try:
            score = _clamp01(float(v.get("score", 0.0)))
        except (TypeError, ValueError):
            score = 0.0
        if _has_multi_source_match(doc):
            score = _clamp01(score + _MULTI_SOURCE_BOOST)
        category = v.get("category") if isinstance(v.get("category"), str) else "noise"
        reason = v.get("reason") if isinstance(v.get("reason"), str) else ""
        out.append({"signal_id": signal_id(doc), "score": score, "category": category, "reason": reason})
    return out


async def get_pending_triage_signals(
    since: datetime, rubric_version: str = RUBRIC_VERSION
) -> list[dict[str, Any]]:
    """`signals` docs with ts_start >= `since` that either have no `triage`
    field yet, or were scored under a different rubric_version (the future
    re-scoring path RUBRIC_VERSION's docstring describes). Newest first is
    not required here (score_signals treats the batch as an unordered set),
    so no explicit sort — callers that care about order can add one."""
    db = get_db()
    cursor = db.signals.find(
        {
            "ts_start": {"$gte": since},
            "$or": [
                {"triage": {"$exists": False}},
                {"triage.rubric_version": {"$ne": rubric_version}},
            ],
        }
    )
    return [d async for d in cursor]


async def run_triage_tick(settings: Any) -> int:
    """One triage tick: get_pending_triage_signals -> score_signals ->
    write `triage` back via signals.upsert_signal. Returns the number of
    signals actually scored+written (a signal score_signals couldn't score
    this tick — see its fail-safe contract — is skipped, left without a
    `triage` field, and picked up again by the next tick).

    Intended to be called by a triage_job in app/main.py, itself wrapped in
    try/except there per this repo's "a failing tick must never kill the
    scheduler" convention — this function does not need its own top-level
    try/except.
    """
    since = utcnow() - timedelta(days=settings.triage_lookback_days)
    pending = await get_pending_triage_signals(since)
    if not pending:
        return 0

    results = await score_signals(pending)
    scored_at = utcnow()
    written = 0
    for doc, result in zip(pending, results):
        if result["score"] is None:
            log.info("run_triage_tick: %s not scored this tick, will retry", result["signal_id"])
            continue
        triage = {
            "score": result["score"],
            "category": result["category"],
            "verdict": verdict_for_score(result["score"], settings.triage_low, settings.triage_high),
            "reason": result["reason"],
            "rubric_version": RUBRIC_VERSION,
            "scored_at": scored_at,
        }
        await upsert_signal(
            {"source": doc["source"], "source_id": doc["source_id"], "triage": triage}
        )
        written += 1
    return written


# ── triage_feedback (write-only audit trail — future learning-loop input) ──

_VALID_HUMAN_VERDICTS = {"approved", "rejected", "pulled_from_dropped"}


async def record_triage_feedback(signal_id: str, human_verdict: str, note: str | None = None) -> None:
    """Append one human-feedback record on a triaged signal to the
    `triage_feedback` collection — write-only for now, the seed for a FUTURE
    learning loop that recalibrates the rubric (not implemented here, see
    module docstring). `human_verdict` — "approved" | "rejected" |
    "pulled_from_dropped"; raises ValueError on anything else, same
    fail-loud contract as a malformed MCP tool argument should get (the
    MCP-facing wrapper, submit_triage_feedback in app/mcp_readonly.py, turns
    this into an {"ok": false, "error": ...} result instead of propagating
    the exception)."""
    if human_verdict not in _VALID_HUMAN_VERDICTS:
        raise ValueError(
            f"invalid human_verdict {human_verdict!r}, expected one of "
            f"{sorted(_VALID_HUMAN_VERDICTS)}"
        )
    db = get_db()
    await db.triage_feedback.insert_one(
        {
            "signal_id": signal_id,
            "human_verdict": human_verdict,
            "note": note,
            "created_at": utcnow(),
        }
    )
