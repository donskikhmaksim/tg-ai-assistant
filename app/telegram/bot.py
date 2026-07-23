"""Bot / Dispatcher wiring."""
from __future__ import annotations

from aiogram import Bot, Dispatcher

from ..config import get_settings
from . import handlers_messages, handlers_repost, handlers_ui


def build_bot() -> Bot:
    return Bot(token=get_settings().bot_token)


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    # UI router first so commands/menu buttons are matched before the
    # catch-all message capture.
    dp.include_router(handlers_ui.router)
    dp.include_router(handlers_repost.router)
    dp.include_router(handlers_messages.router)
    return dp
