"""Deadline formatting helpers for the pipeline."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# A wall-clock datetime with no offset, e.g. "2026-06-25T17:00" or with a space.
_LOCAL_DT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?$")

# Neutral fallback for direct callers; the production path passes
# settings.default_timezone explicitly (batch.py). Keep UTC so no owner-specific
# zone is ever baked in.
DEFAULT_TIMEZONE = "UTC"


def _zone(name: str | None) -> ZoneInfo | None:
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, ModuleNotFoundError):
        logger.warning("Unknown timezone %r, ignoring", name)
        return None


def to_ticktick_due(
    deadline: str | None,
    tz: str | None = None,
    default_tz: str = DEFAULT_TIMEZONE,
) -> str | None:
    """Format Claude's deadline into TickTick's ISO `YYYY-MM-DDThh:mm:ss+0000`.

    - Bare date (YYYY-MM-DD): start-of-day; the task is also marked all-day (see
      is_all_day_deadline), so the time/offset is irrelevant.
    - Wall-clock datetime (YYYY-MM-DDThh:mm): interpreted in `tz` if the
      conversation named a city/zone, otherwise in `default_tz` (the owner's
      home zone, NOT UTC), and converted to the correct offset (DST-aware).
    - Anything already carrying an offset is passed through untouched.
    """
    if not deadline:
        return None
    d = deadline.strip()
    if _DATE_RE.match(d):
        # All-day date: anchor to LOCAL midnight in the owner's zone, NOT UTC.
        # Midnight-UTC is shown by a negative-offset account (e.g. Los Angeles)
        # as the PREVIOUS day — the is_all_day flag does not reliably prevent it.
        zone = _zone(tz) or _zone(default_tz) or timezone.utc
        try:
            dt = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=zone)
        except ValueError:
            return f"{d}T00:00:00+0000"
        return dt.strftime("%Y-%m-%dT%H:%M:%S%z")
    if _LOCAL_DT_RE.match(d):
        d2 = d.replace(" ", "T")
        if d2.count(":") == 1:
            d2 += ":00"
        zone = _zone(tz) or _zone(default_tz) or timezone.utc
        try:
            dt = datetime.strptime(d2, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=zone)
        except ValueError:
            return d
        return dt.strftime("%Y-%m-%dT%H:%M:%S%z")
    return d  # already a full timestamp with offset; pass through


def is_all_day_deadline(deadline: str | None) -> bool:
    """True when the deadline is a bare date (no time) — render it as all-day.

    Without this, a date becomes midnight UTC, which TickTick shows in the
    account's timezone (e.g. 5 PM the previous day in Los Angeles)."""
    return bool(deadline) and bool(_DATE_RE.match(deadline.strip()))
