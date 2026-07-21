"""Watchdog: per-error alert policy (immediate on new, ≤1/day, morning repeat)."""
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from app.llm import claude, qwen
from app.pipeline import watchdog

TZ = ZoneInfo("America/Los_Angeles")
HOUR = 9


def _dt(day, hh):
    return datetime(2026, 7, day, hh, 0, tzinfo=TZ)


def test_new_error_alerts_immediately_any_hour():
    state = {}
    # 3am, brand-new error → still alerts (a new breakage isn't gated to morning).
    to_alert, changed = watchdog.decide_alerts(state, ["claude"], _dt(20, 3), HOUR)
    assert to_alert == ["claude"] and changed
    assert state["claude"] == {"active": True, "date": "2026-07-20"}


def test_same_day_repeat_suppressed():
    state = {"claude": {"active": True, "date": "2026-07-20"}}
    to_alert, _ = watchdog.decide_alerts(state, ["claude"], _dt(20, 15), HOUR)
    assert to_alert == []


def test_next_day_repeat_held_until_morning():
    state = {"claude": {"active": True, "date": "2026-07-20"}}
    # 07:00 next day — before the 9am gate → hold.
    to_alert, _ = watchdog.decide_alerts(state, ["claude"], _dt(21, 7), HOUR)
    assert to_alert == []
    # 09:00 next day — fire the daily repeat.
    to_alert, _ = watchdog.decide_alerts(state, ["claude"], _dt(21, 9), HOUR)
    assert to_alert == ["claude"]
    assert state["claude"]["date"] == "2026-07-21"


def test_recovery_resets_then_recurrence_next_day_alerts():
    state = {"claude": {"active": True, "date": "2026-07-20"}}
    # No longer a problem → cleared.
    to_alert, changed = watchdog.decide_alerts(state, [], _dt(20, 16), HOUR)
    assert to_alert == [] and changed
    assert state["claude"]["active"] is False
    # Recurs next day → new breakage, alerts immediately.
    to_alert, _ = watchdog.decide_alerts(state, ["claude"], _dt(21, 10), HOUR)
    assert to_alert == ["claude"]


def test_recovery_then_recurrence_same_day_is_capped():
    # Alerted today, recovered, breaks again same day → no second alert (anti-flap).
    state = {"claude": {"active": True, "date": "2026-07-20"}}
    watchdog.decide_alerts(state, [], _dt(20, 12), HOUR)  # recover
    to_alert, _ = watchdog.decide_alerts(state, ["claude"], _dt(20, 13), HOUR)
    assert to_alert == []


def test_errors_are_independent():
    state = {}
    to_alert, _ = watchdog.decide_alerts(state, ["claude", "ticktick"], _dt(20, 14), HOUR)
    assert set(to_alert) == {"claude", "ticktick"}


def test_collect_problems_returns_keys(monkeypatch):
    async def q(base_url=None):
        return (True, "")

    async def c():
        return (False, "boom 500")

    async def tt():
        return (True, "")

    monkeypatch.setattr(qwen, "healthcheck", q)
    monkeypatch.setattr(claude, "healthcheck", c)
    monkeypatch.setattr(watchdog, "_ticktick_ok", tt)
    problems = asyncio.run(watchdog.collect_problems())
    assert problems == [("claude", "boom 500")]


def test_alert_text_is_russian():
    assert "Извлечение задач" in watchdog._HUMAN["claude"]
    assert "TickTick" in watchdog._HUMAN["ticktick"]
