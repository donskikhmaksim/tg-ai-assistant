"""Pure reconciliation / attribution / diff helpers for out-of-band capture.

No I/O, no DB — just the logic the poller applies to each polled change so it is
unit-testable in isolation (see tests/test_audit_log.py). The design's §2b rules:

  1. Match against in-band records. If a polled change on `target.id` matches one
     of OUR recent in-band audit records (within a time window, field delta
     agreeing), it is our own automation echoing back through the provider's sync
     feed → DROP it (already logged).
  2. Else attribute by feed identity. If the feed names a modifying user other
     than the owner → `collaborator` (+ who). Owner identity and no in-band match
     → `owner_manual`. If the feed can't say → `unknown` / low confidence.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

# Normalized verbs used across every server (design §2 schema `op`).
OP_CREATE = "create"
OP_UPDATE = "update"
OP_DELETE = "delete"
OP_COMPLETE = "complete"


def infer_op(before: dict[str, Any] | None, after: dict[str, Any] | None) -> str:
    """Normalize a before/after pair into a verb.

    Presence rules: no-before → create, no-after → delete, else update — with a
    completion special-case (status flipped to a done-ish value) surfaced as
    `complete` so re-open restores route correctly.
    """
    if not before and after:
        return OP_CREATE
    if before and not after:
        return OP_DELETE
    before = before or {}
    after = after or {}
    if _is_completion(before, after):
        return OP_COMPLETE
    return OP_UPDATE


def _is_completion(before: dict[str, Any], after: dict[str, Any]) -> bool:
    """A transition into a completed/done status (TickTick status 2 or a
    done-ish string), used to tag the op as `complete` rather than plain update."""
    b = _status_key(before.get("status"))
    a = _status_key(after.get("status"))
    if b == a:
        return False
    return a in {"2", "done", "completed", "complete"}


def _status_key(value: Any) -> str:
    return str(value).strip().lower() if value is not None else ""


# Fields we never treat as a meaningful edit (server bookkeeping / echoes).
_IGNORED_DIFF_FIELDS = {"modifiedTime", "etag", "updatedAt", "sortOrder"}


def build_diff(
    before: dict[str, Any] | None, after: dict[str, Any] | None
) -> list[str]:
    """Human-first list of changed fields as `field: old → new` strings.

    Compares the union of keys, skipping server-bookkeeping fields. Values are
    stringified shallowly; a missing side renders as `∅`. Order is stable
    (sorted by field) so records diff deterministically in tests.
    """
    before = before or {}
    after = after or {}
    keys = sorted((set(before) | set(after)) - _IGNORED_DIFF_FIELDS)
    out: list[str] = []
    for k in keys:
        bv = before.get(k, _MISSING)
        av = after.get(k, _MISSING)
        if bv == av:
            continue
        out.append(f"{k}: {_render(bv)} → {_render(av)}")
    return out


_MISSING = object()


def _render(value: Any) -> str:
    if value is _MISSING or value is None:
        return "∅"
    text = str(value)
    return text if len(text) <= 120 else text[:117] + "…"


def changed_fields(
    before: dict[str, Any] | None, after: dict[str, Any] | None
) -> set[str]:
    """The set of field names that actually changed (for echo matching)."""
    before = before or {}
    after = after or {}
    fields: set[str] = set()
    for k in (set(before) | set(after)) - _IGNORED_DIFF_FIELDS:
        if before.get(k, _MISSING) != after.get(k, _MISSING):
            fields.add(k)
    return fields


def is_inband_echo(
    target_id: str,
    change_fields: set[str],
    change_ts: datetime,
    inband_records: list[dict[str, Any]],
    window_seconds: int = 120,
) -> bool:
    """True when this polled change is our own automation echoing back.

    Matches when an in-band audit record on the SAME target exists within
    ±window_seconds AND its changed fields overlap this change's fields (or the
    in-band record recorded no field-level diff, e.g. a create/delete — a same-
    target, in-window in-band op is enough). Field disagreement means a genuine
    later hand-edit on top of ours → NOT an echo.
    """
    window = timedelta(seconds=window_seconds)
    for rec in inband_records:
        if (rec.get("target") or {}).get("id") != target_id:
            continue
        rec_ts = rec.get("ts")
        if not isinstance(rec_ts, datetime):
            continue
        if abs(_aware(change_ts) - _aware(rec_ts)) > window:
            continue
        rec_fields = set(rec.get("diff_fields") or [])
        if not change_fields or not rec_fields or (change_fields & rec_fields):
            return True
    return False


def _aware(dt: datetime) -> datetime:
    """Best-effort: leave tz-aware datetimes as-is (subtraction needs matching
    awareness; the poller always stores tz-aware UTC)."""
    return dt


def classify_source(
    modifier: dict[str, Any] | None,
    owner_identity: str | None,
    is_echo: bool,
) -> dict[str, Any]:
    """Attribute an out-of-band change to a source (design §2b step 2).

    Returns an `actor` fragment: {kind, source, who, attribution_confidence}.
      • is_echo               → automation (our own edit round-tripping).
      • modifier names a user != owner → collaborator (high confidence + who).
      • modifier IS the owner → owner_manual (high).
      • feed named nobody     → owner_manual, but LOW confidence (best guess: the
                                 owner's own single-user surface).
    """
    if is_echo:
        return {
            "kind": "automation",
            "source": "delta_poll",
            "who": None,
            "attribution_confidence": "high",
        }
    who_id = (modifier or {}).get("user_id") or (modifier or {}).get("id")
    who_name = (modifier or {}).get("name")
    who_email = (modifier or {}).get("email")
    named = bool(who_id or who_name or who_email)

    if named and not _is_owner(modifier, owner_identity):
        return {
            "kind": "collaborator",
            "source": "delta_poll",
            "who": {"user_id": who_id, "name": who_name, "email": who_email},
            "attribution_confidence": "high",
        }
    if named:  # named, and it's the owner
        return {
            "kind": "owner_manual",
            "source": "delta_poll",
            "who": {"user_id": who_id, "name": who_name, "email": who_email},
            "attribution_confidence": "high",
        }
    # Feed named nobody: assume the owner's own hand-edit, flagged low-confidence.
    return {
        "kind": "owner_manual",
        "source": "delta_poll",
        "who": None,
        "attribution_confidence": "low",
    }


def _is_owner(modifier: dict[str, Any] | None, owner_identity: str | None) -> bool:
    """Does this modifier identity match the owner? Compares id/email/name to the
    single configured owner identity (any match wins). Unknown owner → not owner
    (so a named non-owner is still attributed collaborator)."""
    if not owner_identity or not modifier:
        return False
    owner = str(owner_identity).strip().lower()
    for field in ("user_id", "id", "email", "name"):
        val = modifier.get(field)
        if val is not None and str(val).strip().lower() == owner:
            return True
    return False
