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
from aiogram.filters import (
    ChatMemberUpdatedFilter,
    Command,
    CommandObject,
    CommandStart,
    JOIN_TRANSITION,
)
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

from .. import repositories as repo
from ..config import get_settings
from ..onboarding.invites import create_invite, has_access, redeem_invite
from ..onboarding.notes import create_note
from ..onboarding.ticktick_resolve import get_user_ticktick, set_user_mcp_url
from .notify import group_watch_announcement

logger = logging.getLogger(__name__)

router = Router(name="ui")


class ChatSettings(StatesGroup):
    awaiting_value = State()
    bulk_select = State()


BTN_BIND = "🔗 Привязать проект"
BTN_LIST = "📋 Мои привязки"
BTN_UNBIND = "❌ Отвязать"
BTN_APP = "🗂 Открыть мини-апку"
BTN_SETTINGS = "⚙️ Настройки чата"
BTN_GLOBAL = "🌐 Глобальные"

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
    rows = [
        [KeyboardButton(text=BTN_BIND)],
        [KeyboardButton(text=BTN_LIST), KeyboardButton(text=BTN_UNBIND)],
        [KeyboardButton(text=BTN_SETTINGS), KeyboardButton(text=BTN_GLOBAL)],
    ]
    # The Mini App (Phase 2) — a one-tap WebApp for managing all bindings at once.
    url = get_settings().webapp_url
    if url:
        rows.insert(0, [KeyboardButton(text=BTN_APP, web_app=WebAppInfo(url=url.rstrip("/") + "/app"))])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


# ---------------------------------------------------------------------------
# Ownership: only the Business owner may configure bindings.
# ---------------------------------------------------------------------------

async def _is_owner(user_id: int | None) -> bool:
    """True if the actor is the primary owner. Until the owner is known (Business
    not yet connected) we don't block, so first-time setup isn't locked out."""
    owner = await repo.get_bot_state("owner_id")
    if owner is None:
        return True
    return user_id is not None and int(owner) == int(user_id)


async def _is_tenant(user_id: int | None) -> bool:
    """True if the actor is a tenant of this bot: the primary owner, or a user
    who has connected their own Business account. Before any owner is known we
    don't block (bootstrap). Non-tenants get the onboarding invite instead of
    the management menu."""
    if await _is_owner(user_id):
        return True
    if user_id is None:
        return False
    return await repo.get_owner_connection_count(str(user_id)) > 0


async def _is_chat_owner(user_id: int | None, chat_key: str) -> bool:
    """True if the actor owns THIS chat (its tasks route to their TickTick).
    Groups (and legacy chats) fall back to the primary owner."""
    if user_id is None:
        return False
    owner = await repo.resolve_chat_owner(chat_key)
    if owner is None:
        return True  # no owner known yet — allow bootstrap
    return str(owner) == str(user_id)


# ---------------------------------------------------------------------------
# TickTick lookups (best-effort — never raise into a handler).
# ---------------------------------------------------------------------------

async def _tt(user_id: int | None):
    """The actor's own TickTick client (multi-tenant), or None if not connected."""
    return await get_user_ticktick(str(user_id)) if user_id is not None else None


async def _safe_projects(user_id: int | None) -> list[dict[str, str]]:
    tt = await _tt(user_id)
    if tt is None:
        return []
    try:
        return await tt.get_projects()
    except Exception:  # noqa: BLE001
        logger.exception("get_projects failed")
        return []


async def _safe_sections(user_id: int | None, project_id: str) -> list[dict[str, str]]:
    tt = await _tt(user_id)
    if tt is None:
        return []
    try:
        return await tt.get_sections(project_id)
    except Exception:  # noqa: BLE001
        logger.exception("get_sections failed")
        return []


async def _project_name(user_id: int | None, project_id: str) -> str:
    for p in await _safe_projects(user_id):
        if p["id"] == project_id:
            return p["name"]
    return project_id


async def _section_name(user_id: int | None, project_id: str, section_id: str) -> str:
    for s in await _safe_sections(user_id, project_id):
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


