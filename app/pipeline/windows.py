"""Conversation-window construction (spec §7).

The window answers ONLY "which fresh raw messages to look at now". Long-term
context is handled separately via chat_summary + open tasks.

Walk backward from the newest message; stop when a gap > CONV_GAP_HOURS opens
between consecutive messages, or when we'd exceed MAX_LOOKBACK_HOURS from the
newest message. The result is the current live conversation, whole.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo


def build_window(
    messages: list[dict[str, Any]],
    *,
    gap_hours: int,
    max_lookback_hours: int,
) -> list[dict[str, Any]]:
    """`messages` must be sorted ascending by `date`. Returns the window slice
    (also ascending). Empty input → empty window."""
    if not messages:
        return []

    gap = timedelta(hours=gap_hours)
    newest = messages[-1]["date"]
    floor = newest - timedelta(hours=max_lookback_hours)

    window: list[dict[str, Any]] = []
    prev: datetime | None = None  # date of the (later) message we already kept
    for msg in reversed(messages):
        when = msg["date"]
        if when < floor:
            break
        if prev is not None and (prev - when) > gap:
            break
        window.append(msg)
        prev = when

    window.reverse()
    return window


def render_window(messages: list[dict[str, Any]], tz: str | None = None) -> str:
    """Human/LLM-readable transcript with direction, sender, time, message id.

    Timestamps are rendered in `tz` (the owner's zone, e.g. America/Los_Angeles)
    so the extractor anchors relative dates ("сегодня"/"завтра") to LOCAL time,
    not UTC. `date` is stored UTC; we convert on display."""
    zone: ZoneInfo | None = None
    if tz:
        try:
            zone = ZoneInfo(tz)
        except Exception:  # noqa: BLE001
            zone = None
    lines: list[str] = []
    for m in messages:
        d = m["date"]
        if zone is not None:
            d = (d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d).astimezone(zone)
        ts = d.strftime("%Y-%m-%d %H:%M")
        direction = m.get("direction", "in")
        sender = "me" if direction == "out" else (m.get("senderName") or "counterparty")
        text = (m.get("text") or "").replace("\n", " ").strip()
        lines.append(f"[{ts}] ({direction}) {sender} #{m.get('messageId')}: {text}")
    return "\n".join(lines)
