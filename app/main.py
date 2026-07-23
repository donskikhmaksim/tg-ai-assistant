"""Entrypoint: start Mongo, the batch scheduler, and bot polling together."""
from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from aiogram.types import MenuButtonWebApp, WebAppInfo

from .audit.poller import run_ticktick_audit_poll
from .config import get_settings
from .db import close_db, init_db
from .pipeline.batch import run_batch
from .pipeline.summary import run_daily_summary
from .pipeline.watchdog import run_watchdog
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
    if not settings.ticktick_mcp_url:
        logger.warning(
            "TICKTICK_MCP_URL is not set — extracted tasks will be stored locally "
            "but NOT pushed to TickTick. Deploy your own ticktick-mcp and set the URL."
        )
    if settings.default_timezone == "UTC":
        logger.warning(
            "DEFAULT_TIMEZONE is UTC — timed deadlines will be interpreted in UTC, "
            "not your home zone. Set DEFAULT_TIMEZONE to your IANA zone (e.g. "
            "America/Los_Angeles) and keep it EQUAL to ticktick-mcp's USER_TIMEZONE "
            "and your TickTick account zone, or timed deadlines land in the wrong "
            "local time (all-day dates are zone-independent and unaffected). See #36."
        )

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

    # Audit/restore out-of-band poller (Phase 0): read-only delta poll of TickTick
    # to capture hand-edits + collaborator edits into the durable `audit_log`.
    # Fail-open and read-only; no-ops when no TickTick connector is configured.
    # Google pollers (Drive/Gmail/Calendar) are a later phase.
    if settings.audit_enabled:
        scheduler.add_job(
            run_ticktick_audit_poll,
            "interval",
            seconds=settings.audit_poll_interval_seconds,
            id="audit_poll_ticktick",
            max_instances=1,
            coalesce=True,
        )
        logger.info(
            "Audit out-of-band poller (ticktick) scheduled: every %d s",
            settings.audit_poll_interval_seconds,
        )

    bot = build_bot()
    dp = build_dispatcher()

    # Extraction watchdog: probe the chain often so a NEW breakage is caught and
    # DM'd to the owner within minutes; the watchdog itself rate-limits repeats
    # to once/day per error. Added after the bot exists (it needs it to DM);
    # APScheduler accepts jobs post-start().
    if settings.healthcheck_enabled:
        scheduler.add_job(
            run_watchdog,
            "interval",
            minutes=settings.healthcheck_interval_min,
            id="watchdog",
            max_instances=1,
            coalesce=True,
            kwargs={"bot": bot},
        )
        logger.info(
            "Extraction watchdog scheduled: every %d min (daily repeat gated to %02d:00 %s)",
            settings.healthcheck_interval_min, settings.healthcheck_hour, settings.default_timezone,
        )

    # End-of-day group summary: a daily cron at summary_hour in default_timezone
    # posts a short recap into each opted-in group. OFF by default (per-chat /
    # global toggle gates who actually receives one). Needs the bot to post.
    scheduler.add_job(
        run_daily_summary,
        "cron",
        hour=settings.summary_hour,
        timezone=settings.default_timezone,
        id="daily_summary",
        max_instances=1,
        coalesce=True,
        kwargs={"bot": bot},
    )
    logger.info(
        "Daily group summary scheduled: %02d:00 %s (opt-in per chat)",
        settings.summary_hour, settings.default_timezone,
    )

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
