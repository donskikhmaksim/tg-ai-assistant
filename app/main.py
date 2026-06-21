"""Entrypoint: start Mongo, the batch scheduler, and bot polling together."""
from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .config import get_settings
from .db import close_db, init_db
from .pipeline.batch import run_batch
from .telegram.bot import build_bot, build_dispatcher

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

    try:
        # resolve_used_update_types() ensures business_* updates are requested.
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()
        await close_db()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
