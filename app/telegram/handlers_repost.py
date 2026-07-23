"""Repost a DM dialogue into a group (owner-only).

Flow (all in the owner's PRIVATE chat with the bot, so nothing leaks to the
counterparty and no business-connection send is needed):

  1. `/repost` (or the "Переслать диалог" reply button) → pick a DM source
     (callback `rpsrc:<chatId>`)
  2. pick a filter — только мои / только собеседника / все
     (callback `rpflt:<chatId>:<mode>`)
  3. pick a target group the bot sits in
     (callback `rpgo:<chatId>:<mode>:<groupChatId>`) → build + send.

Source is `raw_messages` (the captured dialogue). When the selected messages are
natively forwardable (`repost.can_native_forward`) we `copy_messages`; DM messages
arrive over a business connection and are NOT, so those fall back to the formatted
HTML repost. Callbacks are stateless — everything needed rides in the callback
data — matching the bind flow, so they survive a redeploy mid-flow.
"""
from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .. import repositories as repo
from . import repost as rp

logger = logging.getLogger(__name__)

router = Router(name="repost")

BTN_REPOST = "↪️ Переслать диалог"

_FILTER_LABELS = [
    (rp.FILTER_MINE, "🙋 Только мои"),
    (rp.FILTER_THEIRS, "🧑 Только собеседника"),
    (rp.FILTER_ALL, "👥 Все"),
]
_FILTER_HUMAN = {
    rp.FILTER_MINE: "только мои",
    rp.FILTER_THEIRS: "только собеседника",
    rp.FILTER_ALL: "все",
}


async def _is_owner(user_id: int | None) -> bool:
    """True if the actor is the one owner (mirrors handlers_ui). Until the owner
    is known we don't block, so first-time setup isn't locked out."""
    owner = await repo.get_bot_state("owner_id")
    if owner is None:
        return True
    return user_id is not None and int(owner) == int(user_id)


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------

async def _dm_sources_keyboard() -> InlineKeyboardMarkup:
    """Recent DM chats (chatId starts with "user_"), newest first."""
    chats = [c for c in await repo.list_known_chats() if c["chatId"].startswith("user_")]
    rows = [
        [InlineKeyboardButton(
            text=c.get("title") or c["chatId"],
            callback_data=f"rpsrc:{c['chatId']}",
        )]
        for c in chats[:30]
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _filter_keyboard(src_chat: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"rpflt:{src_chat}:{mode}")]
        for mode, label in _FILTER_LABELS
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _groups_keyboard(src_chat: str, mode: str) -> InlineKeyboardMarkup:
    """Groups the bot has seen (chatId starts with "group_"), newest first."""
    chats = [c for c in await repo.list_known_chats() if c["chatId"].startswith("group_")]
    rows = [
        [InlineKeyboardButton(
            text=c.get("title") or c["chatId"],
            callback_data=f"rpgo:{src_chat}:{mode}:{c['chatId']}",
        )]
        for c in chats[:30]
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _safe_edit(callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    msg = callback.message
    if not isinstance(msg, Message):
        return
    try:
        await msg.edit_text(text, reply_markup=markup)
    except Exception:  # noqa: BLE001 — message too old to edit, etc.
        logger.debug("edit_text failed", exc_info=True)


# ---------------------------------------------------------------------------
# Entry: /repost and the reply-menu button (private chat only)
# ---------------------------------------------------------------------------

@router.message(Command("repost"), F.chat.type == "private")
@router.message(F.text == BTN_REPOST, F.chat.type == "private")
async def cmd_repost(message: Message) -> None:
    actor = message.from_user.id if message.from_user else None
    if not await _is_owner(actor):
        return  # silent for non-owners — this is a private instance
    kb = await _dm_sources_keyboard()
    if not kb.inline_keyboard:
        await message.answer("Пока нет ни одного личного диалога для пересылки.")
        return
    await message.answer("Какой личный диалог переслать в группу?", reply_markup=kb)


@router.callback_query(F.data.startswith("rpsrc:"))
async def on_pick_source(callback: CallbackQuery) -> None:
    if not callback.data:
        return
    if not await _is_owner(callback.from_user.id):
        await callback.answer("Только владелец.", show_alert=True)
        return
    src_chat = callback.data.split(":", 1)[1]
    await callback.answer()
    title = await repo.get_chat_title(src_chat)
    await _safe_edit(
        callback,
        f"Диалог «{title}». Что переслать?",
        _filter_keyboard(src_chat),
    )


@router.callback_query(F.data.startswith("rpflt:"))
async def on_pick_filter(callback: CallbackQuery) -> None:
    if not callback.data:
        return
    if not await _is_owner(callback.from_user.id):
        await callback.answer("Только владелец.", show_alert=True)
        return
    _, src_chat, mode = callback.data.split(":", 2)
    await callback.answer()
    kb = await _groups_keyboard(src_chat, mode)
    if not kb.inline_keyboard:
        await _safe_edit(
            callback,
            "Бот пока не состоит ни в одной группе — переслать некуда.",
        )
        return
    title = await repo.get_chat_title(src_chat)
    await _safe_edit(
        callback,
        f"Диалог «{title}», {_FILTER_HUMAN.get(mode, mode)}.\nВ какую группу переслать?",
        kb,
    )


@router.callback_query(F.data.startswith("rpgo:"))
async def on_execute(callback: CallbackQuery, bot: Bot) -> None:
    if not callback.data:
        return
    if not await _is_owner(callback.from_user.id):
        await callback.answer("Только владелец.", show_alert=True)
        return
    _, src_chat, mode, group_chat = callback.data.split(":", 3)
    await callback.answer("Пересылаю…")

    messages = await repo.get_chat_messages(src_chat)
    selected = rp.filter_messages(messages, mode)
    if not selected:
        await _safe_edit(callback, "В этом диалоге нет подходящих сообщений для пересылки.")
        return

    try:
        target_id = int(group_chat.removeprefix("group_"))
    except ValueError:
        await _safe_edit(callback, "Не понял группу назначения.")
        return

    src_title = await repo.get_chat_title(src_chat)
    group_title = await repo.get_chat_title(group_chat)
    header = (
        f"↪️ Пересланный диалог с «{src_title}» "
        f"({_FILTER_HUMAN.get(mode, mode)}):"
    )

    sent_native = False
    if rp.can_native_forward(selected):
        try:
            await bot.copy_messages(
                chat_id=target_id,
                from_chat_id=int(src_chat.removeprefix("user_")),
                message_ids=[m["messageId"] for m in selected],
            )
            sent_native = True
        except Exception:  # noqa: BLE001 — fall back to the formatted repost
            logger.info("native copy_messages failed, using formatted repost", exc_info=True)

    if not sent_native:
        owner_label = rp.derive_owner_label(messages)
        chunks = rp.build_repost(messages, mode, owner_label)
        if not chunks:
            await _safe_edit(callback, "В этом диалоге нет текста для пересылки.")
            return
        try:
            await bot.send_message(target_id, header)
            for chunk in chunks:
                await bot.send_message(target_id, chunk, parse_mode="HTML")
        except Exception:  # noqa: BLE001
            logger.exception("repost send failed")
            await _safe_edit(
                callback,
                "⚠️ Не смог отправить в группу. Проверь, что бот всё ещё в ней "
                "и может писать.",
            )
            return

    await _safe_edit(
        callback,
        f"✅ Переслал диалог с «{src_title}» ({_FILTER_HUMAN.get(mode, mode)}) "
        f"в «{group_title}».",
    )
