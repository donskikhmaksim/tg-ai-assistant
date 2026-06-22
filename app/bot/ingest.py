"""Преобразование Telegram-апдейтов в документы raw_messages (§5, §6 ТЗ).

Сохраняем КАЖДЫЙ апдейт атомарно до любой обработки — потеря безвозвратна,
повторно из Telegram не придёт.
"""
from __future__ import annotations

import logging

from aiogram.types import Message

from app.db import repositories as repo

log = logging.getLogger(__name__)


def _sender_name(message: Message) -> str:
    u = message.from_user
    if u is None:
        return "?"
    return u.full_name or (f"@{u.username}" if u.username else str(u.id))


def _text(message: Message) -> str | None:
    return message.text or message.caption


def _build_doc(
    message: Message, chat_type: str, chat_id: str, direction: str
) -> dict:
    return {
        "chatId": chat_id,
        "type": chat_type,
        "direction": direction,
        "fromId": str(message.from_user.id) if message.from_user else None,
        "senderName": _sender_name(message),
        "text": _text(message) or "",
        "messageId": message.message_id,
        "businessConnectionId": message.business_connection_id,
        "date": message.date,
    }


async def ingest_dm(message: Message, owner_id: int | None) -> None:
    """Личка через Telegram Business. direction по from.id == owner."""
    text = _text(message)
    if not text:
        return  # медиа без текста не несёт задач
    chat_id = f"user_{message.chat.id}"
    direction = (
        "out"
        if owner_id is not None and message.from_user and message.from_user.id == owner_id
        else "in"
    )
    doc = _build_doc(message, "dm", chat_id, direction)
    await repo.save_raw_message(doc)


async def ingest_group(message: Message, owner_id: int | None) -> None:
    """Группа: обычный message, privacy off. direction out если автор — владелец."""
    text = _text(message)
    if not text:
        return
    chat_id = f"group_{message.chat.id}"
    direction = (
        "out"
        if owner_id is not None and message.from_user and message.from_user.id == owner_id
        else "in"
    )
    doc = _build_doc(message, "group", chat_id, direction)
    await repo.save_raw_message(doc)
