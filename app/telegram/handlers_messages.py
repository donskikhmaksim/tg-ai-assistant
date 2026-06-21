"""Capture every relevant update into raw_messages BEFORE any processing.

Sources (spec §6):
  - business_connection      -> remember owner id (defines `direction`)
  - business_message / edited -> private 1-1 (Telegram Business), in + out
  - message (groups)         -> bot in group with privacy off

Loss is irreversible: Telegram delivers each update once and never resends.
"""
from __future__ import annotations

import logging

from aiogram import Router
from aiogram.types import (
    BusinessConnection,
    BusinessMessagesDeleted,
    Message,
)

from .. import repositories as repo

logger = logging.getLogger(__name__)

router = Router(name="messages")

OWNER_ID_KEY = "owner_id"
BUSINESS_CONNECTION_KEY = "business_connection_id"


@router.business_connection()
async def on_business_connection(conn: BusinessConnection) -> None:
    """Owner connected/updated the bot to their Premium account."""
    await repo.set_bot_state(OWNER_ID_KEY, conn.user.id)
    await repo.set_bot_state(BUSINESS_CONNECTION_KEY, conn.id)
    logger.info(
        "Business connection %s for owner %s (enabled=%s)",
        conn.id,
        conn.user.id,
        conn.is_enabled,
    )


async def _owner_id() -> int | None:
    val = await repo.get_bot_state(OWNER_ID_KEY)
    return int(val) if val is not None else None


def _text_of(message: Message) -> str | None:
    return message.text or message.caption


@router.business_message()
@router.edited_business_message()
async def on_business_message(message: Message) -> None:
    """Private 1-1 conversation via Telegram Business (incoming + owner's own)."""
    text = _text_of(message)
    if not text:
        return  # nothing to extract from stickers/media-only

    owner_id = await _owner_id()
    from_id = message.from_user.id if message.from_user else None
    direction = "out" if (owner_id is not None and from_id == owner_id) else "in"

    # chat.id is the counterparty in both directions.
    chat_id = f"user_{message.chat.id}"
    await repo.save_raw_message(
        {
            "chatId": chat_id,
            "type": "dm",
            "direction": direction,
            "fromId": str(from_id) if from_id is not None else None,
            "senderName": _sender_name(message),
            "text": text,
            "messageId": message.message_id,
            "businessConnectionId": message.business_connection_id,
            "date": message.date,
        }
    )


@router.deleted_business_messages()
async def on_business_messages_deleted(event: BusinessMessagesDeleted) -> None:
    # We keep the raw archive intact (our DB is the only history); just log.
    logger.debug("Business messages deleted in chat %s: %s", event.chat.id, event.message_ids)


@router.message()
async def on_group_message(message: Message) -> None:
    """Group messages (bot is a member with privacy off)."""
    if message.chat.type not in ("group", "supergroup"):
        return  # private chats with the bot are handled by the UI router
    text = _text_of(message)
    if not text:
        return

    owner_id = await _owner_id()
    from_id = message.from_user.id if message.from_user else None
    direction = "out" if (owner_id is not None and from_id == owner_id) else "in"

    chat_id = f"group_{message.chat.id}"
    await repo.save_raw_message(
        {
            "chatId": chat_id,
            "type": "group",
            "direction": direction,
            "fromId": str(from_id) if from_id is not None else None,
            "senderName": _sender_name(message),
            "text": text,
            "messageId": message.message_id,
            "businessConnectionId": None,
            "date": message.date,
        }
    )


def _sender_name(message: Message) -> str | None:
    u = message.from_user
    if not u:
        return None
    return u.full_name or u.username
