"""Pure logic for reposting a DM dialogue into a group.

The owner reposts a captured 1-1 conversation (from `raw_messages`) into one of
the groups the bot sits in. Two things happen here, both side-effect-free and
unit-tested:

  - FILTER: keep only the owner's own messages, only the counterparty's, or all.
    We key off the stored `direction` ("out" = owner, "in" = counterparty), which
    is exactly how capture labels every message — no owner-id comparison needed.
  - FORMAT: render each kept message as a bold "who wrote it" label on its own
    line, the message text on the next, preserving chronological order. Output is
    HTML (the bot has no default parse mode) and chunked to Telegram's per-message
    length limit so a long dialogue is sent as several messages.

Native Telegram forwarding is handled by the caller; `can_native_forward` decides
feasibility (business-connection DM messages are NOT natively forwardable by a
bot, so those always fall back to the formatted repost).
"""
from __future__ import annotations

import html
from typing import Any

# Filter modes (also used verbatim in callback data).
FILTER_MINE = "mine"      # только мои (owner's own, direction == "out")
FILTER_THEIRS = "theirs"  # только собеседника (direction == "in")
FILTER_ALL = "all"        # все

# Telegram hard limit per text message.
TELEGRAM_TEXT_LIMIT = 4096

DEFAULT_OWNER_LABEL = "Я"
DEFAULT_PEER_LABEL = "Собеседник"


def filter_messages(messages: list[dict[str, Any]], mode: str) -> list[dict[str, Any]]:
    """Keep the subset selected by `mode`, preserving order.

    Unknown modes fall back to "all" so the caller never silently drops
    everything on a typo.
    """
    if mode == FILTER_MINE:
        return [m for m in messages if m.get("direction") == "out"]
    if mode == FILTER_THEIRS:
        return [m for m in messages if m.get("direction") == "in"]
    return list(messages)


def _label(m: dict[str, Any], owner_label: str) -> str:
    """Human name for the author of a message."""
    name = m.get("senderName")
    if name:
        return str(name)
    if m.get("direction") == "out":
        return owner_label
    return DEFAULT_PEER_LABEL


def format_message(m: dict[str, Any], owner_label: str = DEFAULT_OWNER_LABEL) -> str:
    """One message as `<b>Who:</b>\\n<text>` (HTML, escaped)."""
    label = _label(m, owner_label)
    text = (m.get("text") or "").strip()
    return f"<b>{html.escape(label)}:</b>\n{html.escape(text)}"


def _split_block(block: str, limit: int) -> list[str]:
    """Hard-split a single oversized block on line/char boundaries."""
    out: list[str] = []
    remaining = block
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        out.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        out.append(remaining)
    return out


def build_repost(
    messages: list[dict[str, Any]],
    mode: str,
    owner_label: str = DEFAULT_OWNER_LABEL,
    *,
    limit: int = TELEGRAM_TEXT_LIMIT,
) -> list[str]:
    """Filter + format a dialogue into a list of ready-to-send HTML chunks.

    Messages with no text are skipped. Order is preserved. Consecutive blocks are
    packed into as few chunks as possible without exceeding `limit`; a single
    block longer than `limit` is hard-split.
    """
    selected = filter_messages(messages, mode)
    blocks = [
        format_message(m, owner_label)
        for m in selected
        if (m.get("text") or "").strip()
    ]
    if not blocks:
        return []

    chunks: list[str] = []
    current = ""
    sep = "\n\n"
    for block in blocks:
        pieces = _split_block(block, limit) if len(block) > limit else [block]
        for piece in pieces:
            if not current:
                current = piece
            elif len(current) + len(sep) + len(piece) <= limit:
                current += sep + piece
            else:
                chunks.append(current)
                current = piece
    if current:
        chunks.append(current)
    return chunks


def can_native_forward(messages: list[dict[str, Any]]) -> bool:
    """True only if every selected message could be forwarded natively.

    A bot cannot natively forward/copy messages it saw over a business
    connection (the owner's private DMs), so any business-connection message
    forces the formatted-text fallback. Empty selection is not forwardable.
    """
    if not messages:
        return False
    return all(not m.get("businessConnectionId") for m in messages)


def derive_owner_label(messages: list[dict[str, Any]]) -> str:
    """Best owner display name from the dialogue (latest own message's sender)."""
    for m in reversed(messages):
        if m.get("direction") == "out" and m.get("senderName"):
            return str(m["senderName"])
    return DEFAULT_OWNER_LABEL
