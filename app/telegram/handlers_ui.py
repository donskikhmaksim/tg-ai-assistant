"""Phase-1 UX: bind a chat to a TickTick project (spec §9).

No notifications are sent on task creation — the bot exists only as a remote
for project binding. The created task appearing in TickTick is the result.

Binding flow (same for a group auto-prompt on join and an explicit /bind):
  1. pick a project  (callback `pp:<projectId>`)
  2. pick a section/column inside it, if the project has any
     (callback `ps:<projectId>:<sectionId|none>`)

Callbacks are stateless — everything needed is in the callback data and the
chat the button lives in — so they survive a redeploy mid-flow. In a group the
picker is visible to everyone but only the owner may actually press it.

Binding targets the chat in which the action is issued: a group via join/`/bind`,
or the current chat via the reply-menu. Per-counterparty DM binding is better
served by the Phase-2 WebApp; capture of those DMs already works regardless.
"""
from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import ChatMemberUpdatedFilter, Command, CommandStart, JOIN_TRANSITION
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
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

from .. import github as gh

from .. import repositories as repo
from ..config import get_settings
from ..ticktick.mcp_client import get_ticktick
from .notify import group_watch_announcement

logger = logging.getLogger(__name__)

router = Router(name="ui")


class Onboarding(StatesGroup):
    waiting_github = State()

BTN_BIND = "🔗 Привязать проект"
BTN_LIST = "📋 Мои привязки"
BTN_UNBIND = "❌ Отвязать"
BTN_APP = "🗂 Открыть мини-апку"

# Group join greeting — the bot's persona is "Большой Брат" (Big Brother).
def _welcome_text(bot_name: str) -> str:
    return (
        f"👁 На связи {bot_name}.\n\n"
        "С этой минуты я слышу каждое слово в этом чате. Каждое «сделаю», "
        "«отправлю», «давай к пятнице» — я замечу, запомню и превращу в задачу. "
        "Ничего не упущу.\n\n"
        "Думайте, что обещаете: сказанное здесь не забывается — оно тихо ложится "
        "в TickTick.\n\n"
        "Я не предупреждаю дважды. Я просто знаю."
    )


def _main_menu() -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(text=BTN_BIND)], [KeyboardButton(text=BTN_LIST), KeyboardButton(text=BTN_UNBIND)]]
    # The Mini App (Phase 2) — a one-tap WebApp for managing all bindings at once.
    url = get_settings().webapp_url
    if url:
        rows.insert(0, [KeyboardButton(text=BTN_APP, web_app=WebAppInfo(url=url.rstrip("/") + "/app"))])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


# ---------------------------------------------------------------------------
# Ownership: only the Business owner may configure bindings.
# ---------------------------------------------------------------------------

async def _is_owner(user_id: int | None) -> bool:
    """True if the actor is the owner. Until the owner is known (Business not yet
    connected) we don't block, so first-time setup isn't locked out."""
    owner = await repo.get_bot_state("owner_id")
    if owner is None:
        return True
    return user_id is not None and int(owner) == int(user_id)


# ---------------------------------------------------------------------------
# TickTick lookups (best-effort — never raise into a handler).
# ---------------------------------------------------------------------------

async def _safe_projects() -> list[dict[str, str]]:
    try:
        return await get_ticktick().get_projects()
    except Exception:  # noqa: BLE001
        logger.exception("get_projects failed")
        return []


async def _safe_sections(project_id: str) -> list[dict[str, str]]:
    try:
        return await get_ticktick().get_sections(project_id)
    except Exception:  # noqa: BLE001
        logger.exception("get_sections failed")
        return []


async def _project_name(project_id: str) -> str:
    for p in await _safe_projects():
        if p["id"] == project_id:
            return p["name"]
    return project_id


async def _section_name(project_id: str, section_id: str) -> str:
    for s in await _safe_sections(project_id):
        if s["id"] == section_id:
            return s["name"]
    return section_id


def _projects_keyboard(projects: list[dict[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=p["name"], callback_data=f"pp:{p['id']}")]
            for p in projects
        ]
    )


