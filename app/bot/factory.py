"""Сборка Bot и Dispatcher (§3, §6 ТЗ)."""
from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot.handlers import router
from app.mcp.ticktick import TickTickMCP


def build_bot(token: str) -> Bot:
    return Bot(token=token)


def build_dispatcher(ticktick: TickTickMCP) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp["ticktick"] = ticktick  # доступно хендлерам как kwarg `ticktick`
    dp.include_router(router)
    return dp
