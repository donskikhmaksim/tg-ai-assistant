"""Extraction watchdog.

Probes the extraction chain frequently and DMs the owner ("Большой Брат") in
plain Russian when something breaks. Alert policy, PER distinct error:
  - a NEW breakage (healthy → broken) is reported immediately;
  - while it stays broken it repeats at most ONCE PER DAY — the repeat is held
    until HEALTHCHECK_HOUR local time so it lands in the morning, not at 00:01;
  - a recovered error resets, so a later recurrence alerts again (still ≤1/day).
Qwen / Claude / TickTick are tracked independently — each has its own daily
budget. State lives in Mongo (bot_state) so it survives restarts. Raw technical
detail goes to the LOGS only; the Telegram message is human Russian.

The batch pipeline (Qwen → Claude) is untouched; this only observes.
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot

from .. import repositories as repo
from ..config import get_settings
from ..llm import claude, qwen
from ..ticktick.mcp_client import TickTickMCP

logger = logging.getLogger(__name__)

WATCHDOG_STATE_KEY = "watchdog_state"
OWNER_ID_KEY = "owner_id"

# Human-Russian one-liner per error key — no raw jargon in the DM.
_HUMAN = {
    "qwen": (
        "🔎 Не работает первичный фильтр сообщений (Qwen). Пока он лежит, всё "
        "идёт напрямую в Claude — задачи не теряются, но расход подписки выше."
    ),
    "claude": (
        "🧠 Извлечение задач НЕ работает — недоступен ИИ-обработчик (Claude-шим). "
        "Новые задачи из чатов сейчас не создаются."
    ),
    "ticktick": (
        "✅ TickTick недоступен — задачи извлекаются, но не попадают в твой "
        "список."
    ),
}


async def _ticktick_ok() -> tuple[bool, str]:
    s = get_settings()
    if not s.ticktick_mcp_url:
        return True, ""  # not configured → nothing to probe
    try:
        await TickTickMCP(s.ticktick_mcp_url).get_projects()
        return True, ""
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {str(e)[:200]}"


async def collect_problems() -> list[tuple[str, str]]:
    """(key, raw_detail) per current failure, probed in pipeline order
    (tier-1 Qwen → tier-2 Claude → TickTick)."""
    problems: list[tuple[str, str]] = []
    # Resolve the tier-1 endpoint the SAME way the pipeline does: Mini App global
    # setting, else env. Empty → tier-1 is off and healthcheck skips (reports ok).
    # Fail safe to env if the settings read isn't available (e.g. DB not ready).
    try:
        qwen_base_url = (await repo.get_global_settings()).get("qwen_base_url")
    except Exception:  # noqa: BLE001
        qwen_base_url = None
    qwen_base_url = qwen_base_url or get_settings().qwen_base_url
    ok, detail = await qwen.healthcheck(base_url=qwen_base_url)
    if not ok:
        problems.append(("qwen", detail))
    ok, detail = await claude.healthcheck()
    if not ok:
        problems.append(("claude", detail))
    ok, detail = await _ticktick_ok()
    if not ok:
        problems.append(("ticktick", detail))
    return problems


def decide_alerts(
    state: dict, current_keys: list[str], now: datetime, hour: int
) -> tuple[list[str], bool]:
    """Pure alert policy. Mutates `state` (per-key {active, date}).

    Returns (keys_to_alert, state_changed). `now` is owner-local; `hour` is the
    morning-repeat gate (HEALTHCHECK_HOUR)."""
    today = now.strftime("%Y-%m-%d")
    to_alert: list[str] = []
    changed = False
    for key in current_keys:
        st = state.get(key) or {"active": False, "date": None}
        should = False
        if not st.get("active"):
            # healthy → broken (or recovered → recurred): immediate, but never
            # more than once on the same calendar day (anti-flap).
            if st.get("date") != today:
                should = True
        elif st.get("date") != today and now.hour >= hour:
            # still broken into a new day: the daily reminder, held till morning.
            should = True
        st["active"] = True
        if should:
            st["date"] = today
            to_alert.append(key)
        state[key] = st
        changed = True
    # Reset recovered errors so a future recurrence alerts again.
    for key in list(state.keys()):
        if key not in current_keys and state[key].get("active"):
            state[key]["active"] = False
            changed = True
    return to_alert, changed


async def run_watchdog(bot: Bot) -> None:
    s = get_settings()
    if not s.healthcheck_enabled:
        return
    problems = await collect_problems()
    current = dict(problems)
    if problems:
        logger.error("Watchdog: %s", "; ".join(f"{k}: {d}" for k, d in problems))

    state = (await repo.get_bot_state(WATCHDOG_STATE_KEY)) or {}
    now = datetime.now(ZoneInfo(s.default_timezone))
    to_alert, changed = decide_alerts(state, list(current.keys()), now, s.healthcheck_hour)
    if changed:
        await repo.set_bot_state(WATCHDOG_STATE_KEY, state)

    if not to_alert:
        return
    owner_id = await repo.get_bot_state(OWNER_ID_KEY)
    if not owner_id:
        logger.warning("Watchdog: %d alert(s) pending but no owner_id to notify", len(to_alert))
        return
    body = "\n\n".join(_HUMAN.get(k, f"Ошибка: {k}") for k in to_alert)
    text = "🚨 Большой Брат — что-то сломалось:\n\n" + body
    try:
        await bot.send_message(int(owner_id), text)
    except Exception:  # noqa: BLE001
        logger.exception("Watchdog: failed to notify owner %s", owner_id)
