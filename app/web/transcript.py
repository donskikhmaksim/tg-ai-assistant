"""Pure helpers for the Telegram-style chat transcript page.

Kept free of web/runtime deps so the presentation logic (sender colouring,
initials, consecutive-message grouping) is unit-testable on its own.
"""
from __future__ import annotations

import hashlib
from typing import Any

# A small, Telegram-ish palette. Colour is picked by a stable hash of the
# sender name so the same person always gets the same colour across renders.
NAME_PALETTE = [
    "#e17076",  # red
    "#eda86c",  # orange
    "#a695e7",  # violet
    "#7bc862",  # green
    "#6ec9cb",  # cyan
    "#65aadd",  # blue
    "#ee7aae",  # pink
    "#f0a04b",  # amber
]


def sender_color(name: str | None) -> str:
    """Stable colour for a sender name (case/space-insensitive)."""
    key = (name or "").strip().lower()
    digest = hashlib.md5(key.encode("utf-8")).hexdigest()
    return NAME_PALETTE[int(digest, 16) % len(NAME_PALETTE)]


def initials(name: str | None) -> str:
    """1–2 letter avatar initials for a sender name."""
    parts = [p for p in (name or "").strip().split() if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[1][0]).upper()


def group_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse consecutive messages from the same side+sender into groups.

    Returns a list of {"direction", "senderName", "messages": [...]} preserving
    order, so the name/avatar can be shown once per group (Telegram style)."""
    groups: list[dict[str, Any]] = []
    for m in messages:
        direction = m.get("direction")
        sender = m.get("senderName") or ""
        if groups and groups[-1]["direction"] == direction and groups[-1]["senderName"] == sender:
            groups[-1]["messages"].append(m)
        else:
            groups.append({"direction": direction, "senderName": sender, "messages": [m]})
    return groups
