"""Хендлеры бота: приём апдейтов в БД и UX привязки проектов (§6, §9 ТЗ)."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import (
    JOIN_TRANSITION,
    ChatMemberUpdatedFilter,
    Command,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BusinessConnection,
    CallbackQuery,
    ChatMemberUpdated,
    Message,
)

from app.bot import ingest, keyboards
from app.db import repositories as repo
from app.mcp.ticktick import TickTickMCP

log = logging.getLogger(__name__)
router = Router()

_owner_id: int | None = None


async def _get_owner_id() -> int | None:
    global _owner_id
    if _owner_id is None:
        _owner_id = await repo.get_setting("owner_id")
    return _owner_id


async def _is_owner(user_id: int | None) -> bool:
    """True, если действие совершает владелец. Пока владелец неизвестен
    (Business ещё не подключён) — не блокируем."""
    owner_id = await _get_owner_id()
    return owner_id is None or user_id == owner_id


class BindFlow(StatesGroup):
    waiting_target = State()


# ── Telegram Business: соединение ─────────────────────────────────────────────
@router.business_connection()
async def on_business_connection(conn: BusinessConnection) -> None:
    """Сохранить владельца (это «я» для определения direction)."""
    global _owner_id
    _owner_id = conn.user.id
    await repo.set_setting("owner_id", conn.user.id)
    await repo.set_setting("business_connection_id", conn.id)
    log.info("Business подключён: owner_id=%s, conn=%s", conn.user.id, conn.id)


# ── Личка через Business (вход + исходящие владельца) ─────────────────────────
@router.business_message()
async def on_business_message(message: Message) -> None:
    await ingest.ingest_dm(message, await _get_owner_id())


@router.edited_business_message()
async def on_edited_business_message(message: Message) -> None:
    await ingest.ingest_dm(message, await _get_owner_id())


# ── Группы: бота добавили → приветствие + авто-выбор проекта (§9) ─────────────
@router.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=JOIN_TRANSITION))
async def on_added_to_group(event: ChatMemberUpdated, ticktick: TickTickMCP) -> None:
    log.info(
        "my_chat_member: чат=%s тип=%s статус=%s",
        event.chat.id,
        event.chat.type,
        event.new_chat_member.status,
    )
    if event.chat.type not in ("group", "supergroup"):
        return
    bot = event.bot
    chat_key = f"group_{event.chat.id}"
    me = await bot.me()

    mapping = await repo.get_project_mapping(chat_key)
    if mapping:
        target = f"проект «{mapping.get('projectName', '?')}»"
    else:
        target = "проект по умолчанию (Inbox) — пока не выбран другой"

    await bot.send_message(
        event.chat.id,
        f"👁 {me.full_name} теперь в этой группе.\n\n"
        f"Я вижу всё, что здесь говорится. Каждое обещание, каждая "
        f"договорённость, каждое «сделаю завтра» — будет замечено, записано "
        f"и превращено в задачу.\n"
        f"Думай, что говоришь: сказанное здесь не забывается.\n\n"
        f"Собранное тихо ложится в TickTick: {target}. "
        f"Я не предупреждаю — я просто знаю.",
    )

    projects = await _safe_projects(ticktick)
    if projects:
        await bot.send_message(
            event.chat.id,
            "Куда складывать собранное? Выбери проект "
            "(доступно только владельцу):",
            reply_markup=keyboards.projects_inline(projects, chat_key),
        )
    else:
        await bot.send_message(
            event.chat.id,
            "Проект задаётся командой /bind (доступно только владельцу).",
        )


# ── Группы: обычные message с privacy off ─────────────────────────────────────
@router.message(F.chat.type.in_({"group", "supergroup"}), Command("bind"))
async def on_group_bind(message: Message, ticktick: TickTickMCP) -> None:
    if not await _is_owner(message.from_user.id if message.from_user else None):
        return  # чужие /bind в группе тихо игнорируем
    chat_key = f"group_{message.chat.id}"
    projects = await _safe_projects(ticktick)
    if not projects:
        await message.reply("Не удалось получить проекты TickTick.")
        return
    await message.reply(
        "Выберите проект для этой группы (настроить может только владелец):",
        reply_markup=keyboards.projects_inline(projects, chat_key),
    )


@router.message(F.chat.type.in_({"group", "supergroup"}))
async def on_group_message(message: Message) -> None:
    await ingest.ingest_group(message, await _get_owner_id())


# ── Приватный чат с ботом: меню управления ────────────────────────────────────
@router.message(Command("start"), F.chat.type == "private")
async def on_start(message: Message) -> None:
    await message.answer(
        "👁 Большой Брат на связи.\n\n"
        "Я тихо наблюдаю за твоей перепиской — личкой и группами — и всё, что "
        "похоже на задачу, обещание или договорённость, складываю в TickTick. "
        "Уведомлений не присылаю: задачи появляются сами. Я всё вижу.\n\n"
        "Меню ниже — чтобы указать, какой чат в какой проект отправлять.",
        reply_markup=keyboards.main_menu(),
    )


@router.message(F.text == keyboards.BTN_BIND, F.chat.type == "private")
async def on_bind_start(message: Message, state: FSMContext) -> None:
    await state.set_state(BindFlow.waiting_target)
    await message.answer(
        "Перешлите сюда любое сообщение из чата/группы, которую нужно привязать "
        "(или отправьте её идентификатор: `user_<id>`, `group_<id>` или числовой id).",
        parse_mode="Markdown",
    )


@router.message(BindFlow.waiting_target, F.chat.type == "private")
async def on_bind_target(
    message: Message, state: FSMContext, ticktick: TickTickMCP
) -> None:
    chat_key = _chat_key_from_message(message)
    if not chat_key:
        await message.answer(
            "Не понял чат. Перешлите сообщение из него или пришлите id "
            "(`user_<id>` / `group_<id>`).",
            parse_mode="Markdown",
        )
        return
    await state.clear()
    projects = await _safe_projects(ticktick)
    if not projects:
        await message.answer("Не удалось получить проекты TickTick.")
        return
    await message.answer(
        f"Чат `{chat_key}`. Выберите проект:",
        parse_mode="Markdown",
        reply_markup=keyboards.projects_inline(projects, chat_key),
    )


@router.message(F.text == keyboards.BTN_LIST, F.chat.type == "private")
async def on_list(message: Message) -> None:
    mappings = await repo.list_project_mappings()
    if not mappings:
        await message.answer("Привязок пока нет.")
        return
    lines = [f"• `{m['chatId']}` → {m.get('projectName', '?')}" for m in mappings]
    await message.answer("Текущие привязки:\n" + "\n".join(lines), parse_mode="Markdown")


@router.message(F.text == keyboards.BTN_UNBIND, F.chat.type == "private")
async def on_unbind_menu(message: Message) -> None:
    mappings = await repo.list_project_mappings()
    if not mappings:
        await message.answer("Отвязывать нечего.")
        return
    await message.answer(
        "Что отвязать?", reply_markup=keyboards.bindings_inline(mappings)
    )


# ── Инлайн-колбэки ────────────────────────────────────────────────────────────
@router.callback_query(F.data.startswith("bind|"))
async def on_bind_callback(call: CallbackQuery, ticktick: TickTickMCP) -> None:
    if not await _is_owner(call.from_user.id):
        await call.answer("Настраивать привязки может только владелец.", show_alert=True)
        return
    _, chat_key, project_id = call.data.split("|", 2)
    name = await _project_name(ticktick, project_id)
    await repo.set_project_mapping(chat_key, project_id, name)
    await call.answer("Привязано")
    if isinstance(call.message, Message):
        await call.message.edit_text(f"✅ `{chat_key}` → {name}", parse_mode="Markdown")


@router.callback_query(F.data.startswith("unbind|"))
async def on_unbind_callback(call: CallbackQuery) -> None:
    if not await _is_owner(call.from_user.id):
        await call.answer("Настраивать привязки может только владелец.", show_alert=True)
        return
    _, chat_key = call.data.split("|", 1)
    removed = await repo.unset_project_mapping(chat_key)
    await call.answer("Отвязано" if removed else "Не найдено")
    if isinstance(call.message, Message):
        await call.message.edit_text(f"🗑 Отвязано: `{chat_key}`", parse_mode="Markdown")


# ── Вспомогательное ───────────────────────────────────────────────────────────
async def _safe_projects(ticktick: TickTickMCP) -> list[dict[str, str]]:
    try:
        return await ticktick.get_projects()
    except Exception:  # noqa: BLE001
        log.exception("Ошибка get_projects")
        return []


async def _project_name(ticktick: TickTickMCP, project_id: str) -> str:
    for p in await _safe_projects(ticktick):
        if p["id"] == project_id:
            return p["name"]
    return project_id


def _chat_key_from_message(message: Message) -> str | None:
    """Извлечь chatId привязки из пересланного сообщения или текста."""
    origin = message.forward_origin
    if origin is not None:
        user = getattr(origin, "sender_user", None)
        if user is not None:
            return f"user_{user.id}"
        chat = getattr(origin, "sender_chat", None) or getattr(origin, "chat", None)
        if chat is not None:
            return f"group_{chat.id}"

    text = (message.text or "").strip()
    if text.startswith(("user_", "group_")):
        return text
    if text.lstrip("-").isdigit():
        return f"user_{text}"
    return None
