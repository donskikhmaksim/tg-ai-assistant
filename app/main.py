"""Entrypoint: start Mongo, the batch scheduler, and bot polling together."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from aiogram.types import MenuButtonWebApp, WebAppInfo

from .audit.poller import run_ticktick_audit_poll
from .backup.mongo_backup import run_mongo_backup
from .config import get_settings
from .db import close_db, init_db
from .pipeline.batch import run_batch
from .pipeline.calendar_events import run_calendar_events_tick
from .pipeline.claims import run_claims_tick
from .pipeline.summary import run_daily_summary
from .pipeline.triage import run_triage_tick
from .pipeline.watchdog import run_watchdog
from .repositories import (
    get_signals_last_run_at,
    init_global_defaults,
    set_signals_last_run_at,
)
from .signals import ingest_telegram_signals
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

    # Signals + triage (ports omi-task-extractor's signals/triage layer —
    # see app/signals.py, app/pipeline/triage.py). A failing tick must NEVER
    # kill the scheduler — every job body below is wrapped, same convention
    # as run_batch/run_watchdog/run_daily_summary already follow.
    async def signals_job() -> None:
        """Fold recent raw_messages into the unified `signals` collection.
        Cursor: bot_state key "signals_last_run_at" (see
        repositories.get/set_signals_last_run_at) — a tick only reprocesses
        chat/day buckets touched since the previous successful run. No
        stored cursor yet (first run) -> look back 24h."""
        try:
            last_run_at = await get_signals_last_run_at()
            since = last_run_at or (datetime.now(timezone.utc) - timedelta(hours=24))
            count = await ingest_telegram_signals(since)
            await set_signals_last_run_at(datetime.now(timezone.utc))
            logger.info("Signals ingest: processed %d signal(s)", count)
        except Exception:  # noqa: BLE001
            logger.exception("signals ingest tick failed")

    async def triage_job() -> None:
        """Score recent `signals` lacking a `triage` field (see
        app/pipeline/triage.py) into score/category/verdict, one batched
        Claude call per tick. Unlike signals_job, this has no cursor of its
        own — get_pending_triage_signals re-scans TRIAGE_LOOKBACK_DAYS of
        signals every tick, filtering on "no triage field / stale
        rubric_version" in the query itself, so re-running never re-scores
        anything already done."""
        try:
            count = await run_triage_tick(settings)
            logger.info("Triage: scored %d signal(s)", count)
        except Exception:  # noqa: BLE001
            logger.exception("triage tick failed")

    async def claims_job() -> None:
        """Turn triaged-but-not-yet-claimed `signals` (verdict "auto"/
        "decision") into `claims` cards (see app/pipeline/claims.py):
        cross-source dedup + one batched Claude call per tick. Runs AFTER
        triage_job in pipeline order — it only ever consumes signals
        triage_job has already scored, never races it (no shared cursor;
        "claimed" is a per-signal flag, same idempotent-tick shape as
        triage_job itself)."""
        try:
            count = await run_claims_tick(settings)
            logger.info("Claims: wrote %d claim(s)", count)
        except Exception:  # noqa: BLE001
            logger.exception("claims tick failed")

    async def calendar_events_job() -> None:
        """Turn triaged-but-not-yet-calendar_extracted `signals` into
        `calendar_events` rows on the isolated "AI Captured" Google
        Calendar (see app/pipeline/calendar_events.py). Extraction + Mongo
        bookkeeping always run; the real Google write is separately gated
        by CALENDAR_EVENTS_ENABLED (see run_calendar_events_tick). Runs
        independently of claims_job — both read the same triaged signals
        pool but write to disjoint collections (claims vs calendar_events)
        with their own `claimed`/`calendar_extracted` flags, so there is no
        ordering dependency between them."""
        try:
            extracted, created = await run_calendar_events_tick(settings)
            logger.info(
                "Calendar events: extracted %d signal(s), created %d event(s) on Google",
                extracted, created,
            )
        except Exception:  # noqa: BLE001
            logger.exception("calendar events tick failed")

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        run_batch,
        "interval",
        minutes=settings.batch_interval_min,
        id="batch",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        signals_job,
        "interval",
        minutes=settings.signals_ingest_interval_minutes,
        id="signals_ingest",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        triage_job,
        "interval",
        minutes=settings.triage_interval_minutes,
        id="triage",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        claims_job,
        "interval",
        minutes=settings.claims_interval_minutes,
        id="claims",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        calendar_events_job,
        "interval",
        minutes=settings.calendar_events_interval_minutes,
        id="calendar_events",
        max_instances=1,
        coalesce=True,
    )
    logger.info(
        "Signals+triage+claims scheduled: ingest every %d min, triage every %d min, "
        "claims every %d min",
        settings.signals_ingest_interval_minutes, settings.triage_interval_minutes,
        settings.claims_interval_minutes,
    )
    calendar_creation_on = bool(
        settings.calendar_events_enabled
        and settings.calendar_mcp_url
        and settings.calendar_target_calendar_id
    )
    logger.info(
        "Calendar events: extraction ON (every %d min), creation %s",
        settings.calendar_events_interval_minutes,
        "ON" if calendar_creation_on else "OFF",
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

    # Scheduled Mongo backup to an S3-compatible bucket (Cloudflare R2
    # recommended). Fail-open / disabled by default: run_mongo_backup no-ops
    # (logged once) unless BACKUP_S3_* is fully configured — never required
    # for a fresh deploy. See app/backup/mongo_backup.py.
    scheduler.add_job(
        run_mongo_backup,
        "cron",
        hour=settings.backup_hour,
        timezone=settings.default_timezone,
        id="mongo_backup",
        max_instances=1,
        coalesce=True,
    )
    logger.info(
        "Mongo backup scheduled: %02d:00 %s (no-op unless BACKUP_S3_* is configured)",
        settings.backup_hour, settings.default_timezone,
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
