"""Claims layer — the tg-ai-assistant port of omi-task-extractor's
app/pipeline/claims.py (see that repo for the shared architecture this
mirrors 1:1; only the storage plumbing differs — no Repo class here, same
"reach Mongo via app.db.get_db()" convention as app/pipeline/triage.py).

Turns triaged (`signals.triage.verdict` != "drop"), cross-source-deduped
signals (app/pipeline/claim_dedup.py) into user-facing task CARDS in the
`claims` Mongo collection: the surface an external morning review reads via
get_claims_queue (app/mcp_readonly.py) — the tg-ai-assistant analogue of
omi-task-extractor's Cowork morning review.

Flow, one tick (claims_job in app/main.py, every CLAIMS_INTERVAL_MINUTES,
running AFTER triage_job in pipeline order — see app/main.py):

    get_claimable_signals    -> triaged signals (triage.verdict "auto" or
                                 "decision" — NEVER "drop", those are already
                                 filtered out by the triage stage itself),
                                 not yet `claimed`, in the last
                                 CLAIMS_LOOKBACK_DAYS days
    claim_dedup.group_signals -> cross-source dedup groups (see that
                                 module's docstring for why this is ported
                                 and tested even though this repo has only
                                 ONE signal source today — every group comes
                                 back a singleton until a second source
                                 exists); ALSO writes matched_signal_ids
                                 back onto every multi-signal group's
                                 members (the field app/pipeline/triage.py's
                                 _has_multi_source_match reads)
    generate_claim_cards      -> ONE batched Claude call (see
                                 app/llm/claude.py::generate_claims_batch)
                                 turns every group into a card
    run_claims_tick            -> inserts one `claims` doc per successfully
                                 generated card, and marks every member
                                 signal `claimed: true`

"claimed" tracking — DESIGN CHOICE, same as the omi reference: a plain
boolean `claimed` field written directly on each `signals` doc (via
signals.upsert_signal), NOT "scan the `claims` collection for a doc whose
signal_ids array contains this id". The latter needs a multikey scan against
an ever-growing collection on every tick; a boolean flag on the signal
itself is an O(1) lookup via the SAME query shape
app/pipeline/triage.py::get_pending_triage_signals already uses, and
matches the existing convention of denormalized per-signal fields
(`triage`, `matched_signal_ids`) living on the signal doc itself rather
than being derived by joining another collection.

A group whose card generation FAILS this tick (see generate_claims_batch's
fail-open contract) is simply left unclaimed — get_claimable_signals picks
its member signals up again next tick (and claim_dedup.group_signals will
very likely re-form the SAME group, since matched_signal_ids already links
them). A signal already claimed as part of one group is never reconsidered
by a later tick even if a new corroborating signal shows up afterwards —
extending an existing claim with late-arriving evidence is out of scope
here (documented limitation, not attempted, same as the omi reference).

── ADAPTATION vs. the omi reference ────────────────────────────────────────
  - No `Repo` class: every Mongo access here goes through app.db.get_db()
    directly, same convention app/pipeline/triage.py already established
    in this repo. Functions below therefore do not take a `repo` argument.
  - TickTick client resolution uses app/ticktick/mcp_client.py's
    resolve_ticktick() rather than a bare `TickTickMCP(settings.ticktick_
    mcp_url) if settings.ticktick_mcp_url else None` — this repo's TickTick
    URL can also come from a runtime `/connect` override stored in
    `bot_state` (see resolve_ticktick's own docstring), and using the same
    helper app/pipeline/batch.py already uses keeps project routing
    consistent with how task creation resolves its own TickTick connector.
  - The claim-document schema itself (claim_id, signal_ids, title,
    subtasks, description, due_date, needs_due_clarification, project,
    with_whom, needs_clarification, verdict, created_at) is carried over
    unchanged — the two repos' `signals` docs already share the same shape
    (see app/signals.py's own module docstring), so no field-level
    adaptation was needed here.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

from ..db import get_db
from ..llm import claude
from ..repositories import utcnow
from ..signals import upsert_signal
from ..ticktick.mcp_client import TickTickMCP, resolve_ticktick
from . import claim_dedup
from .triage import signal_id

log = logging.getLogger(__name__)

# Fallback project name when the LLM's chosen `project` doesn't match any
# name in the closed list of existing TickTick projects (including when no
# TickTick connector is configured at all, or the LLM already said
# "unsorted" itself) — see CLAIMS_SYSTEM's `project` field rule.
UNSORTED_PROJECT = "unsorted"


async def get_claimable_signals(since: datetime) -> list[dict[str, Any]]:
    """`signals` docs with ts_start >= `since`, triage.verdict in
    ("auto", "decision"), and not yet `claimed` — see module docstring for
    why `claimed` is a boolean flag rather than a claims-collection scan."""
    db = get_db()
    cursor = db.signals.find(
        {
            "ts_start": {"$gte": since},
            "triage.verdict": {"$in": ["auto", "decision"]},
            "claimed": {"$ne": True},
        }
    )
    return [d async for d in cursor]


async def _get_project_names(tt: TickTickMCP | None) -> list[str]:
    """Existing TickTick project NAMES — the closed list the card's
    `project` field must pick from (plus the always-implicit "unsorted").
    [] without a connector or on error, same fail-open convention as
    app/pipeline/batch.py's own project routing — every card then falls
    back to "unsorted"."""
    if tt is None:
        return []
    try:
        projects = await tt.get_projects()
        return [p["name"] for p in projects if p.get("name")]
    except Exception:  # noqa: BLE001 — routing is best-effort
        log.exception("claims: get_projects failed — every card falls back to unsorted")
        return []


def _group_verdict(group: list[dict[str, Any]]) -> str:
    """The claim's own verdict: "decision" if ANY member signal was scored
    "decision" (a matter corroborated across sources deserves at least as
    much attention as its most urgent member), else "auto"."""
    verdicts = {(d.get("triage") or {}).get("verdict") for d in group}
    return "decision" if "decision" in verdicts else "auto"


def _claim_item(doc: dict[str, Any]) -> dict[str, Any]:
    """One generate_claims_batch per-signal input item from a `signals`
    doc — same shape triage.score_signals builds for score_triage_batch, for
    consistency between what the two LLM stages see."""
    return {
        "title": doc.get("title") or "",
        "summary": doc.get("summary") or "",
        "participants_raw": doc.get("participants_raw") or [],
        "source": doc.get("source"),
        "ts_start": doc.get("ts_start").isoformat()
        if isinstance(doc.get("ts_start"), datetime)
        else doc.get("ts_start"),
    }


def _build_claim_doc(
    group: list[dict[str, Any]], card: dict[str, Any], project_names: set[str]
) -> dict[str, Any]:
    project = card.get("project") if isinstance(card.get("project"), str) else None
    if not project or project not in project_names:
        project = UNSORTED_PROJECT
    subtasks = card.get("subtasks")
    return {
        "claim_id": uuid.uuid4().hex,
        "signal_ids": [signal_id(d) for d in group],
        "title": (card.get("title") or "").strip(),
        "subtasks": (
            [t for t in subtasks if isinstance(t, str) and t.strip()]
            if isinstance(subtasks, list)
            else []
        ),
        "description": (card.get("description") or "").strip(),
        "due_date": card.get("due_date") if isinstance(card.get("due_date"), str) else None,
        "needs_due_clarification": bool(card.get("needs_due_clarification")),
        "project": project,
        "with_whom": card.get("with_whom") if isinstance(card.get("with_whom"), str) else None,
        "needs_clarification": bool(card.get("needs_clarification")),
        "verdict": _group_verdict(group),
        "created_at": utcnow(),
    }


async def generate_claim_cards(
    groups: list[list[dict[str, Any]]], project_names: list[str]
) -> list[dict[str, Any] | None]:
    """Card dict per group, SAME ORDER as `groups`, or None where the model
    didn't return one this tick (see generate_claims_batch's fail-open
    contract) — the caller must leave that group's signals unclaimed."""
    if not groups:
        return []
    items = [[_claim_item(d) for d in g] for g in groups]
    raw_cards = await claude.generate_claims_batch(items, project_names)
    by_index = {
        c.get("index"): c
        for c in raw_cards
        if isinstance(c, dict) and isinstance(c.get("index"), int)
    }
    return [by_index.get(i) for i in range(len(groups))]


