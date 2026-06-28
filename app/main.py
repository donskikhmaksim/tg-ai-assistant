"""Entrypoint: start Mongo, the batch scheduler, and bot polling together."""
from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from aiogram.types import MenuButtonWebApp, WebAppInfo

from .config import get_settings
from .db import close_db, init_db
from .pipeline.batch import run_batch
from .repositories import init_global_defaults
from .telegram.bot import build_bot, build_dispatcher
from .web.server import start_web

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def main() -> None:
    settings = get_settings()
    if not settings.bot_token:
        raise SystemExit("BOT_TOKEN is not set")

    await init_db()
    await init_global_defaults()

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        run_batch,
        "interval",
        minutes=settings.batch_interval_min,
        id="batch",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    logger.info("Batch scheduler started: every %d min", settings.batch_interval_min)

    bot = build_bot()
    dp = build_dispatcher()

    # Phase-2 Mini App: HTTP server alongside polling (binds Railway's $PORT).
    web_runner = await start_web(bot)

    # Point the bot's menu button at the WebApp so the owner opens it in a tap.
    if settings.webapp_url:
        try:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="Проекты",
                    web_app=WebAppInfo(url=settings.webapp_url.rstrip("/") + "/app"),
                )
            )
            logger.info("Menu button set -> %s/app", settings.webapp_url.rstrip("/"))
        except Exception:  # noqa: BLE001
            logger.exception("Failed to set menu button")

    try:
        # resolve_used_update_types() ensures business_* updates are requested.
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        scheduler.shutdown(wait=False)
        await web_runner.cleanup()
        await bot.session.close()
        await close_db()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