def _sections_keyboard(project_id: str, sections: list[dict[str, str]]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=s["name"], callback_data=f"ps:{project_id}:{s['id']}")]
        for s in sections
    ]
    rows.append([InlineKeyboardButton(text="— без раздела —", callback_data=f"ps:{project_id}:none")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _chat_id_for(message: Message) -> str:
    if message.chat.type in ("group", "supergroup"):
        return f"group_{message.chat.id}"
    return f"user_{message.chat.id}"


def _chat_key_from_callback(callback: CallbackQuery) -> str | None:
    msg = callback.message
    if not isinstance(msg, Message):
        return None
    if msg.chat.type in ("group", "supergroup"):
        return f"group_{msg.chat.id}"
    return f"user_{msg.chat.id}"


async def _confirm_bind(
    callback: CallbackQuery, chat_key: str, project_name: str, section_name: str | None = None
) -> None:
    """Edit the picker into a confirmation. In a group it's the Big Brother
    'отбивка' announcing what surveillance now feeds into."""
    if chat_key.startswith("group_"):
        await _safe_edit(callback, group_watch_announcement(project_name, section_name))
    else:
        target = f"«{project_name}»" + (f" / раздел «{section_name}»" if section_name else "")
        await _safe_edit(callback, f"📌 `{chat_key}` → {target} ✅")


async def _safe_edit(
    callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None
) -> None:
    msg = callback.message
    if not isinstance(msg, Message):
        return
    try:
        await msg.edit_text(text, reply_markup=markup)
    except Exception:  # noqa: BLE001 — e.g. message too old to edit
        logger.debug("edit_text failed", exc_info=True)


async def _send_project_picker(bot: Bot, chat_id: int) -> None:
    """Post the project picker into the given chat (group or DM)."""
    projects = await _safe_projects()
    if not projects:
        await bot.send_message(
            chat_id,
            "Не вижу проектов в TickTick (или не настроен TICKTICK_MCP_URL). "
            "Когда починится — нажми /bind.",
        )
        return
    await bot.send_message(
        chat_id,
        "Куда складывать собранное? Выбери проект "
        "(нажать может только владелец):",
        reply_markup=_projects_keyboard(projects),
    )


# ---------------------------------------------------------------------------
# Group join: greet, then immediately offer the project picker in the group.
# ---------------------------------------------------------------------------

@router.my_chat_member(ChatMemberUpdatedFilter(JOIN_TRANSITION))
async def on_added_to_group(event: ChatMemberUpdated, bot: Bot) -> None:
    chat = event.chat
    if chat.type not in ("group", "supergroup"):
        return
    chat_id = f"group_{chat.id}"
    await repo.touch_chat_state(chat_id, repo.utcnow(), chat.title)

    me = await bot.me()
    try:
        await bot.send_message(chat.id, _welcome_text(me.full_name))
    except Exception:  # noqa: BLE001
        logger.exception("Failed to send welcome to group %s", chat_id)
    await _send_project_picker(bot, chat.id)


# ---------------------------------------------------------------------------
# Explicit /bind (group or DM) and the DM reply-menu button.
# ---------------------------------------------------------------------------

@router.message(Command("bind"))
@router.message(F.text == BTN_BIND)
async def start_bind(message: Message) -> None:
    if message.chat.type in ("group", "supergroup"):
        actor = message.from_user.id if message.from_user else None
        if not await _is_owner(actor):
            return  # someone else's /bind in a group — ignore silently
    await _send_project_picker(message.bot, message.chat.id)


@router.callback_query(F.data.startswith("pp:"))
async def on_pick_project(callback: CallbackQuery) -> None:
    if not callback.data:
        return
    if not await _is_owner(callback.from_user.id):
        await callback.answer("Выбирать проект может только владелец.", show_alert=True)
        return
    chat_key = _chat_key_from_callback(callback)
    if chat_key is None:
        await callback.answer("Не понял чат.", show_alert=True)
        return

    project_id = callback.data.split(":", 1)[1]
    project_name = await _project_name(project_id)
    sections = await _safe_sections(project_id)

    if not sections:
        # No sections on this project — bind right away.
        await repo.set_project_binding(chat_key, project_id, project_name)
        await callback.answer("Привязано ✅")
        await _confirm_bind(callback, chat_key, project_name)
        return

    await callback.answer()
    await _safe_edit(
        callback,
        f"Проект «{project_name}». Теперь выбери раздел "
        "(сюда будет падать всё на разбор):",
        _sections_keyboard(project_id, sections),
    )


@router.callback_query(F.data.startswith("ps:"))
async def on_pick_section(callback: CallbackQuery) -> None:
    if not callback.data:
        return
    if not await _is_owner(callback.from_user.id):
        await callback.answer("Выбирать раздел может только владелец.", show_alert=True)
        return
    chat_key = _chat_key_from_callback(callback)
    if chat_key is None:
        await callback.answer("Не понял чат.", show_alert=True)
        return

    _, project_id, section_id = callback.data.split(":", 2)
    project_name = await _project_name(project_id)

    if section_id == "none":
        await repo.set_project_binding(chat_key, project_id, project_name)
        await callback.answer("Привязано ✅")
        await _confirm_bind(callback, chat_key, project_name)
        return

    section_name = await _section_name(project_id, section_id)
    await repo.set_project_binding(chat_key, project_id, project_name, section_id, section_name)
    await callback.answer("Привязано ✅")
    await _confirm_bind(callback, chat_key, project_name, section_name)


# ---------------------------------------------------------------------------
# Misc commands / menu.
# ---------------------------------------------------------------------------

@router.message(Command("app"))
async def cmd_app(message: Message) -> None:
    url = get_settings().webapp_url
    if not url:
        await message.answer("Мини-апка ещё не настроена (нет WEBAPP_URL).")
        return
    await message.answer("Открыть управление привязками:", reply_markup=_main_menu())


def _deploy_prompt(repo: str) -> str:
    return (
        f"Please help me deploy my own instance of the tg-ai-assistant.\n\n"
        f"Steps:\n"
        f"1. Clone the repo: `gh repo clone {repo}`\n"
        f"2. `cd tg-ai-assistant`\n"
        f"3. Install Railway CLI if missing: `npm i -g @railway/cli`\n"
        f"4. `railway login` (browser will open)\n"
        f"5. `railway init` — create a new project called tg-ai-assistant\n"
        f"6. Add MongoDB: `railway add --plugin mongodb`\n"
        f"7. Set required env vars (ask me for the values one by one):\n"
        f"   BOT_TOKEN, ANTHROPIC_API_KEY, TICKTICK_MCP_URL, MCP_SECRET\n"
        f"8. `railway up` — deploy\n"
        f"9. Verify the bot is online by sending /start to it on Telegram\n\n"
        f"Ask the user for each value you don't know. Do not invent secrets."
    )


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    s = get_settings()
    if await _is_owner(message.from_user.id if message.from_user else None):
        await message.answer(
            "👁 Большой Брат на связи.\n\n"
            "Я извлекаю задачи и договорённости из переписки — лички и групп — и "
            "завожу их в TickTick. Уведомлений не шлю: задачи просто появляются. "
            "Я всё вижу.\n\n"
            "Через меню ниже можно привязать этот чат к проекту.",
            reply_markup=_main_menu(),
        )
        return

    # Non-owner: start onboarding flow.
    if not s.github_token or not s.github_repo:
        await message.answer(
            "👋 Привет! Этот бот можно развернуть у себя.\n\n"
            "Онбординг пока не настроен владельцем — напиши ему напрямую."
        )
        return

    await message.answer(
        "👋 Привет! Хочешь поставить себе такого же бота?\n\n"
        "Он будет работать только на тебя — твои данные у тебя, я их не вижу.\n\n"
        "Есть ли у тебя аккаунт на GitHub?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да, есть", callback_data="ob:has_github"),
                InlineKeyboardButton(text="❌ Нет", callback_data="ob:no_github"),
            ]
        ]),
    )


