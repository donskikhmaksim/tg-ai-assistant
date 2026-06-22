"""Клавиатуры бота: нижнее reply-меню и инлайн-кнопки (§9 ТЗ, Фаза 1)."""
from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

BTN_BIND = "🔗 Привязать проект"
BTN_LIST = "📋 Мои привязки"
BTN_UNBIND = "❌ Отвязать"


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_BIND)],
            [KeyboardButton(text=BTN_LIST), KeyboardButton(text=BTN_UNBIND)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def projects_inline(
    projects: list[dict[str, str]], chat_key: str
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=p["name"], callback_data=f"bind|{chat_key}|{p['id']}"
            )
        ]
        for p in projects
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def bindings_inline(mappings: list[dict]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"❌ {m.get('projectName', '?')} ← {m['chatId']}",
                callback_data=f"unbind|{m['chatId']}",
            )
        ]
        for m in mappings
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)