async def run_claims_tick(settings: Any) -> int:
    """One claims tick: get_claimable_signals -> claim_dedup.group_signals ->
    generate_claim_cards -> insert into `claims` + mark member signals
    claimed. Returns the number of claims actually written.

    Called by claims_job in app/main.py, itself wrapped in try/except there
    per this repo's "a failing tick must never kill the scheduler"
    convention — this function does not need its own top-level try/except.
    """
    since = utcnow() - timedelta(days=settings.claims_lookback_days)
    pending = await get_claimable_signals(since)
    if not pending:
        return 0

    groups = await claim_dedup.group_signals(pending, settings)
    if not groups:
        return 0

    tt = await resolve_ticktick()
    project_names = await _get_project_names(tt)
    project_name_set = set(project_names)

    cards = await generate_claim_cards(groups, project_names)

    db = get_db()
    written = 0
    for group, card in zip(groups, cards):
        if card is None:
            log.info(
                "run_claims_tick: group %s not generated this tick, will retry",
                [signal_id(d) for d in group],
            )
            continue
        claim_doc = _build_claim_doc(group, card, project_name_set)
        await db.claims.insert_one(claim_doc)
        for doc in group:
            await upsert_signal(
                {"source": doc["source"], "source_id": doc["source_id"], "claimed": True}
            )
        written += 1
    return written
