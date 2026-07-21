"""Capture every relevant update into raw_messages BEFORE any processing.

Sources (spec §6):
  - business_connection      -> remember owner id (defines `direction`)
  - business_message / edited -> private 1-1 (Telegram Business), in + out
  - message (groups)         -> bot in group with privacy off

Loss is irreversible: Telegram delivers each update once and never resends.
"""
from __future__ import annotations

import logging

from aiogram import Bot, Router
from aiogram.types import (
    BusinessConnection,
    BusinessMessagesDeleted,
    Message,
)

from .. import repositories as repo
from ..config import get_settings
from ..tenancy import is_multi_tenant_allowed
from ..transcribe import transcribe_audio

logger = logging.getLogger(__name__)

router = Router(name="messages")

OWNER_ID_KEY = "owner_id"
BUSINESS_CONNECTION_KEY = "business_connection_id"


@router.business_connection()
async def on_business_connection(conn: BusinessConnection) -> None:
    """Owner connected/updated the bot to their Premium account."""
    # Single-tenant lock: while multi-tenant is OFF (the default) only the
    # primary owner is served. If an owner already exists and a DIFFERENT user
    # connects their Business account, do NOT register them as a new tenant —
    # the primary owner keeps connecting/updating normally, and a fresh deploy's
    # first connection still bootstraps the primary owner below.
    primary = await repo.get_bot_state(OWNER_ID_KEY)
    if (
        primary is not None
        and int(primary) != conn.user.id
        and not is_multi_tenant_allowed()
    ):
        logger.warning(
            "Ignoring Business connection %s from non-owner %s (single-tenant lock)",
            conn.id,
            conn.user.id,
        )
        return
    # Multi-tenant: record this connection -> owner mapping.
    await repo.set_business_connection(conn.id, conn.user.id, conn.is_enabled)
    # Back-compat: first/primary owner also lives in bot_state (legacy readers,
    # group fallback, Mini App bootstrap). We only set it once so a second
    # tenant connecting doesn't hijack the "primary" owner.
    if await repo.get_bot_state(OWNER_ID_KEY) is None:
        await repo.set_bot_state(OWNER_ID_KEY, conn.user.id)
    await repo.set_bot_state(BUSINESS_CONNECTION_KEY, conn.id)
    # One-time migration: move the owner's legacy global TickTick URL into their
    # own per-user vault entry, so we can retire the shared/global fallback.
    try:
        from ..onboarding.ticktick_resolve import seed_owner_from_env

        await seed_owner_from_env(str(conn.user.id))
    except Exception:  # noqa: BLE001
        logger.exception("Failed to seed owner TickTick URL into vault")
    logger.info(
        "Business connection %s for owner %s (enabled=%s)",
        conn.id,
        conn.user.id,
        conn.is_enabled,
    )


async def _owner_id() -> int | None:
    val = await repo.get_bot_state(OWNER_ID_KEY)
    return int(val) if val is not None else None


async def _conn_owner(connection_id: str | None) -> int | None:
    """Owner of a specific business connection (multi-tenant), falling back to
    the primary owner for legacy connections not yet in the registry."""
    owner = await repo.get_connection_owner(connection_id)
    if owner is not None:
        return int(owner)
    return await _owner_id()


async def _download(bot: Bot, file_id: str) -> bytes:
    f = await bot.get_file(file_id)
    buf = await bot.download_file(f.file_path)
    return buf.read()


async def _resolve_text(message: Message, bot: Bot) -> str | None:
    """Text/caption if present; otherwise transcribe a voice/audio/video note.

    Transcription runs on the Mac mini's Whisper service. Fails soft: if there's
    no text and transcription is off or errors, we return None and skip capture.
    """
    text = message.text or message.caption
    if text:
        return text
    media = message.voice or message.video_note or message.audio
    if media is None or not get_settings().transcribe_url:
        return None
    try:
        data = await _download(bot, media.file_id)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to download media for transcription")
        return None
    return await transcribe_audio(data, "audio.ogg")


@router.business_message()
@router.edited_business_message()
async def on_business_message(message: Message, bot: Bot) -> None:
    """Private 1-1 conversation via Telegram Business (incoming + owner's own)."""
    text = await _resolve_text(message, bot)
    if not text:
        return  # nothing to extract from stickers/media-only

    # Single-tenant lock: if this connection is positively registered to a
    # NON-primary owner (e.g. a tenant onboarded before the lock was turned on),
    # stop serving them. This never touches the primary owner (whose connection
    # resolves to `primary`) nor unregistered/legacy connections (registered is
    # None → falls back to the primary owner as before).
    if not is_multi_tenant_allowed():
        registered = await repo.get_connection_owner(message.business_connection_id)
        primary = await _owner_id()
        if registered is not None and primary is not None and int(registered) != primary:
            return

    owner_id = await _conn_owner(message.business_connection_id)
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
        },
        title=message.chat.full_name or message.chat.username,
        owner_id=str(owner_id) if owner_id is not None else None,
    )


@router.deleted_business_messages()
async def on_business_messages_deleted(event: BusinessMessagesDeleted) -> None:
    # We keep the raw archive intact (our DB is the only history); just log.
    logger.debug("Business messages deleted in chat %s: %s", event.chat.id, event.message_ids)


@router.message()
async def on_group_message(message: Message, bot: Bot) -> None:
    """Group messages (bot is a member with privacy off)."""
    if message.chat.type not in ("group", "supergroup"):
        return  # private chats with the bot are handled by the UI router
    text = await _resolve_text(message, bot)
    if not text:
        return

    # Groups carry no business connection; they belong to the primary owner.
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
        },
        title=message.chat.title,
        owner_id=str(owner_id) if owner_id is not None else None,
    )


def _sender_name(message: Message) -> str | None:
    u = message.from_user
    if not u:
        return None
    return u.full_name or u.username
