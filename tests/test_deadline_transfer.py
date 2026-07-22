"""Deadline transfer on semantic dedup (audit P2.3): when a NEW extracted task
duplicates an EXISTING one, the new deadline is copied onto the existing task —
but ONLY when the existing task has no due date recorded. Pure decision logic:
_should_transfer_deadline(new_task, match).

Candidate shapes the predicate must understand:
  - rich TickTick project cards (get_project_tasks) carry "due" when set;
  - local (Mongo) candidates carry the doc's "deadline".
"""
from app.pipeline.batch import _should_transfer_deadline


def test_transfers_when_new_has_deadline_and_match_has_none():
    assert _should_transfer_deadline(
        {"task": "оплатить счёт", "deadline": "2026-07-25"},
        {"title": "Оплатить счёт", "ticktickTaskId": "t1", "projectId": "p1"},
    ) is True


def test_transfers_onto_local_match_without_deadline():
    assert _should_transfer_deadline(
        {"deadline": "2026-07-25T17:00"},
        {"title": "x", "chatId": "user_1", "dedupHash": "h", "deadline": None},
    ) is True


def test_no_transfer_when_new_has_no_deadline():
    assert _should_transfer_deadline({"task": "позвонить"}, {"title": "x"}) is False
    assert _should_transfer_deadline({"deadline": None}, {"title": "x"}) is False


def test_no_transfer_when_new_deadline_is_empty_or_whitespace():
    assert _should_transfer_deadline({"deadline": ""}, {"title": "x"}) is False
    assert _should_transfer_deadline({"deadline": "   "}, {"title": "x"}) is False


def test_no_transfer_when_match_has_ticktick_due():
    # Rich project card with a due date set — never overwrite it.
    assert _should_transfer_deadline(
        {"deadline": "2026-07-25"},
        {"title": "x", "due": "2026-07-20 00:00", "ticktickTaskId": "t1"},
    ) is False


def test_no_transfer_when_match_has_local_deadline():
    assert _should_transfer_deadline(
        {"deadline": "2026-07-25"},
        {"title": "x", "deadline": "2026-07-20", "dedupHash": "h", "chatId": "c"},
    ) is False


def test_empty_string_due_on_match_counts_as_missing():
    # "" / whitespace due or deadline is "none recorded" → transfer.
    assert _should_transfer_deadline(
        {"deadline": "2026-07-25"}, {"title": "x", "due": ""}
    ) is True
    assert _should_transfer_deadline(
        {"deadline": "2026-07-25"}, {"title": "x", "due": "  ", "deadline": ""}
    ) is True


def test_either_due_or_deadline_on_match_blocks_transfer():
    # Both fields present on a hybrid candidate — either one being set blocks.
    assert _should_transfer_deadline(
        {"deadline": "2026-07-25"},
        {"title": "x", "due": "2026-07-20 00:00", "deadline": ""},
    ) is False
    assert _should_transfer_deadline(
        {"deadline": "2026-07-25"},
        {"title": "x", "due": "", "deadline": "2026-07-20"},
    ) is False
