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
    """Format Claude's deadline into a TickTick due string.

      - bare date (YYYY-MM-DD)  -> the LITERAL date, passed through unchanged. An
        all-day deadline is a ZONE-INDEPENDENT calendar date (the caller sets the
        all-day flag alongside); attaching any offset only shows the right day
        while three zones (this default_tz, ticktick-mcp's USER_TIMEZONE, and the
        TickTick account zone) stay equal, and shifts −1 the moment they diverge
        (#36). Never bake a home-zone or UTC midnight into it.
      - wall-clock (…Thh:mm)    -> read in `tz` if the conversation named a zone,
        else the owner's zone; then normalized to the owner's zone.
      - offset-carrying / other -> parsed and CONVERTED (same instant) to the
        owner's zone — so a stray UTC/offset can never leak through.

    Only TIMED deadlines carry a timezone; all-day dates never do.
    """
    if not deadline:
        return None
    d = deadline.strip()
    home = _zone(default_tz) or timezone.utc
    if _DATE_RE.match(d):
        # Validate it's a real calendar date, then emit it verbatim (all-day).
        try:
            datetime.strptime(d, "%Y-%m-%d")
        except ValueError:
            return None
        return d
    if _LOCAL_DT_RE.match(d):
        d2 = d.replace(" ", "T")
        if d2.count(":") == 1:
            d2 += ":00"
        # Named zone (if Claude gave one) keeps its own offset — TickTick renders
        # it in the account's zone (LA) anyway, so the same instant shows as the
        # correct LOCAL time. Otherwise it's already the owner's zone.
        zone = _zone(tz) or home
        try:
            dt = datetime.strptime(d2, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=zone)
        except ValueError:
            return None
        return dt.strftime("%Y-%m-%dT%H:%M:%S%z")
    # Full timestamp with an offset (or 'Z'), or any other ISO form Claude might
    # emit — parse and pull it into the owner's zone. Unparseable → drop (better
    # no deadline than a wrong/UTC one).
    try:
        dt = datetime.fromisoformat(d.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=home)
    return dt.astimezone(home).strftime("%Y-%m-%dT%H:%M:%S%z")


def is_all_day_deadline(deadline: str | None) -> bool:
    """True when the deadline is a bare date (no time) — render it as all-day.

    Without this, a date becomes midnight UTC, which TickTick shows in the
    account's timezone (e.g. 5 PM the previous day in Los Angeles)."""
    return bool(deadline) and bool(_DATE_RE.match(deadline.strip()))