async def _send_project_picker(bot: Bot, chat_id: int, user_id: int | None) -> None:
    """Post the project picker into the given chat (group or DM), using the
    actor's own TickTick projects."""
    projects = await _safe_projects(user_id)
    if not projects:
        await bot.send_message(
            chat_id,
            "Не вижу проектов в твоём TickTick. Подключи свой коннектор командой "
            "/connect <url>, потом нажми /bind.",
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
    actor = event.from_user.id if event.from_user else None
    await _send_project_picker(bot, chat.id, actor)


# ---------------------------------------------------------------------------
# Explicit /bind (group or DM) and the DM reply-menu button.
# ---------------------------------------------------------------------------

@router.message(Command("bind"))
@router.message(F.text == BTN_BIND)
async def start_bind(message: Message) -> None:
    actor = message.from_user.id if message.from_user else None
    if message.chat.type in ("group", "supergroup"):
        if not await _is_chat_owner(actor, f"group_{message.chat.id}"):
            return  # only the group's owner may bind it
    await _send_project_picker(message.bot, message.chat.id, actor)


@router.callback_query(F.data.startswith("pp:"))
async def on_pick_project(callback: CallbackQuery) -> None:
    if not callback.data:
        return
    chat_key = _chat_key_from_callback(callback)
    if chat_key is None:
        await callback.answer("Не понял чат.", show_alert=True)
        return
    if not await _is_chat_owner(callback.from_user.id, chat_key):
        await callback.answer("Выбирать проект может только владелец чата.", show_alert=True)
        return

    project_id = callback.data.split(":", 1)[1]
    actor = callback.from_user.id
    project_name = await _project_name(actor, project_id)
    sections = await _safe_sections(actor, project_id)

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
    chat_key = _chat_key_from_callback(callback)
    if chat_key is None:
        await callback.answer("Не понял чат.", show_alert=True)
        return
    if not await _is_chat_owner(callback.from_user.id, chat_key):
        await callback.answer("Выбирать раздел может только владелец чата.", show_alert=True)
        return

    _, project_id, section_id = callback.data.split(":", 2)
    actor = callback.from_user.id
    project_name = await _project_name(actor, project_id)

    if section_id == "none":
        await repo.set_project_binding(chat_key, project_id, project_name)
        await callback.answer("Привязано ✅")
        await _confirm_bind(callback, chat_key, project_name)
        return

    section_name = await _section_name(actor, project_id, section_id)
    await repo.set_project_binding(chat_key, project_id, project_name, section_id, section_name)
    await callback.answer("Привязано ✅")
    await _confirm_bind(callback, chat_key, project_name, section_name)


# ---------------------------------------------------------------------------
# Misc commands / menu.
# ---------------------------------------------------------------------------

@router.message(Command("connect"))
async def cmd_connect(message: Message) -> None:
    """Register the CALLER's own ticktick-mcp connector URL (multi-tenant).

    The URL is itself the credential, so we best-effort delete the command
    message after storing it, and verify the connector actually answers.
    """
    uid = message.from_user.id if message.from_user else None
    if uid is None:
        return
    parts = (message.text or "").split(maxsplit=1)
    url = parts[1].strip() if len(parts) > 1 else ""
    if not url or "/mcp/" not in url:
        await message.answer(
            "Пришли свой личный адрес ticktick-mcp так:\n"
            "`/connect https://<твой>.up.railway.app/mcp/<секрет>`\n\n"
            "Это твой персональный коннектор — задачи полетят в ТВОЙ TickTick, "
            "ни в чей другой.",
            parse_mode="Markdown",
        )
        return

    await set_user_mcp_url(str(uid), url)
    # The URL is a secret — don't leave it sitting in the chat.
    try:
        await message.delete()
    except Exception:  # noqa: BLE001
        logger.debug("could not delete /connect message", exc_info=True)

    ok = False
    try:
        tt = await get_user_ticktick(str(uid))
        if tt is not None:
            await tt.get_projects()
            ok = True
    except Exception:  # noqa: BLE001
        logger.exception("connect verify failed for user %s", uid)

    if ok:
        await message.answer("✅ Твой TickTick подключён. Теперь можно /bind.")
    else:
        await message.answer(
            "⚠️ Сохранил адрес, но проверка не прошла — коннектор недоступен или "
            "URL неверный. Проверь и пришли /connect ещё раз."
        )


# Connector onboarding. Invite-gated: the owner mints a one-time invite
# (/invite), which arrives as a self-destruct note holding a `?start=inv_<token>`
# deep link. Opening it grants onboarding access; only then do the per-service
# buttons hand out the install command (each in its own 5-minute self-destruct
# note, so the owner's shared secrets never sit in Telegram history).
NOTE_TTL_SECONDS = 300  # share links live 5 minutes


def _onboarding_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Подключить Google", callback_data="onb:google")],
            [InlineKeyboardButton(text="🔗 Подключить TickTick", callback_data="onb:ticktick")],
        ]
    )


