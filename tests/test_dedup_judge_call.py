"""Regression tests for the 2026-08-05 duplicate flood.

Symptom: near-identical tasks (cosine 0.97) kept landing in TickTick as separate
items. The semantic-dedup pool was fine and embeddings were fine — the break was
downstream, in HOW the LLM judge was consulted:

  1. batch.py handed the judge two BARE TITLES, although judge_same_task() is
     documented to weigh full cards. With only titles the judge cannot tell
     "same task, one side just names the channel/time" from "different URL or
     amount hidden in the details", and its prompt made it answer «different» —
     so nothing ever merged (a live backtest against prod merged 1 of 12 pairs).
  2. _JUDGE_SYSTEM told the judge that a difference in ANY detail means NOT the
     same task. Extra detail on ONE side (a parenthetical, "на почту") is not a
     conflict — it is the same task captured twice with different completeness.
  3. _parse_yes_no() only accepted a reply STARTING with yes/no; on multi-line
     cards the judge sometimes prefixes a few words, which silently became None
     → «distinct» → yet another duplicate.

These tests pin all three so the pool/threshold work can't be undone by the call
path again. The end-to-end prompt behaviour itself is covered by the live golden
set in test_judge_prompt_live.py (opt-in, needs the real judge).
"""
from __future__ import annotations

import asyncio

from app.llm import claude
from app.llm.claude import _JUDGE_SYSTEM, _parse_yes_no
from app.pipeline import batch
from app.pipeline.batch import _judge_card


def _run(coro):
    return asyncio.run(coro)


# ── _judge_card ───────────────────────────────────────────────────────────
def test_card_carries_details_and_due():
    card = _judge_card("Выслать эстимейт Джозефу", "по коммерческим дверям", "2026-08-06")
    assert "Выслать эстимейт Джозефу" in card
    assert "по коммерческим дверям" in card
    assert "2026-08-06" in card


def test_card_without_extras_is_just_the_title():
    # Degrades to exactly the old bare-title behaviour — no empty labels.
    assert _judge_card("Позвонить риелтору", None, "") == "Позвонить риелтору"
    assert _judge_card("Позвонить риелтору", "   ", None) == "Позвонить риелтору"


# ── _parse_yes_no ─────────────────────────────────────────────────────────
def test_parse_plain_answers():
    assert _parse_yes_no("yes") is True
    assert _parse_yes_no("No.") is False
    assert _parse_yes_no("Yes — same task") is True


def test_parse_answer_with_a_preamble():
    # The exact silent-None shape: a judged card answered with a short preamble
    # before the verdict. Must be read as an answer, not as "judge unavailable".
    assert _parse_yes_no("Task A and Task B describe the same thing: yes") is True
    assert _parse_yes_no("These are different stages, no") is False


def test_parse_no_verdict_is_none():
    assert _parse_yes_no("") is None
    assert _parse_yes_no("unclear") is None


def test_parse_word_boundaries_not_substrings():
    # "not"/"nothing"/"november" must never be read as a "no" verdict.
    assert _parse_yes_no("nothing conclusive here") is None
    assert _parse_yes_no("not enough information") is None


# ── the judge's contract in the prompt ────────────────────────────────────
def test_prompt_says_one_sided_extra_detail_is_not_a_conflict():
    # The rule whose absence caused the flood: "X" vs "X (уточнение)" is SAME.
    low = _JUDGE_SYSTEM.lower()
    assert "extra detail on one side is not a conflict" in low
    assert "contained in the other" in low


def test_prompt_still_rejects_conflicting_values_and_stages():
    low = _JUDGE_SYSTEM.lower()
    assert "different means a conflict" in low
    assert "декларация за 2025" in _JUDGE_SYSTEM  # different period → different
    assert "approve ≠ pay" in _JUDGE_SYSTEM       # different stage → different


def test_prompt_no_longer_treats_any_detail_as_a_difference():
    # The old wording that made the judge answer «different» on every rewording.
    assert "any distinguishing detail" not in _JUDGE_SYSTEM.lower()


