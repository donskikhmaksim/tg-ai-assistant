"""Deadline formatting helpers for the pipeline."""
from __future__ import annotations

import re

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def to_ticktick_due(deadline: str | None) -> str | None:
    """Claude emits a plain YYYY-MM-DD (or null). TickTick's create_task wants
    ISO `YYYY-MM-DDThh:mm:ss+0000`. Treat the date as start-of-day UTC.

    For a date-only deadline the task should be ALL-DAY (see is_all_day_deadline)
    so TickTick shows just the day with no clock time and no timezone shift."""
    if not deadline:
        return None
    if _DATE_RE.match(deadline):
        return f"{deadline}T00:00:00+0000"
    return deadline  # already a full timestamp; pass through


def is_all_day_deadline(deadline: str | None) -> bool:
    """True when the deadline is a bare date (no time) — render it as all-day.

    Without this, a date becomes midnight UTC, which TickTick shows in the
    account's timezone (e.g. 5 PM the previous day in Los Angeles)."""
    return bool(deadline) and bool(_DATE_RE.match(deadline))