def _service_command(service: str, s) -> tuple[str, str] | None:
    """(human title, paste-ready install command) for one service, or None when
    the owner has not configured that service's shared secrets."""
    if service == "ticktick":
        if not (s.onboarding_ticktick_client_id and s.onboarding_ticktick_client_secret):
            return None
        cmd = (
            f"bash <(curl -fsSL {s.onboarding_ticktick_setup_url}) "
            f"--client-id {s.onboarding_ticktick_client_id} "
            f"--client-secret {s.onboarding_ticktick_client_secret}"
        )
        return ("TickTick", cmd)
    if service == "google":
        if not (
            s.onboarding_google_client_id
            and s.onboarding_google_client_secret
            and s.onboarding_relay_secret
        ):
            return None
        cmd = (
            f"bash <(curl -fsSL {s.onboarding_google_setup_url}) "
            f"--client-id {s.onboarding_google_client_id} "
            f"--client-secret {s.onboarding_google_client_secret} "
            f"--relay-secret {s.onboarding_relay_secret}"
        )
        return ("Google (Gmail / Drive / Docs / Sheets / Calendar)", cmd)
    return None


async def _has_onboarding_access(uid: int | None) -> bool:
    if uid is None:
        return False
    return await _is_owner(uid) or await has_access(str(uid))


@router.message(Command("invite"))
async def cmd_invite(message: Message, bot: Bot) -> None:
    """Owner-only: mint a one-time invite delivered as a self-destruct note that
    carries a deep link into this bot."""
    uid = message.from_user.id if message.from_user else None
    if not await _is_owner(uid):
        return  # silent for non-owners
    s = get_settings()
    if not s.notes_base_url:
        await message.answer("Не задан NOTES_BASE_URL — приглашения недоступны.")
        return
    token = await create_invite()
    me = await bot.me()
    deep_link = f"https://t.me/{me.username}?start=inv_{token}"
    note_text = (
        "Тебя пригласили подключить свои сервисы (TickTick / Google) к своему "
        "Claude через бота. Открой ссылку в Telegram и нажми Start:\n\n"
        f"{deep_link}\n\n"
        "Дальше бот покажет кнопки — жми и следуй подсказкам."
    )
    try:
        link = await create_note(
            s.notes_base_url, note_text, ttl_seconds=NOTE_TTL_SECONDS, one_view=True
        )
    except Exception:  # noqa: BLE001
        logger.exception("invite note failed")
        await message.answer("⚠️ Не смог создать приглашение. Попробуй ещё раз.")
        return
    await message.answer(
        "🎟 Одноразовое приглашение готово. Перешли ЭТУ ссылку человеку — она "
        "живёт 5 минут и откроется один раз:\n\n"
        f"{link}",
        disable_web_page_preview=True,
    )


@router.message(Command("setup"))
async def cmd_setup(message: Message) -> None:
    """Show the connector-onboarding buttons — only to invited users / the owner."""
    uid = message.from_user.id if message.from_user else None
    if not await _has_onboarding_access(uid):
        await message.answer(
            "Чтобы подключать сервисы, нужно приглашение от владельца бота."
        )
        return
    await message.answer(
        "Выбери, что подключить к своему Claude:", reply_markup=_onboarding_menu()
    )


@router.callback_query(F.data.in_({"onb:google", "onb:ticktick"}))
async def on_onboarding_pick(cb: CallbackQuery) -> None:
    """Hand out one service's install command as a fresh 5-minute self-destruct note."""
    uid = cb.from_user.id if cb.from_user else None
    if not await _has_onboarding_access(uid):
        await cb.answer("Нужно приглашение от владельца.", show_alert=True)
        return
    s = get_settings()
    if not s.notes_base_url:
        await cb.answer("Онбординг не настроен владельцем.", show_alert=True)
        return
    service = "google" if cb.data == "onb:google" else "ticktick"
    built = _service_command(service, s)
    if built is None:
        await cb.answer("Этот коннектор не настроен владельцем.", show_alert=True)
        return
    title, cmd = built
    note_text = (
        f"Команда установки — {title}. Открой Терминал (Mac: Cmd+Space → Terminal; "
        "Windows: WSL или Git Bash) и вставь — она развернёт твой личный сервер на "
        "ТВОЁМ Railway и подключит к ТВОЕМУ Claude:\n\n"
        f"{cmd}\n\n"
        "Скрипт сам поставит Railway CLI, залогинит и всё развернёт — дальше следуй "
        "подсказкам в терминале."
    )
    try:
        link = await create_note(
            s.notes_base_url, note_text, ttl_seconds=NOTE_TTL_SECONDS, one_view=True
        )
    except Exception:  # noqa: BLE001
        logger.exception("create_note failed for onboarding pick %s", service)
        await cb.answer("Не смог создать ссылку, попробуй ещё раз.", show_alert=True)
        return
    await cb.answer()
    await cb.message.answer(
        f"🔐 {title} — ссылка на команду (живёт 5 минут, откроется один раз):\n\n"
        f"{link}\n\n"
        "Открой её на компьютере, где будешь ставить, и скопируй команду.",
        disable_web_page_preview=True,
    )


