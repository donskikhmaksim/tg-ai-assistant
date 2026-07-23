"""Audit-log writers — the two-phase `record_mutation` / `finalize_mutation`.

Discipline mirrors `raw_messages` (persist before processing): write the
pre-record with the BEFORE snapshot + intent immediately, run the mutation, then
patch the AFTER + result. That way a crash mid-mutation still leaves a record of
what was attempted, with enough BEFORE state to restore.

FAIL-OPEN: if auditing is disabled (`AUDIT_ENABLED=false`) or any write raises
(Mongo down, bad doc), these functions swallow it and return None / no-op. Audit
logging must NEVER break the pipeline — the caller treats a None record_id as
"not logged" and carries on.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from bson import ObjectId

from .. import repositories as repo
from ..config import get_settings
from .reconcile import build_diff, changed_fields, infer_op

logger = logging.getLogger(__name__)

SCHEMA_V = 1

# All human-facing timestamps render in America/Los_Angeles (owner tz), never
# UTC. `ts` is stored tz-aware UTC (the TTL index and range queries key on it);
# `ts_local` is the denormalized LA string for humans reading the trail.
_LA = ZoneInfo("America/Los_Angeles")


def audit_enabled() -> bool:
    """Master gate. Everything in this module no-ops when False."""
    return bool(get_settings().audit_enabled)


def _ts_local(dt: datetime) -> str:
    """Denormalized LA string, e.g. `2026-07-22 15:31:04 -07:00` (colon offset)."""
    local = dt.astimezone(_LA)
    off = local.strftime("%z")  # e.g. -0700
    if off:
        off = off[:3] + ":" + off[3:]
    return local.strftime("%Y-%m-%d %H:%M:%S ") + off


async def record_mutation(
    *,
    server: str,
    tool: str,
    target: dict[str, Any],
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    op: str | None = None,
    capture_plane: str = "in_band",
    actor: dict[str, Any] | None = None,
    restore: dict[str, Any] | None = None,
    ts: datetime | None = None,
) -> ObjectId | None:
    """Write the pre-record (BEFORE + intent) and return its id.

    `op` is inferred from before/after when not given. `after` is optional here
    (the *intended* end state, if known); it's normally filled by
    finalize_mutation once the mutation returns. Returns None — meaning "not
    logged" — when auditing is off or the write fails; the caller must tolerate
    that and never gate the real mutation on it.
    """
    if not audit_enabled():
        return None
    try:
        now = ts or datetime.now(timezone.utc)
        resolved_op = op or infer_op(before, after)
        diff = build_diff(before, after) if after is not None else []
        doc: dict[str, Any] = {
            "ts": now,
            "ts_local": _ts_local(now),
            "server": server,
            "tool": tool,
            "op": resolved_op,
            "capture_plane": capture_plane,
            "actor": actor or {"kind": "automation", "source": "batch"},
            "target": target,
            "before": before,
            "after": after,
            "diff": diff,
            # Flattened changed-field names — cheap echo-matching key for the
            # out-of-band poller (see reconcile.is_inband_echo).
            "diff_fields": sorted(changed_fields(before, after)) if after is not None else [],
            "result": {
                "status": "pending",
                "record_id": None,
                "error": None,
                "verified": False,
            },
            "restore": restore or {},
            "schema_v": SCHEMA_V,
        }
        return await repo.insert_audit_record(doc)
    except Exception:  # noqa: BLE001 — fail open, never break the pipeline
        logger.warning("audit.record_mutation failed; continuing", exc_info=True)
        return None


async def finalize_mutation(
    record_id: ObjectId | None,
    *,
    after: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
) -> None:
    """Patch a pre-record with the mutation's outcome.

    No-op when `record_id` is None (auditing off / pre-record failed). Recomputes
    the human diff + diff_fields against the record's BEFORE when a fresh `after`
    is supplied. Fail-open: a patch failure is logged, never raised.
    """
    if record_id is None or not audit_enabled():
        return
    try:
        fields: dict[str, Any] = {}
        if result is not None:
            fields["result"] = result
        if after is not None:
            existing = await repo.get_audit_record(record_id)
            before = (existing or {}).get("before") if existing else None
            fields["after"] = after
            fields["diff"] = build_diff(before, after)
            fields["diff_fields"] = sorted(changed_fields(before, after))
        if fields:
            await repo.finalize_audit_record(record_id, fields)
    except Exception:  # noqa: BLE001 — fail open
        logger.warning("audit.finalize_mutation failed; continuing", exc_info=True)
