"""APScheduler: запуск батч-прогона по расписанию (§3, §7 ТЗ)."""
from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.pipeline.processor import Processor

log = logging.getLogger(__name__)


def create_scheduler(processor: Processor, interval_min: int) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        processor.run_batch,
        trigger="interval",
        minutes=interval_min,
        id="batch",
        max_instances=1,           # прогоны не наслаиваются
        coalesce=True,             # пропущенные тики схлопываются в один
        next_run_time=None,        # первый прогон — по интервалу, не на старте
    )
    log.info("Планировщик настроен: каждые %d мин", interval_min)
    return scheduler