@router.message(Command("app"))
async def cmd_app(message: Message) -> None:
    url = get_settings().webapp_url
    if not url:
        await message.answer("Мини-апка ещё не настроена (нет WEBAPP_URL).")
        return
    await message.answer("Открыть управление привязками:", reply_markup=_main_menu())


def _deploy_prompt(repo_url: str) -> str:
    """A paste-ready Claude Code prompt that deploys a fully-isolated instance.

    Everything the new person creates is THEIRS: their own bot, their own
    MongoDB, their own ticktick-mcp (so tasks go to THEIR TickTick), their own
    Anthropic key. Nothing points back at the original owner.
    """
    repo = repo_url or "the tg-ai-assistant repo"
    return (
        "Please help me deploy my OWN private instance of tg-ai-assistant on "
        "Railway. Everything must be mine — I want the original author to have "
        "no access to my data.\n\n"
        "Prerequisites (ask me for each; never invent secrets):\n"
        "- A Telegram bot token from @BotFather (I will create a new bot).\n"
        "- An Anthropic API key.\n"
        "- My OWN ticktick-mcp instance. If I don't have one yet, first deploy "
        "  it from its repo (see its README/ONBOARDING) so I get MY OWN "
        "  TICKTICK_MCP_URL bound to MY OWN TickTick account. Never reuse a URL "
        "  someone shared with me.\n\n"
        "Steps:\n"
        f"1. Fork/clone {repo} into MY account, then `cd tg-ai-assistant`.\n"
        "2. `npm i -g @railway/cli` (if missing), then `railway login`.\n"
        "3. `railway init` — a new project under MY Railway account.\n"
        "4. `railway add --database mongo` — provision MY own MongoDB.\n"
        "5. Set env vars (ask me for each value):\n"
        "   BOT_TOKEN, ANTHROPIC_API_KEY, TICKTICK_MCP_URL (my own),\n"
        "   MONGO_URL=${{Mongo.MONGO_URL}} (wire it to the Railway Mongo plugin),\n"
        "   DEFAULT_TIMEZONE (my IANA zone), and WEBAPP_URL once I have the domain.\n"
        "6. `railway up` — deploy.\n"
        "7. In @BotFather enable Business/Secretary Mode + turn Group Privacy off, "
        "   then connect the bot to my account via Telegram Business.\n"
        "8. Send /start to my bot to confirm it's online.\n\n"
        "Ask me for each value you don't know. Do not invent secrets."
    )


