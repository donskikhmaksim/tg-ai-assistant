"""Watchdog: alert formatting + failure collection in pipeline order."""
import asyncio

from app.llm import claude, qwen
from app.pipeline import watchdog


def test_format_alert_lists_problems():
    text = watchdog.format_alert(["• Tier-2 Claude (shim): 500"])
    assert "Большой Брат" in text
    assert "• Tier-2 Claude (shim): 500" in text
    # Owner should be told the concrete consequence.
    assert "не создаются" in text


def _patch(monkeypatch, qwen_ok, claude_ok):
    async def q():
        return (qwen_ok, "" if qwen_ok else "boom")

    async def c():
        return (claude_ok, "" if claude_ok else "500")

    monkeypatch.setattr(qwen, "healthcheck", q)
    monkeypatch.setattr(claude, "healthcheck", c)


def test_collect_problems_healthy(monkeypatch):
    _patch(monkeypatch, True, True)
    assert asyncio.run(watchdog.collect_problems()) == []


def test_collect_problems_reports_each_tier(monkeypatch):
    _patch(monkeypatch, False, False)
    problems = asyncio.run(watchdog.collect_problems())
    assert len(problems) == 2
    # Pipeline order: tier-1 Qwen must come before tier-2 Claude.
    assert "Tier-1 Qwen" in problems[0]
    assert "Tier-2 Claude" in problems[1]


def test_collect_problems_only_claude_down(monkeypatch):
    _patch(monkeypatch, True, False)
    problems = asyncio.run(watchdog.collect_problems())
    assert len(problems) == 1
    assert "Tier-2 Claude" in problems[0]
