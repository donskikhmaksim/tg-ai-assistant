"""Daily extraction watchdog.

Once a day (scheduled in main.py) the bot probes the extraction chain and, if
anything is down, DMs the primary owner — so a dead tier-2 shim (e.g. `claude`
logged out on the Mac mini → every /claude POST 500s) can't silently stall task
creation for days, like it did in July 2026.

It probes tier-1 (Qwen) THEN tier-2 (Claude) — the same order as the real
pipeline — but is entirely separate from it: the batch flow (Qwen → Claude) is
untouched. Honest probes (qwen.healthcheck / claude.healthcheck) are used, not
has_task(), which fails open and would hide a dead Qwen.
"""
from __future__ import annotations

import logging

from aiogram import Bot

from .. import repositories as repo
from ..config import get_settings
from ..llm import claude, qwen

logger = logging.getLogger(__name__)


def format_alert(problems: list[str]) -> str:
    """Compose the owner-facing nag. Plain text (no parse_mode) so endpoint URLs
    and error strings can't break message entity parsing."""
    return (
        "🚨 Большой Брат: извлечение задач НЕ работает.\n\n"
        + "\n".join(problems)
        + "\n\nНовые задачи из чатов сейчас не создаются, пока это не починить."
    )


async def collect_problems() -> list[str]:
    """Probe the chain in pipeline order (tier-1 first). Returns a list of
    human-readable failures; empty means healthy."""
    s = get_settings()
    problems: list[str] = []

    ok, detail = await qwen.healthcheck()
    if not ok:
        problems.append(f"• Tier-1 Qwen ({s.qwen_base_url}): {detail}")

    ok, detail = await claude.healthcheck()
    if not ok:
        where = s.claude_cli_url or "Anthropic API"
        problems.append(f"• Tier-2 Claude ({where}): {detail}")

    return problems


async def run_watchdog(bot: Bot) -> None:
    s = get_settings()
    if not s.healthcheck_enabled:
        return
    problems = await collect_problems()
    if not problems:
        logger.info("Watchdog: extraction chain healthy")
        return

    logger.error("Watchdog: extraction chain DEGRADED:\n%s", "\n".join(problems))
    owner_id = await repo.get_bot_state("owner_id")
    if not owner_id:
        logger.warning("Watchdog: no owner_id in bot_state; cannot notify owner")
        return
    try:
        await bot.send_message(int(owner_id), format_alert(problems))
    except Exception:  # noqa: BLE001
        logger.exception("Watchdog: failed to notify owner %s", owner_id)