@router.message(CommandStart())
async def cmd_start(
    message: Message, state: FSMContext, command: CommandObject
) -> None:
    s = get_settings()
    uid = message.from_user.id if message.from_user else None

    # Invite deep link: `?start=inv_<token>`. Redeeming grants onboarding access
    # and drops the person straight onto the connector buttons.
    payload = (command.args or "").strip()
    if payload.startswith("inv_"):
        if uid is not None and await redeem_invite(payload[4:], str(uid)):
            await message.answer(
                "✅ Приглашение принято! Выбери, что подключить к своему Claude:",
                reply_markup=_onboarding_menu(),
            )
        else:
            await message.answer(
                "Это приглашение недействительно или уже использовано. "
                "Попроси у владельца новое."
            )
        return

    if await _is_tenant(uid):
        # A tenant: primary owner, or someone who connected their own Business
        # account. Both manage only their own chats. Nudge /connect if they
        # haven't linked their own TickTick yet.
        has_tt = uid is not None and await get_user_ticktick(str(uid)) is not None
        tail = (
            "Через меню ниже можно привязать этот чат к проекту."
            if has_tt else
            "Сначала подключи свой TickTick: пришли `/connect <адрес твоего "
            "ticktick-mcp>`. После этого задачи полетят в ТВОЙ аккаунт, ни в чей "
            "другой."
        )
        await message.answer(
            "👁 Большой Брат на связи.\n\n"
            "Я извлекаю задачи и договорённости из переписки — лички и групп — и "
            "завожу их в TickTick. Уведомлений не шлю: задачи просто появляются. "
            "Я всё вижу.\n\n" + tail,
            reply_markup=_main_menu(),
            parse_mode="Markdown",
        )
        return

    # Never connected Business: offer to deploy their OWN fully-isolated instance
    # (alternatively they can add this bot to their Business account, then
    # /connect their TickTick — but self-hosting keeps their data fully private).
    # The repo is public — no GitHub access, no collaborator invites, no shared
    # infra. Their bot, their MongoDB, their ticktick-mcp, their data.
    lines = [
        "👋 Привет! Хочешь такого же бота — себе?\n",
        "Он развернётся полностью у тебя: свой бот, своя база, свой TickTick. "
        "Твои переписки остаются только у тебя — я их не вижу.\n",
    ]
    buttons: list[list[InlineKeyboardButton]] = []
    if s.onboarding_railway_template_url:
        buttons.append([InlineKeyboardButton(
            text="🚀 Развернуть на Railway", url=s.onboarding_railway_template_url)])
    if s.onboarding_repo_url:
        buttons.append([InlineKeyboardButton(
            text="📦 Репозиторий и инструкция", url=s.onboarding_repo_url)])

    if s.onboarding_repo_url or s.onboarding_railway_template_url:
        lines.append(
            "Проще всего — открой *Claude Code* и вставь этот промпт, он проведёт "
            "тебя по всем шагам:"
        )
        prompt = _deploy_prompt(s.onboarding_repo_url)
        lines.append(f"```\n{prompt}\n```")
    else:
        lines.append("Онбординг пока не настроен владельцем — напиши ему напрямую.")

    await message.answer(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None,
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
    chat_id = _chat_id_for(message)
    if message.chat.type in ("group", "supergroup"):
        actor = message.from_user.id if message.from_user else None
        if not await _is_chat_owner(actor, chat_id):
            return
    if await repo.delete_project_binding(chat_id):
        await message.answer(f"Привязка для `{chat_id}` снята.")
    else:
        await message.answer(f"Для `{chat_id}` привязки не было.")


# ---------------------------------------------------------------------------
# Chat settings UI
# ---------------------------------------------------------------------------

# Field metadata: label, placeholder, emoji
_CS_FIELDS: dict[str, tuple[str, str, str]] = {
    "who": (
        "Кто этот человек / о чём чат",
        "Напр.: деловой партнёр, вместе ведём автопарк в Лос-Анджелесе",
        "👤",
    ),
    "topics": (
        "Что обычно обсуждаете",
        "Напр.: закупки, платежи, вэны, ремонт, планы поездок",
        "💬",
    ),
    "task_side": (
        "Кому ставятся задачи",
        "Напр.: задачи в основном мне, он больше информирует и напоминает",
        "🎯",
    ),
    "filter_rules": (
        "Как понять, задача это или нет",
        "Напр.: любые упоминания денег и дат — это задача, даже если сказано вскользь. Не считать задачей просто вопросы без обязательства",
        "🔍",
    ),
    "extract_rules": (
        "Как именно вытаскивать задачи",
        "Напр.: всегда фиксируй сумму если упомянута; дедлайн считай жёстким даже если «наверное»; задачи на меня помечай who=me",
        "✍️",
    ),
    "importance": (
        "Когда задача важная, а когда нет",
        "Напр.: важны только задачи с деньгами или дедлайном; бытовые «надо бы» — не важны",
        "⭐",
    ),
    "people": (
        "Кто ещё участвует (имена, роли)",
        "Напр.: Коля — прораб, Артём — водитель, Оскар — финансы",
        "👥",
    ),
}


async def _owner_query(actor_id: int | None) -> dict:
    """Mongo filter on chat_state restricting to the actor's own chats (tenant
    isolation). The primary owner also sees legacy/unowned chats so nothing
    disappears during the multi-tenant transition."""
    if actor_id is None:
        return {}
    primary = await repo.get_bot_state("owner_id")
    if primary is not None and int(primary) == int(actor_id):
        return {"$or": [{"ownerId": str(actor_id)}, {"ownerId": {"$in": [None]}},
                        {"ownerId": {"$exists": False}}]}
    return {"ownerId": str(actor_id)}


async def _cs_chats_keyboard(actor_id: int | None) -> InlineKeyboardMarkup:
    """Top 20 of the actor's own chats sorted by last activity."""
    from ..db import get_db
    db = get_db()
    q = await _owner_query(actor_id)
    cursor = db.chat_state.find(q, {"chatId": 1, "title": 1}).sort("lastMessageAt", -1).limit(20)
    chats = [d async for d in cursor]
    rows = [
        [InlineKeyboardButton(
            text=d.get("title") or d["chatId"],
            callback_data=f"cs_chat:{d['chatId']}",
        )]
        for d in chats
    ]
    if not rows:
        rows = [[InlineKeyboardButton(text="(нет чатов)", callback_data="cs_noop")]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _cs_card_text(chat_id: str, title: str, settings_doc: dict) -> str:
    if chat_id == "__global__":
        header = "🌐 Глобальные настройки (применяются ко всем чатам как база)\n"
    else:
        header = f"⚙️ Настройки · {title}\n"
    lines = [header]
    for field, (label, _, emoji) in _CS_FIELDS.items():
        value = settings_doc.get(field)
        display = f"→ {value}" if value else "→ не задано"
        lines.append(f"{emoji} {label}\n{display}")
    return "\n\n".join(lines)


def _cs_card_keyboard(chat_id: str, settings_doc: dict) -> InlineKeyboardMarkup:
    rows = []
    for field, (label, _, emoji) in _CS_FIELDS.items():
        value = settings_doc.get(field)
        btn_row = [InlineKeyboardButton(text=f"{emoji} ✏️", callback_data=f"cs_edit:{chat_id}:{field}")]
        if value:
            btn_row.append(InlineKeyboardButton(text="🔄", callback_data=f"cs_reset:{chat_id}:{field}"))
        rows.append(btn_row)
    rows.append([InlineKeyboardButton(text="📤 Применить к нескольким чатам", callback_data=f"cs_bulk:{chat_id}")])
    back_cb = "cs_back_global" if chat_id == "__global__" else "cs_back_chats"
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=back_cb)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(F.text == BTN_SETTINGS)
async def cmd_settings(message: Message) -> None:
    actor = message.from_user.id if message.from_user else None
    if not await _is_tenant(actor):
        return
    kb = await _cs_chats_keyboard(actor)
    await message.answer("Выбери чат для настройки:", reply_markup=kb)


@router.callback_query(F.data == "cs_noop")
async def cs_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(F.data == "cs_back_chats")
async def cs_back_chats(callback: CallbackQuery) -> None:
    await callback.answer()
    kb = await _cs_chats_keyboard(callback.from_user.id)
    msg = callback.message
    if isinstance(msg, Message):
        await msg.edit_text("Выбери чат для настройки:", reply_markup=kb)


@router.callback_query(F.data.startswith("cs_chat:"))
async def cs_pick_chat(callback: CallbackQuery) -> None:
    await callback.answer()
    if not callback.data:
        return
    chat_id = callback.data.split(":", 1)[1]
    settings_doc = await repo.get_chat_settings(chat_id)
    title = await repo.get_chat_title(chat_id)
    text = _cs_card_text(chat_id, title, settings_doc)
    kb = _cs_card_keyboard(chat_id, settings_doc)
    msg = callback.message
    if isinstance(msg, Message):
        await msg.edit_text(text, reply_markup=kb)


@router.callback_query(F.data.startswith("cs_edit:"))
async def cs_edit_field(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if not callback.data:
        return
    _, chat_id, field = callback.data.split(":", 2)
    meta = _CS_FIELDS.get(field)
    if not meta:
        return
    label, placeholder, emoji = meta
    settings_doc = await repo.get_chat_settings(chat_id)
    current = settings_doc.get(field)

    await state.set_state(ChatSettings.awaiting_value)
    await state.update_data(cs_chat_id=chat_id, cs_field=field)

    current_line = f"Текущее: {current}" if current else "Текущее: не задано"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"cs_cancel:{chat_id}")]
    ])
    msg = callback.message
    if isinstance(msg, Message):
        await msg.answer(
            f"{emoji} {label}\n\n"
            f"{current_line}\n\n"
            f"{placeholder}\n\n"
            "Отправь новое значение:",
            reply_markup=kb,
        )


