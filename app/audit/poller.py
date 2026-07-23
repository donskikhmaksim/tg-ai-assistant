"""Out-of-band delta poller — TickTick (Phase 0 scaffolding).

Catches changes WE did not make: the owner editing in the TickTick app, or a
collaborator editing a shared list. Runs on the shared AsyncIOScheduler at
`AUDIT_POLL_INTERVAL_SECONDS`, reads TickTick's current state, diffs it against
our last-known `state_snapshots`, reconciles each change against recent in-band
records (dropping our own echoes), and writes `capture_plane: "out_of_band"`
audit records attributed to owner_manual vs collaborator.

SAFETY (non-negotiable for this phase):
  • READ-ONLY against TickTick — never mutates.
  • Fully fail-open — any error (no connector, MCP down, unparseable output) is
    logged and the cycle no-ops; it never raises into the scheduler.
  • First cycle SEEDS snapshots without emitting records (so we don't log every
    pre-existing task as a "create").

Phase-0 scope & deferrals (see design §2b):
  • Implemented: create + field-update detection from an open-task snapshot diff,
    reconciliation/echo-drop, source attribution, before/after records.
  • Deferred to a later phase (clear TODOs below):
      – get_changes / get_task_activity for reliable delete-vs-complete semantics
        and shared-list "who edited it" collaborator attribution.
      – Google pollers (Drive changes.list, Gmail history.list, Calendar
        syncToken). Not wired here.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import repositories as repo
from ..config import get_settings
from ..ticktick.mcp_client import TickTickMCP, resolve_ticktick
from . import log as audit_log
from . import reconcile

logger = logging.getLogger(__name__)

SERVER = "ticktick"
PROVIDER = "ticktick"
# How many projects / tasks-per-project to scan per cycle, so a huge account
# can't blow up a poll. Bounded, read-only.
_PROJECT_CAP = 100
_TASK_CAP = 200


async def run_ticktick_audit_poll() -> None:
    """Scheduler entry point. Fail-open, read-only, never raises."""
    if not audit_log.audit_enabled():
        return
    try:
        tt = await resolve_ticktick()
        if tt is None:
            logger.debug("audit poll: no TickTick connector — skipping")
            return
        await _poll_ticktick(tt)
    except Exception:  # noqa: BLE001 — a poll failure must never break the scheduler
        logger.warning("audit poll (ticktick) failed; skipping this cycle", exc_info=True)


async def _poll_ticktick(tt: TickTickMCP) -> None:
    current = await _current_state(tt)
    if current is None:
        # Feed unavailable / unparseable this cycle — no-op (already logged).
        return

    # Whether we've ever COMPLETED a seed is gated on the sync_cursor row, not on
    # `state_snapshots` being non-empty. A seed that's interrupted partway through
    # (raised exception swallowed by the outer fail-open try/except, or the
    # process just dying) leaves `state_snapshots` partially populated but never
    # reaches the `set_sync_cursor` write below. If we gated on snapshot
    # emptiness, the next cycle would see a non-empty (but partial) snapshot set,
    # conclude seeding was already done, and diff against it — every task the
    # partial seed missed would show before=None and get emitted as a spurious
    # OP_CREATE (the false-creation-storm this poller exists to avoid). Gating on
    # the cursor means a never-completed seed always re-seeds; re-seeding is
    # idempotent (upsert), so this is safe even on a hard crash mid-loop.
    cursor = await repo.get_sync_cursor(PROVIDER)
    seeding = cursor is None

    if seeding:
        seeded = 0
        for target_id, state in current.items():
            try:
                await repo.upsert_state_snapshot(SERVER, target_id, state)
                seeded += 1
            except Exception:  # noqa: BLE001 — one bad write can't abort the whole seed
                logger.debug(
                    "audit poll: failed to seed snapshot for %s", target_id, exc_info=True
                )
        # Cursor write comes LAST, after the full loop, so a genuinely-interrupted
        # process (not just a caught exception) never writes it — the next boot
        # sees no cursor and safely re-seeds.
        await repo.set_sync_cursor(PROVIDER, {"lastPollAt": _now().isoformat()})
        logger.info(
            "audit poll (ticktick): seeded %d/%d snapshot(s), no records", seeded, len(current)
        )
        return

    snapshots = await repo.list_state_snapshots(SERVER)
    changes = _diff_states(current, snapshots)
    if not changes:
        await repo.set_sync_cursor(PROVIDER, {"lastPollAt": _now().isoformat()})
        return

    # Pull recent in-band records once, so our own edits echoing back through the
    # feed are recognized and dropped rather than double-logged.
    window = max(get_settings().audit_poll_interval_seconds, 120)
    since = _now() - timedelta(seconds=window * 2)
    inband = await repo.get_recent_audit_records(
        SERVER, [c["target_id"] for c in changes], since
    )
    owner_identity = await _owner_identity()

    written = 0
    for change in changes:
        try:
            await _record_change(change, inband, owner_identity, window)
            await repo.upsert_state_snapshot(SERVER, change["target_id"], change["after"] or {})
            written += 1
        except Exception:  # noqa: BLE001 — one bad change can't stall the cycle
            logger.debug("audit poll: failed to record change for %s", change.get("target_id"), exc_info=True)

    await repo.set_sync_cursor(PROVIDER, {"lastPollAt": _now().isoformat()})
    logger.info("audit poll (ticktick): %d out-of-band change(s) recorded", written)


async def _record_change(
    change: dict[str, Any],
    inband: list[dict[str, Any]],
    owner_identity: str | None,
    window: int,
) -> None:
    """Reconcile one polled change and write its out-of-band audit record."""
    before = change.get("before")
    after = change.get("after")
    fields = reconcile.changed_fields(before, after)
    now = _now()

    is_echo = reconcile.is_inband_echo(
        change["target_id"], fields, now, inband, window_seconds=window
    )
    if is_echo:
        # Our own automation round-tripping — already logged in-band; skip.
        logger.debug("audit poll: dropped in-band echo for %s", change["target_id"])
        return

    actor = reconcile.classify_source(change.get("modifier"), owner_identity, is_echo=False)
    actor.setdefault("trace_id", f"delta_poll-{now.strftime('%Y-%m-%dT%H:%M')}")

    op = reconcile.infer_op(before, after)
    target = {
        "id": change["target_id"],
        "parent_id": change.get("parent_id"),
        "title": (after or before or {}).get("title"),
        "url": None,
    }
    restore = {
        "native_available": op in ("delete", "complete"),
        # get_trash → restore_tasks for deletions; re-open via update for completions.
        "native_hint": "get_trash → restore_tasks" if op == "delete" else None,
        "restorable_from_log": before is not None,
    }
    # Two-phase, mirroring the in-band discipline: pre-record the BEFORE, then
    # finalize with AFTER + a success result.
    record_id = await audit_log.record_mutation(
        server=SERVER,
        tool="delta_poll",
        target=target,
        before=before,
        op=op,
        capture_plane="out_of_band",
        actor=actor,
        restore=restore,
        ts=now,
    )
    await audit_log.finalize_mutation(
        record_id,
        after=after,
        result={"status": "success", "record_id": None, "error": None, "verified": False},
    )


async def _current_state(tt: TickTickMCP) -> dict[str, dict[str, Any]] | None:
    """Snapshot the owner's OPEN tasks as {taskId: state}.

    Enumerates each project's tasks via the existing tolerant parser. Returns
    None (→ no-op this cycle) if the project list itself can't be read, so a
    transient MCP hiccup never looks like "everything was deleted".

    NOTE (Phase 2b): this open-task snapshot cannot by itself distinguish a
    deletion from a completion (both drop the task off the open list) nor name
    the collaborator who edited a shared list. The reliable path is
    `get_changes` + `get_task_activity`; wire those here in a later phase.
    """
    try:
        projects = await tt.get_projects()
    except Exception:  # noqa: BLE001
        logger.debug("audit poll: get_projects failed", exc_info=True)
        return None

    state: dict[str, dict[str, Any]] = {}
    for proj in projects[:_PROJECT_CAP]:
        pid = proj.get("id")
        if not pid:
            continue
        try:
            cards = await tt.get_project_tasks(pid, limit=_TASK_CAP)
        except Exception:  # noqa: BLE001 — one project failing shouldn't abort the poll
            logger.debug("audit poll: get_project_tasks failed for %s", pid, exc_info=True)
            continue
        for card in cards:
            tid = card.get("id")
            if not tid:
                continue
            state[tid] = _card_state(card, pid)
    return state


def _card_state(card: dict[str, Any], project_id: str) -> dict[str, Any]:
    """Reduce a get_project_tasks card to the fields we snapshot/diff on."""
    state = {"projectId": project_id}
    for k in ("title", "due", "priority", "status", "content"):
        if card.get(k) is not None:
            state[k] = card[k]
    return state


def _diff_states(
    current: dict[str, dict[str, Any]], snapshots: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Compare the freshly-polled state against stored snapshots.

    Emits creates (new id) and updates (state changed). Disappearances are NOT
    emitted in Phase 0 — an open-task snapshot can't tell a delete from a
    completion; that's resolved via get_changes/get_trash in Phase 2b. `modifier`
    is left None (the open-task feed carries no editor identity → owner_manual/
    low), also filled in by get_task_activity in a later phase.
    """
    changes: list[dict[str, Any]] = []
    for target_id, after in current.items():
        snap = snapshots.get(target_id)
        before = snap.get("state") if snap else None
        if before is None:
            changes.append({
                "target_id": target_id,
                "before": None,
                "after": after,
                "parent_id": after.get("projectId"),
                "modifier": None,
            })
        elif reconcile.changed_fields(before, after):
            changes.append({
                "target_id": target_id,
                "before": before,
                "after": after,
                "parent_id": after.get("projectId"),
                "modifier": None,
            })
    # TODO(phase-2b): handle `snapshots` keys absent from `current` (potential
    # deletions/completions) via get_changes/get_trash for correct semantics.
    return changes


async def _owner_identity() -> str | None:
    """Best-effort owner identity (id/email/name) for collaborator attribution.

    TODO(phase-2b): populate from the TickTick account profile once
    get_task_activity gives per-edit user ids on shared lists. For now returns
    None → un-named edits fall to owner_manual/low, named non-owner edits still
    attribute to collaborator.
    """
    try:
        return await repo.get_bot_state("ticktick_owner_identity")
    except Exception:  # noqa: BLE001
        return None


def _now() -> datetime:
    return datetime.now(timezone.utc)