# ── the call path: the judge must receive CARDS, not bare titles ──────────
class _FakeTT:
    """Enough of the TickTick client for _create_new_tasks; records creates."""

    def __init__(self):
        self.created: list[str] = []

    async def create_task(self, **kw):
        self.created.append(kw.get("title"))
        return "tt-new"

    async def get_project_tasks(self, project_id, limit=None):
        return []

    async def add_task_comment(self, *a, **kw):
        return None


def _patch_pipeline(monkeypatch, judge_calls, inserted, *, verdict=True):
    async def fake_embed(texts):
        # One dimension per distinct title → identical titles score 1.0; here the
        # actual vectors only need to put the pair above dedup_low.
        return [[1.0, 0.99] for _ in texts]

    async def fake_judge(a, b):
        judge_calls.append((a, b))
        return verdict

    async def noop(*a, **kw):
        return None

    async def get_binding(chat_id):
        return {"ticktickProjectId": "p1", "projectName": "P", "ticktickSectionId": None}

    async def get_task_vectors(scope):
        return {}

    async def insert_task_if_new(doc):
        inserted.append(doc["task"])
        return True

    monkeypatch.setattr(batch, "embed", fake_embed)
    monkeypatch.setattr(claude, "judge_same_task", fake_judge)
    monkeypatch.setattr(batch.repo, "get_project_binding", get_binding)
    monkeypatch.setattr(batch.repo, "get_task_vectors", get_task_vectors)
    monkeypatch.setattr(batch.repo, "store_task_vectors", noop)
    monkeypatch.setattr(batch.repo, "get_chat_settings", lambda *a: _async({}))
    monkeypatch.setattr(batch.repo, "get_chat_title", lambda *a: _async(None))
    monkeypatch.setattr(batch.repo, "append_task_details", noop)
    monkeypatch.setattr(batch.repo, "set_task_ticktick_id", noop)
    monkeypatch.setattr(batch.repo, "insert_task_if_new", insert_task_if_new)


async def _async(value):
    return value


def _existing_open_task():
    return {
        "chatId": "group_1",
        "task": "Выслать материалы/эстимейт по коммерческим дверям Джозефу на почту",
        "details": "обсуждали на встрече",
        "deadline": "2026-08-06",
        "dedupHash": "h-old",
        "ticktickTaskId": "tt-old",
        "projectId": "p1",
        "status": "open",
    }


def _new_task():
    return {
        "task": "Выслать материалы/эстимейт по коммерческим дверям Джозефу",
        "who": "me",
        "details": "он ждёт цифры",
        "source_message_ids": [],
    }


def test_judge_receives_full_cards_not_bare_titles(monkeypatch):
    judge_calls: list[tuple[str, str]] = []
    inserted: list[str] = []
    _patch_pipeline(monkeypatch, judge_calls, inserted)
    tt = _FakeTT()

    _run(batch._create_new_tasks(
        "group_1", [_new_task()], tt=tt, open_tasks=[_existing_open_task()],
    ))

    assert len(judge_calls) == 1, "the judge must be consulted above dedup_low"
    new_card, match_card = judge_calls[0]
    # THE regression: both sides carried only a title, so the judge was blind.
    assert "он ждёт цифры" in new_card
    assert "обсуждали на встрече" in match_card
    assert "2026-08-06" in match_card


def test_judged_duplicate_is_not_created(monkeypatch):
    judge_calls: list[tuple[str, str]] = []
    inserted: list[str] = []
    _patch_pipeline(monkeypatch, judge_calls, inserted, verdict=True)
    tt = _FakeTT()

    _run(batch._create_new_tasks(
        "group_1", [_new_task()], tt=tt, open_tasks=[_existing_open_task()],
    ))

    assert inserted == [], "a judged duplicate must not be inserted locally"
    assert tt.created == [], "a judged duplicate must not reach TickTick"


def test_judged_distinct_is_still_created(monkeypatch):
    # The safety half of the contract: «different» always creates the task.
    judge_calls: list[tuple[str, str]] = []
    inserted: list[str] = []
    _patch_pipeline(monkeypatch, judge_calls, inserted, verdict=False)
    tt = _FakeTT()

    _run(batch._create_new_tasks(
        "group_1", [_new_task()], tt=tt, open_tasks=[_existing_open_task()],
    ))

    assert inserted == [_new_task()["task"]]
    assert tt.created == [_new_task()["task"]]