@router.callback_query(F.data == "ob:no_github")
async def onboarding_no_github(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    msg = callback.message
    if isinstance(msg, Message):
        await msg.edit_text(
            "Нужно зарегистрироваться на GitHub — это бесплатно и займёт 2 минуты:\n\n"
            "1. Зайди на [github.com](https://github.com) → *Sign up*\n"
            "2. Придумай username (он понадобится на следующем шаге)\n"
            "3. Вернись сюда и нажми /start",
            parse_mode="Markdown",
        )


@router.callback_query(F.data == "ob:has_github")
async def onboarding_has_github(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(Onboarding.waiting_github)
    msg = callback.message
    if isinstance(msg, Message):
        await msg.edit_text(
            "Отлично! Напиши свой *GitHub username* (то что после github.com/...)",
            parse_mode="Markdown",
        )


@router.message(Onboarding.waiting_github)
async def onboarding_github(message: Message, state: FSMContext) -> None:
    s = get_settings()
    username = (message.text or "").strip().lstrip("@")
    if not username or " " in username:
        await message.answer("Это не похоже на GitHub username. Попробуй ещё раз:")
        return

    await message.answer(f"⏳ Добавляю @{username} в репозиторий...")
    try:
        await gh.add_collaborator(s.github_token, s.github_repo, username)
    except Exception:
        logger.exception("GitHub invite failed for %s", username)
        await message.answer(
            "❌ Не удалось отправить приглашение. Проверь username и попробуй позже."
        )
        return

    await state.clear()

    prompt = _deploy_prompt(s.github_repo)
    await message.answer(
        f"✅ Приглашение отправлено на GitHub — прими его по email.\n\n"
        f"Дальше: открой *Claude Code* на маке и вставь вот этот промпт:\n\n"
        f"```\n{prompt}\n```\n\n"
        f"Claude Code сам пройдёт все шаги деплоя. Если застрянешь — пиши сюда.",
        parse_mode="Markdown",
    )


@router.message(F.text == BTN_LIST)
@router.message(Command("bindings"))
async def list_bindings(message: Message) -> None:
    bindings = await repo.list_project_bindings()
    if not bindings:
        await message.answer("Пока нет ни одной привязки.")
        return
    lines = []
    for b in bindings:
        line = f"• `{b['chatId']}` → «{b.get('projectName', '?')}»"
        if b.get("sectionName"):
            line += f" / раздел «{b['sectionName']}»"
        lines.append(line)
    await message.answer("Текущие привязки:\n" + "\n".join(lines))


@router.message(F.text == BTN_UNBIND)
@router.message(Command("unbind"))
async def unbind(message: Message) -> None:
    if message.chat.type in ("group", "supergroup"):
        actor = message.from_user.id if message.from_user else None
        if not await _is_owner(actor):
            return
    chat_id = _chat_id_for(message)
    if await repo.delete_project_binding(chat_id):
        await message.answer(f"Привязка для `{chat_id}` снята.")
    else:
        await message.answer(f"Для `{chat_id}` привязки не было.")
