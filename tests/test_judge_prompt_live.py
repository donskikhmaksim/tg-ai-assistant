"""Golden set for the dedup judge — runs the REAL judge (opt-in).

The 2026-08-05 duplicate flood was a PROMPT failure: every offline test passed
while the live judge answered «different» on 8 of 10 real duplicate pairs, so
nothing ever merged. Only a live run catches that class of regression, so this
file exists — skipped by default (it costs LLM calls), enabled with:

    JUDGE_LIVE=1 pytest tests/test_judge_prompt_live.py -q

It needs the same config the pipeline uses (CLAUDE_CLI_URL/TOKEN or
ANTHROPIC_API_KEY). Pairs below are verbatim from production (chats of
2026-08-04/05) plus hand-made negatives that must never merge.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from app.llm.claude import judge_same_task

pytestmark = pytest.mark.skipif(
    not os.getenv("JUDGE_LIVE"),
    reason="live judge test — set JUDGE_LIVE=1 (spends real LLM calls)",
)

# (task A, task B, same?) — A/B are what the pipeline hands the judge.
DUPLICATES = [
    ("Выслать материалы/эстимейт по коммерческим дверям Джозефу",
     "Выслать материалы/эстимейт по коммерческим дверям Джозефу на почту"),
    ("Разработать механизм занесения и списания материалов по объектам (кто заносит, кто проверяет)",
     "Разработать механизм занесения и списания материалов (кто заносит, кто проверяет)"),
    ("Посмотреть очередь/статус по лицензии",
     "Посмотреть очередь/статус по лицухе (лицензии)"),
    ("Разобраться, как предлагать клиентам новую резиденшиал дверь",
     "Разобраться, как предлагать клиентам новую резиденшиал дверь "
     "(приложения от производителей, буклеты и т.д.)"),
    ("Съездить в 10:30 посмотреть маленькое помещение вместо Максима, т.к. Максим не успевает",
     "Съездить в 10:30 посмотреть маленькое помещение (риелтор показывает в 10 и в 10:30), "
     "т.к. Максим не успевает"),
    ("Позвонить риелтору насчёт помещений",
     "Позвонить риелтору насчёт помещений (после 4:30)"),
    ("Разобраться с кредиткой Влада",
     "Разобраться с кредиткой Влада (не прошёл платёж HD, кредитка закрыта, на счету 1300$)"),
]

DISTINCT = [
    ("Посмотреть рилс https://instagram.com/reel/AAA111",
     "Посмотреть рилс https://instagram.com/reel/BBB222"),
    ("Подать декларацию за 2025", "Подать декларацию за 2026"),
    ("Оплатить счёт Джозефу на 1500$", "Оплатить счёт Джозефу на 2300$"),
    ("Согласовать смету по объекту Финч", "Оплатить смету по объекту Финч"),
    ("Купить лампочки для офиса", "Установить лампочки в офисе"),
    ("Позвонить риелтору насчёт помещений", "Позвонить Джозефу насчёт помещений"),
    ("Заказать двери на объект Финч", "Заказать двери на объект Мейпл"),
]


def _verdicts(pairs):
    async def run():
        return await asyncio.gather(*[judge_same_task(a, b) for a, b in pairs])
    return asyncio.run(run())


def test_real_duplicate_pairs_are_judged_same():
    verdicts = _verdicts(DUPLICATES)
    missed = [p for p, v in zip(DUPLICATES, verdicts) if v is not True]
    assert not missed, f"judge failed to merge real duplicates: {missed}"


def test_distinct_pairs_are_never_merged():
    # The expensive direction: a false merge DROPS a real task.
    verdicts = _verdicts(DISTINCT)
    wrong = [p for p, v in zip(DISTINCT, verdicts) if v is True]
    assert not wrong, f"judge merged distinct tasks: {wrong}"
