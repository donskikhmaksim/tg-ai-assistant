"""Deadline formatting helpers for the pipeline."""
from __future__ import annotations

import re

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def to_ticktick_due(deadline: str | None) -> str | None:
    """Claude emits a plain YYYY-MM-DD (or null). TickTick's create_task wants
    ISO `YYYY-MM-DDThh:mm:ss+0000`. Treat the date as start-of-day UTC."""
    if not deadline:
        return None
    if _DATE_RE.match(deadline):
        return f"{deadline}T00:00:00+0000"
    return deadline  # already a full timestamp; pass through
