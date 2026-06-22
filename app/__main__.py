"""Точка входа: бот (polling) + батч-планировщик в одном процессе (§3, §4 ТЗ)."""
from __future__ import annotations

import asyncio
import logging

from app.bot.factory import build_bot, build_dispatcher
from app.config import get_settings
from app.db import mongo
from app.llm.claude import ClaudeExtractor
from app.llm.qwen import QwenTriage
from app.logging_setup import setup_logging
from app.mcp.ticktick import TickTickMCP
from app.pipeline.processor import Processor
from app.pipeline.scheduler import create_scheduler

log = logging.getLogger(__name__)


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)

    await mongo.connect(settings.mongo_url, settings.mongo_db, settings.raw_ttl_days)

    qwen = QwenTriage(settings.qwen_base_url, settings.qwen_model, settings.qwen_api_key)
    claude = ClaudeExtractor(settings.anthropic_api_key, settings.anthropic_model)
    ticktick = TickTickMCP(
        settings.ticktick_mcp_url,
        settings.ticktick_mcp_transport,
        settings.ticktick_mcp_auth_token,
    )

    processor = Processor(settings, qwen, claude, ticktick)
    scheduler = create_scheduler(processor, settings.batch_interval_min)

    bot = build_bot(settings.bot_token)
    dp = build_dispatcher(ticktick)

    scheduler.start()
    log.info("Бот запускается (polling)…")
    try:
        await dp.start_polling(
            bot, allowed_updates=dp.resolve_used_update_types()
        )
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()
        await mongo.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Остановка")
