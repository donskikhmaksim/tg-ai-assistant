"""End-of-day group summary.

Once a day (summary_hour in default_timezone) posts a short Russian recap INTO
each group chat that has opted in — what the bot did that day for that chat:
tasks it created and tasks it completed/updated. OFF by default; a chat is
included only when `daily_summary` resolves to "on" (per-chat override, else
global, else the env default) AND there was activity that day (empty days are
skipped, never spammed).

Privacy: the message is posted into the GROUP, visible to its members, so it
only ever contains that group's own task titles — the same titles the bot
already surfaced. Personal (DM) chats are never summarized here.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot

from .. import repositories as repo
from ..config import get_settings

logger = logging.getLogger(__name__)

# Cap the bulleted list of created titles so a busy day stays readable.
MAX_LIST = 5


def _plural_tasks(n: int) -> str:
    """Russian plural for «задача» (1 задача, 2 задачи, 5 задач)."""
    if 11 <= (n % 100) <= 14:
        return "задач"
    tail = n % 10
    if tail == 1:
        return "задача"
    if 2 <= tail <= 4:
        return "задачи"
    return "задач"


def should_send_summary(mode: str, created_count: int, closed_count: int) -> bool:
    """Whether a chat gets a summary: opted in AND had activity that day."""
    return mode == "on" and (created_count + closed_count) > 0


def compose_summary(
    date_label: str,
    created_titles: list[str],
    closed_count: int,
    max_list: int = MAX_LIST,
) -> str | None:
    """Build the summary text, or None when there's nothing to report.

    `date_label` is the local date (e.g. "20.07.2026"). `created_titles` are the
    titles of tasks created that day; `closed_count` how many were
    completed/updated. Returns None for an empty day so the caller skips it.
    """
    created_count = len(created_titles)
    if created_count == 0 and closed_count == 0:
        return None
    header = (
        f"📋 Итог дня ({date_label}): создано {created_count} "
        f"{_plural_tasks(created_count)}, обновлено {closed_count}."
    )
    lines = [header]
    shown = [t.strip() for t in created_titles if t.strip()][:max_list]
    if shown:
        lines.append("")
        lines.extend(f"• {t}" for t in shown)
        extra = created_count - len(shown)
        if extra > 0:
            lines.append(f"…и ещё {extra}")
    return "\n".join(lines)


def _group_chat_to_telegram_id(chat_id: str) -> int:
    """"group_-100123" → -100123 (the Telegram chat id to post to)."""
    return int(chat_id[len("group_"):])


def _day_bounds_utc(now_local: datetime) -> tuple[datetime, datetime]:
    """UTC [start, end) covering the local calendar day of `now_local`."""
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


async def run_daily_summary(bot: Bot) -> None:
    """Post the end-of-day recap into every opted-in group chat."""
    s = get_settings()
    tz = ZoneInfo(s.default_timezone)
    now_local = datetime.now(tz)
    start_utc, end_utc = _day_bounds_utc(now_local)
    date_label = now_local.strftime("%d.%m.%Y")

    global_doc = await repo.get_global_settings()
    global_mode = global_doc.get("daily_summary")

    sent = 0
    for chat_id in await repo.list_group_chat_ids():
        per_chat = await repo.get_chat_settings(chat_id)
        # Per-chat override wins on a non-empty value, else global, else env.
        mode = per_chat.get("daily_summary") or global_mode or s.daily_summary
        if mode != "on":
            continue

        created = await repo.get_tasks_created_between(chat_id, start_utc, end_utc)
        closed = await repo.get_tasks_closed_between(chat_id, start_utc, end_utc)
        created_titles = [t.get("task", "") for t in created if t.get("task")]
        if not should_send_summary(mode, len(created_titles), len(closed)):
            continue

        text = compose_summary(date_label, created_titles, len(closed))
        if not text:
            continue
        try:
            await bot.send_message(_group_chat_to_telegram_id(chat_id), text)
            sent += 1
            logger.info("Daily summary sent to %s", chat_id)
        except Exception:  # noqa: BLE001 — one bad chat shouldn't stall the rest
            logger.exception("Daily summary: failed to post to %s", chat_id)

    if sent:
        logger.info("Daily summary: posted to %d group(s)", sent)