@router.message(ChatSettings.awaiting_value)
async def cs_save_value(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    chat_id = data.get("cs_chat_id", "")
    field = data.get("cs_field", "")
    new_value = (message.text or "").strip()
    if not new_value:
        await message.answer("Значение не может быть пустым. Попробуй ещё раз:")
        return
    await repo.update_chat_settings(chat_id, {field: new_value})
    await state.clear()

    settings_doc = await repo.get_chat_settings(chat_id)
    title = await repo.get_chat_title(chat_id)
    text = _cs_card_text(chat_id, title, settings_doc)
    kb = _cs_card_keyboard(chat_id, settings_doc)
    await message.answer("✅ Сохранено")
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("cs_cancel:"))
async def cs_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    if not callback.data:
        return
    chat_id = callback.data.split(":", 1)[1]
    settings_doc = await repo.get_chat_settings(chat_id)
    title = await repo.get_chat_title(chat_id)
    text = _cs_card_text(chat_id, title, settings_doc)
    kb = _cs_card_keyboard(chat_id, settings_doc)
    msg = callback.message
    if isinstance(msg, Message):
        await msg.edit_text(text, reply_markup=kb)


@router.callback_query(F.data.startswith("cs_reset:"))
async def cs_reset_field(callback: CallbackQuery) -> None:
    await callback.answer()
    if not callback.data:
        return
    _, chat_id, field = callback.data.split(":", 2)
    await repo.clear_chat_settings_field(chat_id, field)
    settings_doc = await repo.get_chat_settings(chat_id)
    title = await repo.get_chat_title(chat_id)
    text = _cs_card_text(chat_id, title, settings_doc)
    kb = _cs_card_keyboard(chat_id, settings_doc)
    msg = callback.message
    if isinstance(msg, Message):
        await msg.edit_text(text, reply_markup=kb)


