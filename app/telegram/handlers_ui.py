"""Phase-1 UX: bind a chat to a TickTick project (spec §9).

No notifications are sent on task creation — the bot exists only as a remote
for project binding. The created task appearing in TickTick is the result.

Binding targets the chat in which the action is issued: a group via /bind, or
the current chat via the reply-menu. Per-counterparty DM binding is better
served by the Phase-2 WebApp; capture of those DMs already works regardless.
"""
from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import ChatMemberUpdatedFilter, Command, CommandStart, JOIN_TRANSITION
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

from .. import repositories as repo
from ..config import get_settings
from ..ticktick.mcp_client import get_ticktick

logger = logging.getLogger(__name__)

router = Router(name="ui")

BTN_BIND = "🔗 Привязать проект"
BTN_LIST = "📋 Мои привязки"
BTN_UNBIND = "❌ Отвязать"
BTN_APP = "🗂 Открыть мини-апку"

# Transient per-user bind session: user_id -> {"chatId": str, "projects": {id: name}}
_bind_sessions: dict[int, dict] = {}


def _main_menu() -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(text=BTN_BIND)], [KeyboardButton(text=BTN_LIST), KeyboardButton(text=BTN_UNBIND)]]
    # The Mini App (Phase 2) — a one-tap WebApp for managing all bindings at once.
    url = get_settings().webapp_url
    if url:
        rows.insert(0, [KeyboardButton(text=BTN_APP, web_app=WebAppInfo(url=url.rstrip("/") + "/app"))])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def _open_app_markup() -> InlineKeyboardMarkup | None:
    url = get_settings().webapp_url
    if not url:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Привязать к проекту", web_app=WebAppInfo(url=url.rstrip("/") + "/app"))]
        ]
    )


@router.message(Command("app"))
async def cmd_app(message: Message) -> None:
    url = get_settings().webapp_url
    if not url:
        await message.answer("Мини-апка ещё не настроена (нет WEBAPP_URL).")
        return
    await message.answer("Открыть управление привязками:", reply_markup=_main_menu())


@router.my_chat_member(ChatMemberUpdatedFilter(JOIN_TRANSITION))
async def on_added_to_group(event: ChatMemberUpdated, bot: Bot) -> None:
    """Bot was just added to a group — register it and nudge the owner to bind.

    Capture already starts automatically (no command needed); this only makes
    binding to a project a one-tap follow-up.
    """
    chat = event.chat
    if chat.type not in ("group", "supergroup"):
        return
    chat_id = f"group_{chat.id}"
    await repo.touch_chat_state(chat_id, repo.utcnow(), chat.title)

    owner_id = await repo.get_bot_state("owner_id")
    if not owner_id:
        return  # owner unknown until the bot is connected to Telegram Business
    try:
        await bot.send_message(
            int(owner_id),
            f"➕ Меня добавили в «{chat.title or chat_id}». Я уже слушаю эту группу — "
            "осталось привязать её к проекту TickTick.",
            reply_markup=_open_app_markup(),
        )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to notify owner about new group %s", chat_id)


def _chat_id_for(message: Message) -> str:
    if message.chat.type in ("group", "supergroup"):
        return f"group_{message.chat.id}"
    return f"user_{message.chat.id}"


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Привет! Я извлекаю задачи и договорённости из переписки и завожу их в TickTick.\n\n"
        "Уведомлений не шлю — задачи просто появляются в TickTick. "
        "Через меню ниже можно привязать этот чат к проекту.",
        reply_markup=_main_menu(),
    )


@router.message(Command("bind"))
@router.message(F.text == BTN_BIND)
async def start_bind(message: Message) -> None:
    if not message.from_user:
        return
    try:
        projects = await get_ticktick().get_projects()
    except Exception:  # noqa: BLE001
        logger.exception("get_projects failed")
        await message.answer("Не удалось получить список проектов из TickTick. Проверь TICKTICK_MCP_URL.")
        return
    if not projects:
        await message.answer("В TickTick нет проектов.")
        return

    chat_id = _chat_id_for(message)
    _bind_sessions[message.from_user.id] = {
        "chatId": chat_id,
        "projects": {p["id"]: p["name"] for p in projects},
    }
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=p["name"], callback_data=f"bind:{p['id']}")] for p in projects]
    )
    await message.answer(f"Выбери проект для этого чата (`{chat_id}`):", reply_markup=kb)


@router.callback_query(F.data.startswith("bind:"))
async def on_bind_choice(callback: CallbackQuery) -> None:
    if not callback.from_user or not callback.data:
        return
    project_id = callback.data.split(":", 1)[1]
    session = _bind_sessions.get(callback.from_user.id)
    if not session or project_id not in session["projects"]:
        await callback.answer("Сессия привязки истекла, начни заново.", show_alert=True)
        return

    chat_id = session["chatId"]
    name = session["projects"][project_id]
    await repo.set_project_binding(chat_id, project_id, name)
    _bind_sessions.pop(callback.from_user.id, None)

    await callback.answer("Привязано ✅")
    if callback.message:
        await callback.message.edit_text(f"Чат `{chat_id}` → проект «{name}» ✅")


@router.message(F.text == BTN_LIST)
@router.message(Command("bindings"))
async def list_bindings(message: Message) -> None:
    bindings = await repo.list_project_bindings()
    if not bindings:
        await message.answer("Пока нет ни одной привязки.")
        return
    lines = [f"• `{b['chatId']}` → «{b.get('projectName', '?')}»" for b in bindings]
    await message.answer("Текущие привязки:\n" + "\n".join(lines))


@router.message(F.text == BTN_UNBIND)
@router.message(Command("unbind"))
async def unbind(message: Message) -> None:
    chat_id = _chat_id_for(message)
    if await repo.delete_project_binding(chat_id):
        await message.answer(f"Привязка для `{chat_id}` снята.")
    else:
        await message.answer(f"Для `{chat_id}` привязки не было.")