# ---------------------------------------------------------------------------
# Global settings
# ---------------------------------------------------------------------------

@router.message(F.text == BTN_GLOBAL)
async def cmd_global_settings(message: Message) -> None:
    if not await _is_tenant(message.from_user.id if message.from_user else None):
        return
    settings_doc = await repo.get_global_settings()
    text = _cs_card_text("__global__", "", settings_doc)
    kb = _cs_card_keyboard("__global__", settings_doc)
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "cs_back_global")
async def cs_back_global(callback: CallbackQuery) -> None:
    await callback.answer()
    settings_doc = await repo.get_global_settings()
    text = _cs_card_text("__global__", "", settings_doc)
    kb = _cs_card_keyboard("__global__", settings_doc)
    msg = callback.message
    if isinstance(msg, Message):
        await msg.edit_text(text, reply_markup=kb)


# ---------------------------------------------------------------------------
# Bulk Apply
# ---------------------------------------------------------------------------

async def _bulk_chats_keyboard(
    source_chat_id: str, selected: list[str], actor_id: int | None
) -> InlineKeyboardMarkup:
    """Build the bulk-select keyboard with checkboxes (actor's own chats only)."""
    from ..db import get_db as _get_db
    db = _get_db()
    q = await _owner_query(actor_id)
    cursor = db.chat_state.find(q, {"chatId": 1, "title": 1}).sort("lastMessageAt", -1).limit(50)
    chats = [d async for d in cursor]

    rows = []
    for d in chats:
        cid = d["chatId"]
        title = d.get("title") or cid
        chat_type = "группа" if cid.startswith("group_") else "личка"
        check = "☑️" if cid in selected else "☐"
        rows.append([InlineKeyboardButton(
            text=f"{check} {title}   {chat_type}",
            callback_data=f"cs_toggle:{source_chat_id}:{cid}",
        )])

    rows.append([
        InlineKeyboardButton(text="✅ Все", callback_data=f"cs_select_all:{source_chat_id}"),
        InlineKeyboardButton(text="👤 Лички", callback_data=f"cs_select_dms:{source_chat_id}"),
        InlineKeyboardButton(text="👥 Группы", callback_data=f"cs_select_groups:{source_chat_id}"),
        InlineKeyboardButton(text="❌ Очистить", callback_data=f"cs_clear_sel:{source_chat_id}"),
    ])
    rows.append([InlineKeyboardButton(text="📤 Применить к выбранным", callback_data=f"cs_apply_bulk:{source_chat_id}")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"cs_chat:{source_chat_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _all_chat_ids(actor_id: int | None) -> list[str]:
    from ..db import get_db as _get_db
    db = _get_db()
    q = await _owner_query(actor_id)
    cursor = db.chat_state.find(q, {"chatId": 1})
    return [d["chatId"] async for d in cursor]


@router.callback_query(F.data.startswith("cs_bulk:"))
async def cs_bulk_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if not callback.data:
        return
    source_chat_id = callback.data.split(":", 1)[1]
    await state.set_state(ChatSettings.bulk_select)
    await state.update_data(source_chat_id=source_chat_id, selected=[])

    source_title = (
        "Глобальные" if source_chat_id == "__global__"
        else await repo.get_chat_title(source_chat_id)
    )
    kb = await _bulk_chats_keyboard(source_chat_id, [], callback.from_user.id)
    msg = callback.message
    if isinstance(msg, Message):
        await msg.edit_text(
            f"Выбери чаты для применения настроек из «{source_title}»:",
            reply_markup=kb,
        )


@router.callback_query(F.data.startswith("cs_toggle:"))
async def cs_toggle(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if not callback.data:
        return
    _, source_chat_id, target_chat_id = callback.data.split(":", 2)
    data = await state.get_data()
    selected: list[str] = list(data.get("selected", []))
    if target_chat_id in selected:
        selected.remove(target_chat_id)
    else:
        selected.append(target_chat_id)
    await state.update_data(selected=selected)
    kb = await _bulk_chats_keyboard(source_chat_id, selected, callback.from_user.id)
    msg = callback.message
    if isinstance(msg, Message):
        try:
            await msg.edit_reply_markup(reply_markup=kb)
        except Exception:
            logger.debug("edit_reply_markup failed", exc_info=True)


@router.callback_query(F.data.startswith("cs_select_all:"))
async def cs_select_all(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if not callback.data:
        return
    source_chat_id = callback.data.split(":", 1)[1]
    all_ids = await _all_chat_ids(callback.from_user.id)
    await state.update_data(selected=all_ids)
    kb = await _bulk_chats_keyboard(source_chat_id, all_ids, callback.from_user.id)
    msg = callback.message
    if isinstance(msg, Message):
        try:
            await msg.edit_reply_markup(reply_markup=kb)
        except Exception:
            logger.debug("edit_reply_markup failed", exc_info=True)


@router.callback_query(F.data.startswith("cs_select_dms:"))
async def cs_select_dms(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if not callback.data:
        return
    source_chat_id = callback.data.split(":", 1)[1]
    all_ids = await _all_chat_ids(callback.from_user.id)
    dms = [cid for cid in all_ids if cid.startswith("user_")]
    data = await state.get_data()
    selected = list(set(list(data.get("selected", [])) + dms))
    await state.update_data(selected=selected)
    kb = await _bulk_chats_keyboard(source_chat_id, selected, callback.from_user.id)
    msg = callback.message
    if isinstance(msg, Message):
        try:
            await msg.edit_reply_markup(reply_markup=kb)
        except Exception:
            logger.debug("edit_reply_markup failed", exc_info=True)


@router.callback_query(F.data.startswith("cs_select_groups:"))
async def cs_select_groups(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if not callback.data:
        return
    source_chat_id = callback.data.split(":", 1)[1]
    all_ids = await _all_chat_ids(callback.from_user.id)
    groups = [cid for cid in all_ids if cid.startswith("group_")]
    data = await state.get_data()
    selected = list(set(list(data.get("selected", [])) + groups))
    await state.update_data(selected=selected)
    kb = await _bulk_chats_keyboard(source_chat_id, selected, callback.from_user.id)
    msg = callback.message
    if isinstance(msg, Message):
        try:
            await msg.edit_reply_markup(reply_markup=kb)
        except Exception:
            logger.debug("edit_reply_markup failed", exc_info=True)


@router.callback_query(F.data.startswith("cs_clear_sel:"))
async def cs_clear_sel(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if not callback.data:
        return
    source_chat_id = callback.data.split(":", 1)[1]
    await state.update_data(selected=[])
    kb = await _bulk_chats_keyboard(source_chat_id, [], callback.from_user.id)
    msg = callback.message
    if isinstance(msg, Message):
        try:
            await msg.edit_reply_markup(reply_markup=kb)
        except Exception:
            logger.debug("edit_reply_markup failed", exc_info=True)


@router.callback_query(F.data.startswith("cs_apply_bulk:"))
async def cs_apply_bulk(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if not callback.data:
        return
    source_chat_id = callback.data.split(":", 1)[1]
    data = await state.get_data()
    selected: list[str] = data.get("selected", [])
    if not selected:
        await callback.answer("Не выбрано ни одного чата.", show_alert=True)
        return

    # Copy settings from source to all selected chats
    source_doc = (
        await repo.get_global_settings()
        if source_chat_id == "__global__"
        else await repo.get_chat_settings(source_chat_id)
    )
    # Only copy the 7 known fields
    fields_to_copy = {
        k: v for k, v in source_doc.items()
        if k in _CS_FIELDS and v
    }
    for target_id in selected:
        if target_id == source_chat_id:
            continue
        if fields_to_copy:
            await repo.update_chat_settings(target_id, fields_to_copy)

    await state.clear()
    msg = callback.message
    if isinstance(msg, Message):
        await msg.edit_text(f"✅ Применено к {len(selected)} чатам.")
